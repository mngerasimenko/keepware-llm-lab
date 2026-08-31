#!/usr/bin/env python3
"""Тесты про сам инструмент проверки починок.

Отдельным файлом по той же причине, что и тесты мутационной проверки: этот
инструмент прогоняет ИМЕНОВАННЫЕ тесты, и если бы его собственные лежали
среди них, прогон получился бы вложенным сам в себя.

Проверяется прежде всего то, ради чего инструмент вообще написан иначе, чем
сосед: он обязан находить тест, который остаётся зелёным на откате, - и
обязан не трогать рабочее дерево.
"""

import io
import os
import shutil
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout, redirect_stderr

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPTS_DIR)
import sys
sys.path.insert(0, SCRIPTS_DIR)

import revert_check


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class RevertCheckIsAlive(unittest.TestCase):
    """Инструмент отвечает на свой вопрос, а не на соседний."""

    def fake_repo(self, test_body=None):
        """Крошечное дерево: одна «починка» и один тест на неё."""
        folder = tempfile.mkdtemp(prefix="revert-self-")
        self.addCleanup(shutil.rmtree, folder, True)
        scripts = os.path.join(folder, "scripts")
        os.makedirs(scripts)
        # main() отказывается работать в неполном дереве - значит линтер
        # должен быть на месте хотя бы заглушкой.
        write(os.path.join(scripts, "check_memory_index.py"), "# заглушка\n")
        write(os.path.join(scripts, "probe.py"), 'VALUE = "починено"\n')
        write(os.path.join(scripts, "test_probe.py"), test_body or (
            "import unittest\n"
            "import probe\n\n\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(probe.VALUE, 'починено')\n"))
        return folder

    def run_tool(self, folder, reverts, argv=None):
        printed = io.StringIO()
        complained = io.StringIO()
        with unittest.mock.patch.object(revert_check, "REPO", folder), \
                unittest.mock.patch.object(revert_check, "REVERTS", reverts), \
                redirect_stdout(printed), redirect_stderr(complained):
            code = revert_check.main(argv or [])
        return code, printed.getvalue() + complained.getvalue()

    def test_a_revert_that_reddens_its_test_is_a_pass(self):
        folder = self.fake_repo()
        code, printed = self.run_tool(folder, [(
            "значение подменено", os.path.join("scripts", "probe.py"),
            'VALUE = "починено"', 'VALUE = "сломано"',
            "test_probe.Probe.test_value")])
        self.assertEqual(code, 0, printed)
        self.assertIn("различает", printed)

    def test_a_test_that_stays_green_on_the_revert_is_reported(self):
        """Главный вопрос инструмента. Зелёный тест на откате - находка.

        Ровно этот случай пять кругов подряд оказывался здесь главным: тест
        под починку написан, выглядит осмысленно и остаётся зелёным, если
        починку убрать.
        """
        folder = self.fake_repo()
        code, printed = self.run_tool(folder, [(
            "правка, которой тест не касается",
            os.path.join("scripts", "probe.py"),
            'VALUE = "починено"\n', 'VALUE = "починено"  # хвост\n',
            "test_probe.Probe.test_value")])
        self.assertEqual(code, 1, printed)
        self.assertIn("НЕ РАЗЛИЧАЕТ", printed)
        self.assertIn("Не различает", printed)

    def test_a_stale_anchor_is_a_hard_error(self):
        """Ненайденный фрагмент - отказ, а не пропуск.

        Молчаливо пропущенный откат превратил бы инструмент ровно в то, что
        он ищет: проверку, которая ничего не проверяет и рапортует успех.
        """
        folder = self.fake_repo()
        code, printed = self.run_tool(folder, [(
            "фрагмента давно нет", os.path.join("scripts", "probe.py"),
            "ЧЕГО ЗДЕСЬ НЕТ", "неважно", "test_probe.Probe.test_value")])
        self.assertEqual(code, 1, printed)
        self.assertIn("Якорь протух", printed)

    def test_a_skipped_test_is_not_passed_off_as_distinguishing(self):
        """Пропуск возвращает ноль - как и успех. Их надо различать.

        Без этой ветки `skipTest` на чужой системе печатался бы как «не
        различает», а инструмент врал бы в обе стороны: то жаловался на
        исправный тест, то засчитывал пропуск за проверку.
        """
        folder = self.fake_repo(
            "import unittest\n"
            "import probe\n\n\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.skipTest('на этой системе не проверить')\n")
        code, printed = self.run_tool(folder, [(
            "значение подменено", os.path.join("scripts", "probe.py"),
            'VALUE = "починено"', 'VALUE = "сломано"',
            "test_probe.Probe.test_value")])
        self.assertIn("пропущен", printed)
        self.assertIn("различение не проверено", printed)
        self.assertEqual(code, 0, printed)

    def test_the_tree_it_reads_is_never_written_to(self):
        """Откат уходит в копию. Дерево остаётся байт в байт тем же.

        Инструмент нужен как раз тогда, когда в дереве лежит незакоммиченная
        работа: портить её ради проверки значило бы делать то самое, от чего
        этот репозиторий защищается.
        """
        folder = self.fake_repo()
        before = {}
        for root, _dirs, files in os.walk(folder):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as fh:
                    before[path] = fh.read()

        self.run_tool(folder, [(
            "значение подменено", os.path.join("scripts", "probe.py"),
            'VALUE = "починено"', 'VALUE = "сломано"',
            "test_probe.Probe.test_value")])

        after = {}
        for root, _dirs, files in os.walk(folder):
            for name in files:
                path = os.path.join(root, name)
                if path.endswith(".pyc"):
                    continue
                with open(path, "rb") as fh:
                    after[path] = fh.read()
        self.assertEqual(after, before, "инструмент правил читаемое дерево")

    def test_anchors_only_does_not_run_any_test(self):
        """Быстрая ветка проверяет применимость и молчит про тесты.

        Имя теста здесь заведомо несуществующее: запусти инструмент его - и
        прогон вернул бы ошибку загрузки, то есть «различает» по причине, к
        починке отношения не имеющей.
        """
        folder = self.fake_repo()
        code, printed = self.run_tool(folder, [(
            "значение подменено", os.path.join("scripts", "probe.py"),
            'VALUE = "починено"', 'VALUE = "сломано"',
            "test_probe.НетТакогоКласса.test_value")], ["--anchors-only"])
        self.assertEqual(code, 0, printed)
        self.assertIn("применим", printed)

    def test_an_incomplete_tree_is_refused(self):
        folder = tempfile.mkdtemp(prefix="revert-empty-")
        self.addCleanup(shutil.rmtree, folder, True)
        code, printed = self.run_tool(folder, [])
        self.assertEqual(code, 1)
        self.assertIn("дерево неполное", printed)


