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

import argparse
import hashlib
import io
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

LINTER = os.path.join("scripts", "check_memory_index.py")
HOOK = os.path.join(".githooks", "pre-commit")
# Слепок файла, пока он мутирован. try/finally спасает от Ctrl+C, но не от
# закрытого терминала, kill, OOM или пропавшего электричества. Оставшаяся
# мутация в глаза не бросается: файл выглядит правдоподобно, линтер отвечает
# «Память согласована», хук пропускает коммиты - то есть инструмент, который
# ищет тихие отказы, производит тихий отказ в самой защите. Проверено: убил
# прогон, `TASK_MARKS = set()` осталось в дереве, прогон по памяти - код 0.
BACKUP = ".mutation-backup"
# Потолок на один прогон набора. Замер на Windows: базовый прогон от двух
# до четырёх с половиной минут, а две мутации намеренно возвращают
# квадратичность и растягивают его дальше. Запас тут примерно двукратный,
# а не пятнадцатикратный, как читалось из прежней оценки «около минуты»:
# сорвавшийся таймаут печатается как «ЗАВИСЛА» и идёт в выжившие.
SUITE_TIMEOUT = 900

# (что ломаем, файл, точный фрагмент, чем заменить)
MUTATIONS = [
    # --- разбор индекса ---
    ("забор кода закрывается чем угодно", LINTER,
     "    return mark[0] == opened[0] and len(mark) >= len(opened)",
     "    return True"),
    # Три мутации - про чеклисты, про приоритет первого определения и про
    # угловые скобки в адресе метки - удалены вместе с формами, которые они
    # охраняли. Осталась одна: чужая форма обязана отвергаться ГРОМКО, иначе
    # строки не разберутся молча и файлы всплывут сиротами.
    ("чужая форма строки индекса пропускается молча", LINTER,
     '            rows.append((reference.group(1).strip(), "", lineno, "wrong-form"))',
     "            pass"),
    ("определение метки пропускается молча", LINTER,
     '            rows.append((definition.group(1).strip(), "", lineno, "wrong-form"))',
     "            pass"),
    ("счётчик потерянных строк не сбрасывается забором", LINTER,
     '                opened_fence = ""\n                lost_rows = 0',
     '                opened_fence = ""'),
    # Якорь опирается на СЛЕДУЮЩУЮ СТРОКУ КОДА, а не на комментарий над ним:
    # сам по себе «lost_rows = 0» в файле дважды, а переформулировка
    # комментария, к проверяемой ветке отношения не имеющего, роняла бы
    # инструмент на пустом месте.
    ("счётчик потерянных строк не сбрасывается комментарием", LINTER,
     "            lost_rows = 0\n        line = COMPLETE_COMMENT",
     "        line = COMPLETE_COMMENT"),
    # Две мутации про метку-комментарий («действует изнутри блока кода» и
    # «действует из инлайнового кода») удалены вместе с самой меткой: способ
    # пометить файл теперь один, и держится он шапкой, а не обходом заборов.
    ("метка orphan читается не только из шапки", LINTER,
     "    head, _unclosed = frontmatter_lines(cached_text(path, rel, cache))\n"
     "    return any(FRONTMATTER_ORPHAN.match(line) for line in head)",
     "    return any(FRONTMATTER_ORPHAN.match(line)\n"
     "               for line in cached_text(path, rel, cache).splitlines())"),
    ("граница «user.mdx - другой файл» снята", LINTER,
     r'MD_ANCHOR = re.compile(r"\.md(?![\w\-])(?!\.\w)", re.I)',
     r'MD_ANCHOR = re.compile(r"\.md(?!\.\w)", re.I)'),
    ("граница «user.md.txt - другой файл» снята", LINTER,
     r'MD_ANCHOR = re.compile(r"\.md(?![\w\-])(?!\.\w)", re.I)',
     r'MD_ANCHOR = re.compile(r"\.md(?![\w\-])", re.I)'),
    ("граница имени файла слева снята", LINTER,
     "        while start > floor and is_name_char(text[start - 1]):\n"
     "            start -= 1",
     "        pass"),
    ("точка перестала быть частью имени памяти", LINTER,
     'NAME_PUNCTUATION = "_.-/\\\\"',
     'NAME_PUNCTUATION = "_-/\\\\"'),
    ("имя начинается прямо после чужого расширения", LINTER,
     "        floor = match.end()",
     "        floor = 0"),
    ("имя открывается разделителем", LINTER,
     '        while start < match.start() and text[start] in "/\\\\":\n'
     "            start += 1",
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
     "            if is_orphan_ok(exact[rel], rel, cache):", "            if True:"),
    # Мутация «нечитаемый файл сходит за намеренную сироту» удалена вместе со
    # сторожем, на который целилась: он возвращал ровно то же, что и код без
    # него (нечитаемый файл приходит пустой строкой, а в пустом тексте метки
    # нет), то есть был эквивалентным мутантом - вечным «ВЫЖИЛА», под который
    # сопровождающий раз за разом писал бы тест, ничего не меняющий.
    ("обход скрытого перестал помнить посещённые узлы", LINTER,
     "            if (actual_rel in seen or actual_rel == start\n"
     "                    or actual_rel in reachable or actual_rel in referenced):",
     "            if (actual_rel == start\n"
     "                    or actual_rel in reachable or actual_rel in referenced):"),
    # Два независимых операнда - две мутации. Общая печаталась «поймана»
    # целиком за счёт половины про `reachable`, а половину про `referenced`
    # (узел, перечисленный в достижимом индексе, спрятанным не считается)
    # не держал ни один тест. Тот же дефект, что расщепляли в bool(...).
    ("скрытым считается и то, что видно из достижимого индекса", LINTER,
     "or actual_rel in reachable or actual_rel in referenced):",
     "or actual_rel in reachable):"),
    ("скрытым считается и то, что уже достижимо", LINTER,
     "or actual_rel in reachable or actual_rel in referenced):",
     "or actual_rel in referenced):"),
    ("шаблоны --allow-orphan не нормализуются", LINTER,
     '    allow_globs = [pattern.replace("\\\\", "/") for pattern in allow_globs]',
     "    allow_globs = list(allow_globs)"),
    ("дубли заголовков не сообщаются", LINTER,
     "        if len(paths) > 1:", "        if False:"),
    ("пустые индексы считаются по всем сразу, а не по одному", LINTER,
     "            for where in empty_indexes:",
     "            for where in (empty_indexes if row_count == 0 else []):"),
    # Плаcходержатели убираем вместе с текстом: настоящая регрессия выглядит
    # именно так - действие выпало целиком, а строка осталась связной.
    ("сообщение про сироту снова без действия", LINTER,
     '            "L2 %s не упомянут ни в одном индексе - агент его не увидит%s%s. "\n'
     '            "Добавьте в %s строку `- [Заголовок](%s) - крючок`; если файл вне "\n'
     '            "индекса намеренно - `orphan: true` в его шапку"\n'
     "            % (rel, hint, head_note, index_name, rel))",
     '            "L2 %s не упомянут ни в одном индексе - агент его не увидит%s%s"\n'
     "            % (rel, hint, head_note))"),
    ("файл из недочитанного индекса снова объявляют забытым", LINTER,
     "        if cut_at:", "        if False:"),
    ("недочитанный индекс глушит и настоящих сирот", LINTER,
     "             if mentioned_in_raw_text({path: index_texts.get(path, \"\")},\n"
     "                                      rel, unique_name)),",
     "             if True),"),
    ("сироту за недостижимым индексом шлют чинить не туда", LINTER,
     "                % (rel, stranded[rel], index_name, index_name, stranded[rel])",
     "                % (rel, stranded[rel], index_name, index_name, rel)"),

    ("адреса в выводе снова не дописываются папкой памяти", LINTER,
     "    errors = [with_memory_folder(line, folder, known) for line in errors]",
     "    errors = list(errors)"),
    ("приписка ставится и там, где ведущий токен не путь", LINTER,
     "    if not match or match.group(2) not in known:\n        return line",
     "    if not match:\n        return line"),

    # --- L4: ссылки [[...]] между фактами ---
    ("битые ссылки [[...]] не сообщаются", LINTER,
     "    dangling = dangling_wiki_links(exact, cache, allow_globs)", "    dangling = []"),
    ("имена каталогов правилу не подчиняются", LINTER,
     "            if not MEMORY_FILE_NAME.match(folder):",
     "            if False:"),
    ("плохой каталог называется на каждом файле внутри", LINTER,
     "            if where in seen_dirs:\n                continue",
     "            pass"),
    ("имя файла памяти может быть любым", LINTER,
     "        if not MEMORY_FILE_NAME.match(stem):",
     "        if False:"),
    ("правило про имя распространяется и на индекс", LINTER,
     "        if base == index_name:\n            continue\n"
     "        stem = os.path.splitext(base)[0]",
     "        stem = os.path.splitext(base)[0]"),
    ("расхождение имени файла и поля name не ошибка", LINTER,
     "                if declared != expected:", "                if False:"),
    # Прежде эта мутация подменяла `expected` на `declared`, и сравнение
    # `declared != expected` становилось всегда ложным - то есть побайтово
    # тем же поведением, что у соседней мутации «расхождение не ошибка».
    # Две мутации, один смысл, и ветка, которую эта называет, не проверялась
    # ничем. Теперь она и правда про регистр.
    ("L5 сравнивает имена без учёта регистра", LINTER,
     "                if declared != expected:",
     "                if declared.casefold() != expected.casefold():"),
    ("проза в двойных скобках считается ссылкой", LINTER,
     "        if len(target) >= 2 and not any(char.isspace() for char in target):",
     "        if len(target) >= 2:"),
    ("якорь в ссылке [[имя#раздел]] обрывает разбор", LINTER,
     '        target = inner.split("#", 1)[0].split("|", 1)[0]',
     '        target = inner.split("|", 1)[0]'),
    ("разбор хвостов связи вернулся в регулярку", LINTER,
     r'WIKI_LINK = re.compile(r"\[\[([^\[\]\n]{2,})\]\]")',
     r'WIKI_LINK = re.compile(r"\[\[([^\s\[\]|#]{2,})(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")'),
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
    ("строка индекса без адреса пропускается молча", LINTER,
     '                errors.append(\n'
     '                    "L1 %s:%d строка без адреса: «%s». Допишите путь к файлу в "\n'
     '                    "круглых скобках: `- [%s](имя-файла.md) - крючок`"\n'
     "                    % (where, lineno, title, title))",
     "                pass"),
    ("сообщения L1 снова без действия", LINTER,
     '                    "L1 %s:%d ссылка в никуда: %s. Создайте этот файл либо "\n'
     '                    "поправьте путь в строке индекса; если файл больше не нужен "\n'
     '                    "- уберите строку" % (where, lineno, target))',
     '                    "L1 %s:%d ссылка в никуда: %s" % (where, lineno, target))'),
    # Обе мутации прежде вклеивали `pass or errors.append(...)`. `pass` -
    # оператор, а не выражение, поэтому мутант не компилировался: тестовый
    # файл импортирует линтер на уровне модуля, загрузка падала, unittest
    # отдавал ненулевой код, и инструмент печатал «поймана», не запустив ни
    # одного теста. Три года такой отчётности стоят ровно ничего.
    ("расхождение регистра в ссылке не ошибка", LINTER,
     "            if twin is not None:\n                errors.append(",
     "            if False:\n                errors.append("),
    ("ссылка за пределы папки памяти не ошибка", LINTER,
     '                errors.append(\n'
     '                    "L1 %s:%d ссылка выходит за папку памяти: %s "\n'
     '                    "(такой путь разрешается по-разному в зависимости от того, "\n'
     '                    "откуда открыли файл). Перенесите файл внутрь папки памяти "\n'
     '                    "и сошлитесь на него оттуда" % (where, lineno, target)\n'
     "                )",
     "                pass"),
    ("файл объявляется источником самого себя", LINTER,
     "            if source == rel:\n                source = None", "            pass"),
    ("самоотсев в карте упоминаний убран", LINTER,
     "            if candidate == rel:\n                continue", "            pass"),

    ("ссылка на каталог диагностируется как ссылка в никуда", LINTER,
     "            elif os.path.isdir(absolute):", "            elif False:"),
    ("незакрытая шапка не объясняет, почему метка не сработала", LINTER,
     "        if has_unclosed_frontmatter(exact[rel], rel, cache):",
     "        if False:"),
    ("горизонтальная линейка выдаётся за незакрытую шапку", LINTER,
     "    looks_like_yaml = any(YAML_PAIR.match(line) for line in head)\n"
     "    return [], looks_like_yaml",
     "    return [], True"),
    # Мутация про нормализацию юникода ушла вместе с самой нормализацией:
    # разные формы записи «é» возможны только в не-латинском имени, а L6 такие
    # имена запрещает. Обработчик того, чего правило не допускает.
    ("лишняя форма адреса пропускается молча", LINTER,
     "            wrong = extra_address_form(target)\n            if wrong:",
     "            wrong = extra_address_form(target)\n            if False:"),
    ("угловые скобки в адресе снова принимаются", LINTER,
     '    if target.startswith("<"):\n        return "в угловых скобках"',
     '    if False:\n        return "в угловых скобках"'),
    # Замена на 1 гасила ветку целиком - то есть делала ровно то же, что
    # соседняя мутация «догадка по голому имени выдаётся за факт», и счёт
    # однофамильцев не проверялся ничем. Двойка ветку, наоборот, включает
    # всегда: подсказка станет утверждать «(2)» там, где однофамилец один.
    ("однофамильцы считаются неверно", LINTER,
     "                namesakes = namesake_counts[os.path.basename(rel)]",
     "                namesakes = 2"),
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
    # Три причины «проверка выполнена не до конца» - три мутации, а не одна на
    # всех. Общая гасила разом незакрытый забор, нечитаемый каталог и
    # нечитаемый файл, называясь только про первое: теста на любую из трёх
    # хватало, чтобы напечатать «поймана» про две другие, которых не сторожит
    # никто.
    ("незакрытый забор не делает прогон непроверяемым", LINTER,
     "            bool(incomplete or unreadable_dirs\n"
     "                 or cache.get(UNREADABLE_KEY)))",
     "            bool(unreadable_dirs or cache.get(UNREADABLE_KEY)))"),
    ("нечитаемый каталог не делает прогон непроверяемым", LINTER,
     "            bool(incomplete or unreadable_dirs\n"
     "                 or cache.get(UNREADABLE_KEY)))",
     "            bool(incomplete or cache.get(UNREADABLE_KEY)))"),
    ("нечитаемый файл не делает прогон непроверяемым", LINTER,
     "            bool(incomplete or unreadable_dirs\n"
     "                 or cache.get(UNREADABLE_KEY)))",
     "            bool(incomplete or unreadable_dirs))"),
    ("нечитаемый файл-факт снова глотается молча", LINTER,
     "            cache.setdefault(UNREADABLE_KEY, set()).add(rel)",
     "            pass"),
    ("итог молчит про пропущенные связанные каталоги", LINTER,
     "        if file_map[3]:", "        if False:"),
    ("непроверяемый прогон отдаёт успех", LINTER,
     "    if unverifiable and not errors:\n        return EXIT_USAGE",
     "    if unverifiable and not errors:\n        return EXIT_OK"),
    ("подсказка про плоскую раскладку исчезла", LINTER,
     "    for stray in looks_like_a_stray_index(root, index_name, exact, cache,\n"
     "                                          allow_globs):",
     "    for stray in []:"),

    # --- хук ---
    ("на пропущенном коммите снова печатается всё дерево", HOOK,
     '    say "pre-commit: поставьте Python 3.9+ либо отключите хук."\n'
     "    advise_briefly",
     '    say "pre-commit: поставьте Python 3.9+ либо отключите хук."\n'
     "    advise_how_to_disable"),
    ("короткая форма молчит про core.hooksPath", HOOK,
     '    say "pre-commit: ключ core.hooksPath вслепую не снимайте - у husky в нём"\n'
     '    say "pre-commit: лежит свой набор, и он уберётся молча, без ошибки."',
     '    say "pre-commit: подробности - в README."'),
    # Сохранение переводов строк (read_source) мутацией не проверяется: оно
    # живёт в ЭТОМ файле, а инструмент правит только линтер и хук - мутировать
    # сам себя во время прогона он не может. Ветку держат три теста в
    # test_mutation_check_self.py (LineEndingsSurviveTheRewrite).
    ("печать подсказки снова может уронить хук", HOOK,
     "    printf '%s\\n' \"$*\" >&2 || :", "    printf '%s\\n' \"$*\" >&2"),
    # Якорь - только код: комментарий рядом к проверяемой ветке отношения не
    # имеет, а его переформулировка роняла бы инструмент на пустом месте.
    ("хук блокирует коммит на непроверяемом прогоне", HOOK,
     "    2)\n", "    99)\n"),
    ("отсутствие строки импорта больше не замечается", HOOK,
     'if [ -n "$CONFIG_SEEN" ] && [ -z "$IMPORT_SEEN" ]; then',
     "if false; then"),
    ("строка импорта ищется только в корневом конфиге", HOOK,
     "for config in CLAUDE.md AGENTS.md .claude/CLAUDE.md .claude/AGENTS.md; do",
     "for config in CLAUDE.md; do"),
    ("имя индекса в проверке импорта зашито намертво", HOOK,
     '    *"--index "*)\n'
     "        IMPORT_CANDIDATE=${IMPORT_ARGS#*--index }",
     '    *"--index "*)\n'
     "        IMPORT_CANDIDATE=",
     ),
    ("форма --index=ИМЯ снова не распознаётся", HOOK,
     '    *"--index="*)', '    *"--index-nikogda-ne-sovpadet="*)'),
    ("имя индекса уходит в grep как регулярка", HOOK,
     '    if grep -q "@[^[:space:]]*$IMPORT_PATTERN" "$config" 2>/dev/null; then',
     '    if grep -q "@[^[:space:]]*$IMPORT_INDEX" "$config" 2>/dev/null; then'),
    ("неотслеживаемые черновики снова блокируют коммит", HOOK,
     "DRAFTS=$(git -c core.quotePath=false ls-files --others --exclude-standard \\\n"
     '             -- "$MEMORY_DIR" 2>/dev/null || true)',
     'DRAFTS=""'),
    ("черновики из .gitignore снова блокируют коммит", HOOK,
     'IGNORED=$(git -c core.quotePath=false ls-files --others --ignored \\\n'
     '              --exclude-standard -- "$MEMORY_DIR" 2>/dev/null || true)',
     'IGNORED=""'),
    ("имя черновика с кириллицей снова приезжает в кавычках", HOOK,
     "DRAFTS=$(git -c core.quotePath=false ls-files --others --exclude-standard \\\n",
     "DRAFTS=$(git ls-files --others --exclude-standard \\\n"),
    ("путь черновика не срезается до папки памяти", HOOK,
     '        | sed -e "s|^$MEMORY_DIR/||" ',
     '        | sed -e "s|^||" '),
    ("значение ключа снова передаётся отдельным словом", HOOK,
     "              -e 's|^|--allow-orphan=|')",
     "              -e 's|^|--allow-orphan |')"),
    ("удаления из репозитория снова приезжают в кавычках", HOOK,
     "GONE=$(git -c core.quotePath=false diff --cached",
     "REMOVED=$(git --no-pager diff --cached"),
    ("переименование снова прячет удаление", HOOK,
     "           --name-only --no-renames --diff-filter=D",
     "              --name-only --diff-filter=D"),
    ("повтор без ключей теряет исключения черновиков", HOOK,
     '    "$PYTHON" "$CHECKER" "$MEMORY_DIR" --quiet "$@"\n    RETRY=$?',
     '    "$PYTHON" "$CHECKER" "$MEMORY_DIR" --quiet\n    RETRY=$?'),
    ("удаление всей папки памяти снова проходит молча", HOOK,
     '    if git diff --cached --name-only -- "$MEMORY_DIR" 2>/dev/null | grep -q .; then',
     "    if false; then"),
    # `set +f` стоит теперь не сразу за разбором: между ними сбор черновиков,
    # которому раскрытие шаблонов тоже противопоказано. Мутация снимает
    # именно `set -f`, а парный `set +f` ниже безвреден и без него.
    ("исключение не действует на похожий на индекс файл в корне", LINTER,
     "        if is_excluded(rel, allow_globs):\n            continue\n"
     "        rows, _unclosed, _lost = parse_index_text",
     "        rows, _unclosed, _lost = parse_index_text"),
    ("негодное имя снова роняет весь обход", LINTER,
     "            except ValueError:\n"
     "                unreadable_dirs.append(path)\n"
     "                continue",
     "            except ValueError:\n"
     "                raise"),
    ("заметка о нечитаемых файлах теряется при нечитаемом индексе", LINTER,
     "    for rel in sorted(cache.get(UNREADABLE_KEY, ())):\n"
     "        notices.append(",
     "    for rel in sorted(()):\n"
     "        notices.append("),
    ("потолок отступа пункта снят", LINTER,
     "            item_indent = indent + 1 + (gap if gap <= 4 else 1)",
     "            item_indent = indent + 1 + gap"),
    ("вычитание снова берёт переименования", HOOK,
     "              --name-only --diff-filter=D -- ",
     "              --name-only --no-renames --diff-filter=D -- "),
    ("аргументы из настройки раскрываются шаблоном", HOOK,
     "set -f\n", ""),

    # --- второй круг ревью ---
    ("исключение не распространяется на связи [[...]]", LINTER,
     "        if is_excluded(rel, allow_globs):\n            continue\n"
     '        opened_fence = ""',
     '        opened_fence = ""'),
    ("блок кода отсчитывается от левого края, а не от пункта", LINTER,
     "        if indent >= item_indent + 4:",
     "        if indent >= 4:"),
    ("буллет перестал задавать отступ содержимого", LINTER,
     "            item_indent = indent + 1 + (gap if gap <= 4 else 1)",
     "            item_indent = 0"),
    ("карта упоминаний снова читает файлы мимо кэша", LINTER,
     "        text = cached_text(path, rel, cache)",
     "        text = read_all([path]).get(path)"),
    ("удаления из индекса снова считаются черновиками", HOOK,
     "    DRAFTS=$KEPT", "    DRAFTS=$DRAFTS"),
    ("уход индекса из репозитория снова проходит молча", HOOK,
     '        say "pre-commit: $MEMORY_DIR/$IMPORT_INDEX уходит из репозитория этим коммитом."',
     "        :"),
    ("файлы скрытых каталогов снова съедают бюджет", HOOK,
     "    DRAFTS=$(printf '%s\\n' \"$DRAFTS\" | grep -v '/\\.[^/]*/' || true)",
     "    :"),
    ("бюджет мерит список путей, а не строку аргументов", HOOK,
     'DRAFT_BYTES=$(printf \'%s%s\' "$DRAFT_ARGS" "$EXTRA_ARGS" | wc -c | tr -d \' \')',
     'DRAFT_BYTES=$(printf \'%s\' "$DRAFTS" | wc -c | tr -d \' \')'),
    ("статус замера длины снова не проверяется", HOOK,
     'MEASURE_OK=1\n[ "$MEASURE_STATUS" = 0 ] || MEASURE_OK=0',
     'MEASURE_OK=1'),
    # Два независимых сторожа - две мутации. Пока запись была одна, её имя
    # обещало обе ветки, а мутировало статус: «поймана» печаталось про
    # проверку, которой мутация не касалась.
    ("результат замера длины снова не проверяется на число", HOOK,
     'case "$DRAFT_BYTES" in\n'
     "    ''|*[!0-9]*) MEASURE_OK=0 ;;\n"
     "esac\n",
     ""),
    ("ключи читаются снова через --get", HOOK,
     "EXTRA_ARGS=$IMPORT_ARGS",
     "EXTRA_ARGS=$(git config --get memorycheck.args 2>/dev/null || true)"),
]


