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

# Метка "этот файл лежит вне индекса намеренно" - по образцу директивы :orphan:
# в Sphinx, которому пришлось её завести ровно по этой причине.
FRONTMATTER_ORPHAN = re.compile(r"^\s*orphan\s*:\s*true\s*$", re.I)
COMMENT_ORPHAN = re.compile(r"<!--\s*linter:\s*orphan-ok\s*-->", re.I)

ROOT_INDEX_NAME = "MEMORY.md"
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
    """Строки индекса: (заголовок, адрес, номер строки).

    Блоки кода и html-комментарии пропускаем: индекс, который документирует
    собственный формат, иначе ругался бы на свой же пример.
    """
    rows = []
    in_fence = False
    in_comment = False
    for lineno, line in enumerate(read_text(path).splitlines(), 1):
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if "<!--" in line and "-->" not in line:
            in_comment = True
            continue
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = ROW.match(line)
        if match:
            rows.append((match.group(1).strip(), match.group(2).strip(), lineno))
    return rows


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
    for folder, _dirs, names in os.walk(root):
        for name in sorted(names):
            path = os.path.normpath(os.path.join(folder, name))
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            exact[rel] = path
            folded.setdefault(rel.casefold(), path)
    return exact, folded


def relative_to_root(root, path):
    """Путь внутри папки памяти или None, если он из неё вышел."""
    rel = os.path.relpath(path, root)
    if rel.startswith(os.pardir) or os.path.isabs(rel):
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
    """Файл сам объявил, что лежит вне индекса намеренно."""
    text = read_text(path)
    if any(FRONTMATTER_ORPHAN.match(line) for line in frontmatter_lines(text)):
        return True
    return any(COMMENT_ORPHAN.search(line) for line in text.splitlines()[:HEAD_LINES])


def find_indexes(root, pattern):
    found = []
    for folder, _dirs, names in os.walk(root):
        for name in sorted(names):
            # fnmatchcase, а не fnmatch: последний на Windows сравнивает без учёта
            # регистра, и набор файлов разъезжается между машиной и CI.
            if fnmatch.fnmatchcase(name, pattern):
                found.append(os.path.normpath(os.path.join(folder, name)))
    return found


def check(root, index_paths, allow_globs):
    """Возвращает (ошибки, предупреждения, число разобранных строк)."""
    errors = []
    warnings = []
    referenced = set()
    titles = defaultdict(set)
    seen_rows = set()
    row_count = 0

    exact, folded = build_file_map(root)
    index_rels = {relative_to_root(root, p) for p in index_paths}
    root_index_rel = ROOT_INDEX_NAME if ROOT_INDEX_NAME in index_rels else None
    if root_index_rel is None and len(index_rels) == 1:
        root_index_rel = next(iter(index_rels))

    # L1: каждая ссылка из индекса ведёт на существующий файл.
    for index_path in index_paths:
        where = relative_to_root(root, index_path) or index_path
        for title, raw_target, lineno in parse_index(index_path):
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
            actual = hit or twin
            if actual is not None:
                actual_rel = relative_to_root(root, actual)
                referenced.add(actual_rel)
                titles[title].add(actual_rel)
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

    if index_paths and row_count == 0:
        errors.append(
            "Разобрано 0 строк индекса - ожидается формат `- [Заголовок](файл.md) - крючок`"
        )

    # L2: каждый файл памяти упомянут хотя бы в одном индексе.
    # Корневой индекс исключён: он загружается сам. Под-индекс - обычный файл,
    # и если на него никто не ссылается, невидим и он, и всё, что за ним.
    for rel in sorted(exact):
        if not rel.lower().endswith(".md"):
            continue
        if rel == root_index_rel or rel in referenced:
            continue
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in allow_globs):
            continue
        if is_orphan_ok(exact[rel]):
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

    try:
        errors, warnings, row_count = check(root, index_paths, args.allow_orphan)
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
