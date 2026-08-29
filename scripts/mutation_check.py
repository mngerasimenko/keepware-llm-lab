#!/usr/bin/env python3
"""Мутационная проверка: врут ли тесты.

Зелёный набор тестов говорит «ошибок не нашли», а не «ошибки не пройдут».
Разница видна, только если сломать код нарочно и посмотреть, заметит ли
кто-нибудь. Здесь это делается по списку: каждая мутация - точечная порча
значимой ветки, после которой набор обязан покраснеть.

Выжившая мутация означает ветку, которую можно молча сломать: ни один тест
её не держит. Это не «тесты плохие» - это конкретный адрес, куда дописать
проверку.

Вопрос ставится именно так: заметит ли мутацию ХОТЬ ОДИН тест из набора.
Не «работает ли тест, написанный под эту мутацию» - на такой вопрос легко
ответить «да», подобрав тест под правку, о которой уже думал.

Запуск из корня репозитория:

    python scripts/mutation_check.py

Коды возврата: 0 - все мутации пойманы; 1 - какая-то выжила либо якорь
мутации больше не находится в коде (см. ниже).

**Инструмент правит файлы в рабочем дереве** и возвращает их обратно.
Запись атомарная (временный файл рядом плюс подмена через os.replace), так
что прерывание не оставит файл пустым - но запускать всё равно стоит на
чистом дереве, чтобы `git status` после прогона был пустым и чужие правки
не смешались с мутациями.

Перед мутациями набор прогоняется на нетронутом коде. Если он красный и
без мутаций, инструмент отказывается работать: иначе «поймана» печаталось
бы на каждой мутации по причине, к ней отношения не имеющей.

**Про якоря.** Мутация ищет точный фрагмент кода. Когда код меняется,
фрагмент перестаёт находиться - и молчаливый пропуск такой мутации
превратил бы этот инструмент ровно в то, что он ищет: проверку, которая
ничего не проверяет и рапортует успех. Поэтому ненайденный якорь - ошибка.
Правило простое: поменяли ветку - поправьте её мутацию здесь же.
"""

import io
import os
import stat
import subprocess
import sys
import tempfile

LINTER = os.path.join("scripts", "check_memory_index.py")
HOOK = os.path.join(".githooks", "pre-commit")
# Слепок файла, пока он мутирован. try/finally спасает от Ctrl+C, но не от
# закрытого терминала, kill, OOM или пропавшего электричества. Оставшаяся
# мутация в глаза не бросается: файл выглядит правдоподобно, линтер отвечает
# «Память согласована», хук пропускает коммиты - то есть инструмент, который
# ищет тихие отказы, производит тихий отказ в самой защите. Проверено: убил
# прогон, `TASK_MARKS = set()` осталось в дереве, прогон по памяти - код 0.
BACKUP = ".mutation-backup"

