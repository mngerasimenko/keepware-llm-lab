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
import shutil
import stat
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout

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

    def test_anchors_do_not_lean_on_comment_text(self):
        """Якорь держится за код, а не за прозу рядом с ним.

        Три якоря включали в себя строки русского комментария. Отказ при
        протухшем якоре громкий и это правильно - но повод для него был
        выдуманный: перефразировка комментария, к проверяемой ветке
        отношения не имеющего, роняла весь инструмент («фрагмент найден 0
        раз»), и сопровождающий получал красноту там, где ничего не менял.

        Если якорь без комментария перестаёт быть уникальным - опирайтесь на
        соседнюю строку КОДА, как сделано для сброса счётчика потерянных
        строк: там взята следующая строка, а не комментарий над ней.
        """
        offenders = []
        for name, _path, old, _new in mutation_check.MUTATIONS:
            for line in old.splitlines():
                if line.strip().startswith("#"):
                    offenders.append("%s: %s" % (name, line.strip()))
        self.assertEqual(offenders, [],
                         "якорь опирается на текст комментария:\n" +
                         "\n".join(offenders))

    def test_self_tests_are_excluded_from_the_measured_suite(self):
        """Прогон, чью реакцию мы измеряем, не должен включать этот файл.

        Иначе «поймана» печаталось бы на каждой мутации по механической
        причине - и весь отчёт инструмента терял бы смысл.

        Проверяется ВЫЗОВ, а не строка в исходнике. Прежняя редакция искала
        литерал `"-p", "test_check_memory_index.py"` в тексте файла и
        оставалась зелёной, если живой аргумент подменить на `test_*.py`,
        а литерал оставить рядом комментарием. Ровно тот дешёвый признак,
        от которого этот инструмент и страхует.
        """
        seen = {}

        class FakeResult(object):
            returncode = 0

        def fake_run(command, **kwargs):
            seen["command"] = list(command)
            seen["env"] = kwargs.get("env") or {}
            return FakeResult()

        with unittest.mock.patch.object(mutation_check.subprocess, "run", fake_run):
            mutation_check.suite_fails()

        command = seen["command"]
        self.assertIn("-p", command)
        self.assertEqual(command[command.index("-p") + 1],
                         "test_check_memory_index.py",
                         "измеряемый набор захватывает самотесты инструмента")
        self.assertEqual(
            seen["env"].get("MEMCHECK_REQUIRE_SH"), "1",
            "без этой переменной тесты хука пропускаются, и три мутации по "
            "хуку печатаются как ВЫЖИВШИЕ - хотя их просто не проверяли")


