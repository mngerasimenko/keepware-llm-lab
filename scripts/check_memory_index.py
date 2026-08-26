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
нужен Python 3.9+ (именно эти версии гоняются в CI).

Примеры:
    python scripts/check_memory_index.py
    python scripts/check_memory_index.py memory --index INDEX.md
    python scripts/check_memory_index.py memory --allow-orphan "templates/*.md"
"""

import argparse
import fnmatch
import os
import re
import stat
import sys
from collections import defaultdict
from urllib.parse import unquote, urlparse

# Строка индекса: "- [Заголовок](файл.md) - крючок".
# Внутри заголовка допускаем один уровень скобок: "[VPScan [beta]](vps.md)".
# Разделитель после ссылки намеренно не разбираем: он бывает дефисом,
# длинным тире или отсутствует - для проверки это неважно.
# Строка индекса. Отступ здесь не разбираем - им занимается parse_index,
# потому что «четыре пробела» значат разное в зависимости от того, что
# стоит выше: под пунктом списка это вложенный пункт, а после абзаца -
# блок кода.
ROW = re.compile(r"^[-*+]\s*\[((?:[^\[\]]|\[[^\[\]]*\])+)\]\(([^)]*)\)")
# Ссылка-метка: "- [Профиль][prof]" и свёрнутая форма "- [prof][]". Законный
# markdown, по которому агент дойдёт - а L2 объявлял такой файл забытым.
ROW_REF = re.compile(r"^[-*+]\s*\[((?:[^\[\]]|\[[^\[\]]*\])+)\]\[([^\]]*)\]")
# Сокращённая форма: "- [prof] - крючок", метка и есть текст ссылки. Третья
# законная форма CommonMark. Отрицательный просмотр вперёд обязателен: без него
# паттерн перехватывал бы обычные строки "- [Заголовок](файл.md)".
# Строкой индекса она считается ТОЛЬКО при наличии определения метки - иначе
# чекбоксы "- [ ]" и любые скобки в прозе стали бы ошибками на пустом месте.
ROW_SHORTCUT = re.compile(r"^[-*+]\s*\[([^\[\]]+)\](?!\s*[\(\[])")
# Определение метки: "[prof]: user.md" либо "[prof]: <моя папка/user.md>".
# Угловые скобки берём целиком: внутри них законен пробел, а \S+ обрывался на
# первом же - получался адрес "<моя", ложная битая ссылка и ложная сирота.
# Ведущие пробелы здесь не разбираем: строка приходит уже без них, отступ
# разбирает parse_index (четыре пробела после абзаца - это блок кода).
DEFINITION = re.compile(r"^\[([^\]]+)\]:\s*(<[^>]*>|\S+)")
BULLET = re.compile(r"^[-*+]\s")
# Строка, похожая на строку индекса - чтобы сказать, сколько их потерялось за
# незакрытым забором. Опечатка в конце файла и проглоченная половина индекса
# выглядят в выводе одинаково, если не назвать число.
ROWLIKE = re.compile(r"^[-*+]\s*\[")

FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*(.*)$")
COMPLETE_COMMENT = re.compile(r"<!--.*?-->", re.S)


def fence_delimiter(line):
    """Забор в начале строки: (символы забора, хвост) либо (None, None)."""
    match = FENCE.match(line)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def closes_fence(mark, opened):
    """CommonMark: закрывающий забор - того же символа и не короче открывающего.

    Без этого правила блок из четырёх кавычек закрывался блоком из трёх, а
    тильда закрывала кавычки. Приём «внешний забор длиннее внутреннего» -
    ровно то, чем документируют формат, и мы сами его и советуем: строка-
    ПРИМЕР становилась настоящей строкой индекса, а файл, которого в индексе
    нет, объявлялся упомянутым.

    Хвост после закрывающего забора обязан быть пустым - иначе `~~~python`
    внутри блока сошёл бы за закрытие.
    """
    return mark[0] == opened[0] and len(mark) >= len(opened)

# Метка "этот файл лежит вне индекса намеренно" - по образцу директивы :orphan:
# в Sphinx, которому пришлось её завести ровно по этой причине.
FRONTMATTER_ORPHAN = re.compile(r"^\s*orphan\s*:\s*true\s*$", re.I)
COMMENT_ORPHAN = re.compile(r"<!--\s*linter:\s*orphan-ok\s*-->", re.I)

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
    try:
        scheme = urlparse(target).scheme
    except ValueError:
        # Неразбираемый адрес - не внешний: пусть L1 честно скажет, что
        # ссылка никуда не ведёт, вместо того чтобы ронять весь прогон.
        return False
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
    pending_refs = []
    definitions = {}
    lost_rows = 0
    opened_fence = ""
    in_comment = False
    after_list_item = False
    for lineno, line in enumerate(read_text(path).splitlines(), 1):
        if opened_fence:
            mark, tail = fence_delimiter(line)
            if mark and closes_fence(mark, opened_fence) and not tail.strip():
                # Забор закрылся - строки внутри были законными примерами,
                # ничего не потеряно.
                opened_fence = ""
                lost_rows = 0
            elif ROWLIKE.match(line.lstrip()):
                lost_rows += 1
            continue
        if in_comment:
            if "-->" not in line:
                if ROWLIKE.match(line.lstrip()):
                    lost_rows += 1
                continue
            line = line.split("-->", 1)[1]
            in_comment = False
        line = COMPLETE_COMMENT.sub("", line)
        mark, _tail = fence_delimiter(line)
        if mark:
            # Раньше поиска незакрытого комментария: внутри блока кода
            # `<!--` - обычный текст, и строка "```markdown <!-- пример"
            # иначе включала бы разом оба состояния.
            opened_fence = mark
            after_list_item = False
            continue
        if "<!--" in line:
            line = line[:line.index("<!--")]
            in_comment = True

        expanded = line.expandtabs(4)
        body = expanded.lstrip(" ")
        if not body.strip():
            continue  # пустая строка не разрывает список
        indent = len(expanded) - len(body)
        # Отступ в четыре пробела - это блок кода ТОЛЬКО после обычного текста.
        # Под пунктом списка те же четыре пробела означают вложенный пункт, и
        # гитхаб рисует его списком; выкидывать такие строки значило бы объявить
        # сиротами всё, что человек сгруппировал по темам.
        if indent >= 4 and not after_list_item:
            after_list_item = False
            continue
        definition = DEFINITION.match(body)
        if definition:
            # Метки регистронезависимы по CommonMark.
            # setdefault, а не присваивание: по CommonMark при повторе метки
            # побеждает ПЕРВОЕ определение. Перезапись давала тихий пропуск -
            # агент и любой рендерер идут по первому, проверка шла по последнему.
            definitions.setdefault(definition.group(1).strip().casefold(),
                                   definition.group(2).strip())
            after_list_item = False
            continue

        after_list_item = bool(BULLET.match(body))
        match = ROW.match(body)
        if match:
            rows.append((match.group(1).strip(), match.group(2).strip(), lineno, "inline"))
            continue
        reference = ROW_REF.match(body)
        if reference:
            title = reference.group(1).strip()
            # Свёрнутая форма "[user][]" берёт метку из текста ссылки.
            label = reference.group(2).strip() or title
            pending_refs.append((title, label, lineno, True))
            continue
        shortcut = ROW_SHORTCUT.match(body)
        if shortcut:
            label = shortcut.group(1).strip()
            # required=False: нет определения - значит это не ссылка, а текст.
            pending_refs.append((label, label, lineno, False))
    # Определения ищутся по всему файлу, поэтому метки разрешаем в конце:
    # "[prof]: user.md" законно стоит и ниже строки, которая на неё ссылается.
    for title, label, lineno, required in pending_refs:
        target = definitions.get(label.casefold())
        if target is not None:
            rows.append((title, target, lineno, "inline"))
        elif required:
            rows.append((title, label, lineno, "ref-missing"))

    unclosed = "блок кода" if opened_fence else ("html-комментарий" if in_comment else None)
    return rows, unclosed, lost_rows


def is_linked_dir(path):
    """Симлинк или виндовый junction.

    os.path.isjunction появился только в 3.12, а os.path.islink перестал
    считать junction ссылкой ещё в 3.8. То есть на 3.8-3.11 под Windows
    обе привычные проверки промахиваются, и обход уходит внутрь чужого
    каталога. Поэтому третий путь - атрибут точки повторного разбора.
    """
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        try:
            return bool(is_junction(path))
        except OSError:
            return False
    try:
        attributes = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def prune_non_memory_dirs(folder, dirs, linked=None):
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
    kept = []
    for name in sorted(dirs):
        if name.startswith("."):
            continue
        if is_linked_dir(os.path.join(folder, name)):
            if linked is not None:
                linked.append(os.path.join(folder, name))
            continue
        kept.append(name)
    dirs[:] = kept
    return dirs


def build_file_map(root):
    """Один обход папки: точные пути, регистро-независимые двойники и то, что не открылось.

    Раньше каждая строка индекса опрашивала файловую систему через os.listdir.
    На Windows это врало: система регистронезависима, поэтому расхождение в
    имени каталога она проглатывала, а сравнение строк потом выдавало файл за
    сироту - одна ошибка превращалась в другую, ложную. Один канонический
    список снимает вопрос целиком.
    """
    exact = {}
    folded = {}
    unreadable_dirs = []
    linked_dirs = []

    def remember(error):
        # os.walk без onerror глотает отказ доступа молча: поддерево с
        # под-индексом и фактами просто исчезает из обхода, а проверка
        # отчитывается «согласована». Это тихий отказ, а не чистая память.
        unreadable_dirs.append(getattr(error, "filename", None) or str(error))

    for folder, dirs, names in os.walk(root, onerror=remember):
        prune_non_memory_dirs(folder, dirs, linked_dirs)
        for name in sorted(names):
            path = os.path.normpath(os.path.join(folder, name))
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            exact[rel] = path
            folded.setdefault(rel.casefold(), path)
    return exact, folded, unreadable_dirs, linked_dirs


def files_count(number):
    """«1 файл», «2 файла», «5 файлов» - инструмент выходит на люди."""
    tail, hundred = number % 10, number % 100
    if tail == 1 and hundred != 11:
        word = "файл"
    elif 2 <= tail <= 4 and not 12 <= hundred <= 14:
        word = "файла"
    else:
        word = "файлов"
    return "%d %s" % (number, word)


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
    # Метку ищем ВНЕ блоков кода: иначе заметка, приводящая её как пример,
    # молча исключает из проверки сама себя - а это ровно тот файл, который
    # рассказывает, как проверка работает.
    #
    # По всему файлу, без лимита на первые строки: наша же конвенция памяти -
    # шапка плюс обязательные разделы - съедает два десятка строк раньше, чем
    # автор дойдёт до пометки, и честно помеченный файл блокировал коммит.
    # От файла ПРО метку теперь защищает фильтр блоков кода, а не лимит строк.
    opened_fence = ""
    for line in text.splitlines():
        mark, tail = fence_delimiter(line)
        if opened_fence:
            if mark and closes_fence(mark, opened_fence) and not tail.strip():
                opened_fence = ""
            continue
        if mark:
            opened_fence = mark
            continue
        if COMMENT_ORPHAN.search(line):
            return True
    return False


def find_indexes(exact, index_name):
    """Все индексы: корневой и под-индексы в подпапках - под одним именем.

    Берём их из уже построенной карты файлов, а не вторым обходом дерева:
    хук зовут на каждый коммит, и повторный os.walk вместе с проверкой каждой
    подпапки на связанность был бесплатной тратой.

    Имя сравнивается точно и с учётом регистра: на Windows сравнение без
    учёта регистра прошло бы, а в CI на Linux набор файлов разъехался бы.
    """
    return [exact[rel] for rel in sorted(exact)
            if os.path.basename(rel) == index_name]


def looks_like_a_stray_index(root, index_name, exact):
    """Файлы в КОРНЕ, названные под старую плоскую раскладку.

    `MEMORY_infra.md` рядом с `MEMORY.md` - это прежняя схема, где под-индексы
    жили в корне и опознавались шаблоном. В строгой модели такой файл индексом
    не считается, его строки не разбираются, и всё перечисленное в нём
    становится сиротами. Стена сирот без объяснения читается как поломка
    проверки, поэтому причину надо назвать вслух.

    Требуем оба признака: похожее имя И строки индексного формата внутри.
    Одного имени мало - `MEMORY_of_incident.md` может быть обычным фактом.
    """
    # Регистр не важен: "memory_infra.md" - тот же случай, что "MEMORY_infra.md",
    # и человек получит ту же стену сирот.
    base = os.path.splitext(index_name)[0].casefold()
    stray = []
    for rel, path in sorted(exact.items()):
        if "/" in rel or rel == index_name:
            continue
        if not rel.lower().endswith(".md"):
            continue
        if not os.path.splitext(rel)[0].casefold().startswith(base):
            continue
        try:
            rows, _unclosed, _lost = parse_index(path)
        except OSError:
            continue
        if rows:
            stray.append(rel)
    return stray


# Имя файла памяти как цельный токен. Максимальная жадность и обязательное
# окончание на .md дают границу сами: в «superuser.md» найдётся именно
# «superuser.md», а не «user.md» внутри него.
FILENAME_TOKEN = re.compile(r"[\w.\-/\\]+\.md", re.I)


def map_mentions(others, wanted, cache):
    """Где встречается каждое из искомых имён: один проход по каждому файлу.

    Прежде для КАЖДОЙ сироты заново сканировались все остальные файлы -
    O(сирот × файлов) поисков по регулярке. На памяти масштаба портфеля
    (240 файлов) сорок сирот превращали проверку из 45 мс в две с половиной
    секунды - и это ровно тот момент, когда хук зовут чаще всего: человек
    чинит разъехавшуюся память и коммитит снова.

    Теперь каждый файл читается и разбирается один раз, а совпадение ищется
    поиском по множеству.
    """
    found = {}
    for path, rel in others:
        text = cache.get(rel)
        if text is None:
            try:
                text = read_text(path)
            except OSError:
                text = ""
            cache[rel] = text
        for token in set(FILENAME_TOKEN.findall(text)):
            normalised = token.replace("\\", "/")
            for candidate in (normalised, os.path.basename(normalised)):
                if candidate in wanted:
                    found.setdefault(candidate, rel)
    return found


def read_all(paths):
    """Тексты файлов одним словарём: нечитаемый - пустая строка, не отказ."""
    texts = {}
    for path in paths:
        try:
            texts[path] = read_text(path)
        except OSError:
            texts[path] = ""
    return texts


def mentioned_in_raw_text(index_texts, *names):
    """Имя файла встречается в тексте индекса, но ссылкой не разобралось.

    Тогда сообщение «не упомянут ни в одном индексе» человека дезориентирует:
    строку он видит своими глазами. Причина бывает разная - оформление
    (жирный заголовок, нумерованный список, таблица), блок кода или
    закомментированная строка, - поэтому и формулировка осторожная.

    Границы слева обязательны: без них «user.md» находится внутри
    «superuser.md», и подсказка утверждала бы небылицу.
    """
    patterns = [re.compile(r"(?<![\w.\-/])" + re.escape(name)) for name in names if name]
    return any(pattern.search(text)
               for text in index_texts.values() for pattern in patterns)


def check(root, index_paths, allow_globs, index_name, file_map):
    """Возвращает (ошибки, структурные заметки, советы, строк, непроверяемо).

    Заметка структурная, если часть проверки не выполнилась: незакрытый блок
    кода, нечитаемый индекс, ноль разобранных строк. Такие печатаются всегда,
    в том числе под --quiet: молчать о невыполненной проверке - тот самый
    тихий отказ. Советы (одинаковые заголовки, пустая новая память) ничего
    не блокируют и под --quiet молчат, чтобы хук не бурчал на каждом коммите.
    """
    errors = []
    notices = []
    warnings = []
    referenced = set()
    # Строки копим по индексу, а не сразу в общий котёл: засчитывать их
    # упомянутыми можно только после того, как известно, достижим ли сам
    # индекс. Иначе под-индекс, до которого неоткуда дойти, «отмывает» свои
    # файлы - и пометка orphan на нём давала зелёный свет невидимой ветке.
    per_index = defaultdict(list)
    titles = defaultdict(set)
    seen_rows = set()
    index_links = defaultdict(set)
    empty_indexes = []
    unreadable = []
    incomplete = []
    row_count = 0

    # Пути внутри проверки нормализованы через прямой слэш, а на Windows
    # человек напишет шаблон через обратный - и ключ молчал бы вхолостую,
    # выглядя рабочим.
    allow_globs = [pattern.replace("\\", "/") for pattern in allow_globs]

    exact, folded, unreadable_dirs, linked_dirs = file_map

    # Каталог, который не открылся, уносит с собой факты: сказать про такую
    # память «согласована» нельзя, это невыполненная проверка (код 2).
    for where in unreadable_dirs:
        notices.append("%s: каталог не читается - его содержимое в проверку не "
                       "попало, о забытых в индексе файлах судить нельзя"
                       % (relative_to_root(root, where) or where))

    # Связанные каталоги пропускаются намеренно (чужая память), но человек по
    # выводу не отличит «проверено и чисто» от «сюда даже не заходили».
    for where in linked_dirs:
        notices.append("%s: связанный каталог (симлинк или junction) - пропущен, "
                       "его содержимое не проверялось"
                       % (relative_to_root(root, where) or where))

    index_rels = {relative_to_root(root, p) for p in index_paths}
    # Корневой индекс ОДИН: файл с заданным именем в самой папке памяти.
    # Под-индекс - файл с тем же именем в подпапке. Двух корневых не бывает
    # по построению, поэтому гадать, какой из лежащих рядом файлов загрузится,
    # не приходится - а прежняя модель гадала и ошибалась в обе стороны:
    # ложной тревогой на здоровой памяти и молчанием на разъехавшейся.
    #
    # Отсутствие корневого - не находка, а невыполнимая проверка: её ловит
    # вызывающий (main) и возвращает код 2 ещё до разбора.
    roots = {index_name} if index_name in index_rels else set()

    # Файлы в корне, названные под старую плоскую раскладку: их строки не
    # разбираются, и всё перечисленное в них станет сиротами. Причину надо
    # назвать, иначе вывод читается как поломка проверки.
    for stray in looks_like_a_stray_index(root, index_name, exact):
        notices.append(
            "%s: лежит в корне и похож на индекс, но индексом не считается - "
            "корневой индекс один (%s), а под-индекс живёт в подпапке под тем "
            "же именем (например infra/%s). Строки этого файла не разбираются, "
            "и то, на что он ссылается, будет считаться сиротами"
            % (stray, index_name, index_name)
        )

    # L1: каждая ссылка из индекса ведёт на существующий файл.
    for index_path in index_paths:
        where = relative_to_root(root, index_path) or index_path
        try:
            index_rows, unclosed, lost_rows = parse_index(index_path)
        except OSError as exc:
            notices.append("%s: файл не читается (%s)" % (where, exc.strerror or exc))
            unreadable.append(where)
            continue
        if unclosed:
            # Число в конце: опечатка в последней строке и проглоченная
            # половина индекса выглядят одинаково, пока не назван масштаб.
            lost = (", из них похожих на строки индекса: %d" % lost_rows
                    if lost_rows else "")
            notices.append(
                "%s: %s не закрыт до конца файла - строки ниже в разбор не попали%s"
                % (where, unclosed, lost)
            )
            # Часть индекса не прочитана - тот же случай, что нечитаемый индекс.
            # Прежде печаталась заметка, а код оставался нулевым: CI зеленел на
            # индексе, разобранном наполовину.
            incomplete.append(where)
        if not index_rows:
            empty_indexes.append(where)
        for title, raw_target, lineno, kind in index_rows:
            row_count += 1
            if kind == "ref-missing":
                errors.append(
                    "L1 %s:%d ссылка-метка «%s» нигде не определена: строки "
                    "«[%s]: файл.md» в индексе нет"
                    % (where, lineno, raw_target, raw_target)
                )
                continue
            target = clean_target(raw_target)
            if not target:
                # Прежде такая строка отбрасывалась вместе с якорями: человек
                # видит строку в индексе и считает файл упомянутым, а адреса нет.
                errors.append("L1 %s:%d строка без адреса: «%s»" % (where, lineno, title))
                continue
            if target.startswith("#"):
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
                per_index[where].append((title, actual_rel))
                if actual_rel in index_rels and actual_rel != where:
                    index_links[where].add(actual_rel)
                row_key = (where, title, actual_rel)
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
    #
    # Считаем по каждому индексу отдельно: если корневой написан не в том
    # формате, а под-индекс разобрался, общий счётчик был бы ненулевым - и
    # человек получил бы стену «сирот» без единого намёка на причину.
    if empty_indexes:
        has_facts = any(rel.lower().endswith(".md") and rel not in index_rels
                        for rel in exact)
        if not has_facts and len(empty_indexes) == len(index_paths):
            warnings.append(
                "Индекс пуст: строк формата `- [Заголовок](файл.md) - крючок` в нём нет. "
                "Для новой памяти это нормально"
            )
        else:
            for where in empty_indexes:
                notices.append(
                    "%s: ни одной строки формата `- [Заголовок](файл.md) - крючок` "
                    "- проверьте формат строк индекса" % where
                )

    # Индекс не прочитан - значит про упоминания сказать НЕЧЕГО, и объявлять
    # файлы сиротами нельзя: это обвинило бы человека в разъехавшейся памяти
    # из-за файла, занятого редактором или антивирусом. L1 по прочитанным
    # индексам остаётся честным, L2 не проверяем вовсе.
    if unreadable:
        notices.append(
            "L2 и L3 не проверялись: не прочитано индексов - %d. Пока их не "
            "прочесть, сказать, какие файлы забыты в индексе, невозможно"
            % len(unreadable)
        )
        return errors, notices, warnings, row_count, True

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

    # Теперь, когда достижимость известна, засчитываем упомянутыми только те
    # строки, что лежат в достижимых индексах.
    for where, rows_of in per_index.items():
        if where not in reachable:
            continue
        for title, actual_rel in rows_of:
            referenced.add(actual_rel)
            titles[title].add(actual_rel)

    orphans = []
    for rel in sorted(exact):
        if not rel.lower().endswith(".md"):
            continue
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in allow_globs):
            continue
        if rel in index_rels:
            if rel in reachable:
                continue
            behind = len({actual_rel for _title, actual_rel in per_index.get(rel, ())})
            # Без придаточного: «1 файл, которые станут» не согласуется, а
            # число тут любое.
            tail = ". За ним скрыто %s" % files_count(behind) if behind else ""
            if is_orphan_ok(exact[rel]):
                # Метка снимает вопрос с самого файла - но не делает достижимым
                # то, что он перечисляет. Молчать об этом нельзя: человек ставит
                # метку, чтобы заглушить предупреждение, и получает зелёный свет
                # вместе с невидимой веткой памяти.
                if behind:
                    notices.append(
                        "%s: под-индекс помечен как намеренно вне индекса, но до "
                        "него неоткуда дойти%s - пометьте скрытое тоже или "
                        "исключите каталог ключом --allow-orphan" % (rel, tail)
                    )
                continue
            errors.append(
                "L2 %s - под-индекс, до которого неоткуда дойти: от корневого "
                "индекса (%s) цепочки ссылок на него нет%s" % (rel, index_name, tail)
            )
            continue
        if rel in referenced or is_orphan_ok(exact[rel]):
            continue
        orphans.append(rel)

    # Подсказки считаем отдельным проходом: тексты соседних файлов читаются
    # один раз на всех сирот, а не заново на каждую. Индексы - тем же приёмом:
    # прежде они перечитывались с диска на КАЖДОГО сироту.
    cache = {}
    index_texts = read_all(index_paths)
    others = [(exact[other], other) for other in sorted(exact)
              if other.lower().endswith(".md") and other not in index_rels]
    wanted = set()
    for rel in orphans:
        wanted.add(rel)
        wanted.add(os.path.basename(rel))
    mentions = map_mentions(others, wanted, cache) if wanted else {}
    # Если файл упомянут в индексе, до которого неоткуда дойти, причина именно
    # в этом - и говорить «проверьте формат строки» значит отправить человека
    # чинить не то.
    stranded = {}
    for where, rows_of in per_index.items():
        if where in reachable:
            continue
        for _title, actual_rel in rows_of:
            stranded.setdefault(actual_rel, where)

    for rel in orphans:
        hint = ""
        if rel in stranded:
            errors.append(
                "L2 %s не упомянут ни в одном достижимом индексе: он перечислен "
                "в %s, а до того от корневого индекса (%s) дойти неоткуда"
                % (rel, stranded[rel], index_name)
            )
            continue
        if mentioned_in_raw_text(index_texts, rel, os.path.basename(rel)):
            hint = (" (имя файла в тексте индекса встречается, но ссылкой не "
                    "разобралось - проверьте формат строки, блок кода, комментарий)")
        else:
            # Частый случай: человек завёл свой файл-список и назвал его
            # по-своему. Для нас это обычный файл памяти, его строки мы не
            # разбираем - и говорить «агент не увидит» без объяснения значит
            # соврать: агент дойдёт по ссылке из корневого индекса.
            source = mentions.get(rel) or mentions.get(os.path.basename(rel))
            if source == rel:
                source = None  # файл упомянул сам себя - это не источник
            if source:
                # Здесь «агент его не увидит» было бы неправдой: по ссылке из
                # индекса он дойдёт. Проблема в другом - строки этого файла мы
                # не разбираем, и битые ссылки внутри него не проверяются.
                errors.append(
                    "L2 %s не упомянут ни в одном индексе. На него ссылается %s, "
                    "но тот индексом не считается: его строки не разбираются, и "
                    "битые ссылки внутри него не ловятся. Индексом считается файл "
                    "с именем %s - переименуйте его так или задайте своё имя "
                    "ключом --index" % (rel, source, index_name)
                )
                continue
        errors.append("L2 %s не упомянут ни в одном индексе - агент его не увидит%s"
                      % (rel, hint))

    # L3: одинаковый заголовок у разных файлов - предупреждение, не ошибка.
    # Индекс намеренно не уникальный ключ, поэтому это сигнал, а не запрет.
    for title, paths in sorted(titles.items()):
        if len(paths) > 1:
            warnings.append("L3 заголовок «%s» ведёт на разные файлы: %s"
                            % (title, ", ".join(sorted(paths))))

    return errors, notices, warnings, row_count, bool(incomplete or unreadable_dirs)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Проверяет, что индекс памяти и файлы памяти не разошлись.",
        epilog="Коды возврата: 0 - порядок, 1 - нарушения L1/L2, 2 - проверку выполнить не удалось.",
    )
    parser.add_argument("memory_dir", nargs="?", default="memory",
                        help="папка памяти (по умолчанию: memory)")
    parser.add_argument("--index", default="MEMORY.md", metavar="ИМЯ",
                        help="имя файла индекса (по умолчанию: MEMORY.md). "
                             "Корневой - файл с этим именем в самой папке памяти; "
                             "под-индекс - файл с тем же именем в подпапке")
    parser.add_argument("--allow-orphan", action="append", default=[], metavar="GLOB",
                        help="файл(ы), которым позволено не быть в индексе; можно повторять")
    parser.add_argument("--quiet", action="store_true",
                        help="молчать, когда нарушений нет")
    return parser


def main(argv=None):
    force_utf8_output()
    args = build_parser().parse_args(argv)

    try:
        # abspath внутри перехвата намеренно: на Linux он зовёт getcwd(), а тот
        # кидает, если текущий каталог удалён - получился бы трейсбек и код 1,
        # то есть "у вас разъехалась память" вместо "проверка не выполнилась".
        root = os.path.abspath(args.memory_dir)
        if not os.path.isdir(root):
            print("Папка памяти не найдена: %s" % args.memory_dir, file=sys.stderr)
            return EXIT_USAGE

        # Имя, а не шаблон. Прежняя форма ключа принимала глоб, и из него
        # выводилось имя корневого - вывод промахивался на "*MEMORY.md" и на
        # шаблонах с двумя звёздочками, причём молча. Глоб теперь отвергаем
        # вслух: тихо «почти работающий» ключ хуже отказа.
        if any(ch in args.index for ch in "*?["):
            print("Ключ --index принимает ИМЯ файла индекса, а не шаблон: %s. "
                  "Например: --index MEMORY.md" % args.index, file=sys.stderr)
            return EXIT_USAGE

        # Один обход дерева на весь прогон: карта файлов строится первой, а
        # индексы берутся из неё.
        file_map = build_file_map(root)
        index_paths = find_indexes(file_map[0], args.index)

        # Корневой обязан лежать в самой папке памяти. Без него проверять
        # нечего: под-индексы сами по себе не загружаются, и «согласовано»
        # тут означало бы, что агент читает пустоту. Это невыполнимая
        # проверка (код 2), а не находка.
        if not any(os.path.dirname(p) == root for p in index_paths):
            if index_paths:
                where = ", ".join(sorted(relative_to_root(root, p) or p
                                         for p in index_paths))
                print("Корневой индекс не найден: %s нет в самой папке %s "
                      "(с этим именем нашлось только глубже: %s). Либо указана "
                      "не та папка, либо корневой индекс назван иначе"
                      % (args.index, args.memory_dir, where), file=sys.stderr)
            else:
                print("Индекс не найден: %s"
                      % os.path.join(args.memory_dir, args.index), file=sys.stderr)
            return EXIT_USAGE

        errors, notices, warnings, row_count, unverifiable = check(
            root, index_paths, args.allow_orphan, args.index, file_map)
    except Exception as exc:  # проверка сломалась - это не нарушение памяти
        print("Проверку выполнить не удалось: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_USAGE

    # Предупреждения первыми: они объясняют, откуда взялись ошибки, и под
    # списком из шести обвинений объяснение никто не читает.
    #
    # Под --quiet (так проверку зовёт хук) отдельно стоящие предупреждения
    # молчат: они ничего не блокируют, а долгоживущее предупреждение вроде
    # одинаковых заголовков повторялось бы на каждом коммите и приучало бы
    # пролистывать вывод, а там и к --no-verify. Вместе с ошибкой они нужны -
    # без причины остаются одни обвинения.
    # Структурные заметки печатаются всегда: они говорят, что часть проверки
    # не выполнилась, и молчать об этом нельзя даже под --quiet.
    for line in notices:
        print(line)
    if errors or unverifiable or not args.quiet:
        for line in warnings:
            print(line)
    for line in errors:
        print(line)

    if unverifiable and not errors:
        return EXIT_USAGE
    if errors:
        print("\nНарушений: %d (строк в индексе: %d)" % (len(errors), row_count))
        return EXIT_VIOLATION
    if not args.quiet:
        total_notes = len(notices) + len(warnings)
        note = ", предупреждений: %d" % total_notes if total_notes else ""
        print("Память согласована: строк в индексе %d, индексов %d%s"
              % (row_count, len(index_paths), note))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