# (что ломаем, файл, точный фрагмент, чем заменить)
MUTATIONS = [
    # --- разбор индекса ---
    ("забор кода закрывается чем угодно", LINTER,
     "    return mark[0] == opened[0] and len(mark) >= len(opened)",
     "    return True"),
    ("чеклисты разбираются как ссылки-метки", LINTER,
     'TASK_MARKS = {"", "x"}', "TASK_MARKS = set()"),
    ("при повторе метки побеждает последнее определение", LINTER,
     "            definitions.setdefault(definition.group(1).strip().casefold(),\n"
     "                                   definition.group(2).strip())",
     "            definitions[definition.group(1).strip().casefold()] = definition.group(2).strip()"),
    ("счётчик потерянных строк не сбрасывается забором", LINTER,
     '                opened_fence = ""\n                lost_rows = 0',
     '                opened_fence = ""'),
    ("счётчик потерянных строк не сбрасывается комментарием", LINTER,
     "            # раздували число в сообщении о настоящей потере.\n            lost_rows = 0",
     "            # раздували число в сообщении о настоящей потере."),
    ("адрес метки не понимает угловые скобки", LINTER,
     r'DEFINITION = re.compile(r"^\[([^\]]+)\]:\s*(<[^>]*>|\S+)")',
     r'DEFINITION = re.compile(r"^\[([^\]]+)\]:\s*(\S+)")'),
    ("метка orphan-ok действует изнутри блока кода", LINTER,
     "        if mark:\n            opened_fence = mark\n            continue\n"
     '        if COMMENT_ORPHAN.search(INLINE_CODE.sub("", line)):',
     '        if COMMENT_ORPHAN.search(INLINE_CODE.sub("", line)):'),
    ("граница имени файла справа снята", LINTER,
     r'MD_ANCHOR = re.compile(r"\.md(?![\w\-])(?!\.\w)", re.I)',
     r'MD_ANCHOR = re.compile(r"\.md", re.I)'),
    ("граница имени файла слева снята", LINTER,
     "        while start > 0 and is_name_char(text[start - 1]):\n"
     "            start -= 1",
     "        pass"),

    # --- три инварианта ---
    ("имя индекса сравнивается без учёта регистра", LINTER,
     "            if os.path.basename(rel) == index_name]",
     "            if os.path.basename(rel).casefold() == index_name.casefold()]"),
    ("корневым считается любой найденный индекс", LINTER,
     "    roots = {index_name} if index_name in index_rels else set()",
     "    roots = set(index_rels)"),
    ("достижимость перестала быть транзитивной", LINTER,
     "            if target not in reachable:\n                reachable.add(target)\n"
     "                queue.append(target)",
     "            reachable.add(target)"),
    ("строки недостижимых индексов засчитываются", LINTER,
     "        if where not in reachable:\n            continue", "        pass"),
    ("метка orphan на под-индексе отмывает ветку за ним", LINTER,
     "            if is_orphan_ok(exact[rel]):", "            if True:"),
    ("обход скрытого перестал помнить посещённые узлы", LINTER,
     "            if (actual_rel in seen or actual_rel == start\n"
     "                    or actual_rel in reachable or actual_rel in referenced):",
     "            if (actual_rel == start\n"
     "                    or actual_rel in reachable or actual_rel in referenced):"),
    ("подсчёт скрытого считает и то, что видно другим путём", LINTER,
     "or actual_rel in reachable or actual_rel in referenced):", "):"),
    ("шаблоны --allow-orphan не нормализуются", LINTER,
     '    allow_globs = [pattern.replace("\\\\", "/") for pattern in allow_globs]',
     "    allow_globs = list(allow_globs)"),
    ("дубли заголовков не сообщаются", LINTER,
     "        if len(paths) > 1:", "        if False:"),
    # Плаcходержатели убираем вместе с текстом: настоящая регрессия выглядит
    # именно так - действие выпало целиком, а строка осталась связной.
    ("сообщение про сироту снова без действия", LINTER,
     '            "L2 %s не упомянут ни в одном индексе - агент его не увидит%s%s. "\n'
     '            "Добавьте в %s строку `- [Заголовок](%s) - крючок`; если файл вне "\n'
     '            "индекса намеренно - `orphan: true` в его шапку"\n'
     "            % (rel, hint, head_note, index_name, rel))",
     '            "L2 %s не упомянут ни в одном индексе - агент его не увидит%s%s"\n'
     "            % (rel, hint, head_note))"),
    ("сироту за недостижимым индексом шлют чинить не туда", LINTER,
     "                % (rel, stranded[rel], index_name, index_name, stranded[rel])",
     "                % (rel, stranded[rel], index_name, index_name, rel)"),

    # --- L4: ссылки [[...]] между фактами ---
    ("битые ссылки [[...]] не сообщаются", LINTER,
     "    dangling = dangling_wiki_links(exact, cache)", "    dangling = []"),
    ("ссылка [[...]] не резолвится по полю name", LINTER,
     "                forms.add(match.group(1).strip())", "                pass"),
    ("проза в двойных скобках считается ссылкой", LINTER,
     r'WIKI_LINK = re.compile(r"\[\[([^\s\[\]|#]{2,})(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")',
     r'WIKI_LINK = re.compile(r"\[\[([^\[\]|#]{2,})(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")'),
    ("якорь в ссылке [[имя#раздел]] обрывает разбор", LINTER,
     r'WIKI_LINK = re.compile(r"\[\[([^\s\[\]|#]{2,})(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")',
     r'WIKI_LINK = re.compile(r"\[\[([^\s\[\]|#]{2,})(?:\|[^\]\n]*)?\]\]")'),
    ("подсказка про формат берёт голое имя при однофамильцах", LINTER,
     "        unique_name = basename if namesake_counts[basename] == 1 else None",
     "        unique_name = basename"),
    ("часть битых ссылок не печатается", LINTER,
     "    for rel, lineno, target, guess in dangling:",
     "    for rel, lineno, target, guess in dangling[:1]:"),
    ("подсказка про похожий файл исчезла", LINTER,
     "                guess = next(iter(candidates)) if len(candidates) == 1 else None",
     "                guess = None"),
    ("подсказка выдаётся и когда кандидатов несколько", LINTER,
     "                guess = next(iter(candidates)) if len(candidates) == 1 else None",
     "                guess = next(iter(sorted(candidates))) if candidates else None"),
    ("под --quiet найденное снова прячется", LINTER,
     "    for line in warnings:\n        print(line)",
     "    if not args.quiet:\n        for line in warnings:\n            print(line)"),
    ("метка orphan-ok снова действует из инлайнового кода", LINTER,
     '        if COMMENT_ORPHAN.search(INLINE_CODE.sub("", line)):',
     "        if COMMENT_ORPHAN.search(line):"),
    ("строка индекса без адреса пропускается молча", LINTER,
     '                errors.append("L1 %s:%d строка без адреса: «%s»" % (where, lineno, title))',
     "                pass"),
    ("расхождение регистра в ссылке не ошибка", LINTER,
     '                errors.append(\n                    "L1 %s:%d регистр не совпадает:',
     '                pass or errors.append(\n                    "L1 %s:%d регистр не совпадает:'),
    ("ссылка за пределы папки памяти не ошибка", LINTER,
     '                errors.append(\n                    "L1 %s:%d ссылка выходит за папку памяти: %s "',
     '                pass or errors.append(\n                    "L1 %s:%d ссылка выходит за папку памяти: %s "'),
    ("файл объявляется источником самого себя", LINTER,
     "            if source == rel:\n                source = None", "            pass"),
    ("самоотсев в карте упоминаний убран", LINTER,
     "            if candidate == rel:\n                continue", "            pass"),

    ("ссылка на каталог диагностируется как ссылка в никуда", LINTER,
     "            elif os.path.isdir(absolute):", "            elif False:"),
    ("незакрытая шапка не объясняет, почему метка не сработала", LINTER,
     "        if has_unclosed_frontmatter(exact[rel]):",
     "        if False:"),
    ("горизонтальная линейка выдаётся за незакрытую шапку", LINTER,
     "    looks_like_yaml = any(YAML_PAIR.match(line) for line in head)\n"
     "    return [], looks_like_yaml",
     "    return [], True"),
    ("разные формы записи имени считаются разными файлами", LINTER,
     '    return unicodedata.normalize("NFC", text)', "    return text"),
    ("однофамильцы считаются неверно", LINTER,
     "                namesakes = namesake_counts[os.path.basename(rel)]",
     "                namesakes = 1"),
    ("догадка по голому имени выдаётся за факт", LINTER,
     "                if by_name is not None and namesakes > 1:",
     "                if False:"),

    # --- обход файловой системы ---
    ("скрытые каталоги обходятся как память", LINTER,
     '        if name.startswith("."):\n            continue', "        pass"),
    ("связанные каталоги обходятся как своя память", LINTER,
     "        if is_linked_dir(os.path.join(folder, name)):",
     "        if False and is_linked_dir(os.path.join(folder, name)):"),
    ("отказ доступа при обходе снова глотается", LINTER,
     "    for folder, dirs, names in os.walk(root, onerror=remember):",
     "    for folder, dirs, names in os.walk(root):"),

    # --- аргументы и коды возврата ---
    ("глоб в --index принимается вместо имени", LINTER,
     '        if any(ch in args.index for ch in "*?["):', "        if False:"),
    ("отсутствие корневого индекса не отказ", LINTER,
     "        if not any(os.path.dirname(p) == root for p in index_paths):",
     "        if False:"),
    ("пропавший корневой индекс снова невыполнимая проверка", LINTER,
     "            if orphaned:", "            if False:"),
    ("памятью считается любой файл, шапка не смотрится", LINTER,
     "    return any(MEMORY_FIELD.match(line) for line in head)",
     "    return True"),
    ("незакрытый забор не делает прогон непроверяемым", LINTER,
     "    return errors, notices, warnings, row_count, bool(incomplete or unreadable_dirs)",
     "    return errors, notices, warnings, row_count, False"),
    ("непроверяемый прогон отдаёт успех", LINTER,
     "    if unverifiable and not errors:\n        return EXIT_USAGE",
     "    if unverifiable and not errors:\n        return EXIT_OK"),
    ("подсказка про плоскую раскладку исчезла", LINTER,
     "    for stray in looks_like_a_stray_index(root, index_name, exact):",
     "    for stray in []:"),

    # --- хук ---
    ("печать подсказки снова может уронить хук", HOOK,
     "    printf '%s\\n' \"$*\" >&2 || :", "    printf '%s\\n' \"$*\" >&2"),
    ("хук блокирует коммит на непроверяемом прогоне", HOOK,
     "    2)\n        # Проверка не смогла выполниться - это не нарушение памяти.",
     "    99)\n        # Проверка не смогла выполниться - это не нарушение памяти."),
    ("отсутствие строки импорта больше не замечается", HOOK,
     'if [ -n "$CONFIG_SEEN" ] && [ -z "$IMPORT_SEEN" ]; then',
     "if false; then"),
    ("строка импорта ищется только в корневом конфиге", HOOK,
     "for config in CLAUDE.md AGENTS.md .claude/CLAUDE.md .claude/AGENTS.md; do",
     "for config in CLAUDE.md; do"),
    ("имя индекса в проверке импорта зашито намертво", HOOK,
     '        [ -n "$IMPORT_CANDIDATE" ] && IMPORT_INDEX=$IMPORT_CANDIDATE',
     "        :"),
    ("неотслеживаемые черновики снова блокируют коммит", HOOK,
     'DRAFTS=$(git ls-files --others --exclude-standard -- "$MEMORY_DIR" 2>/dev/null || true)',
     'DRAFTS=""'),
    ("путь черновика не срезается до папки памяти", HOOK,
     '        set -- "$@" --allow-orphan "${draft#"$MEMORY_DIR"/}"',
     '        set -- "$@" --allow-orphan "$draft"'),
    # `set +f` стоит теперь не сразу за разбором: между ними сбор черновиков,
    # которому раскрытие шаблонов тоже противопоказано. Мутация снимает
    # именно `set -f`, а парный `set +f` ниже безвреден и без него.
    ("аргументы из настройки раскрываются шаблоном", HOOK,
     "set -f\n# shellcheck disable=SC2086\nset -- $EXTRA_ARGS",
     "# shellcheck disable=SC2086\nset -- $EXTRA_ARGS"),
]