def read_source(path):
    """Текст файла и то, какими у него были переводы строк.

    Читаем с трансляцией: якоря мутаций записаны через `\\n`, и на файле с
    CRLF без неё не нашёлся бы ни один. Но записать обратно надо тем же, чем
    было, - иначе первый же прогон молча переводит `.githooks/pre-commit` и
    линтер в LF целиком. В этом репозитории беды не видно, `.gitattributes`
    держит LF; у того, кто скопировал инструмент на CRLF-чекаут, оба файла
    после прогона оказываются переписаны от первой строки до последней.

    Смешанные окончания (кортеж) сводим к LF: гадать, какое из двух вернуть,
    нельзя, а файл и так уже неоднороден.
    """
    with io.open(path, encoding="utf-8") as stream:
        text = stream.read()
        seen = stream.newlines
    return text, seen if isinstance(seen, str) else "\n"


def write_atomically(path, text, newline="\n"):
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
        with io.open(handle, "w", encoding="utf-8", newline=newline) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # На Windows антивирус или индексатор держат файл открытым доли
        # секунды и дают транзиентный PermissionError. Без пары попыток такая
        # случайность роняет прогон с мутацией в дереве - то есть производит
        # ровно ту аварию, от которой этот код и написан.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)
        # Синхронизируем и КАТАЛОГ: без этого на POSIX при пропаже питания
        # может потеряться сама подмена имени, и обещание «либо отработал
        # целиком, либо не тронул оригинал» окажется неполным. На Windows
        # каталог как файл не открывается - там этой заботы нет.
        try:
            folder_handle = os.open(folder, os.O_RDONLY)
        except OSError:
            folder_handle = None
        if folder_handle is not None:
            try:
                os.fsync(folder_handle)
            except OSError:
                pass
            finally:
                os.close(folder_handle)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


