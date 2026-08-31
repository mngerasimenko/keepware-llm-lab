#!/usr/bin/env python3
"""Проверка починок: закреплена ли починка тестом.

Соседний `mutation_check.py` спрашивает: заметит ли порчу ХОТЬ ОДИН тест из
набора. Этот спрашивает другое и более узкое: падает ли ИМЕННО ТОТ тест,
который написан под конкретную починку, если починку откатить.

Второй вопрос выглядит слабее первого, и в общем случае он слабее. Но в
этом репозитории пять кругов ревью подряд главной находкой оказывалась не
дыра в старом коде, а СВЕЖАЯ ПОЧИНКА, не закреплённая ничем: тест под неё
написан, выглядит осмысленно, зелёный - и остаётся зелёным, если починку
убрать. Мутационный реестр такого не видит: мутации пишутся под ветки,
которые уже есть, а не под правку, сделанную час назад.

Разница между двумя инструментами - разница между «эта ветка кем-то
держится» и «этот тест держит эту ветку». Первое отвечает за код, второе -
за тесты, которые к нему только что дописали.

Запуск из корня репозитория:

    python scripts/revert_check.py

Коды возврата: 0 - каждая починка различается своим тестом; 1 - какая-то не
различается, её якорь больше не находится в коде, тест не ответил или
прогон сломался.

**Рабочее дерево инструмент не трогает.** Откат применяется к КОПИИ, и это
не осторожность ради осторожности: инструмент нужен как раз тогда, когда в
дереве лежит незакоммиченная работа, и портить её ради проверки было бы
ровно тем, от чего этот репозиторий защищается.

Отсюда же требование базового прогона. Тест сначала гоняется на копии БЕЗ
отката и обязан быть зелёным: красный по своей причине тест красен и под
откатом, и «различает» напечаталось бы на пустом месте - ровно та ложь,
которую инструмент ищет. Правишь хук, ломаешь по дороге его тест - и без
базового прогона пять откатов отрапортовали бы успех.
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Убийство дерева процессов берём у соседа, а не переписываем: тесты хука
# запускают sh, тот - ещё один python, и внук переживает таймаут прямого
# потомка, продолжая писать в копию, которую мы в это время удаляем.
from mutation_check import run_apart  # noqa: E402

REPO = os.path.dirname(SCRIPTS_DIR)
LINTER = os.path.join("scripts", "check_memory_index.py")
HOOK = os.path.join(".githooks", "pre-commit")
TOOL = os.path.join("scripts", "mutation_check.py")
# Потолок на один тест. Прогон здесь адресный - один именованный тест, а не
# весь набор, - поэтому и запас нужен другой, чем у мутационной проверки.
# Тесты хука поднимают свой репозиторий и на Windows доходят до полуминуты.
TEST_TIMEOUT = 300

# (что откатываем, файл, точный фрагмент, чем заменить, тест, который обязан
#  на этом покраснеть)
#
# Правило то же, что у мутаций: починили дефект - добавьте сюда откат.
# Фрагмент должен находиться РОВНО ОДИН раз, иначе откат молча не
# применится, а «различает» напечатается по чужой причине.
#
# Откат обязан МЕНЯТЬ ПОВЕДЕНИЕ. Ревью предложило для двух записей ниже
# «точную инверсию» - вернуть в условие `left and` и умолчание
# `NEWLINE_BY_NAME.get(kind, "\n")` вместо гашения раннего гарда. Прогон
# показал, что обе такие правки НИЧЕГО НЕ МЕНЯЮТ: гард отсекает раньше, и
# код за ним недостижим, поэтому тест остаётся зелёным. Это не находка про
# тест, а доказательство, что тот код мёртв, - и заодно причина, почему
# откат здесь гасит именно гард: гард И ЕСТЬ починка.
REVERTS = [
    ("восстановление адресует файл строкой из слепка", TOOL,
     "    path = ours\n", "    \n",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_the_file_is_addressed_by_our_name_not_by_the_backup_string"),

    ("опознаётся только линтер, хук забыт", TOOL,
     "    for ours in (LINTER, HOOK):", "    for ours in (LINTER,):",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_the_hook_is_restored_too_not_only_the_linter"),

    ("сторож отпечатка пропускается при пустом поле", TOOL,
     "    if not left:", "    if False:",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_a_backup_without_a_fingerprint_is_refused"),

    ("незнакомый вид переводов строк молча даёт LF", TOOL,
     "    if kind not in NEWLINE_BY_NAME:", "    if False:",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_an_unknown_line_ending_in_the_backup_is_refused"),

    ("оставшийся слепок не останавливает прогон", TOOL,
     "    if os.path.isfile(BACKUP):", "    if False:",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_a_backup_left_behind_stops_the_run"),

    ("пропавший файл не восстанавливается", TOOL,
     "        except FileNotFoundError:\n            current = None\n"
     "            failure = None\n            break\n",
     "",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_a_file_that_vanished_is_restored_not_blamed"),

    ("под-индекс за забором снова получает ложный совет", LINTER,
     "            cut_at = behind_unclosed(rel)\n            if cut_at:",
     "            cut_at = None\n            if cut_at:",
     "test_check_memory_index.FilesAppearInIndex."
     "test_a_sub_index_behind_an_unclosed_fence_is_named_not_blamed"),

    ("файл за таким под-индексом снова обвиняется отдельно", LINTER,
     "            cut_at = behind_unclosed(stranded[rel])",
     "            cut_at = None",
     "test_check_memory_index.FilesAppearInIndex."
     "test_a_file_behind_such_a_sub_index_gets_the_same_cause"),

    ("двойник с обратным слэшем снова принимается за свой", TOOL,
     "            if os.path.exists(path) and not os.path.samefile(path, ours):",
     "            if False:",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_a_lookalike_with_a_backslash_in_its_name_is_refused"),

    ("отказ спросить снова читается как «чужой файл»", TOOL,
     "            return ours\n        return ours", "            return None\n        return ours",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_a_file_we_cannot_ask_about_is_still_ours"),

    ("неудача удалить слепок снова молчит", TOOL,
     "        return True\n    except FileNotFoundError:\n"
     "        return True\n    except OSError:\n        return False",
     "        return True\n    except FileNotFoundError:\n"
     "        return True\n    except OSError:\n        return True",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_a_backup_that_cannot_be_deleted_is_named_correctly"),

    ("потолок снова ниже настоящей границы", HOOK,
     'if [ "$DRAFT_BYTES" -gt 32000 ]; then',
     'if [ "$DRAFT_BYTES" -gt 30000 ]; then',
     "test_check_memory_index.PreCommitHook."
     "test_user_keys_do_not_eat_the_draft_budget_twice"),

    ("замер снова прячется за наличием черновиков", HOOK,
     "DRAFT_BYTES=0\nset +e\nDRAFT_BYTES=$(printf",
     'DRAFT_BYTES=0\nif [ -z "$DRAFT_ARGS" ]; then DRAFT_ARGS=""; fi\n'
     'set +e\nif [ -n "$DRAFT_ARGS" ]; then DRAFT_BYTES=$(printf',
     "test_check_memory_index.PreCommitHook."
     "test_long_user_keys_alone_are_named"),

    ("замер длины снова без set +e", HOOK,
     "set +e\nDRAFT_BYTES=$(printf", "DRAFT_BYTES=$(printf",
     "test_check_memory_index.PreCommitHook."
     "test_a_failing_tr_does_not_abort_the_hook"),

    ("статус замера не проверяется", HOOK,
     '[ "$MEASURE_STATUS" = 0 ] || MEASURE_OK=0\n', "",
     "test_check_memory_index.PreCommitHook."
     "test_a_failing_tr_does_not_abort_the_hook"),

    ("результат замера не проверяется на число", HOOK,
     'case "$DRAFT_BYTES" in\n'
     "    ''|*[!0-9]*) MEASURE_OK=0 ;;\n"
     "esac\n",
     "",
     "test_check_memory_index.PreCommitHook."
     "test_a_failing_wc_does_not_switch_the_budget_off_silently"),

    ("явная проверка статуса sed снята", HOOK,
     '    if [ "$ESCAPE_STATUS" != 0 ] || [ -z "$DRAFT_ARGS" ]; then',
     '    if [ -z "$DRAFT_ARGS" ]; then',
     "test_check_memory_index.PreCommitHook."
     "test_a_failing_sed_does_not_abort_the_hook"),

    ("счётчик не пересчитывается после отсева кавычек", HOOK,
     "    DRAFT_COUNT=0\n    if [ -n \"$DRAFTS\" ]; then\n"
     "        DRAFT_COUNT=$(printf '%s\\n' \"$DRAFTS\" | grep -c . || true)\n"
     "        case \"$DRAFT_COUNT\" in\n"
     "            ''|*[!0-9]*) DRAFT_COUNT=0; DRAFTS=\"\" ;;\n"
     "        esac\n    fi\nfi",
     "fi",
     "test_check_memory_index.PreCommitHook."
     "test_drafts_named_in_quotes_do_not_fake_a_failure"),
]
# Ограничитель обратного прохода (`floor`) сюда намеренно НЕ занесён: его
# держит мутация в `mutation_check.py`, а единственный тест, который на его
# откате краснеет, меряет ВРЕМЯ. Различать реализации временем ожидания
# инструмент объявляет негодным - и делать для себя исключение не станет.
# Тот же откат стоил бы полному прогону лишние сорок пять секунд.

# Состояния прогона одного теста.
RED = "красный"
GREEN = "зелёный"
SKIPPED = "пропущен"
HUNG = "ЗАВИС"
BROKEN = "ПРОГОН СЛОМАН"
# Вердикты по случаю.
DISTINGUISHES = "различает"
BLIND = "НЕ РАЗЛИЧАЕТ"
STALE = "ЯКОРЬ"
BASE_RED = "БАЗА КРАСНАЯ"

RAN = re.compile(r"^Ran (\d+) tests?", re.M)
# Примета сломанного загрузчика - полное имя класса, а не подстрока во всём
# выводе: тест, у которого это слово в имени или в сообщении, иначе выдавал
# бы поломку прогона на пустом месте. Тот же приём, что у соседа.
BROKEN_LOADER = re.compile(r"unittest\.loader\._FailedTest")
OUTCOME = re.compile(r"^(OK|FAILED)\b(.*)$", re.M)
SKIPPED_COUNT = re.compile(r"skipped=(\d+)")


def copy_tree(destination):
    """Копия дерева без .git и мусора сборки.

    `symlinks=True` обязателен: без него висячая ссылка в дереве роняет
    копирование трейсбеком, то есть посторонний файл выключает проверку -
    ровно тот класс отказа, который весь этот репозиторий и ищет.
    """
    shutil.copytree(REPO, destination, symlinks=True,
                    ignore=shutil.ignore_patterns(
                        ".git", "__pycache__", "*.pyc", ".mutation-backup",
                        ".venv", ".idea", ".vscode"))


def apply_revert(folder, relative, old, new):
    """Возвращает текст жалобы, если откат не применился как надо."""
    target = os.path.join(folder, relative)
    with io.open(target, encoding="utf-8", newline="") as stream:
        text = stream.read()
    found = text.count(old)
    if found != 1:
        return "фрагмент найден %d раз вместо одного" % found
    changed = text.replace(old, new, 1)
    # Откат обязан оставить файл РАЗБИРАЕМЫМ. Свой первый негодный откат
    # инструмент нашёл на себе: он вырезал тело `try`, оставив пустой блок,
    # и тест падал на IndentationError - то есть «покраснел» бы по причине,
    # к починке отношения не имеющей. У соседа этот урок уже записан
    # (`missing_anchors` компилирует мутанта), здесь его не было.
    if relative.endswith(".py"):
        try:
            compile(changed, relative, "exec")
        except SyntaxError as exc:
            return "мутант не разбирается: %s" % exc
    # `newline=""` с обеих сторон: переводы строк остаются такими же, какими
    # были. Иначе копия отличается от дерева ещё и ими, и тест проверял бы
    # не тот файл, что лежит в репозитории.
    with io.open(target, "w", encoding="utf-8", newline="") as stream:
        stream.write(changed)
    return None


def run_one(folder, test):
    """Один именованный тест на копии. Состояние и хвост вывода."""
    environment = dict(os.environ,
                       # Без этого тесты хука молча пропускаются там, где не
                       # нашёлся sh, и «не различает» печаталось бы на
                       # исправной починке. Сосед выставляет то же самое.
                       MEMCHECK_REQUIRE_SH="1",
                       # Потомок печатает по-русски; на Windows без этого он
                       # пишет в трубу в cp1251, и диагностика приходит
                       # мусором.
                       PYTHONIOENCODING="utf-8")
    # Своя группа процессов: тест хука запускает sh, тот - ещё один python,
    # и внук переживает таймаут прямого потомка. Он продолжал бы писать в
    # копию ровно тогда, когда мы её удаляем.
    if os.name == "nt":
        apart = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        apart = {"start_new_session": True}
    try:
        done = run_apart(
            [sys.executable, "-m", "unittest", test], apart,
            timeout=TEST_TIMEOUT,
            cwd=os.path.join(folder, "scripts"), env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        # Отдельное состояние, а не «зелёный» и не «красный». Формально
        # зависание реализации ОТЛИЧАЕТ - но отличать их временем ожидания
        # негодно: такой тест не скажет, что сломалось, и покраснеет сам по
        # себе на медленной машине. Первая находка инструмента была именно
        # такой: тест звал main(), и на откате тот уходил в настоящий прогон.
        return HUNG, "тест не ответил за %d с" % TEST_TIMEOUT
    printed = (done.stdout or b"").decode("utf-8", "replace")
    tail = (printed.strip().splitlines() or [""])[-1]
    # Ненулевой код сам по себе не значит «тест упал»: его даёт и опечатка в
    # имени теста, и ошибка импорта, и сломанная копия. Считать это
    # «различает» - тот же класс лжи, который инструмент ищет.
    ran = RAN.search(printed)
    if not ran or int(ran.group(1)) == 0 or BROKEN_LOADER.search(printed):
        return BROKEN, tail or "тестов не запущено"
    outcome = OUTCOME.search(printed)
    if not outcome:
        return BROKEN, tail or "итоговой строки нет"
    # Порядок важен: пропуск смотрим ТОЛЬКО у зелёного исхода. Иначе
    # «FAILED (failures=1, skipped=1)» читалось бы как пропуск, и настоящая
    # находка глохла бы; а слово skipped в чужом сообщении об ошибке
    # выключало бы проверку целиком.
    if outcome.group(1) == "FAILED":
        return RED, tail
    # Пропуск засчитываем, только если пропущено ВСЁ. Класс, где один тест
    # пропущен, а остальные отработали, - это зелёный прогон: иначе один
    # `skipTest` по соседству выключал бы проверку всей починки, и инструмент
    # печатал бы «различение не проверено» там, где оно проверено.
    skipped = SKIPPED_COUNT.search(outcome.group(2))
    if skipped and int(skipped.group(1)) >= int(ran.group(1)):
        return SKIPPED, tail
    return GREEN, tail


def check_one(case, folder, base_cache):
    """Вердикт по одному откату. Копия уже сделана и принадлежит нам."""
    _name, relative, old, new, test = case
    # Сначала база: тест обязан быть зелёным ДО отката. Красный по своей
    # причине тест красен и под откатом, и «различает» напечаталось бы, не
    # проверив ничего.
    if test not in base_cache:
        base_cache[test] = run_one(folder, test)
    base, base_tail = base_cache[test]
    if base == SKIPPED:
        return SKIPPED, base_tail
    if base != GREEN:
        return BASE_RED, "до отката: %s (%s)" % (base, base_tail)

    stale = apply_revert(folder, relative, old, new)
    if stale:
        return STALE, stale
    state, tail = run_one(folder, test)
    if state == RED:
        return DISTINGUISHES, tail
    if state == GREEN:
        return BLIND, tail
    return state, tail


def build_parser():
    parser = argparse.ArgumentParser(
        description="Проверка починок: падает ли тест, если починку "
                    "откатить. Рабочее дерево не трогает - откат "
                    "применяется к копии.",
        epilog="Коды возврата: 0 - каждая починка различается своим тестом, "
               "1 - какая-то не различается, её якорь протух, тест не "
               "ответил или прогон сломался.")
    parser.add_argument(
        "--anchors-only", action="store_true",
        help="только проверить, что все откаты применимы (фрагмент "
             "находится ровно один раз), не прогоняя тестов")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_parser().parse_args(argv)

    if not os.path.isfile(os.path.join(REPO, LINTER)):
        print("Не нахожу %s рядом с собой - дерево неполное" % LINTER,
              file=sys.stderr)
        return 1
    # Пустой реестр - не «всё в порядке». Вычисти его, и прогон печатал бы
    # успех, не проверив ни одной починки.
    if not REVERTS:
        print("Реестр откатов пуст - проверять нечего, и это отказ, а не "
              "успех.", file=sys.stderr)
        return 1

    base = tempfile.mkdtemp(prefix="revert-check-")
    width = max(len(case[0]) for case in REVERTS)
    base_cache = {}
    trouble = {BLIND: [], STALE: [], HUNG: [], BROKEN: [], BASE_RED: []}
    skipped = []
    try:
        for number, case in enumerate(REVERTS, 1):
            folder = os.path.join(base, "case%02d" % number)
            copy_tree(folder)
            try:
                if args.anchors_only:
                    complaint = apply_revert(folder, case[1], case[2], case[3])
                    verdict = STALE if complaint else "применим"
                    detail = complaint or "фрагмент на месте"
                else:
                    verdict, detail = check_one(case, folder, base_cache)
                if verdict in trouble:
                    trouble[verdict].append(case[0])
                elif verdict == SKIPPED:
                    skipped.append(case[0])
                print("[%2d/%d] %-13s %-*s  %s"
                      % (number, len(REVERTS), verdict, width, case[0],
                         detail), flush=True)
            finally:
                # Копия больше не нужна. Держать дюжину деревьев разом
                # незачем, а на Windows они ещё и заметны на диске.
                shutil.rmtree(folder, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print()
    complaints = [
        (STALE, "Якорь протух"),
        (BLIND, "Не различает"),
        (HUNG, "Тест не ответил (различать зависанием негодно)"),
        (BROKEN, "Прогон сломан - тест не запустился, а не упал"),
        (BASE_RED, "Тест красный ДО отката - чинить его, а не починку"),
    ]
    for verdict, caption in complaints:
        for name in trouble[verdict]:
            print("%s: %s" % (caption, name), file=sys.stderr)
    if skipped:
        print("Пропущено на этой системе (различение не проверено): %d из %d"
              % (len(skipped), len(REVERTS)))
        for name in skipped:
            print("   %s" % name)
    if any(trouble.values()):
        print("Починок без охраны: %d, протухших якорей: %d, зависших: %d, "
              "сломанных прогонов: %d, красных до отката: %d."
              % (len(trouble[BLIND]), len(trouble[STALE]), len(trouble[HUNG]),
                 len(trouble[BROKEN]), len(trouble[BASE_RED])),
              file=sys.stderr)
        return 1
    if args.anchors_only:
        print("Все %d откатов применимы: фрагмент на месте." % len(REVERTS))
        return 0
    # Число называем ЧЕСТНОЕ: пропущенные не проверены, и складывать их с
    # проверенными значило бы выдавать пропуск за работу.
    print("Починок различается своими тестами: %d из %d."
          % (len(REVERTS) - len(skipped), len(REVERTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
