#!/usr/bin/env python3
"""Тесты про сам инструмент мутационной проверки.

Отдельным файлом намеренно. `mutation_check.py` измеряет реакцию набора на
порчу кода - и если бы эти тесты лежали в измеряемом наборе, они падали бы
на КАЖДОЙ мутации: они проверяют, что якоря мутаций находятся в коде, а
активная мутация свой же якорь и заменяет. Набор краснел бы всегда, отчёт
«все пойманы» печатался бы независимо от содержательных тестов.

Так и было в первой редакции инструмента. Здесь эти тесты вынесены, а
`suite_fails()` гоняет только `test_check_memory_index.py`.

Общий прогон (`discover -p "test_*.py"`) подхватывает оба файла - в CI
проверяется всё.
"""

import io
import os
import sys
import unittest

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import mutation_check  # noqa: E402


class MutationCheckIsAlive(unittest.TestCase):
    """Мутационная проверка сама не должна стать тихим отказом.

    Её мутации привязаны к точным фрагментам кода. Стоит коду измениться -
    фрагмент перестаёт находиться, мутация тихо не ставится, и инструмент,
    созданный ловить непроверенные ветки, начинает рапортовать успех ни о
    чём. Это ровно тот класс дефекта, который он ищет.

    Здесь проверяется только то, что все якоря на месте: полный прогон
    занимает минуты, а протухший якорь надо ловить на каждом коммите.
    """

    def test_every_mutation_anchor_is_still_found(self):
        previous = os.getcwd()
        os.chdir(REPO_DIR)
        try:
            stale = mutation_check.missing_anchors()
        finally:
            os.chdir(previous)
        self.assertEqual(
            stale, [],
            "мутации отстали от кода - поправьте их в scripts/mutation_check.py")

    def test_mutation_list_is_not_empty(self):
        """Пустой список мутаций отрапортовал бы «все пойманы» ни о чём."""
        self.assertGreater(len(mutation_check.MUTATIONS), 20,
                           "набор мутаций подозрительно мал")

    def test_self_tests_are_excluded_from_the_measured_suite(self):
        """Прогон, чью реакцию мы измеряем, не должен включать этот файл.

        Иначе «поймана» печаталось бы на каждой мутации по механической
        причине - и весь отчёт инструмента терял бы смысл.
        """
        source = io.open(os.path.join(SCRIPTS_DIR, "mutation_check.py"),
                         encoding="utf-8").read()
        self.assertIn('"-p", "test_check_memory_index.py"', source,
                      "suite_fails гоняет не только основной файл тестов")


if __name__ == "__main__":
    unittest.main(verbosity=2)