def write_atomically(path, text):
    """Замена содержимого без окна, в котором файл пуст.

    `open(path, "w")` усекает файл до нуля ЕЩЁ ДО записи. Между усечением и
    `write()` есть точка, где CPython проверяет отложенные сигналы: Ctrl+C,
    закрытие терминала, ошибка диска или лок антивируса в этот момент
    оставляют файл пустым. Инструмент, который правит чужой рабочий файл и
    может его обнулить, опаснее любой ошибки, которую он ищет.

    Пишем во временный файл рядом (та же файловая система - иначе замена не
    атомарна) и подменяем через os.replace: он либо отработал целиком, либо
    не тронул оригинал. Работает и на POSIX, и на Windows.
    """
    folder = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        # Права переносим с оригинала. mkstemp создаёт файл с правами 0600, а
        # замена оставляет права ИСТОЧНИКА - значит на POSIX первый же прогон
        # снял бы с .githooks/pre-commit бит исполняемости. Неисполняемый хук
        # git пропускает МОЛЧА: инструмент, ищущий тихие отказы, произвёл бы
        # тихий отказ в защите, которая охраняет память. На Windows этого не
        # видно вовсе - там замена сохраняет права цели.
        try:
            os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass  # оригинала ещё нет - оставляем права по умолчанию
        with io.open(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_backup(path, text):
    """Кладёт оригинал рядом на время, пока файл мутирован."""
    write_atomically(BACKUP, path + "\n" + text)


def drop_backup():
    try:
        os.remove(BACKUP)
    except OSError:
        pass


def restore_interrupted():
    """Возвращает файл, если прошлый прогон оборвали. True, если пришлось."""
    if not os.path.isfile(BACKUP):
        return False
    with io.open(BACKUP, encoding="utf-8") as stream:
        saved = stream.read()
    path, _newline, text = saved.partition("\n")
    if not path:
        drop_backup()
        return False
    write_atomically(path, text)
    drop_backup()
    print("Прошлый прогон был оборван на середине - %s восстановлен из слепка."
          % path)
    return True


def missing_anchors():
    """Мутации, чей фрагмент больше не находится ровно один раз."""
    stale = []
    for name, path, old, _new in MUTATIONS:
        try:
            text = io.open(path, encoding="utf-8").read()
        except OSError as exc:
            stale.append((name, "файл не читается: %s" % exc))
            continue
        found = text.count(old)
        if found != 1:
            stale.append((name, "фрагмент найден %d раз вместо одного" % found))
    return stale


def suite_fails():
    """True, если набор тестов покраснел. Останавливаемся на первом падении.

    Гоняем ТОЛЬКО `test_check_memory_index.py`, и это не удобство, а условие
    осмысленности. Самотесты инструмента (`test_mutation_check_self.py`)
    проверяют, что якоря мутаций находятся в коде - а активная мутация свой
    же якорь и заменяет. Лежи они в измеряемом наборе, он краснел бы на
    КАЖДОЙ мутации, и отчёт «все пойманы» печатался бы независимо от того,
    заметил ли мутацию хоть один содержательный тест. Инструмент,
    спрашивающий «врут ли тесты», врал бы сам - на всех прогонах.

    Фильтровать по имени класса нельзя: у `unittest` ключ `-k` не понимает
    отрицания (это синтаксис pytest), и `-k "not X"` молча отбирает ноль
    тестов. Поэтому разделение файлами, а не фильтром.
    """
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "scripts",
         "-p", "test_check_memory_index.py", "-f"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # MEMCHECK_REQUIRE_SH здесь по той же причине, что и в CI. Без sh
        # тесты хука пропускаются, и три мутации по .githooks/pre-commit
        # печатались бы как ВЫЖИВШИЕ - то есть «ветку не держит ни один
        # тест», хотя тесты просто не запускались. Инструмент, который
        # спрашивает «врут ли тесты», соврал бы сам, и ровно тем способом,
        # который ищет. С переменной набор краснеет ДО мутаций, на первом же
        # прогоне «зелёный ли он без них», и причина названа вслух.
        env=dict(os.environ, PYTHONIOENCODING="utf-8",
                 MEMCHECK_REQUIRE_SH="1"))
    return result.returncode != 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if not os.path.isfile(LINTER):
        print("Запускать из корня репозитория: %s не найден" % LINTER, file=sys.stderr)
        return 1

    # Прибираемся за прошлым прогоном ДО всего остального: пока мутация лежит
    # в дереве, и якоря протухли, и набор красный - обе следующие проверки
    # отчитались бы о чужой беде.
    restore_interrupted()

    # Протухший якорь - это молчаливо пропущенная мутация, то есть ровно тот
    # тихий отказ, который инструмент и ищет. Поэтому проверяем ДО прогона и
    # отказываемся работать, а не рапортуем неполный успех.
    stale = missing_anchors()
    if stale:
        print("Мутации отстали от кода - поправьте их здесь же:", file=sys.stderr)
        for name, why in stale:
            print("   %s: %s" % (name, why), file=sys.stderr)
        return 1

    # Без этого прогона всё дальнейшее бессмысленно: если набор красный по
    # своей причине - недостающий sh, чужая правка в дереве, сломанный тест, -
    # то краснеть он будет и на каждой мутации, и отчёт «все пойманы» окажется
    # рапортом о причине, к мутациям отношения не имеющей.
    print("Проверяю, что набор зелёный без мутаций...")
    if suite_fails():
        print("Тесты не проходят и БЕЗ мутаций - сначала почините дерево.",
              file=sys.stderr)
        print("Пока набор красный, любая мутация засчитается «пойманной» по "
              "чужой причине.", file=sys.stderr)
        return 1

    survived = []
    for number, (name, path, old, new) in enumerate(MUTATIONS, 1):
        original = io.open(path, encoding="utf-8").read()
        save_backup(path, original)
        try:
            write_atomically(path, original.replace(old, new))
            caught = suite_fails()
        finally:
            write_atomically(path, original)
            drop_backup()
        print("[%2d/%d] %-8s %s" % (number, len(MUTATIONS),
                                    "поймана" if caught else "ВЫЖИЛА", name))
        if not caught:
            survived.append(name)

    print()
    if survived:
        print("Выжило мутаций: %d из %d." % (len(survived), len(MUTATIONS)))
        print("Каждая - ветка, которую можно молча сломать:")
        for name in survived:
            print("   " + name)
        return 1
    print("Все %d мутаций пойманы: набор держит каждую проверенную ветку."
          % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
