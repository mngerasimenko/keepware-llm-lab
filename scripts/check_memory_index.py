#!/usr/bin/env python3
"""Проверка согласованности индекса памяти и файлов памяти.

Три инварианта:

  L1 (жёстко)  каждая ссылка из индекса ведёт на существующий файл;
  L2 (жёстко)  каждый файл памяти упомянут хотя бы в одном индексе;
  L3 (мягко)   нет двух разных файлов с одинаковым заголовком в индексе.

Нарушение L1 или L2 - выход с кодом 1. L3 печатается предупреждением, код 0.
Код 2 - проверку не удалось выполнить (нет папки, нет индекса, сбой): это НЕ
нарушение, и тот, кто вызывает скрипт, не должен путать одно с другим.

Внешние адреса (http, https, mailto и подобные) не проверяются: это отдельная
задача, требующая сети, повторов и кэша. Для неё есть готовые инструменты,
например lychee.

Скрипт только читает файлы и ничего не изменяет. Зависимостей нет,
нужен Python 3.7+.

Примеры:
    python scripts/check_memory_index.py
    python scripts/check_memory_index.py memory --index "MEMORY*.md"
    python scripts/check_memory_index.py memory --allow-orphan "templates/*.md"
"""

import argparse
import fnmatch
import os
import re
import sys
from collections import defaultdict
from urllib.parse import unquote, urlparse

# Строка индекса: "- [Заголовок](файл.md) - крючок".
# Внутри заголовка допускаем один уровень скобок: "[VPScan [beta]](vps.md)".
# Разделитель после ссылки намеренно не разбираем: он бывает дефисом,
# длинным тире или отсутствует - для проверки это неважно.
ROW = re.compile(r"^\s*[-*+]\s*\[((?:[^\[\]]|\[[^\[\]]*\])+)\]\(([^)]*)\)")

FENCE = re.compile(r"^\s*(```|~~~)")
COMPLETE_COMMENT = re.compile(r"<!--.*?-->", re.S)

# Метка "этот файл лежит вне индекса намеренно" - по образцу директивы :orphan:
# в Sphinx, которому пришлось её завести ровно по этой причине.
FRONTMATTER_ORPHAN = re.compile(r"^\s*orphan\s*:\s*true\s*$", re.I)
COMMENT_ORPHAN = re.compile(r"<!--\s*linter:\s*orphan-ok\s*-->", re.I)

HEAD_LINES = 20

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_USAGE = 2


def force_utf8_output():
    """Иначе на Windows русские сообщения уезжают в кодировку консоли."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass  # поток без reconfigure или уже отсоединённый - не повод падать


def read_text(path):
    """Читаем как UTF-8, молча съедая BOM (файлы могли создаваться на Windows)."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def is_external(target):
    """Внешний адрес - это схема длиннее одной буквы.

    Однобуквенная - это диск в Windows ("C:/..."), а не протокол: urlparse
    считает его схемой, и без этой проверки абсолютный путь молча пропускался бы.
    """
    scheme = urlparse(target).scheme
    return len(scheme) > 1


def clean_target(raw):
    """Убираем то, что по markdown частью адреса не является."""
    target = raw.strip()
    if target.startswith("<"):
        end = target.find(">")
        if end != -1:
            return target[1:end].strip()
        return target[1:].strip()
    # Подсказка в кавычках: [X](file.md "подсказка") - легальный markdown.
    match = re.match(r"^(\S+)\s+[\"'].*[\"']$", target)
    if match:
        return match.group(1)
    return target


def parse_index(path):
    """Разбирает индекс: возвращает (строки, что осталось незакрытым).

    Блоки кода и html-комментарии пропускаем: индекс, который документирует
    собственный формат - а мы сами так и советуем, - иначе ругался бы на
    собственный пример.

    Тонкости, каждая из которых иначе молча съедает строки:
    - блок кода проверяется РАНЬШЕ комментария, иначе `<!--` внутри примера
      съест закрывающие кавычки блока и весь остаток индекса;
    - законченные `<!-- ... -->` вырезаются из строки, а не отбрасывают её
      целиком: строка может делить место с комментарием;
    - при незакрытом комментарии обрабатывается часть строки ДО него.

    Потерянная строка невидима: человек видит «файл не упомянут в индексе»
    про файл, который в индексе есть. Поэтому о незакрытом блоке или
    комментарии зовущий обязан сказать вслух - для этого второе значение.
    """
    rows = []
    in_fence = False
    in_comment = False
    for lineno, line in enumerate(read_text(path).splitlines(), 1):
        if in_fence:
            if FENCE.match(line):
                in_fence = False
            continue
        if in_comment:
            if "-->" not in line:
                continue
            line = line.split("-->", 1)[1]
            in_comment = False
        line = COMPLETE_COMMENT.sub("", line)
        if "<!--" in line:
            line = line[:line.index("<!--")]
            in_comment = True
        if FENCE.match(line):
            in_fence = True
            continue
        match = ROW.match(line)
        if match:
            rows.append((match.group(1).strip(), match.group(2).strip(), lineno))
    unclosed = "блок кода" if in_fence else ("html-комментарий" if in_comment else None)
    return rows, unclosed


