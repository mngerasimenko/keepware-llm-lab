#!/usr/bin/env python3
"""Тесты про сам инструмент проверки починок.

Отдельным файлом по той же причине, что и тесты мутационной проверки: этот
инструмент прогоняет ИМЕНОВАННЫЕ тесты, и если бы его собственные лежали
среди них, прогон получился бы вложенным сам в себя.

Проверяется прежде всего то, ради чего инструмент написан иначе, чем сосед:
он обязан находить тест, который остаётся зелёным на откате, обязан не
выдавать за находку чужую поломку - и обязан не трогать рабочее дерево.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout, redirect_stderr

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPTS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import revert_check  # noqa: E402

# Тело теста, который держит «починку» в песочнице.
GREEN_TEST = ("import unittest\n"
              "import probe\n\n\n"
              "class Probe(unittest.TestCase):\n"
              "    def test_value(self):\n"
              "        self.assertEqual(probe.VALUE, 'починено')\n")
# Откат, который эту починку отменяет.
REAL_REVERT = ("значение подменено", os.path.join("scripts", "probe.py"),
               'VALUE = "починено"', 'VALUE = "сломано"',
               "test_probe.Probe.test_value")


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class RevertCheckIsAlive(unittest.TestCase):
    """Инструмент отвечает на свой вопрос, а не на соседний."""

    def fake_repo(self, test_body=None, probe='VALUE = "починено"\n'):
        """Крошечное дерево: одна «починка» и один тест на неё."""
        folder = tempfile.mkdtemp(prefix="revert-self-")
        self.addCleanup(shutil.rmtree, folder, True)
        scripts = os.path.join(folder, "scripts")
        os.makedirs(scripts)
        # main() отказывается работать в неполном дереве - значит линтер
        # должен быть на месте хотя бы заглушкой.
        write(os.path.join(scripts, "check_memory_index.py"), "# заглушка\n")
        write(os.path.join(scripts, "probe.py"), probe)
        write(os.path.join(scripts, "test_probe.py"), test_body or GREEN_TEST)
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
        code, printed = self.run_tool(self.fake_repo(), [REAL_REVERT])
        self.assertEqual(code, 0, printed)
        self.assertIn("различает", printed)

    def test_a_test_that_stays_green_on_the_revert_is_reported(self):
        """Главный вопрос инструмента. Зелёный тест на откате - находка.

        Ровно этот случай пять кругов подряд оказывался здесь главным: тест
        под починку написан, выглядит осмысленно и остаётся зелёным, если
        починку убрать.
        """
        code, printed = self.run_tool(self.fake_repo(), [(
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
        code, printed = self.run_tool(self.fake_repo(), [(
            "фрагмента давно нет", os.path.join("scripts", "probe.py"),
            "ЧЕГО ЗДЕСЬ НЕТ", "неважно", "test_probe.Probe.test_value")])
        self.assertEqual(code, 1, printed)
        self.assertIn("Якорь протух", printed)

    def test_a_typo_in_the_test_name_is_not_a_finding(self):
        """Ненулевой код даёт и упавший тест, и НЕЗАПУЩЕННЫЙ.

        Опечатка в имени, пропавший модуль, сломанная копия - всё это
        `unittest` отдаёт ненулевым кодом, и без разбора вывода инструмент
        печатал бы «различает», не выполнив ни одного утверждения. Тот же
        класс лжи, из-за которого у соседа три мутации «ловились»
        синтаксической ошибкой.
        """
        code, printed = self.run_tool(self.fake_repo(), [(
            "значение подменено", os.path.join("scripts", "probe.py"),
            'VALUE = "починено"', 'VALUE = "сломано"',
            "test_probe.Probe.test_valeu")])
        self.assertEqual(code, 1, printed)
        self.assertIn("ПРОГОН СЛОМАН", printed)
        self.assertNotIn("различает", printed)

    def test_a_test_that_is_red_before_the_revert_is_named(self):
        """База обязана быть зелёной. Иначе «различает» - на пустом месте.

        Инструмент штатно запускают с незакоммиченной работой под руками -
        это записано в CONTRIBUTING. Сломал по дороге тест хука, и без
        базового прогона пять откатов по хуку отрапортовали бы успех.
        """
        code, printed = self.run_tool(
            self.fake_repo(probe='VALUE = "ещё не починено"\n'),
            [REAL_REVERT])
        self.assertEqual(code, 1, printed)
        self.assertIn("БАЗА КРАСНАЯ", printed)
        self.assertIn("чинить его, а не починку", printed)

    def test_a_skipped_test_is_not_passed_off_as_distinguishing(self):
        """Пропуск возвращает ноль - как и успех. Их надо различать.

        Без этой ветки `skipTest` печатался бы как «не различает», и
        инструмент врал бы в обе стороны: то жаловался на исправный тест,
        то засчитывал пропуск за проверку.
        """
        code, printed = self.run_tool(self.fake_repo(
            "import unittest\n"
            "import probe\n\n\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.skipTest('на этой системе не проверить')\n"),
            [REAL_REVERT])
        self.assertIn("пропущен", printed)
        self.assertIn("различение не проверено", printed)
        self.assertEqual(code, 0, printed)

    def test_a_failure_with_a_skip_beside_it_is_still_a_failure(self):
        """`FAILED (failures=1, skipped=1)` - это не пропуск.

        Пропуск ищется только у зелёного исхода. Пока он искался подстрокой
        во всём выводе, настоящая находка глохла: класс, где один тест
        пропущен, а второй упал, читался как «пропущен», и код возврата
        оставался нулевым.
        """
        code, printed = self.run_tool(self.fake_repo(
            "import unittest\n"
            "import probe\n\n\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(probe.VALUE, 'починено')\n\n"
            "    def test_skipped_neighbour(self):\n"
            "        self.skipTest('соседний тест пропущен')\n"),
            [("значение подменено", os.path.join("scripts", "probe.py"),
              'VALUE = "починено"', 'VALUE = "сломано"', "test_probe.Probe")])
        self.assertEqual(code, 0, printed)
        # Проверять подстрокой «различает» тут нельзя: она целиком входит в
        # итоговое «Починок различается своими тестами», и тест оставался
        # зелёным на сломанной реализации. Спрашиваем ровно то, что должно
        # быть в строке случая, и ровно то, чего в ней быть не должно.
        self.assertNotIn("пропущен", printed)
        self.assertIn("различается своими тестами: 1 из 1", printed)

    def test_the_tree_it_reads_is_never_written_to(self):
        """Откат уходит в копию. Дерево остаётся байт в байт тем же.

        Инструмент нужен как раз тогда, когда в дереве лежит незакоммиченная
        работа: портить её ради проверки значило бы делать то самое, от чего
        этот репозиторий защищается.
        """
        folder = self.fake_repo()

        def snapshot():
            seen = {}
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    path = os.path.join(root, name)
                    if path.endswith(".pyc") or "__pycache__" in path:
                        continue
                    with open(path, "rb") as fh:
                        seen[path] = fh.read()
            return seen

        before = snapshot()
        self.run_tool(folder, [REAL_REVERT])
        self.assertEqual(snapshot(), before, "инструмент правил своё дерево")

    def test_no_copies_are_left_behind(self):
        """Копии убираются. Иначе дюжина деревьев за прогон оседает в TEMP.

        Уборка не мелочь: инструмент задуман как частый, и мусор от него
        накапливается ровно у того, кто им пользуется.
        """
        before = set(os.listdir(tempfile.gettempdir()))
        self.run_tool(self.fake_repo(), [REAL_REVERT])
        left = [name for name in set(os.listdir(tempfile.gettempdir())) - before
                if name.startswith("revert-check-")]
        self.assertEqual(left, [], "копии остались в TEMP")

    def test_anchors_only_does_not_run_any_test(self):
        """Быстрая ветка проверяет применимость и молчит про тесты.

        Имя теста здесь заведомо несуществующее: запусти инструмент его - и
        прогон вернул бы поломку, то есть ключ работал бы не так, как
        обещает.
        """
        code, printed = self.run_tool(self.fake_repo(), [(
            "значение подменено", os.path.join("scripts", "probe.py"),
            'VALUE = "починено"', 'VALUE = "сломано"',
            "test_probe.НетТакогоКласса.test_value")], ["--anchors-only"])
        self.assertEqual(code, 0, printed)
        self.assertIn("применим", printed)

    def test_anchors_only_still_reports_a_stale_anchor(self):
        """И при этом остаётся проверкой, а не печатью «применим».

        Без этого теста ветку ключа можно заменить константой «применим», и
        шаг CI, который её зовёт, навсегда стал бы пустым.
        """
        code, printed = self.run_tool(self.fake_repo(), [(
            "фрагмента давно нет", os.path.join("scripts", "probe.py"),
            "ЧЕГО ЗДЕСЬ НЕТ", "неважно", "test_probe.Probe.test_value")],
            ["--anchors-only"])
        self.assertEqual(code, 1, printed)
        self.assertIn("ЯКОРЬ", printed)

    def test_an_empty_registry_is_refused(self):
        """Пустой реестр - отказ, а не «всё в порядке».

        Вычисти его, и прогон печатал бы успех, не проверив ни одной
        починки, - то есть стал бы тем самым тихим отказом.
        """
        code, printed = self.run_tool(self.fake_repo(), [])
        self.assertEqual(code, 1, printed)
        self.assertIn("Реестр откатов пуст", printed)

    def test_an_incomplete_tree_is_refused(self):
        folder = tempfile.mkdtemp(prefix="revert-empty-")
        self.addCleanup(shutil.rmtree, folder, True)
        code, printed = self.run_tool(folder, [REAL_REVERT])
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
            with io.open(path, encoding="utf-8", newline="") as stream:
                text = stream.read()
            self.assertEqual(
                text.count(old), 1,
                "откат «%s»: фрагмент найден %d раз вместо одного в %s"
                % (name, text.count(old), relative))

    def test_every_named_test_exists(self):
        """Опечатка в имени теста - это откат, который ничего не проверяет.

        Первая редакция этого теста была ПУСТОЙ: `loadTestsFromName` с 3.5
        не бросает исключение, а возвращает набор из одного
        `unittest.loader._FailedTest`, поэтому `countTestCases() == 1`
        проходило на любом мусоре - и на несуществующем классе, и на
        несуществующем модуле. Проверять надо загрузчик, а не число.
        """
        for name, _relative, _old, _new, test in revert_check.REVERTS:
            # Свой загрузчик, а не общий: у `defaultTestLoader` ошибки
            # копятся между вызовами, и первая же опечатка красила бы все
            # последующие имена.
            loader = unittest.TestLoader()
            found = loader.loadTestsFromName(test)
            self.assertEqual(loader.errors, [],
                             "откат «%s»: имя %s не загружается" % (name, test))
            broken = [case for case in found
                      if isinstance(case, unittest.loader._FailedTest)]
            self.assertEqual(broken, [],
                             "откат «%s» называет тест %s, которого нет"
                             % (name, test))

    def test_the_registry_did_not_quietly_shrink(self):
        """Порог - от той же болезни, что у соседа: реестр может усохнуть.

        Удали из него всё, кроме одной записи, и прогон останется зелёным,
        а проверка перестанет что-либо проверять.
        """
        self.assertGreaterEqual(
            len(revert_check.REVERTS), 10,
            "реестр откатов усох - проверять стало нечего")


if __name__ == "__main__":
    unittest.main(verbosity=2)
