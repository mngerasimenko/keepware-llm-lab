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

Файлы правятся на месте и возвращаются в исходное состояние в блоке
finally - но это работа с рабочим деревом, поэтому запускать имеет смысл
на чистом дереве, чтобы `git diff` после прогона был пустым.

**Про якоря.** Мутация ищет точный фрагмент кода. Когда код меняется,
фрагмент перестаёт находиться - и молчаливый пропуск такой мутации
превратил бы этот инструмент ровно в то, что он ищет: проверку, которая
ничего не проверяет и рапортует успех. Поэтому ненайденный якорь - ошибка.
Правило простое: поменяли ветку - поправьте её мутацию здесь же.
"""

import io
import os
import subprocess
import sys

LINTER = os.path.join("scripts", "check_memory_index.py")
HOOK = os.path.join(".githooks", "pre-commit")

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
     "            in_comment = False\n            # Комментарий закрылся штатно",
     "            in_comment = False\n            lost_rows = lost_rows\n            # Комментарий закрылся штатно"),
    ("адрес метки не понимает угловые скобки", LINTER,
     r'DEFINITION = re.compile(r"^\[([^\]]+)\]:\s*(<[^>]*>|\S+)")',
     r'DEFINITION = re.compile(r"^\[([^\]]+)\]:\s*(\S+)")'),
    ("метка orphan-ok действует изнутри блока кода", LINTER,
     "        if mark:\n            opened_fence = mark\n            continue\n"
     "        if COMMENT_ORPHAN.search(line):",
     "        if COMMENT_ORPHAN.search(line):"),
    ("граница имени файла справа снята", LINTER,
     r'FILENAME_TOKEN = re.compile(r"[\w.\-/\\]+\.md(?![\w\-])(?!\.\w)", re.I)',
     r'FILENAME_TOKEN = re.compile(r"[\w.\-/\\]+\.md", re.I)'),

    # --- три инварианта ---
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
    ("подсчёт скрытого считает и то, что видно другим путём", LINTER,
     "or actual_rel in reachable or actual_rel in referenced):", "):"),
    ("шаблоны --allow-orphan не нормализуются", LINTER,
     '    allow_globs = [pattern.replace("\\\\", "/") for pattern in allow_globs]',
     "    allow_globs = list(allow_globs)"),
    ("дубли заголовков не сообщаются", LINTER,
     "        if len(paths) > 1:", "        if False:"),
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
    ("аргументы из настройки раскрываются шаблоном", HOOK,
     "set -f\n# shellcheck disable=SC2086\nset -- $EXTRA_ARGS\nset +f",
     "# shellcheck disable=SC2086\nset -- $EXTRA_ARGS"),
]


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
    """True, если набор тестов покраснел. Останавливаемся на первом падении."""
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "scripts",
         "-p", "test_*.py", "-f"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
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

    # Протухший якорь - это молчаливо пропущенная мутация, то есть ровно тот
    # тихий отказ, который инструмент и ищет. Поэтому проверяем ДО прогона и
    # отказываемся работать, а не рапортуем неполный успех.
    stale = missing_anchors()
    if stale:
        print("Мутации отстали от кода - поправьте их здесь же:", file=sys.stderr)
        for name, why in stale:
            print("   %s: %s" % (name, why), file=sys.stderr)
        return 1

    survived = []
    for number, (name, path, old, new) in enumerate(MUTATIONS, 1):
        original = io.open(path, encoding="utf-8").read()
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            original.replace(old, new))
        try:
            caught = suite_fails()
        finally:
            io.open(path, "w", encoding="utf-8", newline="\n").write(original)
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
