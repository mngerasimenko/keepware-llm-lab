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
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_memory_index as linter  # noqa: E402

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPTS_DIR)
SCRIPT = os.path.join(SCRIPTS_DIR, "check_memory_index.py")
HOOK = os.path.join(REPO_DIR, ".githooks", "pre-commit")


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

    def test_star_and_plus_bullets_are_parsed(self):
        self.write({
            "MEMORY.md": "* [Профиль](user.md) - звёздочка\n+ [Сервер](server.md) - плюс\n",
            "user.md": "факт\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 0, output)

    def test_empty_index_says_so_explicitly(self):
        self.write({
            "MEMORY.md": "# Память\n\nни одной строки\n",
            "user.md": "факт\n",
        })
        code, output = self.run_linter()
        self.assertEqual(code, 1, output)
        self.assertIn("0 строк", output)

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

    def test_sub_index_rows_also_count_as_mention(self):
        self.write({
            "MEMORY.md": "- [Под-индекс](MEMORY_infra.md) - инфраструктура\n",
            "MEMORY_infra.md": "- [Сервер](server.md) - прод\n",
            "server.md": "факт\n",
        })
        code, output = self.run_linter("--index", "MEMORY*.md")
        self.assertEqual(code, 0, output)

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
        code, _output = linter.main, None
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
        if not (shutil.which("sh") and shutil.which("git")):
            self.skipTest("нужны sh и git")
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
            ["sh", os.path.join(self.repo, ".githooks", "pre-commit")],
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
        """python3 на Windows часто заглушка Microsoft Store, а не Питон."""
        self.git("add", "-A")
        env = self.fake_python3("#!/bin/sh\necho Python\nexit 49\n")
        result = self.run_hook(env)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))

    def test_rejects_python2_candidate(self):
        """Заглушка - не единственный самозванец: питон второй тоже не подходит."""
        self.write_memory("- [Профиль](net.md) - битая\n")
        self.git("add", "-A")
        # Проверку "это Питон вообще" такой самозванец проходит,
        # проверку версии - нет. Значит хук обязан отбросить его и взять настоящий.
        env = self.fake_python3(
            '#!/bin/sh\ncase "$*" in *version_info*) exit 1 ;; *) exit 0 ;; esac\n'
        )
        result = self.run_hook(env)
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