def prune_non_memory_dirs(folder, dirs):
    """Убирает из обхода то, что памятью не является.

    **Скрытые каталоги** (на точку). У тех, кто держит заметки в markdown-
    редакторе, прямо в папке памяти лежат `.obsidian` с шаблонами и `.trash`
    с удалённым. Это служебное хозяйство инструмента, а не факты, и требовать
    для него строк в индексе бессмысленно. Через хук пользователь исключить
    их не может - хук зовёт проверку без ключей, - так что молчать нельзя,
    правило названо в README.

    **Связанные каталоги** (симлинки и виндовые junction'ы). Без этого вердикт
    зависит от системы: os.walk заходит внутрь junction'а, но не заходит внутрь
    симлинка, и одна и та же папка памяти давала бы разные ответы на Windows и
    в CI на Linux. А junction, указывающий на предка, загонял обход в петлю до
    упора в предел длины пути - и проверка молча не выполнялась вовсе.
    Содержимое связанного каталога - чужая память, за неё мы не отвечаем;
    но ссылку внутрь такого каталога L1 всё равно разрешит, если её открывает
    система.
    """
    is_junction = getattr(os.path, "isjunction", None)
    kept = []
    for name in sorted(dirs):
        if name.startswith("."):
            continue
        path = os.path.join(folder, name)
        if os.path.islink(path):
            continue
        if is_junction is not None and is_junction(path):
            continue
        kept.append(name)
    dirs[:] = kept
    return dirs


def build_file_map(root):
    """Один обход папки: точные пути и их регистро-независимые двойники.

    Раньше каждая строка индекса опрашивала файловую систему через os.listdir.
    На Windows это врало: система регистронезависима, поэтому расхождение в
    имени каталога она проглатывала, а сравнение строк потом выдавало файл за
    сироту - одна ошибка превращалась в другую, ложную. Один канонический
    список снимает вопрос целиком.
    """
    exact = {}
    folded = {}
    for folder, dirs, names in os.walk(root):
        prune_non_memory_dirs(folder, dirs)
        for name in sorted(names):
            path = os.path.normpath(os.path.join(folder, name))
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            exact[rel] = path
            folded.setdefault(rel.casefold(), path)
    return exact, folded


def relative_to_root(root, path):
    """Путь внутри папки памяти или None, если он из неё вышел.

    ValueError - это ссылка на другой диск или сетевую шару: между ними
    относительный путь не существует в принципе. Такая строка не должна
    ронять весь прогон, это обычная находка «вышли за папку памяти».
    """
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return None
    if os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/")


