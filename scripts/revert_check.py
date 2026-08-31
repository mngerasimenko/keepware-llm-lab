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
различается либо её якорь больше не находится в коде.

**Рабочее дерево инструмент не трогает.** Откат применяется к КОПИИ, и это
не осторожность ради осторожности: инструмент нужен как раз тогда, когда в
дереве лежит незакоммиченная работа, и портить её ради проверки было бы
ровно тем, от чего этот репозиторий защищается.
"""

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
     '    if os.path.isfile(BACKUP):\n        print("Слепок %s остался',
     '    if False:\n        print("Слепок %s остался',
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_a_backup_left_behind_stops_the_run"),

    ("пропавший файл не восстанавливается", TOOL,
     "        except FileNotFoundError:\n            current = None\n"
     "            failure = None\n            break\n",
     "",
     "test_mutation_check_self.InterruptedRunCleansUpAfterItself."
     "test_a_file_that_vanished_is_restored_not_blamed"),

    ("замер длины снова без set +e", HOOK,
     "    set +e\n    DRAFT_BYTES=", "    DRAFT_BYTES=",
     "test_check_memory_index.PreCommitHook."
     "test_a_failing_tr_does_not_abort_the_hook"),

    ("статус замера не проверяется", HOOK,
     '    [ "$MEASURE_STATUS" = 0 ] || MEASURE_OK=0\n', "",
     "test_check_memory_index.PreCommitHook."
     "test_a_failing_tr_does_not_abort_the_hook"),

    ("результат замера не проверяется на число", HOOK,
     '    case "$DRAFT_BYTES" in\n'
     "        ''|*[!0-9]*) MEASURE_OK=0 ;;\n"
     "    esac\n",
     "",
     "test_check_memory_index.PreCommitHook."
     "test_a_failing_wc_does_not_switch_the_budget_off_silently"),

    ("явная проверка статуса sed снята", HOOK,
     '    if [ "$ESCAPE_STATUS" != 0 ] || [ -z "$DRAFT_ARGS" ]; then',
     '    if [ -z "$DRAFT_ARGS" ]; then',
     "test_check_memory_index.PreCommitHook."
     "test_a_failing_sed_does_not_abort_the_hook"),

    ("счётчик не пересчитывается после отсева кавычек", HOOK,
     '    DRAFT_COUNT=0\n    if [ -n "$DRAFTS" ]; then\n'
     "        DRAFT_COUNT=$(printf '%s\\n' \"$DRAFTS\" | grep -c . || true)\n"
     "    fi\nfi",
     "fi",
     "test_check_memory_index.PreCommitHook."
     "test_drafts_named_in_quotes_do_not_fake_a_failure"),

    ("ограничитель обратного прохода снят", LINTER,
     "        while start > floor and is_name_char(text[start - 1]):",
     "        while start > 0 and is_name_char(text[start - 1]):",
     "test_check_memory_index.FilenameTokenScan."
     "test_many_anchors_inside_one_run_do_not_blow_up"),
]

DISTINGUISHES = "различает"
BLIND = "НЕ РАЗЛИЧАЕТ"
SKIPPED = "пропущен"
STALE = "ЯКОРЬ"
HUNG = "ЗАВИС"


def copy_tree(destination):
    """Копия дерева без .git и мусора сборки."""
    shutil.copytree(REPO, destination, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", ".mutation-backup"))


def apply_revert(folder, relative, old, new):
    """Возвращает текст жалобы, если откат не применился ровно один раз."""
    target = os.path.join(folder, relative)
    with io.open(target, encoding="utf-8") as stream:
        text = stream.read()
    found = text.count(old)
    if found != 1:
        return "фрагмент найден %d раз вместо одного" % found
    with io.open(target, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text.replace(old, new, 1))
    return None


def run_test(folder, test):
    """Прогон одного именованного теста на копии. Вердикт и хвост вывода."""
    try:
        done = subprocess.run(
            [sys.executable, "-m", "unittest", test],
            cwd=os.path.join(folder, "scripts"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Отдельный вердикт, а не «не различает». Формально зависание
        # реализации ОТЛИЧАЕТ - но отличать их временем ожидания негодно:
        # такой тест не скажет, что сломалось, и на медленной машине
        # покраснеет сам по себе. Первая же находка инструмента была
        # именно такой: тест звал main(), и на откате тот уходил в
        # настоящий прогон мутаций.
        return HUNG, "тест не ответил за %d с" % TEST_TIMEOUT
    printed = done.stdout.decode("utf-8", "replace")
    tail = (printed.strip().splitlines() or [""])[-1]
    # Пропущенный тест возвращает НОЛЬ, как и прошедший. Без этой ветки
    # `skipTest` на чужой системе печатался бы как «не различает», а на своей
    # - как «различает», и инструмент врал бы в обе стороны.
    if "skipped=" in printed or " skipped " in printed:
        return SKIPPED, tail
    if done.returncode == 0:
        return BLIND, tail
    return DISTINGUISHES, tail


def check_one(case, folder):
    _name, relative, old, new, test = case
    stale = apply_revert(folder, relative, old, new)
    if stale:
        return STALE, stale
    return run_test(folder, test)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Проверка починок: падает ли тест, если починку "
                    "откатить. Рабочее дерево не трогает - откат "
                    "применяется к копии.",
        epilog="Коды возврата: 0 - каждая починка различается своим тестом, "
               "1 - какая-то не различается или её якорь протух.")
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

    base = tempfile.mkdtemp(prefix="revert-check-")
    width = max(len(case[0]) for case in REVERTS)
    blind = []
    stale = []
    skipped = []
    hung = []
    try:
        for number, case in enumerate(REVERTS, 1):
            folder = os.path.join(base, "case%02d" % number)
            copy_tree(folder)
            if args.anchors_only:
                complaint = apply_revert(folder, case[1], case[2], case[3])
                verdict = STALE if complaint else "применим"
                detail = complaint or "фрагмент на месте"
            else:
                verdict, detail = check_one(case, folder)
            if verdict == BLIND:
                blind.append(case[0])
            elif verdict == STALE:
                stale.append(case[0])
            elif verdict == SKIPPED:
                skipped.append(case[0])
            elif verdict == HUNG:
                hung.append(case[0])
            print("[%2d/%d] %-12s %-*s  %s"
                  % (number, len(REVERTS), verdict, width, case[0], detail),
                  flush=True)
            # Копия больше не нужна: держать двенадцать деревьев разом
            # незачем, а на Windows они ещё и заметны на диске.
            shutil.rmtree(folder, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print()
    if args.anchors_only and not stale:
        print("Все %d откатов применимы: фрагмент на месте." % len(REVERTS))
        return 0
    for name in stale:
        print("Якорь протух: %s" % name, file=sys.stderr)
    for name in blind:
        print("Не различает: %s" % name, file=sys.stderr)
    for name in hung:
        print("Тест не ответил (различать зависанием негодно): %s" % name,
              file=sys.stderr)
    if skipped:
        print("Пропущено на этой системе (различение не проверено): %d из %d"
              % (len(skipped), len(REVERTS)))
        for name in skipped:
            print("   %s" % name)
    if stale or blind or hung:
        print("Починок без охраны: %d, протухших якорей: %d, зависших: %d."
              % (len(blind), len(stale), len(hung)), file=sys.stderr)
        return 1
    print("Все %d починок различаются своими тестами." % len(REVERTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