class InterruptedRunCleansUpAfterItself(unittest.TestCase):
    """Оборванный прогон не должен оставить линтер молча сломанным.

    `try/finally` спасает от Ctrl+C, но не от закрытого терминала, kill,
    OOM или пропавшего электричества. Проверено вживую: прогон убит, в
    дереве осталось `TASK_MARKS = set()`, и линтер на этом дереве отвечает
    «Память согласована» с кодом 0 - то есть инструмент, который ищет тихие
    отказы, произвёл тихий отказ в самой защите. Ущерб не в пустом файле
    (его видно сразу), а в правдоподобно мутировавшем.
    """

    def sandbox(self):
        folder = tempfile.mkdtemp(prefix="memcheck-interrupt-")
        self.addCleanup(shutil.rmtree, folder, True)
        target = os.path.join(folder, "check.py")
        with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("ОРИГИНАЛ\n")
        return folder, target

    def test_next_start_restores_the_file(self):
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup):
            mutation_check.save_backup(target, "ОРИГИНАЛ\n")
            with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("МУТАЦИЯ\n")           # здесь прогон оборвали
            # Сообщение о восстановлении - для человека за терминалом, в
            # выводе набора оно только мусорит.
            with redirect_stdout(io.StringIO()):
                restored = mutation_check.restore_interrupted()

        self.assertTrue(restored)
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ОРИГИНАЛ\n")
        self.assertFalse(os.path.exists(backup), "слепок остался лежать")

    def test_without_a_backup_nothing_is_touched(self):
        """Вторая половина пары: на чистом дереве восстановление молчит.

        Иначе инструмент правил бы файлы там, где его об этом не просили, -
        а это ровно та беда, от которой он тут страхует.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup):
            restored = mutation_check.restore_interrupted()

        self.assertFalse(restored)
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ОРИГИНАЛ\n")


class AtomicWriteKeepsPermissions(unittest.TestCase):
    """Права файла обязаны пережить подмену - иначе защита гаснет молча.

    Инструмент переписывает `.githooks/pre-commit` и возвращает обратно.
    `mkstemp` создаёт файл с правами 0600, а `os.replace` оставляет права
    ИСТОЧНИКА - значит без явного переноса первый же прогон снимал бы с хука
    бит исполняемости. Неисполняемый хук git пропускает МОЛЧА.

    Это уже случалось (шестой круг) и было единственным классом дефектов,
    где регрессия невидима, - и единственным без регрессионного теста.
    """

    def rewrite(self, mode):
        folder = tempfile.mkdtemp(prefix="memcheck-atomic-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "pre-commit")
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, mode)
        mutation_check.write_atomically(path, "#!/bin/sh\nexit 1\n")
        return path

    def test_executable_bit_survives(self):
        if os.name == "nt":
            self.skipTest("бит исполняемости на Windows не хранится - "
                          "эту сторону закрывает прогон в CI на ubuntu")
        path = self.rewrite(0o755)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755,
                         "с хука снят бит исполняемости - git пропустит его молча")

    def test_content_is_actually_replaced(self):
        """Вторая половина пары: перенос прав не должен отменить саму запись."""
        path = self.rewrite(0o644)
        with io.open(path, encoding="utf-8") as fh:
            self.assertIn("exit 1", fh.read())


class LineEndingsSurviveTheRewrite(unittest.TestCase):
    """Инструмент не должен переводить чужие файлы в LF за компанию.

    Читает он с трансляцией - иначе якоря, записанные через `\\n`, не нашлись
    бы на CRLF-файле ни разу, - а писал всегда LF. В этом репозитории беды не
    видно: `.gitattributes` держит LF. У того, кто скопировал инструмент на
    CRLF-чекаут, первый же прогон переписывал `.githooks/pre-commit` и линтер
    целиком, от первой строки до последней.
    """

    def written_back(self, newline):
        folder = tempfile.mkdtemp(prefix="memcheck-eol-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "file.txt")
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("первая" + newline + "вторая" + newline)
        text, seen = mutation_check.read_source(path)
        mutation_check.write_atomically(path, text, seen)
        with open(path, "rb") as fh:
            return fh.read()

    def test_crlf_file_stays_crlf(self):
        self.assertEqual(self.written_back("\r\n").count(b"\r\n"), 2)

    def test_lf_file_stays_lf(self):
        """Вторая половина пары: LF не должен превратиться в CRLF."""
        written = self.written_back("\n")
        self.assertNotIn(b"\r", written)
        self.assertEqual(written.count(b"\n"), 2)

    def test_backup_remembers_the_line_endings(self):
        """Восстановление после обрыва тоже обязано вернуть те же окончания.

        Иначе оно чинило бы одно и молча переписывало другое.
        """
        folder = tempfile.mkdtemp(prefix="memcheck-eol-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "file.txt")
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("первая\r\nвторая\r\n")
        backup = os.path.join(folder, ".mutation-backup")

        with unittest.mock.patch.object(mutation_check, "BACKUP", backup):
            text, seen = mutation_check.read_source(path)
            mutation_check.save_backup(path, text, seen)
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("МУТАЦИЯ\n")          # здесь прогон оборвали
            with redirect_stdout(io.StringIO()):
                mutation_check.restore_interrupted()

        with open(path, "rb") as fh:
            self.assertEqual(fh.read().count(b"\r\n"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
