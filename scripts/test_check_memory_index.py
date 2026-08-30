#!/usr/bin/env python3
"""Тесты линтера согласованности памяти.

Запуск из корня репозитория:

    python -m unittest discover -s scripts -v

Зависимостей нет, только стандартная библиотека.

Карта: где искать проверки инварианта
-------------------------------------

  L1, ссылки и адреса       IndexLinksToFiles, FilenameTokenBoundaries
  форма строки и разбор     OneRowForm, IndexParsing
  L2, достижимость          FilesAppearInIndex, StrictRootIndexModel, RootIndexLost,
                            OrphansAndWhatHidesBehindThem
  L3, дубли заголовков      DuplicateTitles
  L4, связи [[имя]]         WikiLinksBetweenFacts
  L5 и L6, имена            MemoryFileNameIsASlug
  шапка файла               FrontmatterDiagnostics
  коды возврата 0/1/2       ExitCodes, ExitCodeContract, SilentFailures
  качество сообщений        HintsDoNotOverclaim, SourceHintPointsAtTheRightFile,
                            SummaryTellsWhatBlocks, AddressesAreClickable
  обход файловой системы    MemoryFolderBoundary, LinkedSubtrees, UnreadableEntries
  цена прогона              HotPathStaysLinear, FilenameTokenScan
  хук и CI                  PreCommitHook, HookPrerequisite, CiGuards,
                            DocumentationMatchesReality, OwnMemoryIsConsistent

Прежде часть классов называлась по кругам ревью, в которых находки всплыли:
`PanelReviewFindings`, `SecondRoundFindings`, `ThirdRoundFindings` и далее.
Такое имя говорит, КОГДА находку заметили, а не ЧТО она держит, и ответить по
нему на вопрос «какие тесты покрывают L2» было нельзя, не прочитав все двести.
Своя цена у этого тоже была: шестой круг завёл класс, не заглянув, что тот же
случай уже стоит в старом, - отсюда две пары тестов, совпадающих байт в байт
(`test_sub_indexes_count_by_default_without_flags` /
`test_sub_index_in_subfolder_is_reachable` и `test_unreferenced_sub_index_is_error` /
`test_sub_index_in_subfolder_without_a_link_is_error`). Дубли оставлены:
докстринги у них разные, и каждый фиксирует свою причину.
"""

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import time
import threading
import unittest
import unittest.mock
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_memory_index as linter  # noqa: E402

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPTS_DIR)
SCRIPT = os.path.join(SCRIPTS_DIR, "check_memory_index.py")
HOOK = os.path.join(REPO_DIR, ".githooks", "pre-commit")


MISSING_SH_MESSAGE = "не нашёл sh - тесты хука НЕ выполнялись, это не значит, что хук исправен"


def findings(output, code):
    """Строки-находки с этим кодом, по началу строки.

    Не подстрокой. Итог вывода называет коды словами («L1 ... и L2 ...
    блокируют коммит»), и `assertIn("L4", output)` проходил бы по этой
    строке, ничего не гарантируя, а `assertNotIn("L2", output)` краснел бы
    на исправной памяти. Обе беды - один и тот же дешёвый признак: проверка
    наличия слова вместо проверки находки.
    """
    return [line for line in output.splitlines()
            if line.startswith(code + " ")]


class HookPrerequisite(unittest.TestCase):
    """Один внятный провал вместо девятнадцати одинаковых.

    В CI пропуск тестов хука - это зелёная галочка при невыполненной
    проверке, то есть ровно тот тихий отказ, который эта проверка и ловит.
    Поэтому там выставлена MEMCHECK_REQUIRE_SH, и пропуск становится провалом.
    """

    def test_sh_is_available(self):
        if find_sh() and shutil.which("git"):
            return
        if os.environ.get("MEMCHECK_REQUIRE_SH"):
            self.fail(MISSING_SH_MESSAGE)
        self.skipTest(MISSING_SH_MESSAGE)


def find_sh():
    """sh из PATH, а если его там нет - из комплекта Git.

    На Windows git.exe лежит в PATH, а sh.exe - нет, он рядом в usr/bin.
    Без этого весь класс тестов хука молча пропускался бы ровно на той
    системе, ради которой в хуке половина кода.
    """
    found = shutil.which("sh")
    if found:
        return found
    git = shutil.which("git")
    if not git:
        return None
    root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
    candidate = os.path.join(root, "usr", "bin", "sh.exe")
    return candidate if os.path.isfile(candidate) else None


