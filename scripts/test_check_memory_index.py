#!/usr/bin/env python3
"""Тесты линтера согласованности памяти.

Запуск из корня репозитория:

    python -m unittest discover -s scripts -v

Зависимостей нет, только стандартная библиотека.
"""

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
        self.assertIn("L1", output)

    def test_case_mismatch_is_error_even_on_case_insensitive_fs(self):
        """Windows такую ссылку проглотит, Linux в CI - нет. Ловим на обеих."""
        self.write({
            "MEMORY.md": "- [Профиль](User.md) - кто пользователь\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("регистр", output.lower())

    def test_case_mismatch_reports_exactly_one_error(self):
        """Один дефект - одна строка. Файл не должен всплыть ещё и как сирота."""
        self.write({
            "MEMORY.md": "- [Профиль](User.md) - кто пользователь\n",
            "user.md": "факт\n",
        })
        _code, output = self.run_linter()
        self.assertNotIn("L2", output)
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

    def test_percent_encoded_name_resolves(self):
        self.write({
            "MEMORY.md": "- [Профиль](ok%20space.md) - пробел в имени\n",
            "ok space.md": "факт\n",
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

    def test_link_title_in_quotes_is_stripped(self):
        """[X](file.md \"подсказка\") - легальный markdown, не битая ссылка."""
        self.write({
            "MEMORY.md": '- [Профиль](user.md "Профиль пользователя") - кто\n',
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_angle_bracket_destination_is_stripped(self):
        self.write({
            "MEMORY.md": "- [Профиль](<ok space.md>) - угловые скобки\n",
            "ok space.md": "факт\n",
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
        self.assertIn("L1", output)

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
        self.assertIn("L1", output)

    def test_unc_path_is_a_finding_not_a_crash(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n"
                         "- [Сетевая шара](//server/share/file.md) - UNC\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("L1", output)

    def test_file_named_like_parent_dir_is_not_mistaken_for_escape(self):
        self.write({
            "MEMORY.md": "- [Странное имя](..dotdot.md) - файл, а не выход наверх\n",
            "..dotdot.md": "факт\n",
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
        self.assertIn("L1", output)

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
        """При кривом корневом и рабочем под-индексе иначе не понять, где чинить."""
        self.write({
            "MEMORY.md": "| Заголовок | Файл |\n|---|---|\n| Инфра | MEMORY_infra.md |\n",
            "MEMORY_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        _code, output = self.run_linter()
        self.assertIn("MEMORY.md:", output)

    def test_bom_and_crlf_index_is_read(self):
        self.write({"MEMORY.md": "- [Профиль](user.md) - кто\n"},
                   encoding="utf-8-sig", newline="\r\n")
        self.write({"user.md": "факт\n"})
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

    def test_html_comment_marker_allows_file_outside_index(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто пользователь\n",
            "user.md": "факт\n",
            "zametka.md": "<!-- linter: orphan-ok -->\n\nвторой способ пометки\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

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
            "MEMORY_infra.md": "- [Я сам](MEMORY_infra.md) - самоссылка\n"
                               "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("MEMORY_infra.md", output)

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

        Прежняя модель короновала MEMORY.md, а MEMORY-work.md объявляла
        под-индексом, до которого неоткуда дойти, и давала код 1 на памяти,
        с которой всё в порядке. В строгой модели такой файл - обычный:
        упомянут в индексе, значит вопросов нет.
        """
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n- [Рабочее](MEMORY-work.md) - заметки\n",
            "MEMORY-work.md": "рабочие заметки\n",
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
            "MEMORY_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("MEMORY_infra.md", output)
        self.assertIn("подпапк", output)

    def test_plain_fact_with_index_like_name_does_not_trigger_the_hint(self):
        """Подсказка требует ДВУХ признаков: похожего имени и строк индекса внутри.

        Иначе обычный факт вроде MEMORY_of_incident.md ловил бы заметку
        каждый прогон - шум, приучающий пролистывать вывод.
        """
        self.write({
            "MEMORY.md": "- [Разбор](MEMORY_of_incident.md) - что случилось\n",
            "MEMORY_of_incident.md": "в тот вечер сервис ответил 500\n",
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


class SmallerFindings(MemoryFixture):
    """Мелкое из шестого круга: ложная сирота, молчаливый пропуск, ключ вхолостую."""

    def test_reference_style_row_is_a_real_row(self):
        """`- [Профиль][prof]` с определением ниже - законный markdown.

        Агент по такой ссылке дойдёт, а L2 объявлял файл забытым: строка не
        подходила под шаблон `](`. Ложная тревога на честно оформленном индексе.
        """
        self.write({
            "MEMORY.md": "- [Профиль][prof] - кто пользователь\n"
                         "\n"
                         "[prof]: user.md\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_collapsed_reference_row_is_a_real_row(self):
        """Свёрнутая форма `- [user][]` - тот же случай, метка берётся из текста."""
        self.write({
            "MEMORY.md": "- [user][] - кто пользователь\n"
                         "\n"
                         "[user]: user.md\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_reference_style_row_with_broken_target_is_caught(self):
        """Обратная сторона: разбирать - значит и ловить битое, а не просто молчать."""
        self.write({
            "MEMORY.md": "- [Профиль][prof] - кто\n"
                         "\n"
                         "[prof]: net-takogo.md\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("net-takogo.md", output)

    def test_reference_style_row_without_definition_is_caught(self):
        """Метка без определения - строка никуда не ведёт, и это надо назвать."""
        self.write({
            "MEMORY.md": "- [Профиль][prof] - кто\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("prof", output)

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


class CiGuards(unittest.TestCase):
    """CI обязан падать, когда проверка не выполнилась, а не когда «нет тестов»."""

    def workflow(self):
        path = os.path.join(REPO_DIR, ".github", "workflows", "memory-check.yml")
        if not os.path.isfile(path):
            self.skipTest("workflow не найден")
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
        actual = unittest.defaultTestLoader.discover(SCRIPTS_DIR, "test_*.py").countTestCases()
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
        """
        text = self.workflow()
        self.assertIn("countTestCases", text)


class PanelReviewFindings(MemoryFixture):
    """Находки состязательного ревью. Два критических случая - тихий отказ.

    Общая мысль обоих: проверка объявляет память согласованной, когда часть
    её агенту недоступна. Ровно тот класс, ради которого инструмент написан.
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

    def test_duplicate_definition_resolves_to_the_first_one(self):
        """CommonMark: при повторе метки побеждает ПЕРВОЕ определение.

        Код брал последнее - и это тихий пропуск: ссылка, которую увидит
        агент и любой markdown-рендерер, ведёт в никуда, а проверка молчит.
        """
        self.write({
            "MEMORY.md": "- [Профиль][prof] - кто\n"
                         "\n"
                         "[prof]: net-takogo.md\n"
                         "[prof]: user.md\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("net-takogo.md", output)

    def test_orphan_marker_works_below_the_twentieth_line(self):
        """Метка-комментарий не должна зависеть от того, на какой она строке.

        Наша же конвенция памяти - шапка плюс обязательные разделы - легко
        съедает двадцать строк раньше, чем автор дойдёт до пометки.
        """
        head = "---\n" + "".join("pole_%d: znachenie\n" % i for i in range(1, 19)) + "---\n"
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "draft.md": head + "\nТекст заметки.\n\n<!-- linter: orphan-ok -->\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_marker_inside_a_fence_still_does_not_free_the_file(self):
        """Снятие лимита строк не должно вернуть дефект с меткой в блоке кода."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "zametka.md": "Заметка про линтер.\n" * 30 +
                          "\n```\n<!-- linter: orphan-ok -->\n```\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("zametka.md", output)

    def test_shortcut_reference_row_is_a_real_row(self):
        """Третья законная форма CommonMark: `- [prof]` без вторых скобок.

        Под --quiet, которым зовёт хук, битая цель такой строки не давала
        вообще ничего: пустой вывод и код 0.
        """
        self.write({
            "MEMORY.md": "- [prof] - кто пользователь\n"
                         "\n"
                         "[prof]: user.md\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_shortcut_reference_with_broken_target_is_caught(self):
        """Раз разбираем - значит и ловим битое."""
        self.write({
            "MEMORY.md": "- [prof] - кто\n"
                         "\n"
                         "[prof]: net-takogo.md\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter("--quiet")
        self.assertEqual(code, 1, output)
        self.assertIn("net-takogo.md", output)

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


class MinorPanelFindings(MemoryFixture):
    """Мелкое из панельного ревью. Каждое дёшево чинится и незачем тащить в свет."""

    def test_definition_target_in_angle_brackets_with_a_space(self):
        """`[prof]: <моя папка/user.md>` - законный CommonMark.

        В строке-ссылке пробел в адресе уже обрабатывался (угловые скобки
        снимает clean_target), а в определении метки регулярка обрывалась на
        первом пробеле: получался адрес «<моя», ложная битая ссылка И ложная
        сирота на реально существующий файл.
        """
        self.write({
            "MEMORY.md": "- [Профиль][prof] - кто\n"
                         "\n"
                         "[prof]: <moya papka/user.md>\n",
            "moya papka/user.md": "факт\n",
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
        self.assertIn("подпапк", output)

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


class SecondRoundFindings(MemoryFixture):
    """Второй круг ревью: регрессии, внесённые правками первого круга."""

    def test_task_list_checkbox_is_not_a_shortcut_reference(self):
        """`- [x] сделано` - чеклист, а не ссылка, даже если метка `[x]` определена.

        Сокращённая форма ссылки-метки, добавленная первым кругом, матчила
        любую строку `- [текст]`. Стоило в том же файле оказаться определению
        `[x]: файл.md` - и обычный список задач начинал резолвиться как строки
        индекса, блокируя честный коммит. Чеклисты и определения меток
        сосуществуют в реальных файлах постоянно.
        """
        self.write({
            "MEMORY.md": "- [Один факт](real.md) - обычная строка\n"
                         "\n"
                         "[x]: nowhere.md\n"
                         "\n"
                         "- [x] почистить бэклог\n"
                         "- [ ] следующая задача\n",
            "real.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_shortcut_reference_still_works_next_to_checkboxes(self):
        """Обратная сторона: настоящая сокращённая ссылка не должна пострадать."""
        self.write({
            "MEMORY.md": "- [prof] - кто пользователь\n"
                         "- [ ] это чеклист\n"
                         "\n"
                         "[prof]: user.md\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

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


class ThirdRoundFindings(MemoryFixture):
    """Третий круг. Каждая правка проверяется в обе стороны.

    Все находки круга - маятник: прежняя правка закрывала одну сторону и
    открывала другую. Поэтому здесь на каждый фикс два теста: что он ловит и
    что при этом остаётся законным.
    """

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

    def test_dash_label_is_a_reference_not_a_checkbox(self):
        """`[-]` чекбоксом не является ни в одном спек-совместимом рендерере.

        Исключая его вместе с `[ ]` и `[x]`, мы ломали законную ссылку-метку -
        то есть создавали ровно тот вред, который правка про чеклисты
        закрывала, просто на более редком входе.
        """
        self.write({
            "MEMORY.md": "- [-] - метка, названная дефисом\n"
                         "\n"
                         "[-]: real.md\n",
            "real.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_checkbox_marks_are_still_not_references(self):
        """Обратная сторона: `[ ]` и `[x]` остаются чеклистом.

        Здесь компромисс осознанный: GitHub тоже отдаёт приоритет чекбоксу.
        """
        self.write({
            "MEMORY.md": "- [Один факт](real.md) - обычная строка\n"
                         "\n"
                         "[x]: nowhere.md\n"
                         "\n"
                         "- [x] почистить бэклог\n"
                         "- [X] и заглавной тоже\n"
                         "- [ ] следующая задача\n",
            "real.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

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
        """Обратная сторона: настоящая метка вне блока кода работать не перестала."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "zagotovka.md": "<!-- linter: orphan-ok -->\n\nчерновик\n",
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
        self.assertIn("L1", output)

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
        self.assertIn("L1", output)

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
        self.assertIn("L3", output)

    def test_same_row_twice_is_warned(self):
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - раз\n- [Профиль](user.md) - тот же\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertIn("L3", output)


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

    def test_quiet_hides_standalone_warnings(self):
        """Хук зовёт проверку с --quiet. Бурчание на здоровой памяти приучает к --no-verify."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - раз\n- [Профиль](user2.md) - два\n",
            "user.md": "факт\n",
            "user2.md": "факт\n",
        })
        code, output = self.run_linter("--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "")

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
        with io.open(os.path.join(REPO_DIR, "README.md"), encoding="utf-8") as fh:
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
        self.assertIn("L1", text)

    def test_passes_on_consistent_memory(self):
        self.git("add", "-A")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))

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
        self.assertIn("L1", text)

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
        self.assertIn("L1", text)

    def test_hook_is_committed_executable(self):
        """Неисполняемый хук git пропускает молча - и это уже случалось."""
        if not shutil.which("git"):
            self.skipTest("нужен git")
        listing = subprocess.check_output(
            ["git", "ls-files", "-s", ".githooks/pre-commit"], cwd=REPO_DIR)
        self.assertTrue(listing.startswith(b"100755"),
                        listing.decode("utf-8", "replace"))

    def test_blocks_when_memory_file_is_deleted(self):
        """Удаление файла - самый частый способ осиротить строку индекса."""
        self.git("add", "-A")
        self.git("-c", "user.email=t@e.st", "-c", "user.name=test", "commit", "-qm", "init")
        self.git("rm", "-q", "memory/user.md")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 1, text)
        self.assertIn("L1", text)

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
        self.assertIn("L1", text)

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

    def test_usage_error_points_at_the_user_own_config(self):
        """Опечатка в ключах даёт код 2 и выключает блокировку - связь надо назвать."""
        self.stub_checker("import sys\nsys.exit(2)\n")
        self.git("config", "memorycheck.args", "--alow-orphan opechatka")
        self.git("add", "-A")
        result = self.run_hook()
        text = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, text)
        self.assertIn("memorycheck.args", text)

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