def frontmatter_lines(text):
    """Строки шапки --- ... --- (если её нет, пусто)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    head = []
    for line in lines[1:]:
        if line.strip() == "---":
            return head
        head.append(line)
    return []


def is_orphan_ok(path):
    """Файл сам объявил, что лежит вне индекса намеренно.

    Нечитаемый файл (битый симлинк, отобранные права) - это не «намеренно»,
    но и не повод отменять весь прогон: считаем его обычной находкой.
    """
    try:
        text = read_text(path)
    except OSError:
        return False
    if any(FRONTMATTER_ORPHAN.match(line) for line in frontmatter_lines(text)):
        return True
    return any(COMMENT_ORPHAN.search(line) for line in text.splitlines()[:HEAD_LINES])


def find_indexes(root, pattern):
    found = []
    for folder, dirs, names in os.walk(root):
        prune_non_memory_dirs(folder, dirs)
        for name in sorted(names):
            # fnmatchcase, а не fnmatch: последний на Windows сравнивает без учёта
            # регистра, и набор файлов разъезжается между машиной и CI.
            if fnmatch.fnmatchcase(name, pattern):
                found.append(os.path.normpath(os.path.join(folder, name)))
    return found


def check(root, index_paths, allow_globs, index_pattern):
    """Возвращает (ошибки, предупреждения, число разобранных строк)."""
    errors = []
    warnings = []
    referenced = set()
    titles = defaultdict(set)
    seen_rows = set()
    index_links = defaultdict(set)
    row_count = 0

    exact, folded = build_file_map(root)
    index_rels = {relative_to_root(root, p) for p in index_paths}
    # Корневой индекс - тот, что лежит в самой папке памяти: только он
    # загружается сам. Под-индексы могут жить и в подкаталогах.
    #
    # Имя корневого выводим ИЗ ШАБЛОНА, а не из строки "MEMORY.md": шаблон
    # "MEMORY*.md" без звёздочки даёт "MEMORY.md", "INDEX*.md" - "INDEX.md".
    # Иначе ключ --index, который мы сами рекламируем, ломает проверку:
    # у человека с индексами INDEX*.md корневой оказывался бы «сиротой».
    top_level = {rel for rel in index_rels if "/" not in rel}
    derived_root = index_pattern.replace("*", "")
    if derived_root in top_level:
        roots = {derived_root}
    elif len(top_level) == 1:
        roots = set(top_level)
    else:
        # Не смогли выделить один корневой - считаем корневыми все верхние:
        # какой из них загрузится, решает не проверка.
        roots = set(top_level)

    # L1: каждая ссылка из индекса ведёт на существующий файл.
    for index_path in index_paths:
        where = relative_to_root(root, index_path) or index_path
        index_rows, unclosed = parse_index(index_path)
        if unclosed:
            warnings.append(
                "%s: %s не закрыт до конца файла - строки ниже в разбор не попали"
                % (where, unclosed)
            )
        for title, raw_target, lineno in index_rows:
            row_count += 1
            target = clean_target(raw_target)
            if not target or target.startswith("#"):
                continue  # якорь внутри того же документа - не ссылка на файл
            if is_external(target):
                continue

            absolute = os.path.normpath(
                os.path.join(os.path.dirname(index_path), unquote(target.split("#", 1)[0]))
            )
            rel = relative_to_root(root, absolute)
            if rel is None:
                errors.append(
                    "L1 %s:%d ссылка выходит за папку памяти: %s "
                    "(такой путь разрешается по-разному в зависимости от того, "
                    "откуда открыли файл)" % (where, lineno, target)
                )
                continue

            hit = exact.get(rel)
            twin = folded.get(rel.casefold()) if hit is None else None
            if hit is None and twin is None and os.path.isfile(absolute):
                # Файла нет в обходе, но система его открывает - значит он
                # за связанным каталогом. Агент такой файл прочитает, поэтому
                # ссылка живая, даже если содержимое каталога мы не аудируем.
                # Порядок важен: на Windows isfile подтвердит и ссылку с чужим
                # регистром, поэтому сначала регистр, и только потом эта ветка.
                hit = absolute
            actual = hit or twin
            if actual is not None:
                actual_rel = relative_to_root(root, actual)
                referenced.add(actual_rel)
                titles[title].add(actual_rel)
                if actual_rel in index_rels and actual_rel != where:
                    index_links[where].add(actual_rel)
                row_key = (title, actual_rel)
                if row_key in seen_rows:
                    warnings.append("L3 %s:%d строка повторяется: «%s» → %s"
                                    % (where, lineno, title, actual_rel))
                seen_rows.add(row_key)

            if hit is not None:
                continue
            if twin is not None:
                errors.append(
                    "L1 %s:%d регистр не совпадает: в индексе «%s», на диске «%s» "
                    "(на Windows пройдёт, в CI на Linux упадёт)"
                    % (where, lineno, rel, relative_to_root(root, twin))
                )
            elif os.path.isdir(absolute):
                errors.append("L1 %s:%d ссылка на каталог, а не на файл: %s"
                              % (where, lineno, target))
            else:
                errors.append("L1 %s:%d ссылка в никуда: %s" % (where, lineno, target))

    # Ноль разобранных строк - это «я не понял индекс», а не «память разъехалась».
    # Ошибкой это быть не должно: иначе первый же коммит новой, ещё пустой памяти
    # оказывается заблокирован. Настоящие нарушения (сироты, битые ссылки)
    # заблокируют его сами, если они есть.
    if index_paths and row_count == 0:
        has_facts = any(rel.lower().endswith(".md") and rel not in index_rels
                        for rel in exact)
        if has_facts:
            warnings.append(
                "Индекс не дал ни одной строки формата `- [Заголовок](файл.md) - крючок`, "
                "хотя файлы памяти есть - проверьте формат строк индекса"
            )
        else:
            warnings.append(
                "Индекс пуст: строк формата `- [Заголовок](файл.md) - крючок` в нём нет. "
                "Для новой памяти это нормально"
            )

    # L2: каждый файл памяти упомянут хотя бы в одном индексе.
    # Корневой индекс исключён: он загружается сам. Под-индекс - обычный файл,
    # и если на него никто не ссылается, невидим и он, и всё, что за ним.
    # Достижимость от корня, а не просто «где-то упомянут»: под-индекс,
    # ссылающийся сам на себя, формально упомянут, но прийти к нему неоткуда.
    reachable = set(roots)
    queue = list(roots)
    while queue:
        current = queue.pop()
        for target in index_links.get(current, ()):
            if target not in reachable:
                reachable.add(target)
                queue.append(target)

    for rel in sorted(exact):
        if not rel.lower().endswith(".md"):
            continue
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in allow_globs):
            continue
        if rel in index_rels:
            if rel in reachable or is_orphan_ok(exact[rel]):
                continue
            errors.append(
                "L2 %s - под-индекс, до которого неоткуда дойти: от корневого "
                "индекса ссылок на него нет, а сам он не загружается" % rel
            )
            continue
        if rel in referenced or is_orphan_ok(exact[rel]):
            continue
        errors.append("L2 %s не упомянут ни в одном индексе - агент его не увидит" % rel)

    # L3: одинаковый заголовок у разных файлов - предупреждение, не ошибка.
    # Индекс намеренно не уникальный ключ, поэтому это сигнал, а не запрет.
    for title, paths in sorted(titles.items()):
        if len(paths) > 1:
            warnings.append("L3 заголовок «%s» ведёт на разные файлы: %s"
                            % (title, ", ".join(sorted(paths))))

    return errors, warnings, row_count


def build_parser():
    parser = argparse.ArgumentParser(
        description="Проверяет, что индекс памяти и файлы памяти не разошлись.",
        epilog="Коды возврата: 0 - порядок, 1 - нарушения L1/L2, 2 - проверку выполнить не удалось.",
    )
    parser.add_argument("memory_dir", nargs="?", default="memory",
                        help="папка памяти (по умолчанию: memory)")
    parser.add_argument("--index", default="MEMORY*.md",
                        help='шаблон имени индекса (по умолчанию: MEMORY*.md - '
                             'ловит и корневой индекс, и под-индексы)')
    parser.add_argument("--allow-orphan", action="append", default=[], metavar="GLOB",
                        help="файл(ы), которым позволено не быть в индексе; можно повторять")
    parser.add_argument("--quiet", action="store_true",
                        help="молчать, когда нарушений нет")
    return parser


def main(argv=None):
    force_utf8_output()
    args = build_parser().parse_args(argv)

    root = os.path.abspath(args.memory_dir)
    if not os.path.isdir(root):
        print("Папка памяти не найдена: %s" % args.memory_dir, file=sys.stderr)
        return EXIT_USAGE

    index_paths = find_indexes(root, args.index)
    if not index_paths:
        print("Индекс не найден: %s" % os.path.join(args.memory_dir, args.index),
              file=sys.stderr)
        return EXIT_USAGE

    # Проверка отвечает только за папку памяти. Если индекса нет в ней самой,
    # а он нашёлся где-то в глубине, значит указали не ту папку - например корень
    # репозитория. Разбирать чужое дерево и выдавать сотни «сирот» нельзя:
    # ошибку надо назвать на входе.
    if not any(os.path.dirname(p) == root for p in index_paths):
        print("Это не папка памяти: индекс %s не лежит в %s "
              "(нашёлся только во вложенных каталогах - похоже, указана не та папка)"
              % (args.index, args.memory_dir), file=sys.stderr)
        return EXIT_USAGE

    try:
        errors, warnings, row_count = check(root, index_paths, args.allow_orphan, args.index)
    except Exception as exc:  # проверка сломалась - это не нарушение памяти
        print("Проверку выполнить не удалось: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_USAGE

    for line in errors + warnings:
        print(line)

    if errors:
        print("\nНарушений: %d (строк в индексе: %d)" % (len(errors), row_count))
        return EXIT_VIOLATION
    if not args.quiet:
        note = ", предупреждений: %d" % len(warnings) if warnings else ""
        print("Память согласована: строк в индексе %d, индексов %d%s"
              % (row_count, len(index_paths), note))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