class TheRevertRegistryIsCurrent(unittest.TestCase):
    """Реестр откатов обязан находиться в сегодняшнем коде."""

    def test_every_revert_anchor_is_still_found(self):
        """Поменяли ветку - поправьте её откат здесь же.

        Тот же договор, что у мутаций, и по той же причине: откат, который
        больше не применяется, - это молчаливо пропущенная проверка.
        """
        for name, relative, old, _new, _test in revert_check.REVERTS:
            path = os.path.join(REPO_DIR, relative)
            with io.open(path, encoding="utf-8") as stream:
                text = stream.read()
            self.assertEqual(
                text.count(old), 1,
                "откат «%s»: фрагмент найден %d раз вместо одного в %s"
                % (name, text.count(old), relative))

    def test_every_named_test_exists(self):
        """Опечатка в имени теста читалась бы как «различает».

        Несуществующий тест `unittest` грузить отказывается и возвращает
        ненулевой код - то есть выглядел бы ровно как тест, упавший на
        откате. Проверка дешёвая, а цена ошибки - тихо не работающий откат.
        """
        loader = unittest.defaultTestLoader
        for name, _relative, _old, _new, test in revert_check.REVERTS:
            found = loader.loadTestsFromName(test)
            self.assertEqual(
                found.countTestCases(), 1,
                "откат «%s» называет тест %s, которого нет" % (name, test))


if __name__ == "__main__":
    unittest.main(verbosity=2)