class MemoryFixture(unittest.TestCase):
    """Собирает временную папку памяти из словаря «имя файла -> содержимое»."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="memcheck-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, files, encoding="utf-8", newline="\n"):
        for name, body in files.items():
            path = os.path.join(self.root, name)
            folder = os.path.dirname(path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            with io.open(path, "w", encoding=encoding, newline=newline) as fh:
                fh.write(body)

    def run_linter(self, *extra):
        """Возвращает (код возврата, напечатанный текст)."""
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = linter.main([self.root] + list(extra))
        return code, out.getvalue() + err.getvalue()

    def snapshot(self):
        """Слепок дерева: пути + хеши содержимого."""
        state = {}
        for folder, _dirs, names in os.walk(self.root):
            for name in sorted(names):
                path = os.path.join(folder, name)
                with open(path, "rb") as fh:
                    state[os.path.relpath(path, self.root)] = hashlib.sha256(fh.read()).hexdigest()
        return state


class IndexLinksToFiles(MemoryFixture):
    """L1: каждая ссылка из индекса ведёт на существующий файл."""

    def test_consistent_memory_passes(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_link_to_missing_file_is_error(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1)
        self.assertTrue(findings(output, "L1"), output)

    def test_case_mismatch_is_error_even_on_case_insensitive_fs(self):
        """Windows такую ссылку проглотит, Linux в CI - нет. Ловим на обеих."""
        self.write({
            "MEMORY.md": "- [Профиль](User.md) - кто пользователь\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("регистр", output.lower())

    def test_every_l1_message_names_an_action(self):
        """Инструмент не должен вести себя по-разному в зависимости от буквы.

        L2 и L4 говорят, что делать, а пять из шести вариантов L1 называли
        только диагноз. Читает вывод и человек, и агент, которому эту память
        чинить: «ссылка в никуда: net.md» - верно и невыполнимо.

        Вход поднимает пять вариантов сразу, чтобы правило проверялось как
        правило, а не на одном удачном примере: новый вариант L1 без действия
        покраснит этот тест.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Битая](net.md) - файла нет\n"
                         "- [Без адреса]() - пусто\n"
                         "- [Каталог](infra) - это папка\n"
                         "- [Регистр](User2.md) - не тот регистр\n"
                         "- [Метка][nowhere] - метка не определена\n",
            "user.md": "факт\n",
            "user2.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "сервер\n",
        })
        _code, output = self.run_linter()
        lines = findings(output, "L1")
        self.assertEqual(len(lines), 5, output)
        imperatives = ("Создайте", "Допишите", "Сошлитесь", "Приведите",
                       "Добавьте", "Перенесите", "уберите")
        for line in lines:
            self.assertTrue(any(word in line for word in imperatives),
                            "сообщение без действия: %s" % line)

    def test_broken_link_names_all_three_ways_out(self):
        """Самый частый вариант L1 - и все три законных выхода из него.

        Файл можно создать, путь поправить, а строку убрать, если факт
        больше не нужен. Агенту важно, что выбор назван: иначе он выберет
        первый попавшийся.
        """
        self.write({
            "MEMORY.md": "- [Битая](net.md) - файла нет\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("Создайте этот файл либо поправьте путь", output)
        self.assertIn("уберите строку", output)

    def test_case_mismatch_reports_exactly_one_error(self):
        """Один дефект - одна строка. Файл не должен всплыть ещё и как сирота."""
        self.write({
            "MEMORY.md": "- [Профиль](User.md) - кто пользователь\n",
            "user.md": "факт\n",
        })
        _code, output = self.run_linter()
        self.assertFalse(findings(output, "L2"), output)
        self.assertIn("Нарушений: 1", output)

    def test_case_mismatch_in_directory_part_is_caught(self):
        """Регистр каталога - та же ловушка, что и у имени файла."""
        self.write({
            "MEMORY.md": "- [Сервер](Sub/server.md) - прод\n",
            "sub/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("регистр", output.lower())
        self.assertIn("Нарушений: 1", output)

    def test_nested_link_resolves(self):
        self.write({
            "MEMORY.md": "- [Сервер](sub/server.md) - прод\n",
            "sub/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_anchor_in_link_is_ignored(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md#раздел) - кто пользователь\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_external_url_is_not_checked(self):
        self.write({
            "MEMORY.md": "- [Дашборд](https://example.com/grafana) - внешний адрес\n"
                         "- [Почта](mailto:hi@example.com) - тоже внешний\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_address_has_one_form_too(self):
        """У адреса, как и у строки, одна форма - голый путь.

        Markdown знает ещё две: в угловых скобках (нужны ровно для пробела в
        пути - после L6 их в памяти не бывает) и с подсказкой в кавычках
        (в индексе бессмысленна: описание несёт крючок после дефиса, а
        читает индекс агент, которому наводить нечем).

        Отвергаем громко: молча не разобрав адрес, проверка превратила бы
        `<user.md>` в «ссылку в никуда», и человек пошёл бы искать пропавший
        файл вместо того, чтобы убрать скобки.
        """
        self.write({
            "MEMORY.md": '- [Раз](user.md "Профиль пользователя") - подсказка\n'
                         '- [Два](<user.md>) - угловые скобки\n',
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        wrong = [line for line in findings(output, "L1") if "адрес записан" in line]
        self.assertEqual(len(wrong), 2, output)
        self.assertIn("с подсказкой в кавычках", wrong[0])
        self.assertIn("в угловых скобках", wrong[1])

    def test_bare_path_is_accepted(self):
        """Вторая половина пары: единственная форма разбирается как прежде."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Сервер](infra/prod.md) - прод\n",
            "user.md": "факт\n",
            "infra/prod.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_brackets_inside_link_text_still_parse(self):
        self.write({
            "MEMORY.md": "- [VPScan [beta]](vps.md) - сканер\n",
            "vps.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_link_outside_memory_root_is_error(self):
        """Урок из живой памяти: '..' уводит за папку и разрешается по-разному."""
        self.write({
            "MEMORY.md": "- [Снаружи](../README.md) - вне памяти\n",
        })
        with io.open(os.path.join(os.path.dirname(self.root), "README.md"), "w",
                     encoding="utf-8") as fh:
            fh.write("снаружи\n")
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("за папку памяти", output)

    def test_windows_drive_path_is_not_treated_as_external(self):
        self.write({
            "MEMORY.md": "- [Профиль](C:/net/takogo.md) - абсолютный путь\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L1"), output)

    def test_link_to_another_drive_is_a_finding_not_a_crash(self):
        """Разные диски: относительный путь между ними не вычисляется вообще.

        Раньше это роняло всю проверку в код 2, а хук трактует код 2 как
        «не блокирую» - то есть одна кривая строка молча выключала проверку.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Чужой диск](D:/other/file.md) - другой том\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L1"), output)

    def test_unc_path_is_a_finding_not_a_crash(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Сетевая шара](//server/share/file.md) - UNC\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L1"), output)

    def test_name_starting_with_dots_is_not_mistaken_for_escape(self):
        """`..dotdot` - имя, а не выход наверх.

        Имя файла памяти с точек теперь начинаться не может (L6), поэтому
        проверяем на каталоге: правило про имена на них не распространяется,
        а спутать «..dotdot/» с «../» по-прежнему нельзя.
        """
        self.write({
            "MEMORY.md": "- [Странное имя](..dotdot/user.md) - каталог, не выход наверх\n",
            "..dotdot/user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_empty_destination_is_not_silently_skipped(self):
        """`- [Заголовок]()` - строка есть, адреса нет: молчать об этом нельзя.

        Прежде такая строка распознавалась и тут же отбрасывалась вместе с
        якорями. Человек видит строку в индексе и считает файл упомянутым.
        """
        self.write({
            "MEMORY.md": "- [Профиль]() - крючок есть, адреса нет\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("адрес", output.lower())

    def test_anchor_only_destination_stays_allowed(self):
        """Якорь внутри того же документа - законная строка, её не трогаем."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [К разделу](#razdel) - якорь внутри индекса\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_link_to_a_directory_says_so(self):
        """`[Инфра](infra)` вместо `infra/MEMORY.md` - частая опечатка.

        Ветка сообщения существовала, но ни один из тестов на неё не попадал:
        мутация «отключить ветку» переживала весь набор. Диагноз «ссылка в
        никуда» вместо «это каталог» отправляет чинить не то.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Инфра](infra) - раздел\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("каталог", output)


class MemoryFileNameIsASlug(MemoryFixture):
    """L6: имя файла памяти - строчная латиница, цифры, дефис, подчёркивание.

    Имя здесь не украшение, а идентификатор: по нему ссылается индекс, по нему
    же ведут [[связи]], и с ним обязано совпадать поле `name`. Пока оно могло
    быть любым, каждая вольность обзаводилась своим обработчиком - пробел,
    процент, регистр.

    Замер на девяти живых памятях: 405 файлов, ни одного нарушения. Правило
    описывает то, как уже пишут, - переименовывать нечего.
    """

    def test_space_in_the_name_is_an_error(self):
        """Пробел - причина, по которой `[[имя со словами]]` не проверялось.

        Ссылка отличается от прозы в двойных скобках только тем, что в имени
        пробела не бывает. Пока он был возможен, приходилось выбирать: либо
        врать на прозе, либо молчать о части ссылок.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Плохое](<moya zametka.md>) - с пробелом\n",
            "user.md": "факт\n",
            "moya zametka.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L6"), output)

    def test_uppercase_in_the_name_is_an_error(self):
        """`User.md` и `user.md` на Windows один файл, на Linux два."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Плохое](Zametka.md) - прописная\n",
            "user.md": "факт\n",
            "Zametka.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L6"), output)

    def test_lawful_names_pass(self):
        """Вторая половина пары: правило описывает то, как уже пишут.

        Дефис, подчёркивание, цифры и файл в подпапке - всё это законно, и
        правило не должно превращаться в запрет на нормальные имена.
        """
        self.write({
            "MEMORY.md": "- [Раз](feedback_ask_dont_guess.md) - подчёркивания\n"
                         "- [Два](project-vpscan-2026.md) - дефисы и цифры\n"
                         "- [Три](infra/prod.md) - в подпапке\n",
            "feedback_ask_dont_guess.md": "факт\n",
            "project-vpscan-2026.md": "факт\n",
            "infra/prod.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_directory_names_obey_the_rule_too(self):
        """Каталог входит в адрес наравне с именем файла.

        `- [Сервер](Моя Папка/prod.md)` - тот же пробел и та же
        неоднозначность, только этажом выше. Пока правило кончалось на файлах,
        ради подпапок приходилось держать разворачивание `%20`, угловые
        скобки в адресе и нормализацию юникода: три механизма на случай,
        которого правило не допускает. Три теста, проверявшие их на именах
        каталогов, удалены - законным путём такие имена больше недостижимы.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Сервер](<ok space/prod.md>) - пробел в каталоге\n",
            "user.md": "факт\n",
            "ok space/prod.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        broken = [line for line in findings(output, "L6") if "каталога" in line]
        self.assertEqual(len(broken), 1, output)

    def test_a_bad_directory_is_named_once_not_per_file(self):
        """Вторая половина пары: каталог называется один раз.

        Иначе одно нарушение печаталось бы столько раз, сколько файлов внутри,
        и человек читал бы стену вместо одной строки.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "Bad Dir/one.md": "факт\n",
            "Bad Dir/two.md": "факт\n",
            "Bad Dir/three.md": "факт\n",
        })
        _code, output = self.run_linter()
        named = [line for line in findings(output, "L6") if "каталога" in line]
        self.assertEqual(len(named), 1, output)

    def test_the_index_itself_is_exempt(self):
        """Индекс набран прописными намеренно, и правило его не касается.

        Иначе `MEMORY.md` - и корневой, и каждый под-индекс - стал бы
        нарушением, то есть правило запретило бы саму схему.
        """
        self.write({
            "MEMORY.md": "- [Инфра](infra/MEMORY.md) - под-индекс\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_cyrillic_file_name_is_an_error(self):
        """Кириллическое имя - нарушение правила, а не особый случай.

        Тест прежде утверждал обратное: «проект русскоязычный, кириллическое
        имя файла - вопрос времени», и проверял, что такой файл резолвится.
        После L6 это неверно, и он проходил по чужой причине - код 1 давала
        сама кириллица, а не то, что тест собирался проверить.

        Цена правила названа прямо в README: кириллица в именах запрещена,
        хотя законна и в git, и в файловой системе.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Заметка](заметка.md) - крючок\n",
            "user.md": "факт\n",
            "заметка.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L6"), output)


class OneRowForm(MemoryFixture):
    """У строки индекса ровно одна форма: `- [Заголовок](файл.md) - крючок`.

    Markdown знает ещё две - через метку (`- [Текст][prof]` плюс
    `[prof]: файл.md`) и сокращённую (`- [prof]`). Обе поддерживались, и
    поддержка стоила дороже всего в разборе: словарь определений, правило
    CommonMark «при повторе побеждает первое», второй проход (определение
    законно стоит НИЖЕ ссылки на него), угловые скобки для путей с пробелом,
    свёрнутая форма `[prof][]` и отдельный список TASK_MARKS - потому что
    `- [ ]` неотличимо от сокращённой формы. Один круг ревью на этом сгорел:
    чинили чеклист, сломали законную метку `[-]`.

    Двенадцать тестов, закреплявших эти формы, удалены вместе с ними. Здесь
    вместо них - стандарт целиком, с обеих сторон.
    """

    def test_reference_row_is_rejected_with_the_right_form_named(self):
        """Отвергать надо ГРОМКО.

        Если просто перестать разбирать чужую форму, строки не станут
        строками, файлы всплывут сиротами, и человек будет видеть свои строки
        глазами, не понимая претензии. Это ровно тот дефект, который чинили в
        сообщении про недочитанный индекс.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - обычная форма\n"
                         "- [Правило][prof] - форма через метку\n"
                         "\n[prof]: feedback.md\n",
            "user.md": "факт\n",
            "feedback.md": "правило\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        rows = [line for line in findings(output, "L1")
                if "записана через метку" in line]
        self.assertEqual(len(rows), 2, output)
        self.assertIn("`- [Заголовок](имя-файла.md) - крючок`", rows[0])

    def test_normal_form_still_works(self):
        """Вторая половина пары: единственная оставшаяся форма разбирается."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - крючок\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_bracketed_words_in_prose_are_not_rows(self):
        """Сокращённая форма ушла - и вместе с ней повод разбирать чеклисты.

        `- [ ]`, `- [x]` и любое `[слово]` в списке теперь просто проза. Пока
        сокращённая форма считалась строкой индекса, их приходилось отличать
        отдельным списком TASK_MARKS, и на этом уже ломались: `[-]` не
        чекбокс ни в одном спек-совместимом рендерере, а из проверки его
        исключили и сломали законную ссылку.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - крючок\n"
                         "- [ ] не сделано\n"
                         "- [x] сделано\n"
                         "- [-] тоже не строка индекса\n"
                         "- [prof] и это не строка\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)


class IndexParsing(MemoryFixture):
    """Строки разбираются только там, где это действительно строки индекса."""

    def test_rows_inside_fenced_code_block_are_ignored(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "Формат строки:\n"
                         "\n"
                         "```markdown\n"
                         "- [Заголовок](primer.md) - крючок\n"
                         "```\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_rows_inside_html_comment_are_ignored(self):
        self.write({
            "MEMORY.md": "<!--\n"
                         "  Пример строки:\n"
                         "  - [Заголовок](primer.md) - крючок\n"
                         "-->\n"
                         "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_row_before_unclosed_comment_survives(self):
        """Комментарий открыт в хвосте строки - сама строка от этого не пропадает.

        Проверяем именно это: user.md разобран и сиротой не объявлен. Код при
        этом 2 - часть файла ниже комментария в разбор не попала.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто <!-- TODO дописать крючок\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 2, output)
        self.assertIn("не закрыт", output.lower())
        self.assertNotIn("L2 user.md", output)


    def test_row_after_comment_end_on_same_line_survives(self):
        self.write({
            "MEMORY.md": "<!--\nслужебная шапка\n--> - [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_comment_inside_fence_does_not_swallow_the_fence(self):
        """Иначе закрывающие кавычки съедаются, и остаток индекса теряется."""
        self.write({
            "MEMORY.md": "```markdown\n"
                         "<!-- пример строки -->\n"
                         "- [Заголовок](primer.md) - крючок\n"
                         "```\n"
                         "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_unclosed_fence_is_reported_not_silent(self):
        """Потерянная строка невидима - о ней надо сказать вслух."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "```markdown\n"
                         "- [Заголовок](primer.md) - забыли закрыть блок\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertIn("не закрыт", output.lower())

    def test_tilde_fence_is_recognised(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "~~~markdown\n"
                         "- [Заголовок](primer.md) - крючок\n"
                         "~~~\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_fence_opener_carrying_a_comment_does_not_swallow_the_tail(self):
        """Тот единственный случай, ради которого блок проверяется раньше комментария."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "```markdown <!-- незакрытый\n"
                         "- [Заголовок](primer.md) - крючок\n"
                         "```\n"
                         "- [Сервер](server.md) - прод\n",
            "user.md": "факт\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_indented_example_is_code_not_a_row(self):
        """Четыре пробела в markdown - это блок кода, а мы советуем писать примеры."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "Формат строки:\n"
                         "\n"
                         "    - [Заголовок](primer.md) - крючок\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_nested_list_rows_are_parsed(self):
        """Отступ под пунктом списка - вложенный пункт, а не блок кода."""
        self.write({
            "MEMORY.md": "- Инфраструктура\n"
                         "    - [Сервер](server.md) - прод\n"
                         "    - [База](db.md) - postgres\n",
            "server.md": "факт\n",
            "db.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_nested_rows_indented_with_tabs_are_parsed(self):
        self.write({
            "MEMORY.md": "- Инфраструктура\n\t- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_broken_link_inside_a_nested_group_is_still_caught(self):
        """Иначе вложенные группы просто выпадают из проверки."""
        self.write({
            "MEMORY.md": "- Инфраструктура\n    - [Сервер](net-takogo.md) - прод\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L1"), output)

    def test_row_indented_by_three_spaces_still_counts(self):
        """Три пробела - всё ещё список, а не код."""
        self.write({
            "MEMORY.md": "   - [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_star_and_plus_bullets_are_parsed(self):
        self.write({
            "MEMORY.md": "* [Профиль](user.md) - звёздочка\n+ [Сервер](server.md) - плюс\n",
            "user.md": "факт\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_empty_index_without_facts_passes_with_warning(self):
        """Свежая память - законное состояние, а не повод рубить первый коммит."""
        self.write({
            "MEMORY.md": "# Память\n\nПока пусто - первые факты появятся здесь.\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertIn("пуст", output.lower())

    def test_empty_index_with_existing_facts_hints_at_format(self):
        """Файлы есть, а строк ноль - почти наверняка индекс написан не так."""
        self.write({
            "MEMORY.md": "| Заголовок | Файл |\n|---|---|\n| Профиль | user.md |\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        # Именно заметка про разбор индекса, а не подсказка про сироту:
        # слово «формат» теперь есть и там, и там.
        self.assertIn("MEMORY.md: ни одной строки формата", output)

    def test_format_hint_names_the_index_that_failed(self):
        """При кривом корневом и рабочем под-индексе иначе не понять, где чинить.

        Под-индекс тут настоящий - `infra/MEMORY.md`, файл с тем же именем в
        подпапке. Прежняя фикстура клала рядом с корневым `MEMORY_infra.md`,
        а в строгой модели это вообще не индекс: индексов в памяти был ОДИН,
        и на таком входе «считаем по каждому индексу отдельно» неотличимо от
        «считаем по всем сразу» - тест не гарантировал ничего.

        Различие видно только на двух индексах: корневой в формате таблицы
        разобрался в ноль строк, под-индекс - нормально. Общий счётчик тут
        ненулевой, и реализация «по всем сразу» промолчала бы, оставив
        человека со стеной сирот без единого намёка на причину.
        """
        self.write({
            "MEMORY.md": "| Заголовок | Файл |\n|---|---|\n| Инфра | infra/MEMORY.md |\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        _code, output = self.run_linter()
        named = [line for line in output.splitlines()
                 if line.startswith("MEMORY.md:") and "ни одной строки" in line]
        self.assertEqual(len(named), 1, output)
        self.assertNotIn("infra/MEMORY.md: ни одной строки", output)

    def test_bom_and_crlf_index_is_read(self):
        self.write({"MEMORY.md": "- [Профиль](user.md) - кто\n"},
                   encoding="utf-8-sig", newline="\r\n")
        self.write({"user.md": "факт\n"})
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_lost_rows_counter_resets_when_a_comment_closes(self):
        """Строки закрытого комментария не должны приплюсовываться к настоящей потере.

        Сброс счётчика стоял только у забора. Строки из благополучно закрытого
        комментария утекали вперёд и раздували число - в том самом сообщении,
        которое завели ради честного масштаба потери.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "<!--\n"
                         "- [A](a.md) - в закрытом комментарии\n"
                         "- [B](b.md) - тоже\n"
                         "-->\n"
                         "\n"
                         "```markdown\n"
                         "- [C](c.md) - единственная настоящая потеря\n",
            "user.md": "факт\n",
        })
        _code, output = self.run_linter()
        lost = [line for line in output.splitlines() if "не закрыт" in line]
        self.assertEqual(len(lost), 1, output)
        self.assertRegex(lost[0], r"индекса: 1\b",
                         "число потерянных строк раздуто закрытым комментарием")

    def test_lost_rows_counter_resets_when_a_fence_closes(self):
        """Строки закрытого забора не должны приплюсовываться к настоящей потере.

        Парный к тесту про комментарий - и найден не рассуждением, а
        мутационным аудитом: сброс счётчика при закрытии ЗАБОРА оказался
        единственной веткой из тридцати одной, которую не ловил ни один тест
        из набора. Тест про комментарий существовал, про забор - нет, хотя
        сбросов в коде два.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "```markdown\n"
                         "- [A](a.md) - в закрытом заборе\n"
                         "- [B](b.md) - тоже\n"
                         "```\n"
                         "\n"
                         "<!--\n"
                         "- [C](c.md) - единственная настоящая потеря\n",
            "user.md": "факт\n",
        })
        _code, output = self.run_linter()
        lost = [line for line in output.splitlines() if "не закрыт" in line]
        self.assertEqual(len(lost), 1, output)
        self.assertRegex(lost[0], r"индекса: 1\b",
                         "число потерянных строк раздуто закрытым забором")

    def test_closing_fence_with_a_tail_does_not_close_the_block(self):
        """Правило про пустой хвост обещано в докстроке и не проверялось нигде.

        Закрывающий забор с хвостом (```python вместо ```) не закрывает блок.
        Иначе пример внутри блока начинает разбираться как настоящий индекс -
        ложная тревога ровно на приёме «документируем свой формат», который
        README и советует.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "````markdown\n"
                         "```python\n"
                         "- [Пример](primer.md) - это пример, не строка индекса\n"
                         "```\n"
                         "````\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_unclosed_fence_notice_says_how_many_rows_were_lost(self):
        """«Строки ниже в разбор не попали» - а сколько их было?

        Знание «потеряно 3 строки» отличает опечатку в конце файла от
        проглоченной половины индекса. Без числа человек не знает, срочно это
        или нет.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "```markdown\n"
                         "- [Раз](a.md) - крючок\n"
                         "- [Два](b.md) - крючок\n"
                         "- [Три](c.md) - крючок\n",
            "user.md": "факт\n",
        })
        _code, output = self.run_linter()
        unclosed = [line for line in output.splitlines() if "не закрыт" in line]
        self.assertEqual(len(unclosed), 1, output)
        self.assertRegex(unclosed[0], r"\b3\b",
                         "не названо, сколько строк индекса пропало")

    def test_bracketed_text_without_a_definition_is_not_a_row(self):
        """Без определения `[что-то]` - обычный текст, а не ссылка.

        Иначе чекбоксы `- [ ]` и любые квадратные скобки в прозе начали бы
        считаться строками индекса и порождать ошибки на пустом месте.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [ ] не сделано\n"
                         "- [заметка на полях] просто текст\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)


class FilesAppearInIndex(MemoryFixture):
    """L2: каждый файл памяти упомянут хотя бы в одном индексе."""

    def test_file_missing_from_index_is_error(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "zabytyi.md": "меня забыли внести в индекс\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1)
        self.assertIn("zabytyi.md", output)

    def test_orphan_points_at_the_list_file_that_is_not_an_index(self):
        """Файл-список назван по-своему: сказать это прямо, а не «агент не увидит»."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Инфра](spisok-infra.md) - список по инфраструктуре\n",
            "user.md": "факт\n",
            "spisok-infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("spisok-infra.md", output)
        self.assertIn("--index", output)

    def test_orphan_hint_does_not_fire_on_a_substring_collision(self):
        """«user.md» находится внутри «superuser.md» - подсказка бы соврала."""
        self.write({
            "MEMORY.md": "- [Супер](superuser.md) - другой файл\n",
            "superuser.md": "факт\n",
            "user.md": "меня в индексе нет вовсе\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("user.md", output)
        self.assertNotIn("встречается", output)

    def test_orphan_marker_in_frontmatter_allows_file_outside_index(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "shablon.md": "---\nname: shablon\norphan: true\n---\n\nвне индекса намеренно\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_orphan_marker_in_body_does_not_count(self):
        """Файл, который лишь ОПИСЫВАЕТ метку, не должен освобождать сам себя."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "howto.md": "Как объявить сироту - поставить в шапке:\n\n---\norphan: true\n---\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("howto.md", output)

    def test_html_comment_marker_no_longer_excuses_a_file(self):
        """Способ пометить файл ровно один - `orphan: true` в шапке.

        Комментарий `<!-- linter: orphan-ok -->` значил ровно то же самое и
        удалён: два написания одного и того же - это два прочтения. И он был
        самой дорогой строчкой функциональности во всём файле: метку в теле
        приходилось искать вне блоков кода и вне бэктиков, иначе заметка ПРО
        метку освобождала себя. На этом сгорели два круга ревью подряд.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "zametka.md": "<!-- linter: orphan-ok -->\n\nвторой способ пометки\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L2"), output)

    def test_frontmatter_marker_excuses_a_file(self):
        """Вторая половина пары: оставшийся способ обязан работать."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "zagotovka.md": "---\nname: zagotovka\norphan: true\n---\n\nчерновик\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_marker_in_the_body_does_not_excuse_the_file(self):
        """Инвариант, ради которого метку и читают только из шапки.

        Файл, ОБЪЯСНЯЮЩИЙ метку, не должен освобождать себя. Прежде это
        держалось обходом заборов и бэктиков, и каждая починка закрывала одну
        лазейку, открывая соседнюю. Теперь держится конструкцией: шапка - это
        шапка, в примере кода её не бывает.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "zametka.md": "Чтобы исключить файл, впишите в шапку "
                          "`orphan: true` - вот так:\n\n```\norphan: true\n```\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("zametka.md", output)

    def test_allow_orphan_glob_excludes_file(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "templates/zagotovka.md": "заготовка\n",
        })
        code, output = self.run_linter("--allow-orphan", "templates/*.md")
        self.assertEqual(code, 0, output)

    def test_allow_orphan_glob_is_case_sensitive(self):
        """Иначе набор исключений разъедется между Windows и Linux в CI."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "templates/zagotovka.md": "заготовка\n",
        })
        code, output = self.run_linter("--allow-orphan", "Templates/*.md")
        self.assertEqual(code, 1, output)

    def test_sub_indexes_count_by_default_without_flags(self):
        """Прибивает умолчание имени индекса.

        Хук зовёт проверку без ключей. Смени умолчание - и у всех, кто разбил
        индекс по подпапкам, начнут падать коммиты.
        """
        self.write({
            "MEMORY.md": "- [Инфра](infra/MEMORY.md) - под-индекс\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)


    def test_sub_index_rows_also_count_as_mention(self):
        """Упоминание в под-индексе закрывает L2 так же, как в корневом."""
        self.write({
            "MEMORY.md": "- [Под-индекс](infra/MEMORY.md) - инфраструктура\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)


    def test_sub_index_reachable_through_a_chain(self):
        """Достижимость транзитивна: корень -> a -> a/b. Иначе это не обход, а один шаг."""
        self.write({
            "MEMORY.md": "- [А](a/MEMORY.md) - первый уровень\n",
            "a/MEMORY.md": "- [Б](b/MEMORY.md) - второй уровень\n",
            "a/b/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "a/b/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)


    def test_detached_cycle_of_sub_indexes_is_error(self):
        """Два под-индекса, ссылающиеся друг на друга, «упомянуты» - и недостижимы."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "b/MEMORY.md": "- [В](../c/MEMORY.md) - сосед\n",
            "c/MEMORY.md": "- [Б](../b/MEMORY.md) - сосед\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)


    def test_cross_listing_the_same_file_from_two_sub_indexes_is_not_a_duplicate(self):
        """L2 разрешает упоминание в любом индексе - значит это законная схема."""
        self.write({
            "MEMORY.md": "- [Инфра](infra/MEMORY.md) - раз\n- [Прод](prod/MEMORY.md) - два\n",
            "infra/MEMORY.md": "- [Сервер](../server.md) - тот же файл\n",
            "prod/MEMORY.md": "- [Сервер](../server.md) - тот же файл, другой раздел\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)


    def test_sub_index_unreachable_from_root_is_error(self):
        """Упоминание где-нибудь - не то же самое, что достижимость от корня.

        Под-индекс, который ссылается сам на себя, формально «упомянут»,
        а на деле к нему неоткуда прийти: сам он не загружается.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "memory_infra.md": "- [Я сам](memory_infra.md) - самоссылка\n"
                               "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("memory_infra.md", output)

    def test_unreferenced_sub_index_is_error(self):
        """Под-индекс, на который никто не ссылается, невидим - и всё за ним тоже."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("infra/MEMORY.md", output)


class StrictRootIndexModel(MemoryFixture):
    """Строгая модель корневого индекса (решение owner'а 26.08).

    Корневой индекс ОДИН, с конкретным именем, в корне папки памяти.
    Под-индекс живёт в подпапке под тем же именем: memory/MEMORY.md -
    корневой, memory/infra/MEMORY.md - под-индекс. Тогда двух корневых
    не бывает по построению, и проверке не приходится гадать, какой из
    лежащих рядом файлов загрузится.

    Шестой круг ревью показал, что прежняя модель (корень выводится из
    шаблона `MEMORY*.md`) била в обе стороны: ложной тревогой на честной
    памяти и молчанием на разъехавшейся.
    """

    def test_second_index_like_file_in_root_is_a_plain_fact(self):
        """Файл рядом с корневым - обычный факт, а не под-индекс.

        Прежняя модель короновала MEMORY.md, а memory-work.md объявляла
        под-индексом, до которого неоткуда дойти, и давала код 1 на памяти,
        с которой всё в порядке. В строгой модели такой файл - обычный:
        упомянут в индексе, значит вопросов нет.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Рабочее](memory-work.md) - заметки\n",
            "memory-work.md": "рабочие заметки\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertNotIn("неоткуда дойти", output)

    def test_missing_root_index_is_unverifiable_not_success(self):
        """Молчание шестого круга: корневого нет, а проверка говорит «согласована».

        MEMORY_a.md и MEMORY_b.md подходили под шаблон, оба становились
        «корневыми», всё оказывалось достижимым - код 0. Агент при этом не
        грузит ничего: файла с ожидаемым именем в корне просто нет.
        """
        self.write({
            "MEMORY_a.md": "- [Профиль](user.md) - кто\n",
            "MEMORY_b.md": "- [Сервер](server.md) - прод\n",
            "user.md": "факт\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 2, output)
        self.assertIn("MEMORY.md", output)

    def test_index_name_is_matched_case_sensitively(self):
        """`infra/memory.md` строчными - обычный файл, а не под-индекс.

        Инвариант объявлен в коде (`find_indexes`), но не проверялся ничем:
        ни тестом, ни мутацией. А цена ровно та, ради которой в CI держат
        матрицу из двух систем: на Windows сравнение без учёта регистра
        прошло бы, и человек получил бы зелёный локальный прогон против
        красного в CI на Linux - при одинаковом наборе файлов.

        Вход различает две реализации: при сравнении без учёта регистра
        `infra/memory.md` стал бы под-индексом, его строки разобрались бы, и
        `infra/server.md` перестал бы быть сиротой.
        """
        self.write({
            "MEMORY.md": "- [Инфра](infra/memory.md) - список, но не индекс\n",
            "infra/memory.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("infra/server.md", output)

    def test_sub_index_in_subfolder_is_reachable(self):
        """Канон строгой модели: под-индекс - это подпапка/<то же имя>."""
        self.write({
            "MEMORY.md": "- [Инфра](infra/MEMORY.md) - под-индекс\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_sub_index_in_subfolder_without_a_link_is_error(self):
        """Тот же под-индекс, но ссылки на него нет - до него и правда неоткуда дойти."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("infra/MEMORY.md", output)

    def test_unreachable_sub_index_message_does_not_claim_what_loads(self):
        """Проверке никто не сообщает, что грузит харнесс - значит и утверждать нечего.

        Прежний текст говорил «а сам он не загружается». Это знание о чужой
        системе, которого у проверки нет.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        _code, output = self.run_linter()
        self.assertNotIn("не загружается", output)

    def test_index_like_name_in_root_gets_an_explanation(self):
        """Стена сирот без причины - тот самый вред, ради которого правило и меняли.

        Человек разбил индекс по-старому: MEMORY_infra.md в корне. В строгой
        модели это не под-индекс, его строки не разбираются, и перечисленные
        в нём файлы становятся сиротами. Без объяснения такой вывод читается
        как поломка проверки.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "memory_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("memory_infra.md", output)
        self.assertIn("подпапк", output)

    def test_plain_fact_with_index_like_name_does_not_trigger_the_hint(self):
        """Подсказка требует ДВУХ признаков: похожего имени и строк индекса внутри.

        Иначе обычный факт вроде memory_of_incident.md ловил бы заметку
        каждый прогон - шум, приучающий пролистывать вывод.
        """
        self.write({
            "MEMORY.md": "- [Разбор](memory_of_incident.md) - что случилось\n",
            "memory_of_incident.md": "в тот вечер сервис ответил 500\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertNotIn("подпапк", output)

    def test_custom_index_name_works(self):
        """Ключ остаётся, но принимает ИМЯ, а не шаблон."""
        self.write({
            "INDEX.md": "- [Профиль](user.md) - кто\n- [Инфра](infra/INDEX.md) - под-индекс\n",
            "infra/INDEX.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter("--index", "INDEX.md")
        self.assertEqual(code, 0, output)

    def test_glob_in_index_name_is_refused_not_silently_matched(self):
        """Старая форма ключа не должна тихо «почти работать»."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter("--index", "MEMORY*.md")
        self.assertEqual(code, 2, output)
        self.assertIn("имя", output.lower())

    def test_stray_index_hint_is_case_insensitive(self):
        """`memory_infra.md` строчными - тот же случай, что `MEMORY_infra.md`.

        Подсказка про плоскую раскладку сравнивала имя с учётом регистра и на
        строчном варианте молчала - а человек получал ту же стену сирот.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "memory_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        # Ищем именно заметку про этот файл, а не слово «подпапка» где угодно
        # в выводе. Прежняя редакция проверяла весь вывод - и умерла молча,
        # как только то же слово появилось в тексте L2-ошибки: заметку можно
        # было убрать целиком, а тест оставался зелёным. Ровно тот дешёвый
        # признак, от которого страхует мутационная проверка (она это и
        # поймала).
        notice = [line for line in output.splitlines()
                  if line.startswith("memory_infra.md:")]
        self.assertEqual(len(notice), 1,
                         "заметка про плоскую раскладку не напечатана: %s" % output)
        self.assertIn("подпапк", notice[0])


class OwnMemoryIsConsistent(unittest.TestCase):
    """Проверка обязана проходить на памяти собственного репозитория.

    До сих пор это был только шаг CI: локально, перед коммитом, инструмент на
    своей же памяти не запускался ни разу. Репозиторий выкладывается как
    образец подхода - память в нём часть примера, и разъехаться ей нельзя;
    узнавать об этом из красной галочки после push поздно и стыдно.

    Заодно это единственный тест, который гоняет проверку на НЕ синтетическом
    входе: все остальные строят память из словаря в паре строк.
    """

    def test_repository_memory_passes(self):
        folder = os.path.join(REPO_DIR, "memory")
        if not os.path.isdir(folder):
            self.skipTest("папки memory рядом нет - набор гоняется не в "
                          "репозитории инструмента")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = linter.main([folder])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())


class CiGuards(unittest.TestCase):
    """CI обязан падать, когда проверка не выполнилась, а не когда «нет тестов»."""

    def workflow(self):
        path = os.path.join(REPO_DIR, ".github", "workflows", "memory-check.yml")
        if not os.path.isfile(path):
            self.skipTest("workflow не найден - набор гоняется не в репозитории "
                          "инструмента; в нём самом этот пропуск означал бы, "
                          "что весь класс проверок CI молча исчез")
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_ci_guard_and_run_use_the_same_test_pattern(self):
        """Гард и прогон обязаны считать по одному шаблону имён.

        Прежняя редакция искала `-p 'test_*.py'` в тексте - а эта строка есть
        только в шаге «Тесты». Шаблон гарда она не смотрела вовсе, и подмена
        его на `test*.py` проходила незамеченной. Достаём оба литерала и
        сравниваем их друг с другом, а не с ожиданием.
        """
        text = self.workflow()
        in_guard = re.search(r"discover\('scripts',\s*'([^']+)'\)", text)
        in_run = re.search(r"discover -s scripts -p '([^']+)'", text)
        self.assertIsNotNone(in_guard, "не найден шаблон в гарде")
        self.assertIsNotNone(in_run, "прогон тестов не закреплён шаблоном")
        self.assertEqual(in_guard.group(1), in_run.group(1),
                         "гард считает по одному шаблону, прогон идёт по другому")


    def test_ci_threshold_matches_the_real_test_count(self):
        """Порог в гарде не должен разойтись с действительностью.

        Зашитое число ловит «discover собрал ноль», но при чистке тестов оно
        молча превращается в ложную тревогу или, наоборот, перестаёт ловить
        частичную деградацию. Пусть за этим следит тест, а не память автора.
        """
        text = self.workflow()
        match = re.search(r"n >= (\d+)", text)
        self.assertIsNotNone(match, "порог в гарде не найден")
        threshold = int(match.group(1))
        # Свой загрузчик, а не общий: у `defaultTestLoader` ключ -k из
        # командной строки уже выставлен, и при точечном запуске тест считал
        # ОТОБРАННЫЕ тесты вместо всех - «порог 150 выше реального числа 1».
        # Красное по причине, к порогу отношения не имеющей, учит пролистывать.
        actual = unittest.TestLoader().discover(SCRIPTS_DIR, "test_*.py").countTestCases()
        self.assertLessEqual(threshold, actual,
                             "порог %d выше реального числа тестов %d" % (threshold, actual))
        self.assertGreaterEqual(threshold * 2, actual,
                                "порог %d сильно отстал от %d - обновите его"
                                % (threshold, actual))

    def test_ci_guard_survives_windows_console_encoding(self):
        """У inline-скрипта нет force_utf8_output - кодировку задаёт окружение.

        Прежняя редакция искала имя переменной во всём файле и проходила,
        когда та осталась только в комментарии. Требуем её в блоке env.
        """
        text = self.workflow()
        env_block = re.search(r"\n    env:\n((?:      .*\n|\n)+)", text)
        self.assertIsNotNone(env_block, "в workflow не найден блок env")
        self.assertIn("PYTHONIOENCODING", env_block.group(1),
                      "переменная не выставлена ни для одного шага")


    def test_ci_checks_that_tests_were_actually_collected(self):
        """На Python 3.9 сломанный discover даёт зелёную галочку при нуле тестов.

        Свой возврат 5 «ни одного теста не собрано» unittest получил только в
        3.12, а в матрице есть 3.9. Тот же класс, что MEMCHECK_REQUIRE_SH: без
        гарда галочка зелёная, а проверка не выполнялась.

        Гард ЗАПУСКАЕТСЯ, а не грепается. Прежняя редакция искала слово
        `countTestCases` в тексте workflow - и оставалась зелёной, если
        `sys.exit(...)` в гарде заменить на `print(...)`: гард мёртв,
        галочка зелёная, тестов ноль. Дешёвый признак сторожил защиту от
        тихого отказа и сам был тихим отказом.
        """
        text = self.workflow()
        match = re.search(r'run: python -c "([^"]+)"', text)
        self.assertIsNotNone(match, "гард «тесты вообще собрались» не найден")

        folder = tempfile.mkdtemp(prefix="memcheck-ci-")
        self.addCleanup(shutil.rmtree, folder, True)
        os.makedirs(os.path.join(folder, "scripts"))
        result = subprocess.run(
            [sys.executable, "-c", match.group(1)], cwd=folder,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertNotEqual(
            result.returncode, 0,
            "гард не упал на пустом наборе - значит в CI ноль собранных "
            "тестов даст зелёную галочку: %s"
            % result.stdout.decode("utf-8", "replace"))


class OrphansAndWhatHidesBehindThem(MemoryFixture):
    """Сирота, недостижимый под-индекс и счёт того, что скрыто за ним.

    Класс назывался по кругу ревью, в котором эти находки всплыли.
    Имя говорило, КОГДА их заметили, а не ЧТО они держат: чтобы понять,
    какие тесты покрывают L2, приходилось читать все двести.
    """

    def test_orphan_marker_on_sub_index_does_not_legalise_what_it_lists(self):
        """Метка снимает вопрос с ОДНОГО файла, а не со всей ветки за ним.

        Строки всех найденных индексов заливались в «упомянутые» до расчёта
        достижимости. Пометив недостижимый под-индекс как намеренный, человек
        получал зелёный свет вместе с невидимой веткой памяти: сам под-индекс
        прощён, а файлы, которые он перечисляет, уже засчитаны упомянутыми.

        К этому подталкивало само сообщение: «под-индекс, до которого неоткуда
        дойти» читается как «пометь, что он такой намеренно».
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "---\norphan: true\n---\n\n- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт, которого агент не увидит\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("infra/server.md", output)

    def test_allow_orphan_on_sub_index_does_not_legalise_what_it_lists(self):
        """Тот же случай через ключ запуска, а не через метку в файле."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт, которого агент не увидит\n",
        })
        code, output = self.run_linter("--allow-orphan", "infra/MEMORY.md")
        self.assertEqual(code, 1, output)
        self.assertIn("infra/server.md", output)

    def test_whole_disconnected_branch_can_be_excluded_deliberately(self):
        """Обратная сторона: осознанно отключить ВЕТКУ целиком по-прежнему можно.

        Иначе правка выше запирала бы человека с архивным под-индексом: снять
        вопрос с самого файла он может, а с его содержимого - нет.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "archive/MEMORY.md": "- [Старое](staroe.md) - архив\n",
            "archive/staroe.md": "архивный факт\n",
        })
        code, output = self.run_linter("--allow-orphan", "archive/*.md")
        self.assertEqual(code, 0, output)

    def test_unreachable_sub_index_names_how_much_is_lost(self):
        """Сообщение должно называть масштаб, а не только сам под-индекс.

        Человек, видя одну строку про один файл, чинит одну строку. Знание
        «за ним ещё N файлов» меняет решение - и удерживает от того, чтобы
        заглушить предупреждение меткой.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - раз\n- [База](db.md) - два\n",
            "infra/server.md": "факт\n",
            "infra/db.md": "факт\n",
        })
        _code, output = self.run_linter()
        unreachable = [line for line in output.splitlines()
                       if "infra/MEMORY.md" in line and "неоткуда дойти" in line]
        self.assertEqual(len(unreachable), 1, output)
        self.assertRegex(unreachable[0], r"\b2\b",
                         "в строке про недостижимый под-индекс не назван масштаб потери")


    def test_orphan_hint_names_the_unreachable_sub_index_as_the_cause(self):
        """Подсказка обязана назвать НАСТОЯЩУЮ причину, а не «проверьте формат».

        Файл упомянут - но в под-индексе, до которого неоткуда дойти. Прежняя
        подсказка отправляла человека проверять формат строки и блоки кода, то
        есть чинить не то. Сообщение, уводящее в сторону, - тот же класс вреда,
        что и совет, ломающий чужую настройку.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "---\norphan: true\n---\n\n- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        _code, output = self.run_linter()
        hint = [line for line in output.splitlines() if "infra/server.md" in line
                and line.startswith("L2")]
        self.assertEqual(len(hint), 1, output)
        self.assertIn("infra/MEMORY.md", hint[0])
        self.assertNotIn("проверьте формат", hint[0])

    def test_count_of_lost_files_is_declined_correctly(self):
        """«1 файлов» в сообщении инструмента, который выходит в свет, - брак."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - один\n",
            "infra/server.md": "факт\n",
        })
        _code, output = self.run_linter()
        self.assertIn("скрыто 1 файл", output)
        self.assertNotIn("1 файлов", output)

    def test_hidden_count_ignores_what_is_visible_anyway(self):
        """Ссылка «см. общий индекс» из архивной ветки не прячет весь корень.

        Транзитивный подсчёт проваливался внутрь достижимого поддерева и
        засчитывал его целиком как скрытое: на реальной памяти число врало в
        двести раз. Инструмент, завышающий масштаб, толкает к неверному
        решению - ровно то, ради чего число и заводили.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Сервер](server.md) - прод\n"
                         "- [База](db.md) - данные\n",
            "user.md": "факт\n",
            "server.md": "факт\n",
            "db.md": "факт\n",
            "arch/MEMORY.md": "- [Общий](../MEMORY.md) - см. также\n"
                              "- [Своё](staroe.md) - архив\n",
            "arch/staroe.md": "архивный факт\n",
        })
        _code, output = self.run_linter()
        hidden = [line for line in output.splitlines() if "скрыто" in line]
        self.assertEqual(len(hidden), 1, output)
        self.assertIn("скрыто 1 файл", hidden[0])

    def test_hidden_count_still_walks_the_chain(self):
        """Обратная сторона: транзитивность нужна и должна сохраниться.

        Под-индекс в глубине недостижимой цепочки прячет и себя, и свои
        факты. Прямой подсчёт называл бы единицу.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "arch/MEMORY.md": "- [Глубже](nested/MEMORY.md) - под\n",
            "arch/nested/MEMORY.md": "- [Раз](a.md) - факт\n- [Два](b.md) - факт\n",
            "arch/nested/a.md": "факт\n",
            "arch/nested/b.md": "факт\n",
        })
        _code, output = self.run_linter()
        top = [line for line in output.splitlines()
               if "arch/MEMORY.md" in line and "скрыто" in line]
        self.assertEqual(len(top), 1, output)
        self.assertIn("скрыто 3 файла", top[0])

    def test_two_level_orphan_chain_does_not_launder_deep_facts(self):
        """Двойная метка не отмывает ветку: факты в глубине всё равно недостижимы."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "---\norphan: true\n---\n\n- [Глубже](nested/MEMORY.md) - под\n",
            "infra/nested/MEMORY.md": "---\norphan: true\n---\n\n- [Факт](a.md) - раз\n",
            "infra/nested/a.md": "спрятанный факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("infra/nested/a.md", output)

    def test_file_listed_in_both_reachable_and_unreachable_index(self):
        """Упоминания в достижимом индексе достаточно - недостижимый не мешает."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Общий](obshiy.md) - факт\n",
            "user.md": "факт\n",
            "obshiy.md": "факт\n",
            "infra/MEMORY.md": "- [Тот же](../obshiy.md) - и тут\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertNotIn("L2 obshiy.md", output)

    def test_mutual_link_between_sub_indexes_where_one_is_reachable(self):
        """Взаимная ссылка не создаёт остров, если один из двух достижим."""
        self.write({
            "MEMORY.md": "- [Первый](a/MEMORY.md) - под\n",
            "a/MEMORY.md": "- [Второй](../b/MEMORY.md) - сосед\n- [Факт](x.md) - раз\n",
            "a/x.md": "факт\n",
            "b/MEMORY.md": "- [Первый](../a/MEMORY.md) - сосед\n- [Факт](y.md) - два\n",
            "b/y.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_allow_orphan_accepts_backslash_paths(self):
        """На Windows человек напишет путь через обратный слэш - и ключ молчал.

        Пути внутри проверки нормализованы через прямой слэш, поэтому шаблон
        `templates\\*.md` не совпадал ни с чем, а ключ выглядел рабочим.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "templates/zagotovka.md": "заготовка\n",
        })
        code, output = self.run_linter("--allow-orphan", "templates\\*.md")
        self.assertEqual(code, 0, output)

class SourceHintPointsAtTheRightFile(MemoryFixture):
    """Подсказка «на него ссылается X» обязана называть верный файл.

    Ошибиться тут дороже, чем промолчать: человек идёт править файл,
    который ни при чём. Отсюда границы имени, отсев самоупоминания и
    осторожность при однофамильцах.
    """

    def test_mention_hint_respects_the_path_boundary(self):
        """`vendor/user.md` в чужом тексте - не упоминание нашего `user.md`.

        Замена перебора на токенизацию потеряла границу слева, которую прежний
        код проверял явно: имя сравнивалось ещё и по basename, а тот обрубает
        любой ведущий путь. Подсказка начинала указывать на посторонний файл -
        и отправляла чинить не то.
        """
        self.write({
            "MEMORY.md": "- [Индекс](other.md) - обычный файл памяти\n",
            "other.md": "Пример структуры лежит в vendor/user.md в их репозитории.\n",
            "user.md": "реальный факт, забыт в индексе\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertNotIn("ссылается other.md", output)

    def test_self_mention_does_not_hide_the_real_source(self):
        """Файл, упомянувший сам себя, не должен занимать место настоящего источника.

        Прежний перебор исключал сам файл до поиска. Новый механизм
        регистрировал первое совпадение по алфавиту - и если файл упоминал сам
        себя, полезная подсказка про настоящий источник терялась.

        Проверяем именно строку ПРО сироту: файл-список тоже сирота и даёт
        собственную ошибку, на которую легко поймать себя ложным совпадением.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "aaa_orphan.md": "Смотри также aaa_orphan.md для истории.\n",
            "zzz_spisok.md": "- пункт со ссылкой на aaa_orphan.md\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines()
                 if line.startswith("L2 aaa_orphan.md")]
        self.assertEqual(len(about), 1, output)
        self.assertIn("zzz_spisok.md", about[0])


    def test_mention_hint_does_not_confuse_md_with_mdx(self):
        """`user.mdx` - другой файл, а не наш `user.md` с хвостом."""
        self.write({
            "MEMORY.md": "- [Профиль](profil.md) - кто\n"
                         "\n"
                         "Раньше был файл user.mdx, другой формат.\n",
            "profil.md": "факт\n",
            "user.md": "забытый факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertNotIn("в тексте индекса встречается", output)

    def test_same_basename_elsewhere_is_a_real_source(self):
        """Упоминание в файле с тем же именем, но в другой папке - не самоупоминание.

        Само-исключение сравнивало кандидата с basename просматриваемого
        файла, поэтому упоминание «user.md» внутри `a/user.md` считалось
        упоминанием самим себя - и подсказка про настоящий источник пропадала.
        """
        self.write({
            "MEMORY.md": "- [Профиль](a/user.md) - кто\n",
            "a/user.md": "См. user.md в этом же разделе.\n",
            "c/user.md": "забытый факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines() if line.startswith("L2 c/user.md")]
        self.assertEqual(len(about), 1, output)
        self.assertIn("a/user.md", about[0])

    def test_a_file_mentioning_only_itself_is_not_its_own_source(self):
        """Обратная сторона: самоупоминание источником по-прежнему не считается."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "odinokiy.md": "Смотри также odinokiy.md для истории.\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines() if line.startswith("L2 odinokiy.md")]
        self.assertEqual(len(about), 1, output)
        self.assertNotIn("ссылается", about[0])

    def test_file_in_a_subfolder_citing_its_own_bare_name_is_not_its_own_source(self):
        """Тот же случай на НЕвырожденном входе - в подпапке путь и имя различаются.

        Предыдущая редакция парного теста клала файл в корень, где `rel` и
        basename совпадают: там обе реализации отсева ведут себя одинаково, и
        тест физически не мог отличить нужную проверку от мёртвой. Я на этом
        основании удалил рабочий guard - и получил сообщение «на него
        ссылается он сам» с советом переименовать сироту в индекс.

        Правило, которое отсюда следует: парный тест обязан стоять на входе,
        который РАЗЛИЧАЕТ старую и новую реализацию. Иначе и он, и мутация
        дают ложную уверенность.
        """
        self.write({
            "MEMORY.md": "- [Якорь](anchor.md) - кто\n",
            "anchor.md": "факт\n",
            "sub/odinokiy.md": "Этот файл раньше назывался просто odinokiy.md.\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines()
                 if line.startswith("L2 sub/odinokiy.md")]
        self.assertEqual(len(about), 1, output)
        self.assertNotIn("ссылается", about[0])
        self.assertNotIn("переименуйте", about[0])

    def test_bare_name_source_is_not_stated_as_fact_when_namesakes_exist(self):
        """Совпало голое имя, а файлов с таким именем несколько - это догадка.

        Словарь упоминаний общий на все файлы с одним именем, побеждает первый
        по алфавиту. Самоцитирование в `a/notes.md` занимало слот и объявлялось
        источником для `zzz_real/notes.md`, хотя настоящая ссылка лежала рядом,
        в той же папке. Утвердительное «на него ссылается» тут неправда.
        """
        self.write({
            "MEMORY.md": "- [Корень](root.md) - обычный факт\n",
            "root.md": "факт\n",
            "a/notes.md": "Этот файл notes.md сам про себя.\n",
            "zzz_real/referrer.md": "Реальная ссылка: подробности в notes.md.\n",
            "zzz_real/notes.md": "забытый факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines()
                 if line.startswith("L2 zzz_real/notes.md")]
        self.assertEqual(len(about), 1, output)
        self.assertNotIn("На него ссылается", about[0])
        self.assertIn("Возможно", about[0])

    def test_bare_name_source_is_stated_as_fact_when_it_is_unique(self):
        """Обратная сторона: один однофамилец - утверждать можно и нужно.

        Смягчать формулировку всегда значило бы обесценить подсказку там, где
        она однозначна.
        """
        self.write({
            "MEMORY.md": "- [Корень](root.md) - обычный факт\n",
            "root.md": "факт\n",
            "spisok.md": "Подробности в zabytyy.md смотри там.\n",
            "zabytyy.md": "забытый факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines()
                 if line.startswith("L2 zabytyy.md")]
        self.assertEqual(len(about), 1, output)
        self.assertIn("На него ссылается spisok.md", about[0])

class FilenameTokenBoundaries(MemoryFixture):
    """Где кончается имя файла в связном тексте.

    «user.md» внутри «superuser.md», «user.mdx» как другой файл, точка в
    конце предложения - каждая граница стоила отдельной находки, и
    каждая правка тут однажды ломала соседнюю.
    """

    def test_filename_followed_by_a_sentence_period_is_found(self):
        """`... лежит в user.md.` - точка кончает предложение, а не имя файла.

        Просмотр вперёд, добавленный ради `user.mdx`, запрещал точку после
        имени - и упоминание в обычной прозе переставало находиться вовсе.
        Связный текст с точками в конце предложений - стиль этого репозитория.
        """
        self.write({
            "MEMORY.md": "- [Список](spisok.md) - перечень\n",
            "spisok.md": "Полный текст правил лежит в user.md.\n",
            "user.md": "забытый факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines() if line.startswith("L2 user.md")]
        self.assertEqual(len(about), 1, output)
        self.assertIn("spisok.md", about[0])

    def test_filename_with_a_longer_extension_is_still_a_different_file(self):
        """Обратная сторона: `user.mdx` - другой файл, ложным совпадением быть не должен."""
        self.write({
            "MEMORY.md": "- [Список](spisok.md) - перечень\n",
            "spisok.md": "Раньше был файл user.mdx, другой формат.\n",
            "user.md": "забытый факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines() if line.startswith("L2 user.md")]
        self.assertEqual(len(about), 1, output)
        self.assertNotIn("spisok.md", about[0])

class FrontmatterDiagnostics(MemoryFixture):
    """Шапка: метка внутри неё, незакрытая шапка и что об этом сказано.

    Человек уже сделал правильное действие - поставил метку, - и ему
    надо объяснить, почему оно не засчиталось.
    """

    def test_unclosed_frontmatter_explains_why_the_marker_did_not_work(self):
        """Метка внутри незакрытой шапки не читается - об этом надо сказать.

        Человек написал `orphan: true` правильно, а получал голое обвинение
        «файл не упомянут». Причина - незакрытая `---`, и без подсказки её не
        видно: остальные сообщения инструмента объясняют механизм, это молчало.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "shablon.md": "---\nname: shablon\norphan: true\n\nтело без закрытия шапки\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines() if line.startswith("L2 shablon.md")]
        self.assertEqual(len(about), 1, output)
        self.assertIn("не закрыта", about[0])

    def test_closed_frontmatter_marker_still_works(self):
        """Обратная сторона: правильно закрытая шапка освобождает файл как прежде."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "shablon.md": "---\nname: shablon\norphan: true\n---\n\nтело\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_unclosed_frontmatter_is_explained_even_when_a_source_is_found(self):
        """Две правки одного коммита: первая сделала вторую недостижимой.

        Ветка про однофамильцев заканчивается досрочным выходом, а подсказка
        про незакрытую шапку стояла после неё. У файла, который кто-то забыл
        проиндексировать, упоминание обычно ЕСТЬ - значит подсказка не
        показывалась почти никогда, а совет уводил не туда: «переименуйте
        файл в индекс» вместо «допишите три символа».
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "shablon.md": "---\nname: shablon\norphan: true\n\nтело без закрытия\n",
            "spisok.md": "Смотри детали в shablon.md.\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines() if line.startswith("L2 shablon.md")]
        self.assertEqual(len(about), 1, output)
        self.assertIn("не закрыта", about[0])

    def test_horizontal_rule_is_not_called_an_unclosed_frontmatter(self):
        """`---` первой строкой - законная горизонтальная линейка, а не шапка.

        Обратная сторона подсказки: она заявляла факт «шапка открыта» там,
        где есть лишь совпадение по первой строке. Требуем признак YAML -
        хотя бы одну строку вида `ключ: значение`.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "zametka.md": "---\n\nЗаметка начинается с линейки.\n\nВторая мысль.\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        about = [line for line in output.splitlines() if line.startswith("L2 zametka.md")]
        self.assertEqual(len(about), 1, output)
        self.assertNotIn("не закрыта", about[0])

    def test_marker_works_anywhere_in_a_long_frontmatter(self):
        """Метка не зависит от того, какой строкой шапки записана.

        Прежде здесь проверялась метка-комментарий и её независимость от
        номера строки в теле - вместе с меткой этот вопрос не исчез, а
        переехал: наша конвенция держит в шапке и `name`, и `description`, и
        `metadata`, так что `orphan` легко оказывается не первым.

        Три соседних теста, проверявшие метку в блоке кода, в бэктиках и в
        прозе, удалены вместе с ней. Инвариант, ради которого они писались -
        файл, ОБЪЯСНЯЮЩИЙ метку, себя не освобождает, - теперь держится
        конструкцией и проверяется в FilesAppearInIndex.
        """
        head = ("---\nname: draft\n"
                + "".join("pole_%d: znachenie\n" % i for i in range(1, 19))
                + "orphan: true\n---\n")
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "draft.md": head + "\nТекст заметки.\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

class HiddenBehindTerminates(unittest.TestCase):
    """Обход скрытого обязан завершаться на любой топологии ссылок.

    Дедупликация по посещённым узлам не была покрыта ни тестом, ни мутацией:
    её снятие проходило весь набор чисто, а на цикле из трёх узлов, не
    проходящем через стартовый, обход зависал навсегда. Зависший pre-commit -
    замороженный терминал без единой строки объяснения, худший вид отказа из
    всех, что этот инструмент старается не допускать.
    """

    def test_cycle_not_through_the_start_still_terminates(self):
        sys.path.insert(0, SCRIPTS_DIR)
        import check_memory_index as linter_module

        per_index = {
            "a/MEMORY.md": [("Б", "b/MEMORY.md")],
            "b/MEMORY.md": [("В", "c/MEMORY.md")],
            "c/MEMORY.md": [("Б", "b/MEMORY.md")],
        }
        index_rels = {"a/MEMORY.md", "b/MEMORY.md", "c/MEMORY.md"}

        finished = []

        def call():
            finished.append(linter_module.hidden_behind(
                "a/MEMORY.md", per_index, index_rels, set(), set()))

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "обход не завершился - цикл не разорван")
        self.assertEqual(finished[0], {"b/MEMORY.md", "c/MEMORY.md"})


class HotPathStaysLinear(MemoryFixture):
    """Защита от возврата квадратичности - мутациями её не поймать.

    Мутационная проверка отвечает на вопрос «изменилось ли поведение», а
    квадратичная и линейная версии ведут себя одинаково: разница только в
    цене. Именно поэтому возврат перебора всех файлов на каждую сироту
    прошёл незамеченным - и дал 15 секунд на памяти в тысячу файлов, то есть
    на каждом коммите.

    Сторожей двое, и это намеренно.

    Первый считает РАБОТУ, а не время: сколько раз прогон открыл файлы и
    сколько раз строил карту упоминаний. Квадратичность - это буквально
    «карта строится на каждую сироту», и счётчик её видит независимо от
    того, на какой машине и под какой нагрузкой идёт прогон.

    Второй меряет время и оставлен сетью на случай квадратичности, которую
    счётчики не заметят (например перебор по уже прочитанному тексту). Но
    потолок ему поднят: прежние 5 секунд стояли всего вдвое выше обычного
    прогона, и на нагруженной машине набор падал по чужой причине. Тест,
    который иногда краснеет сам по себе, учит пролистывать красноту - а это
    ровно та привычка, из-за которой не заметили бы настоящую находку.
    """

    def build_memory(self):
        """500 файлов, 200 из них сироты: 300 в индексе, 200 мимо него."""
        files = {}
        rows = []
        for number in range(300):
            name = "fakt_%03d.md" % number
            files[name] = "факт %d\n" % number
            rows.append("- [Факт %d](%s) - крючок\n" % (number, name))
        for number in range(200):
            files["sirota_%03d.md" % number] = "см. fakt_001.md рядом\n"
        files["MEMORY.md"] = "".join(rows)
        self.write(files)
        return len(files)

    def test_orphan_hints_are_built_once_not_once_per_orphan(self):
        """Карта упоминаний строится один раз на прогон.

        Это и есть определение той регрессии: «для КАЖДОЙ сироты заново
        сканировались все остальные файлы». Проверка не зависит от скорости
        машины, поэтому она здесь главная, а не таймер.
        """
        total = self.build_memory()
        calls = []
        real_map = linter.map_mentions
        real_read = linter.read_text

        def counted_map(*args, **kwargs):
            calls.append("map")
            return real_map(*args, **kwargs)

        def counted_read(path):
            calls.append("read")
            return real_read(path)

        linter.map_mentions = counted_map
        linter.read_text = counted_read
        try:
            code, output = self.run_linter("--quiet")
        finally:
            linter.map_mentions = real_map
            linter.read_text = real_read

        self.assertEqual(code, 1, output)
        maps = calls.count("map")
        reads = calls.count("read")
        self.assertLessEqual(maps, 1,
                             "карта упоминаний построена %d раз - на 200 сирот "
                             "это и есть перебор всех файлов на каждую" % maps)
        self.assertLessEqual(
            reads, 2 * total,
            "%d открытий файла на %d файлов - файл читается заново вместо "
            "общего кэша" % (reads, total))

    def test_many_orphans_do_not_make_the_run_quadratic(self):
        self.build_memory()

        started = time.perf_counter()
        code, output = self.run_linter("--quiet")
        spent = time.perf_counter() - started

        self.assertEqual(code, 1, output)
        self.assertLess(spent, 20.0,
                        "500 файлов с 200 сиротами заняли %.1f с - похоже на "
                        "возврат перебора всех файлов на каждую сироту" % spent)

    def test_folder_is_walked_only_once(self):
        """Дерево обходится один раз, а не дважды на каждый коммит.

        find_indexes и build_file_map ходили по папке независимо, повторяя и
        обход, и проверку каждой подпапки на связанность. Хук зовут на каждый
        коммит - второй обход был бесплатной тратой.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Инфра](infra/MEMORY.md) - под\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "факт\n",
        })
        real_walk = os.walk
        calls = []

        def counting_walk(top, *args, **kwargs):
            calls.append(top)
            return real_walk(top, *args, **kwargs)

        with unittest.mock.patch.object(linter.os, "walk", counting_walk):
            code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(calls), 1, "дерево обошли %d раз: %s" % (len(calls), calls))


class FilenameTokenScan(unittest.TestCase):
    """Поиск имён файлов в тексте: та же граница, но линейной ценой.

    Квадратичность тут была не по числу файлов, а по СОДЕРЖИМОМУ одного:
    точка входила и в класс имени, и в обязательный хвост «.md», и на длинном
    прогоне подходящих символов движок откатывался. Достаточно было одного
    факта с base64-блобом внутри и одной сироты где угодно - и pre-commit хук
    задумывался на десятки секунд.
    """

    def test_boundaries_are_exactly_as_before(self):
        """Первая половина пары: смысл не изменился, изменилась только цена.

        Каждая строка тут - чья-то прошлая находка: «user.md» внутри
        «superuser.md», «user.mdx» как другой файл, точка в конце предложения,
        «user.md.txt» как третий файл.
        """
        cases = [
            ("лежит в user.md рядом", ["user.md"]),
            ("файл superuser.md", ["superuser.md"]),
            ("был файл user.mdx, другой формат", []),
            ("правила лежат в user.md.", ["user.md"]),
            ("это user.md.txt, не наш", []),
            ("см. vendor/user.md", ["vendor/user.md"]),
            ("путь docs\\user.md", ["docs\\user.md"]),
            ("голое .md именем не является", []),
            ("сразу два: a.md и b.md", ["a.md", "b.md"]),
        ]
        for text, expected in cases:
            self.assertEqual(linter.filename_tokens(text), expected, text)

    def test_a_long_run_of_name_characters_does_not_blow_up(self):
        """Вторая половина: 40 000 символов без «.md» - это не десятки секунд.

        Потолок тут честный, а не двукратный: прежняя редакция считала этот
        вход 18.8 с, нынешняя - сотые доли. Между ними три порядка, и от
        загрузки машины такой разрыв не зависит.
        """
        blob = "a" * 40000
        started = time.perf_counter()
        found = linter.filename_tokens(blob)
        spent = time.perf_counter() - started
        self.assertEqual(found, [])
        self.assertLess(spent, 5.0,
                        "40 000 символов заняли %.1f с - похоже на возврат "
                        "отката в поиске имени файла" % spent)


class MemoryFolderBoundary(MemoryFixture):
    """Линтер отвечает только за папку памяти и не выходит за её пределы."""

    def test_folder_without_root_index_is_usage_error(self):
        """Указали корень репозитория вместо памяти - отказ, а не разбор чужого дерева."""
        self.write({
            "sub/MEMORY.md": "- [Профиль](user.md) - кто\n",
            "sub/user.md": "факт\n",
            "README.md": "не память\n",
            "node_modules/paket/README.md": "чужое\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 2, output)
        self.assertIn("корневой индекс не найден", output.lower())


    def test_hidden_dirs_inside_memory_are_not_memory(self):
        """У тех, кто держит заметки в Obsidian, это лежит прямо в папке памяти."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            ".obsidian/templates/zagotovka.md": "шаблон редактора\n",
            ".trash/udalennoe.md": "лежит в корзине\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_sub_index_may_live_in_subfolder(self):
        """Индекс при росте бьют по каталогам - это разрешено, лишь бы корневой был на месте."""
        self.write({
            "MEMORY.md": "- [Инфра](sub/MEMORY.md) - под-индекс\n",
            "sub/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "sub/server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)


class RootIndexLost(MemoryFixture):
    """Пропавший корневой индекс - нарушение, а не невыполнимая проверка.

    Худшее, что может случиться с этой памятью: в контекст не грузится
    ничего, а строка импорта `@memory/MEMORY.md` указывает в пустоту. Пока
    случай делил код 2 с «указана не та папка», хук такой коммит пропускал:
    `git rm memory/MEMORY.md` проходил молча, а забытый черновик рядом -
    блокировался.
    """

    FACT = "---\nname: %s\ndescription: крючок\n---\n\nфакт\n"

    def test_memory_without_root_index_is_a_violation(self):
        """Файлы памяти есть, входа в них нет - агент не увидит ни одного.

        Вход намеренно держит файлы и в корне, и в подпапке: у файла из
        подпапки путь и имя не совпадают, а прежняя реализация возвращала
        код 2 независимо от содержимого - на вырожденном входе из одного
        файла в корне обе реализации были бы неразличимы по коду возврата.
        """
        self.write({
            "user.md": self.FACT % "user",
            "infra/prod.md": self.FACT % "prod",
            "infra/staging.md": self.FACT % "staging",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L2"), output)
        self.assertIn("3 файла", output)

    def test_folder_that_is_not_memory_stays_a_usage_error(self):
        """Вторая половина пары: указали не ту папку - совет прежний, код 2.

        Без этого различения проверка, направленная на корень репозитория,
        обвиняла бы человека в разъехавшейся памяти из-за README.md.
        """
        self.write({
            "README.md": "не память, обычный readme\n",
            "docs/ustanovka.md": "инструкция без шапки\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 2, output)
        self.assertIn("не найден", output.lower())

    def test_sub_indexes_without_root_are_still_a_usage_error(self):
        """Прежний случай не переехал в нарушения: файлов памяти в папке нет."""
        self.write({
            "sub/MEMORY.md": "- [Профиль](user.md) - кто\n",
            "sub/user.md": "факт без шапки\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 2, output)
        self.assertIn("корневой индекс не найден", output.lower())

    def test_losing_the_index_is_not_quieter_than_forgetting_one_file(self):
        """Ущерб и громкость должны идти в одну сторону, а шли в разные."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": self.FACT % "user",
            "zabytyj.md": self.FACT % "zabytyj",
        })
        forgotten_one, _ = self.run_linter()

        os.remove(os.path.join(self.root, "MEMORY.md"))
        lost_index, output = self.run_linter()

        self.assertEqual(forgotten_one, 1)
        self.assertEqual(lost_index, 1, output)


class AddressesAreClickable(MemoryFixture):
    """Адрес в выводе должен указывать туда, откуда проверку запустили.

    Пути внутри проверки отсчитываются от папки памяти, а читают вывод из
    каталога запуска: «L2 draft.md» указывает на файл, которого по этому
    пути нет - он лежит в `memory/draft.md`. По такой строке не прыгнет ни
    редактор, ни grep, а агент, получивший вывод как задание, не найдёт файл
    с первой попытки. Хук зовёт проверку именно так - «memory» из корня
    репозитория.
    """

    FILES = {
        "MEMORY.md": "- [Профиль](user.md) - кто\n",
        "user.md": "факт\n",
        "draft.md": "черновик\n",
    }

    def test_relative_run_prefixes_the_address(self):
        self.write(self.FILES)
        name = os.path.basename(self.root)
        previous = os.getcwd()
        os.chdir(os.path.dirname(self.root))
        self.addCleanup(os.chdir, previous)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = linter.main([name])
        output = out.getvalue() + err.getvalue()

        self.assertEqual(code, 1, output)
        self.assertIn("L2 %s/draft.md" % name, output)
        # А вот ссылка ВНУТРИ сообщения обязана остаться прежней: её впишут
        # в индекс, и отсчитывается она от него, а не от каталога запуска.
        self.assertIn("`- [Заголовок](draft.md) - крючок`", output)

    def test_prefix_does_not_touch_lines_that_start_with_a_word(self):
        """Третья сторона: приписка ставится только там, где ведущий токен - путь.

        Разбор идёт по началу строки, и без сверки с реальными путями он
        приписал бы папку к первому слову любой фразы: «Индекс пуст: ...»
        превратилось бы в «memory/Индекс пуст: ...». Проверка, коверкающая
        собственные сообщения, хуже, чем проверка с неудобными адресами.
        """
        self.write({"MEMORY.md": "Пока пусто, ни одной строки формата\n"})
        name = os.path.basename(self.root)
        previous = os.getcwd()
        os.chdir(os.path.dirname(self.root))
        self.addCleanup(os.chdir, previous)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = linter.main([name])
        output = out.getvalue() + err.getvalue()

        self.assertEqual(code, 0, output)
        self.assertIn("Индекс пуст", output)
        self.assertNotIn("%s/Индекс" % name, output)

    def test_absolute_run_leaves_addresses_bare(self):
        """Вторая половина пары: абсолютный путь в каждую строку не дублируем.

        Выигрыш тот же, а цена - сотня лишних символов в каждой строке; на
        памяти с шестью десятками находок это стена. Тот, кто дал абсолютный
        путь, и так знает, где находится.
        """
        self.write(self.FILES)
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("L2 draft.md", output)
        self.assertNotIn(self.root, output)


class LinkedSubtrees(MemoryFixture):
    """Связанные каталоги внутри памяти: junction на Windows, симлинк на POSIX."""

    def link_dir(self, target, link):
        """Пробует связать каталоги; пропускает тест, если система не даёт."""
        try:
            if os.name == "nt":
                result = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if result.returncode != 0:
                    self.skipTest("junction создать не удалось")
            else:
                os.symlink(target, link, target_is_directory=True)
        except OSError:
            self.skipTest("связывание каталогов недоступно")

    def test_linked_subtree_is_not_treated_as_memory(self):
        """Иначе вердикт зависит от системы: junction обход проходит, симлинк - нет."""
        outside = tempfile.mkdtemp(prefix="memcheck-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        with io.open(os.path.join(outside, "chuzhoe.md"), "w", encoding="utf-8") as fh:
            fh.write("не наша память\n")
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        self.link_dir(outside, os.path.join(self.root, "shared"))
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_link_into_linked_subtree_still_resolves(self):
        """Связанный каталог не память, но открыть файл в нём система может."""
        outside = tempfile.mkdtemp(prefix="memcheck-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        with io.open(os.path.join(outside, "zametka.md"), "w", encoding="utf-8") as fh:
            fh.write("факт\n")
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Через связь](shared/zametka.md) - лежит за junction\n",
            "user.md": "факт\n",
        })
        self.link_dir(outside, os.path.join(self.root, "shared"))
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)


    def test_linked_subtree_is_pruned_without_isjunction(self):
        """os.path.isjunction есть только с 3.12, а islink не видит junction с 3.8.

        Значит на 3.8-3.11 под Windows защита не работала бы вовсе - в том
        числе в ячейке CI windows-latest / 3.9.
        """
        outside = tempfile.mkdtemp(prefix="memcheck-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        with io.open(os.path.join(outside, "chuzhoe.md"), "w", encoding="utf-8") as fh:
            fh.write("не наша память\n")
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        self.link_dir(outside, os.path.join(self.root, "shared"))
        with unittest.mock.patch.object(os.path, "isjunction", None, create=True):
            code, output = self.run_linter()
        self.assertEqual(code, 0, output)


class SilentFailures(MemoryFixture):
    """Самый опасный класс: проверка говорит «всё в порядке» на разъехавшейся памяти.

    Найдено шестым кругом ревью по линзе «молчит ли». Общий признак у всех
    пяти: часть разбора не выполнилась, а вывод об этом не сказал ни слова -
    и код возврата остался нулевым.
    """

    def test_nested_fence_does_not_end_the_outer_block(self):
        """Забор из четырёх кавычек не закрывается забором из трёх.

        Приём «индекс документирует свой формат» README сам и советует:
        внешний блок берут длиннее, чтобы внутри показать обычный. Прежде
        внутренний закрывал внешний, и строка-ПРИМЕР становилась настоящей
        строкой индекса - файл, которого в индексе нет, объявлялся упомянутым.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "````markdown\n"
                         "```\n"
                         "- [Пример](primer.md) - так выглядит строка\n"
                         "```\n"
                         "````\n",
            "user.md": "факт\n",
            "primer.md": "этого файла в индексе нет\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("primer.md", output)

    def test_tilde_inside_backtick_fence_does_not_end_it(self):
        """Забор закрывается только своим символом - тильда кавычки не закрывает."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "```markdown\n"
                         "~~~\n"
                         "- [Пример](primer.md) - так выглядит строка\n"
                         "~~~\n"
                         "```\n",
            "user.md": "факт\n",
            "primer.md": "этого файла в индексе нет\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("primer.md", output)

    def test_closing_fence_may_be_longer_than_the_opening_one(self):
        """CommonMark разрешает закрывать более длинным забором - не сломать это."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "```markdown\n"
                         "- [Пример](primer.md) - строка-пример\n"
                         "`````\n"
                         "- [Сервер](server.md) - прод\n",
            "user.md": "факт\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_unreadable_subfolder_is_reported_and_unverifiable(self):
        """Поддерево, которое не открылось, уносит с собой факты - молчать нельзя.

        os.walk без onerror глотает отказ доступа: каталог с под-индексом и
        фактами просто исчезает из обхода, и проверка отчитывается «согласована».
        Сказать про такую память что-либо нельзя - это код 2, а не код 0.
        """
        real_walk = os.walk

        def fake_walk(top, onerror=None, **kw):
            for folder, dirs, names in real_walk(top, **kw):
                if "zakrytoe" in dirs:
                    dirs.remove("zakrytoe")
                    if onerror is not None:
                        err = OSError(13, "Permission denied")
                        err.filename = os.path.join(folder, "zakrytoe")
                        onerror(err)
                yield folder, dirs, names

        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "zakrytoe/tayna.md": "факт, которого проверка не увидит\n",
        })
        with unittest.mock.patch.object(linter.os, "walk", fake_walk):
            code, output = self.run_linter()
        self.assertIn("zakrytoe", output)
        self.assertEqual(code, 2, output)

    def test_orphan_marker_quoted_in_prose_does_not_free_the_file(self):
        """Заметка ПРО метку не должна освобождать сама себя.

        Метка ищется по сырым первым строкам, поэтому файл, где она приведена
        как пример внутри блока кода, молча выпадал из L2 - ровно тот файл,
        который рассказывает, как работает проверка.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "zametka.md": "Как исключить файл из индекса:\n"
                          "\n"
                          "```\n"
                          "<!-- linter: orphan-ok -->\n"
                          "```\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("zametka.md", output)

    def test_real_orphan_marker_still_frees_the_file(self):
        """Обратная сторона: настоящая метка работать не перестала."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "zagotovka.md": "---\nname: zagotovka\norphan: true\n---\n\nчерновик\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_unclosed_fence_makes_the_run_unverifiable(self):
        """Незакрытый блок - это невыполненный разбор, а не чистая память.

        Строки ниже незакрытого забора в разбор не попали. Сейчас про это
        печаталась заметка, но код оставался нулевым - то есть CI зеленел на
        индексе, который прочитан наполовину. У нечитаемого индекса такой же
        случай уже даёт код 2; здесь было непоследовательно.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "```markdown\n"
                         "- [Заголовок](primer.md) - забыли закрыть блок\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertIn("не закрыт", output.lower())
        self.assertEqual(code, 2, output)

    def test_linked_subtree_is_mentioned_not_silently_skipped(self):
        """Связанный каталог пропускается намеренно - но об этом надо сказать.

        Правило описано в README, однако человек, глядя на вывод, не может
        отличить «проверено и чисто» от «сюда даже не заходили».
        """
        outside = tempfile.mkdtemp(prefix="memcheck-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        with io.open(os.path.join(outside, "chuzhoe.md"), "w", encoding="utf-8") as fh:
            fh.write("не наша память\n")
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", os.path.join(self.root, "shared"), outside],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if result.returncode != 0:
                    self.skipTest("junction создать не удалось")
            else:
                os.symlink(outside, os.path.join(self.root, "shared"),
                           target_is_directory=True)
        except OSError:
            self.skipTest("связывание каталогов недоступно")
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertIn("shared", output)


class ExitCodeContract(MemoryFixture):
    """Код 1 - только нарушения памяти. Всё прочее обязано быть кодом 2.

    Хук трактует код 2 как «не блокирую», поэтому ошибка в обратную сторону
    молча выключает защиту для всей папки, а не для одной строки.
    """

    def test_unparseable_target_is_a_finding_not_a_crash(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Странное](//[oops) - протокол-относительная\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L1"), output)

    def test_unreadable_index_does_not_abort_the_run(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        real_read = linter.read_text
        index_path = os.path.join(self.root, "MEMORY.md")

        def failing_read(path):
            if os.path.abspath(path) == os.path.abspath(index_path):
                raise OSError(13, "нет доступа")
            return real_read(path)

        with unittest.mock.patch.object(linter, "read_text", failing_read):
            code, output = self.run_linter()
        self.assertIn("не читается", output.lower())
        # Индекс не прочитан - значит про сирот сказать НЕЧЕГО. Код 1 здесь
        # означал бы "у вас разъехалась память" из-за занятого файла, и хук
        # заблокировал бы коммит по чужой вине.
        self.assertEqual(code, 2, output)
        # Ни одного обвинения в сиротстве: проверять их было не по чему.
        self.assertNotIn("не упомянут", output)

    def test_real_violation_still_blocks_even_if_another_index_is_unreadable(self):
        """L1 по прочитанным индексам остаётся честным - молчать о нём нельзя."""
        self.write({
            "MEMORY.md": "- [Профиль](net-takogo.md) - битая ссылка\n"
                         "- [Ещё](MEMORY_extra.md) - под-индекс\n",
            "MEMORY_extra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        real_read = linter.read_text
        extra = os.path.join(self.root, "MEMORY_extra.md")

        def failing_read(path):
            if os.path.abspath(path) == os.path.abspath(extra):
                raise OSError(13, "нет доступа")
            return real_read(path)

        with unittest.mock.patch.object(linter, "read_text", failing_read):
            code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L1"), output)

    def test_internal_failure_is_exit_two_not_one(self):
        """Иначе хук скажет «память разъехалась» поверх трейсбека."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })

        def boom(*_args, **_kwargs):
            raise OSError(5, "ошибка ввода-вывода")

        with unittest.mock.patch.object(linter, "prune_non_memory_dirs", boom):
            code, output = self.run_linter()
        self.assertEqual(code, 2, output)


class UnreadableEntries(MemoryFixture):
    """Один нечитаемый файл не должен отменять весь прогон."""

    def test_dangling_symlink_does_not_abort_the_run(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        try:
            os.symlink(os.path.join(self.root, "net-takogo.md"),
                       os.path.join(self.root, "bitaya.md"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("символические ссылки недоступны")
        code, output = self.run_linter()
        self.assertNotIn("Проверку выполнить не удалось", output)
        # Битая ссылка - обычный файл вне индекса: одна находка, а не отмена
        # всего прогона из-за того, что её не удалось прочитать.
        self.assertEqual(code, 1, output)
        self.assertIn("bitaya.md", output)


class DuplicateTitles(MemoryFixture):
    """L3: одинаковый заголовок у разных файлов - предупреждение, не ошибка."""

    def test_duplicate_title_warns_but_does_not_fail(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - раз\n- [Профиль](user2.md) - два\n",
            "user.md": "факт\n",
            "user2.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertTrue(findings(output, "L3"), output)

    def test_same_row_twice_is_warned(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - раз\n- [Профиль](user.md) - тот же\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertTrue(findings(output, "L3"), output)


class WikiLinksBetweenFacts(MemoryFixture):
    """L4: [[ссылка]] из тела факта ведёт к существующей памяти.

    Форма, которую предписывает сам формат («Связанные памяти линкуем через
    [[их-name]]»), и по портфелю их почти втрое больше, чем строк индекса.
    Рвутся они чаще: строку индекса при переименовании человек правит
    сразу, а упоминания в соседних файлах умирают молча.
    """

    INDEX = "- [Профиль](user.md) - кто\n- [Правило](feedback_rule.md) - как\n"

    def test_dangling_wiki_link_is_reported(self):
        """Вход держит ссылку из ПОДПАПКИ: там путь и имя не совпадают."""
        self.write({
            "MEMORY.md": self.INDEX + "- [Инфра](infra/prod.md) - прод\n",
            "user.md": "факт\n",
            "feedback_rule.md": "правило\n",
            "infra/prod.md": "см. [[project_udalennyj]] рядом\n",
        })
        code, output = self.run_linter()
        self.assertTrue(findings(output, "L4"), output)
        self.assertIn("project_udalennyj", output)
        self.assertEqual(code, 0, output)

    def test_wiki_link_does_not_change_the_exit_code(self):
        """Вторая половина пары: L4 сигналит, но коммит не блокирует.

        Проверка приезжает в память, где такие связи уже накопились. Стань
        она блокирующей - первый же коммит отправил бы человека жать
        --no-verify, и вместе с L4 он выключил бы L1 и L2.
        """
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "см. [[net_takogo]] и [[i_takogo_net]]\n",
            "feedback_rule.md": "правило\n",
        })
        code, _output = self.run_linter()
        self.assertEqual(code, 0)

    def test_link_resolves_by_the_file_name_only(self):
        """Имя у памяти одно - имя её файла.

        Прежде ссылка засчитывалась и по полю `name` из шапки. Это был
        обработчик вместо правила: два написания на одну память, и промахнуться
        мимо обоих оказывалось легче, чем попасть. За совпадение поля с именем
        файла отвечает теперь L5, а L4 разрешает ровно одно имя.
        """
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "см. [[feedback_rule]] рядом\n",
            "feedback_rule.md": "---\nname: feedback_rule\n---\n\nправило\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertFalse(findings(output, "L4"), output)

    def test_link_by_the_name_field_no_longer_counts(self):
        """Вторая половина пары: второе написание больше не принимается.

        Ссылка по полю `name`, разошедшемуся с именем файла, - ровно тот
        случай, ради которого правило и вводилось. Тут он виден дважды: как
        битая связь L4 и как расхождение имён L5.
        """
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "см. [[pravilo-korotko]] рядом\n",
            "feedback_rule.md": "---\nname: pravilo-korotko\n---\n\nправило\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L4"), output)
        self.assertTrue(findings(output, "L5"), output)

    def test_link_with_a_caption_resolves(self):
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "см. [[feedback_rule|как надо]]\n",
            "feedback_rule.md": "правило\n",
        })
        code, output = self.run_linter()
        self.assertFalse(findings(output, "L4"), output)
        self.assertEqual(code, 0, output)

    def test_dash_for_underscore_gets_a_concrete_instruction(self):
        """Сообщение должно быть выполнимым, а не только верным.

        «Не ведёт никуда» - диагноз: по нему нельзя починить, не поискав
        руками. Дефис вместо подчёркивания - причина подавляющего
        большинства битых связей в живых памятях (52 из 98), и в этом случае
        файл называется однозначно. Тогда называем и его, и оба способа
        починки: поправить ссылку либо объявить это имя в шапке файла.
        """
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "см. [[feedback-rule]] рядом\n",
            "feedback_rule.md": "правило\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertIn("Похоже, имелся в виду feedback_rule.md", output)
        self.assertIn("[[feedback_rule]]", output)
        self.assertIn("name: feedback-rule", output)

    def test_ambiguous_spelling_gets_no_guess(self):
        """Вторая половина пары: под одно написание попали двое - молчим.

        Подсказка, которая иногда указывает не на тот файл, хуже отсутствия
        подсказки: по ней правят не тот файл и получают вторую поломку.

        Двойники различаются дефисом и подчёркиванием, а не регистром:
        регистровая пара на Windows схлопывается в один файл, и вход перестал
        бы быть неоднозначным ровно на той системе, где его гоняют чаще
        всего.
        """
        self.write({
            "MEMORY.md": "- [Раз](pravilo_odin.md) - раз\n"
                         "- [Два](pravilo-odin.md) - два\n"
                         "- [Заметка](zametka.md) - три\n",
            "pravilo_odin.md": "первое\n",
            "pravilo-odin.md": "второе\n",
            "zametka.md": "см. [[Pravilo_Odin]] рядом\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertIn("никуда не ведёт", output)
        self.assertNotIn("Похоже, имелся в виду", output)

    def test_nothing_similar_still_says_what_to_do(self):
        """Похожего нет - но действие назвать всё равно обязаны."""
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "см. [[sovsem_drugoe_slovo]] рядом\n",
            "feedback_rule.md": "правило\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertNotIn("Похоже, имелся в виду", output)
        self.assertIn("Поправьте имя в ссылке или заведите такой файл", output)

    def test_link_with_an_anchor_resolves_by_the_name_before_it(self):
        """«[[правило#раздел]]» ведёт к той же памяти, что «[[правило]]».

        Прежде якорь обрывал разбор, и такая ссылка молча не проверялась
        вовсе - ни как целая, ни как битая.
        """
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "см. [[feedback_rule#kak-nado]] и [[net_takogo#razdel]]\n",
            "feedback_rule.md": "правило\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertIn("net_takogo", output)
        self.assertNotIn("feedback_rule]]", output)

    def test_prose_in_double_brackets_is_not_a_link(self):
        """«[[в /ideas]]» - это проза, а не ссылка: имя памяти без пробелов."""
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "решение принимает [[владелец проекта]]\n",
            "feedback_rule.md": "правило\n",
        })
        code, output = self.run_linter()
        self.assertFalse(findings(output, "L4"), output)
        self.assertEqual(code, 0, output)

    def test_example_inside_a_code_fence_is_not_a_link(self):
        """Файл, объясняющий формат, не должен объявлять сам себя сломанным."""
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": "факт\n",
            "feedback_rule.md": "Связи пишутся так:\n\n```\n[[imya-drugoj-pamyati]]\n```\n",
        })
        code, output = self.run_linter()
        self.assertFalse(findings(output, "L4"), output)
        self.assertEqual(code, 0, output)

    def test_every_dangling_link_is_named(self):
        """Названы все до одной, сколько бы их ни было.

        Первая редакция резала список на десятой строке, чтобы стена
        одинаковых сообщений не читалась как поломка самой проверки. Это была
        ошибка: чинить по числу нельзя, чинят по адресу. Каждая строка -
        файл, строка в нём и имя, которого нет; смысл проверки в том, чтобы
        назвать их, а не сосчитать.

        Двадцать пять - заведомо больше любого разумного порога, поэтому вход
        различает «печатаю всё» и «печатаю первые N».
        """
        body = "".join("см. [[net_%02d]]\n" % number for number in range(25))
        self.write({
            "MEMORY.md": self.INDEX,
            "user.md": body,
            "feedback_rule.md": "правило\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count("никуда не ведёт"), 25)
        self.assertIn("net_00", output)
        self.assertIn("net_24", output)


class HintsDoNotOverclaim(MemoryFixture):
    """Подсказка обязана быть верна, иначе она хуже своего отсутствия."""

    def test_namesake_does_not_produce_a_false_format_hint(self):
        """Два файла с одним именем: подсказка говорила про чужую строку.

        Индекс упоминает `user.md` из корня. Сирота `infra/user.md` носит то
        же голое имя - и получала «имя файла в тексте индекса встречается, но
        ссылкой не разобралось». Ложно каждое слово: встречается имя ДРУГОГО
        файла, и оно прекрасно разобралось ссылкой. Заодно эта ветка глушила
        верную подсказку про источник.

        Совет README «бейте индекс на под-индексы по каталогам» ведёт ровно к
        однофамильцам, так что вход тут не выдуманный.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто я\n",
            "user.md": "факт\n",
            "infra/user.md": "другой файл с тем же именем\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("infra/user.md", output)
        self.assertNotIn("ссылкой не разобралось", output)

    CUT_INDEX = ("- [Профиль](user.md) - кто\n"
                 "\n"
                 "<!-- временно спрятал старое\n"
                 "\n"
                 "- [Правило](feedback.md) - как работать\n")

    def test_file_listed_below_a_cut_is_not_called_forgotten(self):
        """Строка про файл в индексе ЕСТЬ - её не успели разобрать.

        Прежде такой файл получал «не упомянут ни в одном индексе - агент его
        не увидит»: ложно и то и другое. Агент читает индекс как текст, и
        строка в контекст ему приезжает; не разобрала её проверка, а не он.
        А после того как в сообщение добавили действие, оно стало ещё и
        вредным - «добавьте строку в индекс» велит вписать дубль вместо того,
        чтобы закрыть комментарий. Задание, выполнив которое агент сделает
        хуже, - худший вид сообщения.

        Причину проверка знает: заметка про незакрытый комментарий печатается
        строкой выше. Здесь она связывается с конкретным файлом.

        Это заметка и код 2 - «проверить не удалось», а не «память
        разъехалась»: пока индекс дочитан наполовину, судить не из чего.
        """
        self.write({
            "MEMORY.md": self.CUT_INDEX,
            "user.md": "факт\n",
            "feedback.md": "правило\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 2, output)
        self.assertFalse(findings(output, "L2"), output)
        self.assertIn("строка про него в MEMORY.md есть", output)
        self.assertIn("не закрыт html-комментарий", output)
        self.assertNotIn("`- [Заголовок](feedback.md) - крючок`", output)

    def test_a_real_orphan_below_a_cut_is_still_reported(self):
        """Вторая половина пары: правку нельзя превращать в глушилку.

        В том же недоразобранном индексе файла `zabytyj.md` нет вовсе - ни
        выше комментария, ни ниже. Про него сказать есть что, и говорим по
        прежнему: нарушение и готовая строка для индекса.
        """
        self.write({
            "MEMORY.md": self.CUT_INDEX,
            "user.md": "факт\n",
            "feedback.md": "правило\n",
            "zabytyj.md": "меня нет в индексе совсем\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertEqual(len(findings(output, "L2")), 1, output)
        self.assertIn("zabytyj.md", findings(output, "L2")[0])
        self.assertIn("`- [Заголовок](zabytyj.md) - крючок`", output)

    def test_unreadable_file_is_not_excused_as_a_deliberate_orphan(self):
        """Нечитаемый файл - это не «лежит вне индекса намеренно».

        Ветку держал единственный тест - про битый симлинк, - и на Windows он
        пропускается: нужны привилегии на создание ссылок. То есть на машине,
        с которой инструмент разрабатывали, эту ветку не проверяло НИЧТО, а
        мутации у неё не было вовсе. Дыра, прикрытая зелёной галочкой.

        Подменяем чтение вместо того, чтобы отбирать права: вход перестаёт
        зависеть и от системы, и от привилегий, и ветка проверяется везде.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "draft.md": "черновик\n",
        })
        real_read = linter.read_text

        def refuse_one(path):
            if path.replace(os.sep, "/").endswith("/draft.md"):
                raise OSError(13, "нет доступа")
            return real_read(path)

        with unittest.mock.patch.object(linter, "read_text", refuse_one):
            code, output = self.run_linter()

        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L2"), output)
        self.assertIn("draft.md", output)

    def test_orphan_message_names_both_lawful_actions(self):
        """Самая частая находка обязана говорить, что делать.

        Ответов два: внести файл в индекс либо объявить, что он лежит вне
        него намеренно. Ни один в сообщении не назывался. Заодно это
        единственное место, где вывод произносит `orphan` - слово, которое
        связывает его с ключом --allow-orphan и с разделом README. Термин
        «сирота» в выводе не встречался ни разу, и человек, ищущий свою
        ошибку по тексту сообщения, документацию не находил.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "draft.md": "черновик\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("`- [Заголовок](draft.md) - крючок`", output)
        self.assertIn("orphan: true", output)

    def test_stranded_file_points_at_the_index_not_at_itself(self):
        """Вторая половина пары: чинить надо путь до индекса, а не файл.

        Файл за недостижимым под-индексом ни в чём не виноват, и правка его
        шапки ничего не даст. Совет обязан указывать на под-индекс, который
        его перечисляет, - вход с файлом в ПОДПАПКЕ различает эти два адреса,
        на файле в корне они совпали бы.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "infra/MEMORY.md": "- [Сервер](server.md) - прод\n",
            "infra/server.md": "сервер\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        stranded = [line for line in output.splitlines()
                    if "infra/server.md не упомянут" in line]
        self.assertEqual(len(stranded), 1, output)
        self.assertIn("`- [Заголовок](infra/MEMORY.md) - крючок`", stranded[0])
        self.assertNotIn("(infra/server.md) - крючок", stranded[0])

    def test_unique_name_still_gets_the_format_hint(self):
        """Вторая половина пары: без однофамильца подсказка обязана остаться.

        Она отвечает на «я же вижу строку своими глазами»: имя в индексе
        есть, но строкой индекса не разобралось.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "**Заготовка**: zabytyj.md - оформлено не строкой\n",
            "user.md": "факт\n",
            "zabytyj.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("ссылкой не разобралось", output)


class SummaryTellsWhatBlocks(MemoryFixture):
    """Итог отвечает на первый вопрос: чинить сейчас или можно коммитить.

    По самим строкам этого не видно. Буква перед адресом отличает нарушение
    от предупреждения, но что она значит, из вывода не следовало никак. А с
    тех пор как предупреждения перестали молчать под --quiet, в одном потоке
    идут и те и другие: шесть строк L4 при коммите человек читает как причину
    отказа, хотя они ничего не блокируют.
    """

    def test_warnings_only_end_with_a_verdict(self):
        """Память с одними предупреждениями обрывалась без единого слова.

        Итог печатался ТОЛЬКО при нарушениях: человек видел стену находок и
        не знал, чем всё кончилось. Под --quiet - как зовёт хук - тем более.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "см. [[net_takogo_fakta]] рядом\n",
        })
        code, output = self.run_linter("--quiet")
        self.assertEqual(code, 0, output)
        self.assertIn("Нарушений нет", output)
        self.assertIn("не блокируют", output)

    def test_violations_name_both_counts_and_the_legend(self):
        """Вторая половина пары: при нарушениях итог называет и те и другие.

        Вход держит нарушение И предупреждение одновременно - только на таком
        видно, что счётчики не перепутаны и что легенда объясняет обе буквы.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Битая](net.md) - файла нет\n",
            "user.md": "см. [[net_takogo_fakta]] рядом\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertTrue(findings(output, "L1"), output)
        self.assertTrue(findings(output, "L4"), output)
        self.assertIn("Нарушений: 1", output)
        self.assertIn("предупреждений: 1", output)
        self.assertIn("Блокируют коммит L1", output)

    def test_the_legend_is_not_mistaken_for_a_finding(self):
        """Легенда называет коды словами - и не должна сойти за находку.

        Находки узнаются по началу строки. Начнись легенда с «L1», и любой
        тест на отсутствие L1 краснел бы на исправной памяти, а тест на его
        наличие проходил бы, ничего не проверяя. Двадцать четыре проверки в
        этом наборе стояли ровно на такой подстроке.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Битая](net.md) - файла нет\n",
            "user.md": "факт\n",
        })
        _code, output = self.run_linter()
        self.assertEqual(len(findings(output, "L1")), 1, output)
        self.assertFalse(findings(output, "L2"), output)


class ExitCodes(MemoryFixture):
    """Код 1 - только нарушения. Всё остальное не должно им притворяться."""

    def test_missing_memory_dir_is_usage_error(self):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = linter.main([os.path.join(self.root, "net-takoi-papki")])
        self.assertEqual(code, 2, out.getvalue() + err.getvalue())

    def test_missing_index_is_usage_error(self):
        self.write({"user.md": "факт\n"})
        code, output = self.run_linter()
        self.assertEqual(code, 2, output)

    def test_quiet_is_silent_only_when_nothing_is_wrong(self):
        """Ключ гасит ровно то, что обещает справкой: строку про согласованность.

        На здоровой памяти под --quiet вывода нет вовсе - хук зовёт проверку
        именно так, и бурчать ему не на что.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter("--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "")

    def test_quiet_does_not_hide_findings(self):
        """Вторая половина пары: найденное под --quiet не прячется.

        Прежде ключ глушил и предупреждения - чтобы хук не бурчал на каждом
        коммите. Цена вскрылась на живых данных: L4 нашёл 97 битых связей в
        девяти памятях, и ни одна не попадалась человеку на глаза, потому что
        хук зовёт проверку с --quiet. Проверка, которая нашла и промолчала,
        отвечает «всё хорошо» о памяти, в которой сама же нашла поломку.

        Вход даёт предупреждение БЕЗ единой ошибки: только на таком и видно
        разницу между «молчит, когда нечего сказать» и «молчит всегда».
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - раз\n- [Профиль](user2.md) - два\n",
            "user.md": "факт\n",
            "user2.md": "см. [[net_takogo_fakta]] рядом\n",
        })
        code, output = self.run_linter("--quiet")
        self.assertEqual(code, 0, output)
        self.assertTrue(findings(output, "L3"), output)
        self.assertTrue(findings(output, "L4"), output)
        self.assertIn("net_takogo_fakta", output)

    def test_quiet_still_shows_warnings_that_explain_errors(self):
        """При блокировке причина нужна: иначе обвинения без объяснения."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "```markdown\n"
                         "- [Заголовок](primer.md) - забыли закрыть блок\n",
            "user.md": "факт\n",
            "zabytyi.md": "меня забыли внести\n",
        })
        code, output = self.run_linter("--quiet")
        self.assertEqual(code, 1, output)
        self.assertIn("не закрыт", output.lower())

    def test_quiet_still_reports_that_part_of_the_index_was_not_read(self):
        """Совет промолчать может, «я не дочитал индекс» - нет. Это тихий отказ.

        Код 2, а не 0: разбор выполнен наполовину, и утверждать, что память
        согласована, нельзя. Хук на коде 2 коммит не блокирует - он говорит
        «проверить не удалось», а не «у вас нарушения».
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "\n"
                         "```markdown\n"
                         "- [Заголовок](primer.md) - пример, блок не закрыт\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter("--quiet")
        self.assertEqual(code, 2, output)
        self.assertIn("не закрыт", output.lower())


    def test_explanation_prints_before_the_accusations(self):
        """Под списком из шести обвинений причину никто не читает."""
        self.write({
            "MEMORY.md": "```markdown\n- [Заголовок](primer.md) - блок не закрыт\n",
            "zabytyi.md": "меня забыли\n",
            "vtoroi.md": "и меня\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertLess(output.index("не закрыт"), output.index("L2"), output)

    def test_quiet_hides_success_line_but_not_errors(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter("--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "")


class ReadOnly(MemoryFixture):
    """Главное обещание инструмента: он ничего не изменяет."""

    def test_run_does_not_touch_any_file(self):
        self.write({
            "MEMORY.md": "- [Профиль](net.md) - битая\n",
            "user.md": "факт\n",
            "shablon.md": "---\norphan: true\n---\n",
        })
        before = self.snapshot()
        self.run_linter()
        self.assertEqual(before, self.snapshot())


class OutputEncoding(MemoryFixture):
    """Русские сообщения не должны ломаться при перенаправлении вывода."""

    def test_output_is_utf8_when_redirected(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
        })
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        result = subprocess.run(
            [sys.executable, SCRIPT, self.root], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.assertEqual(result.returncode, 1)
        text = result.stdout.decode("utf-8")
        self.assertIn("ссылка", text.lower())


class DocumentationMatchesReality(unittest.TestCase):
    """README - часть инструмента: неверная строка оттуда попадает людям в конфиг."""

    def readme(self):
        """README лаборатории - или пропуск, если набор гоняют не в ней.

        README велит копировать `scripts/` к себе, а эти тесты читают файл,
        который в чужой проект не едет. Прежде получался FileNotFoundError:
        человек, взявший инструмент, видел два упавших теста про чужой
        документ и делал верный вывод «эти тесты не про меня» - вместе с
        третьим, который как раз был про него (бит исполняемости хука).
        """
        path = os.path.join(REPO_DIR, "README.md")
        if not os.path.isfile(path):
            self.skipTest("README.md рядом нет - набор гоняется не в репозитории "
                          "инструмента, проверять соответствие нечему")
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_lefthook_snippet_is_a_config_not_a_bare_line(self):
        """Голая строка `sh "..."` для lefthook - не конфиг, а скаляр.

        Для husky строка верна: `.husky/pre-commit` - обычный shell-скрипт. В
        `lefthook.yml` ей нужен вид команды внутри `pre-commit: commands:`.

        Прежняя редакция искала слово `commands:` где угодно в README и
        проходила на сломанном сниппете. Проверяем структуру.
        """
        text = self.readme()
        if "lefthook" not in text:
            self.skipTest("README больше не упоминает lefthook")
        block = re.search(r"```ya?ml\n(.*?)```", text, re.S)
        self.assertIsNotNone(block, "в README нет yaml-блока для lefthook")
        self.assertRegex(
            block.group(1),
            r"pre-commit:\s*\n\s+commands:\s*\n\s+\S+:\s*\n\s+run:\s*\S",
            "сниппет lefthook не является конфигом: %s" % block.group(1))


    def test_readme_does_not_teach_the_destructive_unset(self):
        """Тот же разрушительный совет не должен переехать из хука в документацию."""
        self.assertNotIn("--unset core.hooksPath", self.readme())

    def test_section_the_hook_points_at_exists(self):
        """Хук отсылает к разделу README - раздел обязан существовать.

        Дерево решений «как отключить» переехало из вывода в документацию:
        десять строк на КАЖДОМ успешном коммите - тот самый шум, от которого
        начинают пролистывать вывод. Взамен хук печатает ссылку, а ссылка,
        ведущая в никуда, - ровно тот дефект, который эта проверка ищет в
        чужих памятях.

        Имя раздела достаём из самого хука, а не пишем здесь второй раз:
        иначе переименование в хуке разъехалось бы с тестом молча.
        """
        with io.open(HOOK, encoding="utf-8") as fh:
            hook = fh.read()
        named = re.search(r"README, раздел «([^»]+)»", hook)
        self.assertIsNotNone(named, "хук больше не ссылается на раздел README")
        heading = "## " + named.group(1)
        readme = self.readme()
        self.assertTrue(
            any(line.strip().lstrip("#").strip() == named.group(1)
                for line in readme.splitlines() if line.lstrip().startswith("#")),
            "в README нет раздела «%s», на который ссылается хук (искали %r)"
            % (named.group(1), heading))



class PreCommitHook(unittest.TestCase):
    """Хук - это то, что трогает посторонний человек. Здесь ошибаться дороже всего."""

    def setUp(self):
        self.sh = find_sh()
        if not (self.sh and shutil.which("git")):
            # Провал здесь размножился бы на весь класс - девятнадцать
            # одинаковых строк вместо одной внятной. Требование проверяет
            # отдельный тест ниже, а здесь просто пропускаем.
            self.skipTest(MISSING_SH_MESSAGE)
        self.repo = tempfile.mkdtemp(prefix="memcheck-repo-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        os.makedirs(os.path.join(self.repo, "scripts"))
        os.makedirs(os.path.join(self.repo, ".githooks"))
        os.makedirs(os.path.join(self.repo, "memory"))
        shutil.copy(SCRIPT, os.path.join(self.repo, "scripts"))
        shutil.copy(HOOK, os.path.join(self.repo, ".githooks"))
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.write_memory("- [Профиль](user.md) - кто\n", {"user.md": "факт\n"})

    def write_memory(self, index, files=None):
        with io.open(os.path.join(self.repo, "memory", "MEMORY.md"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(index)
        for name, body in (files or {}).items():
            with io.open(os.path.join(self.repo, "memory", name), "w",
                         encoding="utf-8", newline="\n") as fh:
                fh.write(body)

    def git(self, *args):
        return subprocess.run(["git"] + list(args), cwd=self.repo,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def run_hook(self, env=None):
        return subprocess.run(
            [self.sh, os.path.join(self.repo, ".githooks", "pre-commit")],
            cwd=self.repo, env=env or os.environ.copy(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    def test_untracked_draft_does_not_block_a_healthy_commit(self):
        """Черновик, которого в коммите нет, блокировал коммит, в котором всё цело.

        Git кладёт в коммит только добавленное, а проверка ходит по диску -
        это две разные кучи файлов. Недописанный черновик в папке памяти -
        нормальное рабочее состояние, а человек получал «файл не упомянут в
        индексе» и блокировку за то, чего в коммите нет. Дважды получив её,
        он заводит привычку к --no-verify, и вместе с ней выключает L1 и L2.

        Черновик лежит в ПОДПАПКЕ намеренно: ключ ждёт путь относительно
        папки памяти, а git отдаёт его от корня репозитория. На файле в
        корне памяти обе реализации - со срезом префикса и без - дают один
        и тот же результат, и тест ничего бы не гарантировал.
        """
        self.write_memory("- [Профиль](user.md) - кто\n- [Второй](two.md) - крючок\n",
                          {"two.md": "второй факт\n"})
        os.makedirs(os.path.join(self.repo, "memory", "chernoviki"))
        with io.open(os.path.join(self.repo, "memory", "chernoviki", "draft.md"),
                     "w", encoding="utf-8", newline="\n") as fh:
            fh.write("ещё не дописано\n")
        self.git("add", "memory/MEMORY.md", "memory/user.md", "memory/two.md")

        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertNotIn("draft.md", text)

    def test_staged_file_outside_the_index_still_blocks(self):
        """Вторая половина пары: то, что уезжает в коммит, спрашивается как прежде.

        Иначе правка выше превратилась бы в дыру: достаточно было бы не
        упоминать файл в индексе, чтобы проверка молчала.
        """
        self.write_memory("- [Профиль](user.md) - кто\n",
                          {"zabytyj.md": "факт мимо индекса\n"})
        self.git("add", "-A")

        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertIn("zabytyj.md", text)

    def test_tracked_file_is_not_excused_by_being_uncommitted(self):
        """Отслеживаемый файл под исключение не попадает, даже если правки не сохранены.

        Исключение узкое - только то, что в репозиторий не поедет вовсе.
        Спутать «git о нём не знает» с «изменения ещё не закоммичены» значило
        бы отпустить половину памяти.
        """
        self.write_memory("- [Профиль](user.md) - кто\n- [Второй](two.md) - крючок\n",
                          {"two.md": "второй факт\n"})
        self.git("add", "-A")
        self.git("-c", "user.email=t@e.st", "-c", "user.name=test",
                 "commit", "-qm", "init")
        # Файл отслеживается, но из индекса его убрали - строка на него остаётся.
        self.write_memory("- [Профиль](user.md) - кто\n- [Второй](two.md) - крючок\n")
        os.remove(os.path.join(self.repo, "memory", "two.md"))
        self.git("add", "-A")

        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertTrue(findings(text, "L1"), text)

    def write_config(self, name, body):
        """Кладёт конфиг агента (CLAUDE.md и подобные) в репозиторий."""
        path = os.path.join(self.repo, name)
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)

    def test_missing_import_line_is_reported(self):
        """Без строки импорта агент не увидит НИ ОДНОГО факта.

        Она одна затаскивает индекс в контекст. Нет её - папка на месте, всё
        внутри согласовано, а для агента памяти просто не существует. Любая
        другая поломка стоит одного факта или ветки, эта - всех сразу, и до
        сих пор её не проверял никто: `grep CLAUDE` по линтеру давал ноль.

        Предупреждение, а не блокировка: grep подтверждает, что строка
        написана, но не то, что она сработала.
        """
        self.write_config("CLAUDE.md", "# Проект\n\nОбычные инструкции.\n")
        self.git("add", "-A")

        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertIn("@memory/MEMORY.md", text)

    def test_import_line_in_a_nested_config_is_accepted(self):
        """Вторая половина пары: строка есть - и хук молчит.

        Вход подобран так, чтобы различать реализации. В корне лежит конфиг
        БЕЗ строки, а строка - в `.claude/CLAUDE.md`, с путём на уровень выше
        (`@../memory/MEMORY.md`); это законная и частая раскладка, README про
        неё пишет отдельно.

        Реализация, смотрящая только в корень, увидит там конфиг без строки и
        выдаст предупреждение - тест покраснеет. Если положить строку в
        корневой конфиг, обе реализации молчали бы одинаково, и тест не
        гарантировал бы ничего (проверено: на такой фикстуре он проходил и с
        урезанным списком мест).
        """
        self.write_config("CLAUDE.md", "# Проект\n\nСтроки импорта тут нет.\n")
        self.write_config(".claude/CLAUDE.md", "# Проект\n\n@../memory/MEMORY.md\n")
        self.git("add", "-A")

        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertNotIn("НИ ОДНОГО факта", text)

    def test_no_agent_config_at_all_stays_silent(self):
        """Конфига нет вовсе - судить не по чему, молчим.

        Иначе предупреждение печаталось бы в любом репозитории, который
        просто держит папку `memory/` под git, - в том числе в самой
        лаборатории, где лежит только `CLAUDE.example.md`. Ругаемся лишь
        когда конфиг ЕСТЬ, а строки в нём нет: это забытый шаг установки, а
        не догадка.
        """
        self.git("add", "-A")

        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertNotIn("НИ ОДНОГО факта", text)

    def test_custom_index_name_does_not_trigger_a_false_warning(self):
        """Своё имя индекса не должно оборачиваться ложной тревогой.

        Хук обязан искать в конфиге то же имя, что задано проверке ключом
        --index, иначе человек со своим `INDEX.md` получал бы предупреждение
        на исправной настройке - а ложная тревога учит жать --no-verify.
        """
        os.rename(os.path.join(self.repo, "memory", "MEMORY.md"),
                  os.path.join(self.repo, "memory", "INDEX.md"))
        self.git("config", "memorycheck.args", "--index INDEX.md")
        self.write_config("CLAUDE.md", "# Проект\n\n@memory/INDEX.md\n")
        self.git("add", "-A")

        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertNotIn("НИ ОДНОГО факта", text)

    def fake_python3(self, body):
        """Кладёт в PATH подставной python3 и возвращает окружение с ним."""
        folder = tempfile.mkdtemp(prefix="memcheck-bin-")
        self.addCleanup(shutil.rmtree, folder, True)
        stub = os.path.join(folder, "python3")
        with io.open(stub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.chmod(stub, 0o755)
        return dict(os.environ, PATH=folder + os.pathsep + os.environ.get("PATH", ""))

    def test_blocks_commit_when_index_is_broken(self):
        """Смысл хука. Без этого теста он мог бы вообще ничего не делать."""
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertTrue(findings(text, "L1"), text)

    def test_passes_on_consistent_memory(self):
        self.git("add", "-A")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))

    def test_skipping_branch_does_not_lecture_on_every_commit(self):
        """Пропустили коммит - значит человек не заперт, и лекция ему не нужна.

        Измерено на живом репозитории: успешный коммит без Питона печатал
        ДВЕНАДЦАТЬ строк, из них десять - дерево решений «как отключить», и
        так на каждом коммите, пока Питон не появится. Это ровно тот шум, про
        который проверка сама предупреждает: он приучает пролистывать вывод,
        а оттуда рукой подать до --no-verify.

        Три строки диагноза и ссылка на README остаются - вместе с
        предупреждением про ключ core.hooksPath, которое и есть главное, что
        нельзя потерять при сокращении. Полное дерево остаётся в блокирующей
        ветке: там человек заперт, и выход ему нужен немедленно (за этим
        следит test_unusual_exit_code_is_not_called_a_memory_problem).
        """
        self.write_memory("- [Профиль](user.md) - кто\n", {"user.md": "факт\n"})
        self.git("add", "-A")
        # Заглушки на ВСЕ три имени: хук пробует python3, python и py, и с
        # подменённым только первым он нашёл бы настоящий Питон, отработал
        # начисто и напечатал пусто - тест проходил бы, ничего не проверяя.
        folder = tempfile.mkdtemp(prefix="memcheck-bin-")
        self.addCleanup(shutil.rmtree, folder, True)
        body = '#!/bin/sh\ncase "$*" in *version_info*) exit 1 ;; *) exit 0 ;; esac\n'
        for name in ("python3", "python", "py"):
            stub = os.path.join(folder, name)
            with io.open(stub, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            os.chmod(stub, 0o755)
        env = dict(os.environ,
                   PATH=folder + os.pathsep + os.environ.get("PATH", ""))
        result = self.run_hook(env)
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        printed = [line for line in text.splitlines() if "pre-commit:" in line]
        self.assertLessEqual(len(printed), 6,
                             "на пропущенном коммите снова лекция:\n%s" % text)
        self.assertIn("core.hooksPath", text)
        self.assertNotIn("--show-origin", text)

    def test_ignores_fake_python3_stub(self):
        """python3 на Windows часто заглушка Microsoft Store, а не Питон.

        Память намеренно битая: код 0 вернула бы и ветка «подходящий Питон
        не найден вовсе», поэтому проверяем, что настоящий реально отработал.
        """
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        env = self.fake_python3("#!/bin/sh\necho Python\nexit 49\n")
        result = self.run_hook(env)
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertTrue(findings(text, "L1"), text)

    def test_rejects_python_below_required_version(self):
        """Самозванец не только заглушка: подойдёт и Питон старее нашей планки.

        Подделываем Питон старее нашей планки - такие до сих пор живут на
        старых LTS-системах. Проба обязана спрашивать версию, а не факт
        запуска: «это вообще Питон?» такой кандидат проходит, а версию - нет.
        """
        with io.open(HOOK, encoding="utf-8") as fh:
            self.assertIn("(3, 9)", fh.read(),
                          "планка версии в хуке разошлась с объявленной")
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        env = self.fake_python3(
            '#!/bin/sh\ncase "$*" in *version_info*) exit 1 ;; *) exit 0 ;; esac\n'
        )
        result = self.run_hook(env)
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertTrue(findings(text, "L1"), text)

    def test_hook_is_committed_executable(self):
        """Неисполняемый хук git пропускает молча - и это уже случалось.

        Три исхода, и различать их обязательно. README велит копировать
        scripts/ и .githooks/ к себе - значит набор будут гонять и там, где
        git-репозитория нет. Прежняя редакция звала git без перехвата и
        падала трейсбеком CalledProcessError: первое, что видел взявший
        инструмент, - «сломанные тесты», хотя сломано ничего. Режим файла
        хранит индекс git, поэтому вне репозитория проверять нечем.

        А файл, лежащий в репозитории, но не добавленный в индекс, - это не
        пропуск: такой хук не уедет ни в один клон. Прежняя редакция и здесь
        молчала по-своему - ls-files отдаёт пустую строку, и человек получал
        assertTrue с пустым сообщением. Говорим, что делать: ровно те две
        команды, что стоят в README.
        """
        if not shutil.which("git"):
            self.skipTest("нужен git")
        try:
            listing = subprocess.check_output(
                ["git", "ls-files", "-s", ".githooks/pre-commit"],
                cwd=REPO_DIR, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, OSError):
            self.skipTest("не git-репозиторий - режим хука хранит индекс git, "
                          "проверять нечем")
        if not listing.strip():
            self.fail("хук не добавлен в индекс git - в клон он не уедет вовсе: "
                      "git add .githooks/pre-commit && "
                      "git update-index --chmod=+x .githooks/pre-commit")
        self.assertTrue(listing.startswith(b"100755"),
                        "хук лежит в индексе неисполняемым, а такой хук git "
                        "пропускает МОЛЧА: git update-index --chmod=+x "
                        ".githooks/pre-commit (сейчас: %s)"
                        % listing.decode("utf-8", "replace").strip())

    def test_blocks_when_memory_file_is_deleted(self):
        """Удаление файла - самый частый способ осиротить строку индекса."""
        self.git("add", "-A")
        self.git("-c", "user.email=t@e.st", "-c", "user.name=test", "commit", "-qm", "init")
        self.git("rm", "-q", "memory/user.md")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertTrue(findings(text, "L1"), text)

    def test_skips_during_rebase_but_says_so_out_loud(self):
        """Маркеры rebase залипают - молчаливый пропуск выключил бы хук насовсем."""
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        os.makedirs(os.path.join(self.repo, ".git", "rebase-apply"))
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertIn("rebase", text.lower())

    def test_does_not_skip_during_merge_conflict(self):
        """Конфликтный merge завершается обычным commit - там --no-verify доступен."""
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        with io.open(os.path.join(self.repo, ".git", "MERGE_HEAD"), "w",
                     encoding="utf-8") as fh:
            fh.write("0" * 40 + "\n")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertTrue(findings(text, "L1"), text)

    def test_skips_when_nothing_staged_under_memory(self):
        """Незастейдженная правка памяти не должна блокировать посторонний коммит."""
        self.write_memory("- [Профиль](net.md) - битая, но не в коммите\n")
        with io.open(os.path.join(self.repo, "unrelated.txt"), "w", encoding="utf-8") as fh:
            fh.write("не про память\n")
        self.git("add", "unrelated.txt")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))

    def test_extra_args_can_be_supplied_via_git_config(self):
        """Иначе --allow-orphan из README у пользователя хука не работает вовсе."""
        os.makedirs(os.path.join(self.repo, "memory", "templates"))
        with io.open(os.path.join(self.repo, "memory", "templates", "zagotovka.md"),
                     "w", encoding="utf-8", newline="\n") as fh:
            fh.write("заготовка\n")
        self.git("config", "memorycheck.args", "--allow-orphan templates/*.md")
        self.git("add", "-A")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))

    def test_extra_args_are_not_glob_expanded_against_the_repo(self):
        """Разбиение на аргументы нужно, раскрытие шаблонов - нет.

        Хук работает из корня репозитория. Если там окажется свой templates/,
        шаблон из настройки подменится на найденные пути: при двух файлах
        проверка упадёт на неизвестный аргумент (и хук пропустит коммит
        насовсем), при одном - исключение перестанет совпадать, и человека
        заблокирует ровно тот файл, который он исключил.
        """
        os.makedirs(os.path.join(self.repo, "templates"))
        for name in ("a.md", "b.md"):
            with io.open(os.path.join(self.repo, "templates", name), "w",
                         encoding="utf-8", newline="\n") as fh:
                fh.write("постороннее\n")
        os.makedirs(os.path.join(self.repo, "memory", "templates"))
        with io.open(os.path.join(self.repo, "memory", "templates", "zagotovka.md"),
                     "w", encoding="utf-8", newline="\n") as fh:
            fh.write("заготовка\n")
        self.git("config", "memorycheck.args", "--allow-orphan templates/*.md")
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertNotIn("unrecognized arguments", text)
        self.assertEqual(result.returncode, 0, text)

    def test_does_not_skip_during_cherry_pick(self):
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        with io.open(os.path.join(self.repo, ".git", "CHERRY_PICK_HEAD"), "w",
                     encoding="utf-8") as fh:
            fh.write("0" * 40 + "\n")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertIn("cherry-pick", text.lower())
        # --quit бросает очередь: оставшиеся коммиты диапазона пропадают молча.
        # Подсказка, которая уничтожает работу, хуже отсутствия подсказки.
        self.assertNotIn("--quit", text)
        self.assertIn("--continue", text)

    def test_does_not_skip_during_revert(self):
        """У revert свой --continue - советовать ему cherry-pick нельзя."""
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        with io.open(os.path.join(self.repo, ".git", "REVERT_HEAD"), "w",
                     encoding="utf-8") as fh:
            fh.write("0" * 40 + "\n")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertIn("git revert --continue", text)
        self.assertNotIn("cherry-pick", text)

    def test_skips_during_interactive_rebase(self):
        """rebase-merge - маркер интерактивного rebase, самого частого из всех."""
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        os.makedirs(os.path.join(self.repo, ".git", "rebase-merge"))
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))

    def test_committed_hook_has_lf_line_endings(self):
        """CRLF в хуке ломает его на Linux и в CI - о режиме мы помним, о байтах нет."""
        try:
            blob = subprocess.check_output(
                ["git", "show", "HEAD:.githooks/pre-commit"], cwd=REPO_DIR,
                stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, OSError):
            self.skipTest("хук не найден в истории (свежий репозиторий или архив)")
        self.assertNotIn(b"\r\n", blob)

    def stub_checker(self, body):
        """Подменяет проверку заглушкой с нужным кодом возврата."""
        with io.open(os.path.join(self.repo, "scripts", "check_memory_index.py"),
                     "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)

    def test_unusual_exit_code_is_not_called_a_memory_problem(self):
        """126/127/130 - проверка не отработала. Говорить «память разъехалась» - врать."""
        self.stub_checker("import sys\nsys.exit(130)\n")
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 130, text)
        self.assertIn("НЕ проверена", text)
        self.assertNotIn("разошлись", text)
        # Блокируем - значит обязаны сказать, как обойти.
        self.assertIn("--no-verify", text)
        # И как отключить совсем - в ВЫВОДЕ, а не только в исходнике хука.
        # Три соседних теста читают текст файла и грепают строки: убери все
        # вызовы advise_how_to_disable, оставив саму функцию, - и они
        # останутся зелёными, а человек с husky, которого хук заблокировал,
        # не получит ни строки о том, как выйти.
        self.assertIn("--show-origin", text)

    def test_broken_config_args_no_longer_switch_blocking_off(self):
        """Опечатка в ключах выключала блокировку насовсем и молча.

        Неизвестный ключ - код 2, а на коде 2 хук не блокирует: файл-сирота
        уезжал в коммит с нулём. Настройку ставят один раз и забывают, а
        подсказку, которая печатается на каждом коммите, перестают читать.
        Тихий отказ ровно того вида, ради которого проверка и написана.

        Теперь при коде 2 с непустыми ключами прогон повторяется БЕЗ них, и
        если без них ответ определённый - берётся он. Заглушка отвечает
        по-разному на два набора аргументов, иначе вход не различал бы
        старую реализацию и новую.
        """
        self.stub_checker(
            "import sys\n"
            "extra = [a for a in sys.argv[1:] if a not in ('memory', '--quiet')]\n"
            "sys.exit(2 if extra else 1)\n")
        self.git("config", "memorycheck.args", "--alow-orphan opechatka")
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertIn("memorycheck.args", text)

    def test_genuine_usage_error_does_not_blame_the_config(self):
        """Вторая половина пары: ключи ни при чём - не блокируем и не обвиняем их.

        Прежняя редакция валила вину на настройку при любом коде 2, в том
        числе когда проверка не смогла отработать по своей причине.
        """
        self.stub_checker("import sys\nsys.exit(2)\n")
        self.git("config", "memorycheck.args", "--allow-orphan templates/*.md")
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertNotIn("memorycheck.args", text)

    def test_does_not_block_when_checker_is_missing(self):
        os.remove(os.path.join(self.repo, "scripts", "check_memory_index.py"))
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertIn("check_memory_index.py", text)

    def test_usage_error_does_not_block_commit(self):
        """Ненастроенная проверка - не повод рушить чужую работу."""
        os.remove(os.path.join(self.repo, "memory", "MEMORY.md"))
        self.git("add", "-A")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))

    def printed_lines(self):
        """Строки, которые хук реально печатает человеку.

        Не весь файл: команда, оставшаяся только в комментарии, человеку не
        показывается - а тест, ищущий подстроку по всему файлу, этого не
        замечает. Второй круг уже поймал один такой тест-пустышку.
        """
        with io.open(HOOK, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        return [line for line in lines
                if not line.lstrip().startswith("#") and "pre-commit:" in line]

    def test_hook_never_advises_unsetting_hooks_path(self):
        """Совет, который сносит чужие хуки молча, не должен звучать пользователю.

        `git config --unset core.hooksPath` печатался в трёх ветках. У человека
        с husky в этом ключе лежит `.husky/_`, и команда убирает не наш хук, а
        ВЕСЬ его набор - ту самую интеграцию, которую README и предлагает.

        Первая редакция теста искала строки, начинающиеся с `echo`. Потом вся
        печать переехала на say() - и тест стал проходить всегда, при любом
        содержимом хука: строк на `echo` в нём просто не осталось. Поэтому
        теперь смотрим на все НЕ комментарии (механизм печати может смениться
        снова) и отдельно убеждаемся, что предмет проверки вообще на месте.
        """
        with io.open(HOOK, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        meaningful = [line for line in lines if not line.lstrip().startswith("#")]
        self.assertTrue(
            any("pre-commit:" in line for line in meaningful),
            "в хуке не нашлось ни одной строки, печатаемой пользователю - "
            "проверка потеряла предмет и больше ничего не гарантирует")
        offenders = [line for line in meaningful if "--unset core.hooksPath" in line]
        self.assertEqual(offenders, [], "хук печатает разрушительный совет")


    def test_hook_tells_how_to_find_what_owns_hooks_path(self):
        """Отключать вслепую нечего - сначала надо увидеть, чем хук подключён.

        Прежняя редакция искала `--show-origin` во всём файле и проходила,
        даже когда флаг остался только в комментарии, а из живой команды
        исчез. Смотрим на строку, которую человек реально увидит.
        """
        printed = self.printed_lines()
        origin = [line for line in printed if "core.hooksPath" in line]
        self.assertTrue(origin, "хук не показывает, чем он подключён")
        self.assertTrue(
            any("--show-origin" in line for line in origin),
            "команда диагностики не показывает происхождение ключа: %s" % origin)


    def test_disable_advice_covers_the_empty_answer(self):
        """Диагностика обязана предусмотреть, что ключ не задан вовсе.

        lefthook не использует core.hooksPath - он кладёт хуки прямо в
        .git/hooks. У человека, подключившего нас так, как советует README,
        `--show-origin --get core.hooksPath` не выведет НИЧЕГО.

        Прежняя редакция искала слово «lefthook» во всём файле и проходила,
        даже если содержательная ветка исчезала, а слово оставалось в общей
        фразе. Требуем именно разбор случая «ничего не вывелось».
        """
        printed = self.printed_lines()
        empty_case = [line for line in printed
                      if "не вывела" in line or "не вывело" in line
                      or "ничего не" in line]
        self.assertTrue(
            empty_case,
            "совет не разбирает случай, когда ключ не задан вовсе: %s" % printed)
        self.assertTrue(
            any("lefthook" in line.lower() for line in printed),
            "не назван самый частый источник пустого ответа")


    def test_blocking_branch_offers_bypass_and_no_destructive_command(self):
        """Ветка, которая блокирует коммит, обязана дать выход - но безопасный."""
        self.stub_checker("import sys\nsys.exit(130)\n")
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertIn("--no-verify", text)
        self.assertNotIn("--unset core.hooksPath", text)

    def test_missing_checker_branch_gives_no_destructive_command(self):
        """Ветка «проверки нет на месте» тоже советовала снести чужие хуки."""
        os.remove(os.path.join(self.repo, "scripts", "check_memory_index.py"))
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertNotIn("--unset core.hooksPath", text)

    def test_hook_does_not_block_when_stderr_is_closed(self):
        """Закрытый stderr не должен превращать «пропускаю» в «блокирую молча».

        set -e плюс echo, вернувший ненулевой код, обрывает скрипт ДО exit 0.
        Человека запирает ровно та ветка, которая обязана его выпустить, и без
        единой строки объяснения. Закрытый stderr - не экзотика: так ведут себя
        графические клиенты git и headless-обёртки.

        Дескриптор именно ЗАКРЫВАЕМ (exec 2>&-), а не отправляем в /dev/null:
        в /dev/null запись удаётся, и дефект не воспроизводится.

        Дефект пред-существующий - воспроизводится и на версии до этой ветки.
        """
        os.remove(os.path.join(self.repo, "scripts", "check_memory_index.py"))
        self.git("add", "-A")
        hook = os.path.join(self.repo, ".githooks", "pre-commit").replace("\\", "/")
        result = subprocess.run(
            [self.sh, "-c", 'exec 2>&-; "$0" "$1"', self.sh, hook],
            cwd=self.repo, env=os.environ.copy(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0,
                         "хук заблокировал коммит из-за неудачной печати подсказки")


    def test_failure_message_mentions_the_escape_hatch(self):
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        result = self.run_hook()
        self.assertIn("--no-verify", result.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