NEWLINE_NAMES = {"\n": "lf", "\r\n": "crlf", "\r": "cr"}
NEWLINE_BY_NAME = {name: value for value, name in NEWLINE_NAMES.items()}


def fingerprint(text):
    """Отпечаток содержимого - по нему узнаём свою же мутацию на диске."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_backup(path, text, newline, mutated):
    """Кладёт оригинал рядом на время, пока файл мутирован.

    В шапке слепка три поля: путь, вид переводов строк и отпечаток того, что
    инструмент оставил на диске.

    Переводы строк - чтобы восстановление не вернуло содержимое в LF, то
    есть не починило одно, молча переписав другое.

    Отпечаток - чтобы восстановление не затёрло чужую работу. Без него
    механизм, поставленный охранять целостность дерева, был единственным в
    репозитории, кто способен уничтожить незакоммиченную правку: прогон
    оборвали, человек сам сделал `git checkout`, час правил линтер - а
    следующий запуск молча вернул содержимое месячной давности и удалил
    единственную копию. Слепок лежит в .gitignore, поэтому в `git status`
    его не видно.
    """
    # Отпечаток не необязательный: восстановление без него отказывается
    # работать. Умолчание `mutated=None` тут было, и им пользовались только
    # тесты - то есть единственными, кто проверял ветку «отпечатка нет», были
    # входы, которые в бою не встречаются.
    left = fingerprint(mutated)
    header = "%s\t%s\t%s" % (path, NEWLINE_NAMES.get(newline, "lf"), left)
    write_atomically(BACKUP, header + "\n" + text)


def drop_backup():
    """Убирает слепок. False, если не вышло.

    Раньше отказ глотался молча, и это было безвредно: следующий
    `save_backup` перезаписал бы файл. С тех пор как оставшийся слепок
    ОСТАНАВЛИВАЕТ прогон, молчание стало блокировкой с ложным объяснением -
    человек читал «разобрать не смог» про слепок, который разобран и
    отработан, а дерево при этом чистое.
    """
    try:
        os.remove(BACKUP)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def resolve_ours(path):
    """Наш ли это файл - и если наш, то КАКОЙ ИМЕННО. Иначе None.

    Сравниваем по абсолютному пути с приведёнными слэшами: слепок,
    записанный на Windows, называет `scripts\\check_memory_index.py`, и на
    Linux `os.path.normpath` обратный слэш не трогает - путь переставал
    совпадать сам с собой.

    Возвращается ИМЕННО НАШЕ имя файла, а не то, что записано в
    слепке. Прежде опознание было слэш-агностичным, а запись - нет: строку
    из слепка признавали своей и по ней же писали. На Linux `\\` разделителем
    не работает, поэтому восстановление уходило в файл с обратными слэшами
    в имени, мутация оставалась в дереве, слепок удалялся, а инструмент
    печатал «восстановлен из слепка». Опознали - значит знаем, куда писать,
    и брать адрес из чужой строки больше незачем.
    """
    def shape(name):
        return os.path.normcase(os.path.abspath(name.replace("\\", "/")))

    wanted = shape(path)
    for ours in (LINTER, HOOK):
        if shape(ours) != wanted:
            continue
        # Приведение слэшей НЕИНЪЕКТИВНО на POSIX: там обратный слэш -
        # обычный символ имени, и файл `scripts\check_memory_index.py`
        # (ровно такой мусор оставляла прежняя редакция этого кода) после
        # приведения неотличим от настоящего линтера. Если оба пути на
        # диске есть - спрашиваем систему, один ли это файл, а не свою
        # нормализацию.
        try:
            if os.path.exists(path) and not os.path.samefile(path, ours):
                return None
        except OSError:
            # Отказ СПРОСИТЬ - не ответ «чужой». Прежде любой OSError уводил
            # в «слепок постороннего файла»: файл, который антивирус держит
            # долю секунды (тот самый транзиент, ради которого рядом стоят
            # лестницы из пяти попыток), объявлялся чужим, мутация
            # оставалась в дереве, а прогон после этого не запускался вовсе,
            # пока человек не удалил бы слепок руками. Пути совпали - этого
            # достаточно, чтобы работать дальше; от затирания чужой работы
            # защищает отпечаток, а не эта проверка.
            return ours
        return ours
    return None


def restore_interrupted():
    """Возвращает файл, если прошлый прогон оборвали. True, если пришлось.

    Возвращает ТОЛЬКО свою же мутацию: если на диске лежит не то, что
    инструмент там оставил, значит файл уже кто-то поправил, и переписывать
    его - уничтожать чужую работу. В таком случае слепок остаётся лежать, а
    человеку говорится, где он.
    """
    if not os.path.isfile(BACKUP):
        return False
    try:
        with io.open(BACKUP, encoding="utf-8") as stream:
            saved = stream.read()
    except (OSError, UnicodeDecodeError) as exc:
        print("Слепок %s не читается (%s). Не трогаю ни его, ни дерево - "
              "разберитесь руками." % (BACKUP, exc))
        return False
    header, _split, text = saved.partition("\n")
    parts = header.split("\t")
    # `split` пустого списка не возвращает никогда, поэтому и сторожа тут нет:
    # пустая шапка даёт [""], и дальше это отсеется как «имя пустое».
    path = parts[0]
    kind = parts[1] if len(parts) > 1 else "lf"
    left = parts[2] if len(parts) > 2 else ""
    # Инструмент правит ровно два файла - значит и восстанавливать должен
    # только их. Путь берётся из файла на диске, а файл может быть чей угодно.
    #
    # Слэши приводим к одному виду: слепок, записанный на Windows, называет
    # `scripts\check_memory_index.py`, а на Linux `os.path.normpath` обратный
    # слэш не трогает, и путь перестаёт совпадать с собственным. Прежде эта
    # ветка в таком случае УДАЛЯЛА слепок и молчала - то есть уничтожала
    # единственную копию оригинала и оставляла мутацию в дереве. Ровно та
    # авария, ради которой слепок и написан.
    # Отдельной отсечки пустого имени тут не нужно, и это проверено, а
    # не предположено: откат `if path else None` не покраснел ни на
    # одном тесте. `resolve_ours("")` сравнивает текущий КАТАЛОГ с
    # именем ФАЙЛА и совпасть не может - ветка была эквивалентна
    # своему отсутствию, то есть лишней машинерией, а не сторожем.
    ours = resolve_ours(path)
    if ours is None:
        print("В %s лежит слепок постороннего файла (%s). Не трогаю ни файл, "
              "ни слепок - разберитесь руками." % (BACKUP, path or "имя пустое"))
        return False
    # Дальше работаем СО СВОИМ именем, а не со строкой из слепка: читаем и
    # пишем туда же, где искали. Строка из слепка отслужила своё на опознании
    # и в сообщении выше - ниже она не участвует ни в одном обращении к диску.
    path = ours
    # Без отпечатка сторож «в дереве не моя мутация» пропускается целиком, и
    # механизм, поставленный охранять дерево, снова становится единственным,
    # кто способен затереть незакоммиченную правку. Пустое поле означает слепок
    # ЧУЖОГО или старого формата - в репозитории таких форматов было два, и
    # сегодняшний разбор оба читает как «отпечатка нет». Не знаем, что
    # оставили, - не трогаем ничего.
    # Сторож отпечатка ниже, а не здесь: слепок старого формата, оставшийся
    # от прогона, оборванного ДО того, как мутация легла на диск, дерево не
    # трогает вовсе - там лежит оригинал, и ветка «совпало с оригиналом»
    # разберётся с ним сама. Отказывать до этой ветки значило бы требовать
    # ручного вмешательства там, где чинить нечего.
    # Вид переводов строк тоже принимаем только знакомый: незнакомое слово в
    # поле молча переводило бы весь файл в LF - то есть чинило одно, переписав
    # другое, ровно тот ущерб, ради которого поле и заведено.
    if kind not in NEWLINE_BY_NAME:
        print("В слепке %s незнакомый вид переводов строк (%s). Не трогаю ни "
              "файл, ни слепок - разберитесь руками." % (BACKUP, kind))
        return False
    # Причины отказа читать различаем. Файла нет - восстанавливать законно,
    # это ровно тот случай, ради которого слепок и лежит. Любой другой отказ
    # (отобранные права, файл занят - на Windows это транзиентно, тот же
    # PermissionError, от которого write_atomically страхуется пятью
    # попытками) означает, что мы НЕ ЗНАЕМ, что на диске. Прежде оба сторожа
    # были написаны как «current is not None and ...», и такой отказ
    # проваливался прямо в перезапись: механизм, поставленный охранять
    # целостность дерева, затирал чужую работу, удалял единственную копию и
    # рапортовал «восстановлен из слепка».
    # Лестница попыток та же, что в write_atomically, и по той же причине:
    # на Windows антивирус или индексатор держат файл доли секунды. Отказаться
    # на такой случайности значило бы оставить мутацию в дереве - худший исход
    # по мерке самого инструмента, и достаётся он за пустяк.
    current = None
    failure = None
    for attempt in range(5):
        try:
            current, _newline = read_source(path)
            failure = None
            break
        except FileNotFoundError:
            current = None
            failure = None
            break
        except UnicodeDecodeError as exc:
            # Не транзиент: повторять нечего, содержимое не изменится, а
            # лестница стоила бы секунды сна на пустом месте.
            failure = exc
            break
        except OSError as exc:
            failure = exc
            if attempt < 4:
                time.sleep(0.2)
    if failure is not None:
        print("Не могу прочитать %s (%s), поэтому не знаю, что там лежит. "
              "Ничего не трогаю, слепок оригинала - в %s."
              % (path, failure, BACKUP))
        return False
    # Совпало с ОРИГИНАЛОМ - значит восстанавливать нечего: прогон оборвали до
    # того, как мутация легла на диск, либо уже после того, как её убрали.
    # Без этой ветки такой обрыв печатал ложное «кто-то уже поправил», а
    # обещанный слепок затирался первой же следующей мутацией.
    if current is not None and fingerprint(current) == fingerprint(text):
        if not drop_backup():
            print("В дереве лежит оригинал, восстанавливать нечего, но слепок "
                  "%s удалить не смог - уберите его руками, иначе следующий "
                  "прогон не начнётся." % BACKUP)
        return False
    # Вот теперь отпечаток обязателен. Без него сторож «в дереве не моя
    # мутация» пропускается целиком, и механизм, поставленный охранять
    # дерево, снова становится единственным, кто способен затереть
    # незакоммиченную правку. Пустое поле означает слепок ЧУЖОГО или старого
    # формата - в репозитории таких форматов было два, и сегодняшний разбор
    # оба читает как «отпечатка нет».
    if not left:
        print("В слепке %s нет отпечатка того, что я оставил (старый формат). "
              "Отличить свою мутацию от вашей работы не могу - не трогаю ни "
              "файл, ни слепок." % BACKUP)
        return False
    if current is not None and fingerprint(current) != left:
        print("В дереве лежит НЕ та мутация, которую я оставил: %s кто-то "
              "уже поправил. Ничего не трогаю, слепок оригинала - в %s."
              % (path, BACKUP))
        return False
    write_atomically(path, text, NEWLINE_BY_NAME[kind])
    print("Прошлый прогон был оборван на середине - %s восстановлен из слепка."
          % path)
    if not drop_backup():
        print("Слепок %s удалить не смог - уберите его руками, иначе "
              "следующий прогон не начнётся." % BACKUP)
    return True


def missing_anchors():
    """Мутации, которые нельзя применить осмысленно.

    Две причины, и обе - жёсткая ошибка, а не пропуск.

    Первая: фрагмент не находится ровно один раз. Протухший якорь превращает
    мутацию в тихо не работающую.

    Вторая: мутант не разбирается. Это выяснилось ревью и стоило дорого:
    три мутации вклеивали `pass or errors.append(...)`, а `pass` - оператор,
    не выражение. Тестовый файл импортирует линтер на уровне модуля, поэтому
    загрузка падала, `unittest` отдавал ненулевой код, и инструмент печатал
    «поймана», не запустив НИ ОДНОГО теста. Отчёт «все пойманы» на этих
    ветках означал синтаксическую ошибку, а не работу тестов.

    Хук проверяется так же, `sh -n`. Первая редакция этой проверки смотрела
    только `.py` - и это была ровно половина работы: негодный мутант хука
    импорт не роняет, тесты честно краснеют на его синтаксической ошибке, и
    инструмент печатает «поймана» про ветку, которой не касался. Тот же
    класс лжи, только в другом файле.
    """
    stale = []
    for name, path, old, new in MUTATIONS:
        try:
            with io.open(path, encoding="utf-8") as stream:
                text = stream.read()
        except (OSError, UnicodeDecodeError) as exc:
            stale.append((name, "файл не читается: %s" % exc))
            continue
        found = text.count(old)
        if found != 1:
            stale.append((name, "фрагмент найден %d раз вместо одного" % found))
            continue
        mutated = text.replace(old, new)
        if path.endswith(".py"):
            try:
                compile(mutated, path, "exec")
            except SyntaxError as exc:
                stale.append((name, "мутант не компилируется: %s (строка %s)"
                              % (exc.msg, exc.lineno)))
            continue
        complaint = shell_syntax_error(mutated)
        if complaint:
            stale.append((name, "мутант не разбирается как sh: %s" % complaint))
    return stale


def shell_syntax_error(script):
    """Жалоба `sh -n` на текст скрипта, либо None.

    Если `sh` не найден - молчим: на такой машине и тесты хука пропускаются,
    и требовать большего от мутационной проверки не за что. Но переменную
    MEMCHECK_REQUIRE_SH при этом ЧИТАЕМ: докстрока обещала, что в CI пропуск
    невозможен, а переменная только клалась в окружение потомка и здесь не
    смотрелась - то есть обещание было условно ложным, и `--anchors-only`
    рапортовал «мутант разбирается» про два десятка непроверенных.
    """
    shell = find_shell()
    if not shell:
        if os.environ.get("MEMCHECK_REQUIRE_SH"):
            return ("sh не найден, а MEMCHECK_REQUIRE_SH требует проверки - "
                    "разобрать мутант хука нечем")
        return None
    handle, temporary = tempfile.mkstemp(suffix=".sh")
    try:
        with io.open(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(script)
        checked = subprocess.run([shell, "-n", temporary],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        if checked.returncode == 0:
            return None
        return (checked.stdout or b"").decode("utf-8", "replace").strip()
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def find_shell():
    """Путь к `sh`: в PATH либо в комплекте Git на Windows."""
    found = shutil.which("sh")
    if found:
        return found
    for guess in (r"C:\Program Files\Git\usr\bin\sh.exe",
                  r"C:\Program Files (x86)\Git\usr\bin\sh.exe"):
        if os.path.isfile(guess):
            return guess
    return None


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

    Таймаут обязателен: мутация может увести набор в бесконечный цикл или в
    катастрофический откат регулярки - в этом проекте такое уже было, 8
    минут на одном входе. Зависший прогон человек убивает, а убитый прогон
    оставляет мутированное дерево, то есть ровно ту аварию, ради которой
    написан слепок. Потолок щедрый (набор идёт около минуты, а две мутации
    намеренно замедляют его до двух), но конечный.
    """
    # Своя группа процессов: тесты хука запускают `sh`, а тот - ещё один
    # python. `subprocess.run(timeout=...)` убивает только прямого потомка, и
    # внуки переживали таймаут - жгли процессор оставшиеся мутации и
    # оставляли за собой невычищенные временные деревья.
    if os.name == "nt":
        apart = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        apart = {"start_new_session": True}
    result = run_apart(
        [sys.executable, "-m", "unittest", "discover", "-s", "scripts",
         "-p", "test_check_memory_index.py", "-f"],
        apart,
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
    output = (result.stdout or b"").decode("utf-8", "replace")
    # Ненулевой код сам по себе ничего не значит: его даёт и упавший тест, и
    # несобравшийся набор, и упавший загрузчик. Считать это «поймана» - тот
    # же класс лжи, который инструмент ищет, и в CI он уже закрыт отдельным
    # шагом «тестов собралось не меньше 150». Здесь спрашиваем то же самое:
    # тесты обязаны быть запущены, и загрузчик обязан быть цел.
    #
    # Примета загрузчика - полное имя класса, а не подстрока «_FailedTest» во
    # всём выводе: тест с таким словом в имени или в сообщении убил бы
    # инструмент на первой же мутации.
    ran = re.search(r"^Ran (\d+) tests?", output, re.M)
    broken_loader = re.search(r"unittest\.loader\._FailedTest", output)
    if not ran or int(ran.group(1)) == 0 or broken_loader:
        raise SystemExit(
            "Набор не запустился под мутацией - это не «поймана», а поломка "
            "прогона. Вот его вывод:\n%s" % output)
    return result.returncode != 0


def run_apart(command, apart, timeout=None, **kwargs):
    """Запуск в своей группе процессов: по таймауту гибнет всё дерево.

    `with` и `except BaseException` не украшение. Обёртка заменила
    `subprocess.run`, который на ЛЮБОМ исключении убивает потомка и закрывает
    трубы; первая редакция обрабатывала только таймаут, и на Ctrl+C оставляла
    живой набор и открытый дескриптор. Складывается это скверно: своя группа
    процессов выводит набор из-под Ctrl+C терминала, поэтому прерывание
    убивает теперь только инструмент - а осиротевший набор продолжает
    крутиться на дереве, которое под ним переписывает `finally`.
    """
    # Popen СНАРУЖИ with: `with EXPR as VAR` не вызывает __exit__, если
    # исключение пришло на входе в блок, и в это окно утекают и потомок, и
    # трубы - оба отказа, которые эта обёртка объявляет закрытыми.
    process = subprocess.Popen(command, **dict(kwargs, **apart))
    with process:
        try:
            # Потолок параметром, а не константой: соседний инструмент
            # гоняет ОДИН именованный тест, и ждать его девятьсот секунд
            # значило бы называть «зависанием» то, что давно кончилось.
            out, _err = process.communicate(
                timeout=SUITE_TIMEOUT if timeout is None else timeout)
        except BaseException:
            kill_tree(process)
            try:
                process.communicate()
            except Exception:
                pass
            raise
    return subprocess.CompletedProcess(command, process.returncode, out, None)


def kill_tree(process):
    """Убивает процесс вместе с потомками. Ошибки глотаем: это уборка."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        process.kill()
    except Exception:
        pass


def build_parser():
    """Разбор аргументов. Нужен прежде всего ради `--help`.

    Без него `python scripts/mutation_check.py --help` запускал часовой
    прогон, переписывающий рабочее дерево: аргументы просто не читались.
    Для инструмента, который правит чужие файлы, это неприемлемая цена за
    любопытство.
    """
    parser = argparse.ArgumentParser(
        description="Мутационная проверка: врут ли тесты. Правит файлы в "
                    "рабочем дереве и возвращает их обратно - запускать "
                    "только на чистом дереве.",
        epilog="Коды возврата: 0 - все мутации пойманы, 1 - есть выжившие "
               "или прогон не состоялся.")
    parser.add_argument(
        "--anchors-only", action="store_true",
        help="только проверить, что все мутации применимы (якорь на месте "
             "и мутант компилируется), не прогоняя тесты; уборку за "
             "оборванным прогоном ключ не отменяет")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_parser().parse_args(argv)

    if not os.path.isfile(LINTER):
        print("Запускать из корня репозитория: %s не найден" % LINTER, file=sys.stderr)
        return 1

    # Прибираемся за прошлым прогоном ДО всего остального: пока мутация лежит
    # в дереве, и якоря протухли, и набор красный - обе следующие проверки
    # отчитались бы о чужой беде.
    restored = restore_interrupted()
    # Пока слепок лежит, запускать мутации НЕЛЬЗЯ: первая же перезапишет его,
    # а `finally` удалит - вместе с единственной копией оригинала. Слепок
    # остаётся ровно тогда, когда восстановление не смогло разобраться само и
    # уже сказало об этом человеку. Прежде возврат отбрасывался, и обещание
    # «слепок оригинала - в .mutation-backup» жило до первой мутации: прогон
    # шёл дальше и рапортовал полный успех.
    if os.path.isfile(BACKUP):
        # Два разных повода, и путать их нельзя: человек читает сообщение,
        # чтобы понять, цело ли дерево. «Восстановил, но файл не удалился»
        # означает, что чинить нечего - достаточно убрать слепок.
        if restored:
            print("Дерево я восстановил, но слепок %s удалить не смог. "
                  "Удалите его сами - пока он лежит, прогон не начнётся."
                  % BACKUP, file=sys.stderr)
        else:
            print("Слепок %s остался на месте - разобрать его я не смог. Пока "
                  "он лежит, мутации запускать нельзя: первая же его затрёт. "
                  "Порядок разбора - в CONTRIBUTING.md, раздел «Если он "
                  "говорит „разберитесь руками“»; когда разберётесь, удалите "
                  "файл." % BACKUP, file=sys.stderr)
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

    if args.anchors_only:
        print("Все %d мутаций применимы: якорь на месте, мутант "
              "разбирается." % len(MUTATIONS))
        return 0

    # Без этого прогона всё дальнейшее бессмысленно: если набор красный по
    # своей причине - недостающий sh, чужая правка в дереве, сломанный тест, -
    # то краснеть он будет и на каждой мутации, и отчёт «все пойманы» окажется
    # рапортом о причине, к мутациям отношения не имеющей.
    print("Проверяю, что набор зелёный без мутаций...")
    # Базовый прогон страхуем отдельно: мутаций ещё нет, поэтому и таймаут, и
    # несобравшийся набор говорят не о них, а о дереве. Без обёртки первый
    # выдавал трейсбек, второй - сообщение «набор не запустился ПОД МУТАЦИЕЙ»,
    # которого тут ещё быть не может.
    try:
        base_red = suite_fails()
    except subprocess.TimeoutExpired:
        print("Набор не ответил за %d с ещё БЕЗ мутаций - дело в дереве, не в "
              "них." % SUITE_TIMEOUT, file=sys.stderr)
        return 1
    except SystemExit as exc:
        print("Набор не запустился ещё БЕЗ мутаций:", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1
    if base_red:
        print("Тесты не проходят и БЕЗ мутаций - сначала почините дерево.",
              file=sys.stderr)
        print("Пока набор красный, любая мутация засчитается «пойманной» по "
              "чужой причине.", file=sys.stderr)
        return 1

    survived = []
    for number, (name, path, old, new) in enumerate(MUTATIONS, 1):
        original, newline = read_source(path)
        mutated = original.replace(old, new)
        save_backup(path, original, newline, mutated)
        try:
            write_atomically(path, mutated, newline)
            try:
                caught = suite_fails()
            except subprocess.TimeoutExpired:
                # Не «поймана»: набор не ответил. Обычно это значит, что
                # мутация увела его в бесконечный цикл или в откат регулярки.
                caught = None
        finally:
            write_atomically(path, original, newline)
            drop_backup()
        if caught is None:
            print("[%2d/%d] %-8s %s" % (number, len(MUTATIONS), "ЗАВИСЛА", name))
            survived.append(name + " (набор не ответил за %d с)" % SUITE_TIMEOUT)
            continue
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
