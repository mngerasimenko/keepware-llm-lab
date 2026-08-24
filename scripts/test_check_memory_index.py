#!/usr/bin/env python3
"""Тесты линтера согласованности памяти.

Запуск из корня репозитория:

    python -m unittest discover -s scripts -v

Зависимостей нет, только стандартная библиотека.
"""

import hashlib
import io
import os
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
        """Комментарий открыт в хвосте строки - сама строка от этого не пропадает."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто <!-- TODO дописать крючок\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertIn("не закрыт", output.lower())

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
        self.assertIn("формат", output.lower())

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
        """Прибивает умолчание шаблона индекса.

        Хук зовёт проверку без ключей. Верни умолчание к одному «MEMORY.md» -
        и у всех, кто разбил индекс на части, начнут падать коммиты. Раньше
        оба теста про под-индексы передавали --index явно, и эта регрессия
        прошла бы мимо набора.
        """
        self.write({
            "MEMORY.md": "- [Инфра](MEMORY_infra.md) - под-индекс\n",
            "MEMORY_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_sub_index_rows_also_count_as_mention(self):
        self.write({
            "MEMORY.md": "- [Под-индекс](MEMORY_infra.md) - инфраструктура\n",
            "MEMORY_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter("--index", "MEMORY*.md")
        self.assertEqual(code, 0, output)

    def test_root_index_is_derived_from_the_pattern_not_from_a_fixed_name(self):
        """Ключ --index мы рекламируем - значит он должен работать не только с MEMORY."""
        self.write({
            "INDEX.md": "- [Профиль](user.md) - кто\n- [Инфра](INDEX_infra.md) - под-индекс\n",
            "INDEX_infra.md": "- [Сервер](server.md) - прод\n",
            "user.md": "факт\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter("--index", "INDEX*.md")
        self.assertEqual(code, 0, output)

    def test_sub_index_reachable_through_a_chain(self):
        """Достижимость транзитивна: корень -> A -> B. Иначе это не обход, а один шаг."""
        self.write({
            "MEMORY.md": "- [А](MEMORY_a.md) - первый уровень\n",
            "MEMORY_a.md": "- [Б](MEMORY_b.md) - второй уровень\n",
            "MEMORY_b.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_detached_cycle_of_sub_indexes_is_error(self):
        """Два под-индекса, ссылающиеся друг на друга, «упомянуты» - и недостижимы."""
        self.write({
            "MEMORY.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "MEMORY_b.md": "- [В](MEMORY_c.md) - сосед\n",
            "MEMORY_c.md": "- [Б](MEMORY_b.md) - сосед\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)

    def test_unreferenced_top_level_index_is_error_with_custom_pattern(self):
        """Тот случай, где «корень выведен из шаблона» и «все верхние - корни» расходятся."""
        self.write({
            "INDEX.md": "- [Профиль](user.md) - кто\n",
            "user.md": "факт\n",
            "INDEX_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter("--index", "INDEX*.md")
        self.assertEqual(code, 1, output)
        self.assertIn("INDEX_infra.md", output)

    def test_pattern_with_several_wildcards_does_not_crown_the_wrong_root(self):
        self.write({
            "AGENT_MEMORY.md": "- [Профиль](user.md) - кто\n- [Инфра](MEMORY.md) - под-индекс\n",
            "MEMORY.md": "- [Сервер](server.md) - прод\n",
            "user.md": "факт\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter("--index", "*MEMORY*.md")
        self.assertEqual(code, 0, output)

    def test_cross_listing_the_same_file_from_two_sub_indexes_is_not_a_duplicate(self):
        """L2 разрешает упоминание в любом индексе - значит это законная схема."""
        self.write({
            "MEMORY.md": "- [Инфра](MEMORY_infra.md) - раз\n- [Прод](MEMORY_prod.md) - два\n",
            "MEMORY_infra.md": "- [Сервер](server.md) - прод\n",
            "MEMORY_prod.md": "- [Сервер](server.md) - тот же файл, другой раздел\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)
        self.assertNotIn("L3", output)

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
            "MEMORY_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter("--index", "MEMORY*.md")
        self.assertEqual(code, 1, output)
        self.assertIn("MEMORY_infra.md", output)


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
        self.assertIn("не папка памяти", output.lower())

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
            "MEMORY.md": "- [Инфра](sub/MEMORY_infra.md) - под-индекс\n",
            "sub/MEMORY_infra.md": "- [Сервер](server.md) - прод\n",
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
        self.assertNotIn("Проверку выполнить не удалось", output)
        self.assertIn("не читается", output.lower())

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
        self.assertIn(code, (0, 1), output)
        self.assertNotIn("Проверку выполнить не удалось", output)


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


class PreCommitHook(unittest.TestCase):
    """Хук - это то, что трогает посторонний человек. Здесь ошибаться дороже всего."""

    def setUp(self):
        self.sh = find_sh()
        if not (self.sh and shutil.which("git")):
            self.skipTest("не нашёл sh - тесты хука НЕ выполнялись, это не значит, что он исправен")
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

        Подделываем 3.6 - он ниже заявленных 3.7 и до сих пор живёт на старых
        LTS-системах. Проба обязана спрашивать версию, а не факт запуска:
        «это вообще Питон?» такой кандидат проходит, а проверку версии - нет.
        """
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

    def test_failure_message_mentions_the_escape_hatch(self):
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        result = self.run_hook()
        self.assertIn("--no-verify", result.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
