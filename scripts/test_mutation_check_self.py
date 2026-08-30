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
import subprocess
import time
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

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
        """Пустой список мутаций отрапортовал бы «все пойманы» ни о чём.

        Порог держим близко к фактическому числу, а не «лишь бы не ноль»:
        потолок в 20 остался бы зелёным после удаления двух третей списка.
        Тот же приём, что у гарда «тестов собралось не меньше 150» в CI, -
        и держать его в синхроне приходится руками, других способов нет.
        """
        self.assertGreater(len(mutation_check.MUTATIONS), 95,
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
            stdout = b"Ran 215 tests in 44.0s\n\nOK\n"

        def fake_run(command, apart, **kwargs):
            seen["command"] = list(command)
            seen["env"] = kwargs.get("env") or {}
            seen["apart"] = apart
            seen["kwargs"] = kwargs
            return FakeResult()

        with unittest.mock.patch.object(mutation_check, "run_apart", fake_run):
            verdict = mutation_check.suite_fails()

        # Возврат проверяем, а не только команду. Прежде тест звал функцию и
        # выбрасывал её ответ: инвертируй последнюю строку `suite_fails`, и
        # инструмент начал бы печатать «поймана» ровно там, где мутация
        # выжила, - а самотесты остались бы зелёными.
        self.assertFalse(verdict, "зелёный набор объявлен покрасневшим")
        command = seen["command"]
        self.assertIn("-p", command)
        self.assertEqual(command[command.index("-p") + 1],
                         "test_check_memory_index.py",
                         "измеряемый набор захватывает самотесты инструмента")
        self.assertEqual(
            seen["env"].get("MEMCHECK_REQUIRE_SH"), "1",
            "без этой переменной тесты хука пропускаются, и три мутации по "
            "хуку печатаются как ВЫЖИВШИЕ - хотя их просто не проверяли")
        # Вывод обязан перехватываться. Подмена запуска делает это невидимым:
        # первая редакция этой обёртки потеряла `stdout=PIPE` при правке,
        # вывод набора ушёл в терминал, инструменту досталась пустота - и он
        # отказался работать на базовом прогоне. Тест был зелёный.
        self.assertIs(seen["kwargs"].get("stdout"), subprocess.PIPE,
                      "вывод набора не перехватывается - судить будет не по чему")
        self.assertIs(seen["kwargs"].get("stderr"), subprocess.STDOUT,
                      "поток ошибок теряется мимо вывода")
        # Своя группа процессов - тоже свойство вызова, и его надо спросить.
        # Прежде `apart` захватывался и не проверялся ничем: убери его из
        # вызова - то есть отмени изоляцию целиком, - и набор остался бы
        # зелёным. Тот же урок, что с потерянным stdout, строкой ниже.
        key = "creationflags" if os.name == "nt" else "start_new_session"
        self.assertTrue(seen["apart"].get(key),
                      "набор запускается в группе процессов терминала - по "
                      "таймауту переживут внуки")


    def test_a_red_suite_is_reported_as_red(self):
        """Вторая половина пары: покрасневший набор возвращается как True.

        Без неё зелёная половина доказывала бы только, что функция умеет
        отвечать «нет».
        """
        class FakeResult(object):
            returncode = 1
            stdout = (u"FAIL: test_chto_to\nRan 215 tests in 44.0s\n\n"
                      u"FAILED (failures=1)\n").encode("utf-8")

        with unittest.mock.patch.object(mutation_check, "run_apart",
                                        lambda *a, **k: FakeResult()):
            self.assertTrue(mutation_check.suite_fails())

    def test_a_suite_that_never_ran_is_not_called_caught(self):
        """Ненулевой код при нуле запущенных тестов - поломка, а не находка.

        Так «ловились» три мутации: они вклеивали синтаксически негодный
        Python, импорт линтера на уровне модуля падал, `unittest` заводил
        `_FailedTest` и отдавал код 1. Инструмент печатал «поймана», не
        запустив ни одного содержательного теста.

        В CI этот же вопрос закрыт отдельным шагом «тестов собралось не
        меньше 150». Здесь он должен быть тем более: инструмент только про
        то и написан, чтобы не верить зелёному цвету.
        """
        class FakeResult(object):
            returncode = 1
            stdout = (u"ERROR: unittest.loader._FailedTest\n"
                      u"Ran 1 test in 0.000s\n\nFAILED (errors=1)\n"
                      ).encode("utf-8")

        with unittest.mock.patch.object(mutation_check, "run_apart",
                                        lambda *a, **k: FakeResult()):
            with self.assertRaises(SystemExit) as caught:
                mutation_check.suite_fails()
        self.assertIn("не запустился", str(caught.exception))

    def test_zero_collected_tests_is_not_called_caught_either(self):
        """Каждое условие гарда проверяется своим входом.

        В фикстуре соседнего теста присутствуют ОБЕ приметы разом - и
        `_FailedTest`, и `Ran 1 test`. Поэтому выброси из гарда проверку
        числа тестов, и он останется зелёным: сработает третье условие.
        Здесь примета одна - собрано ноль.
        """
        class FakeResult(object):
            returncode = 1
            stdout = b"Ran 0 tests in 0.000s\n\nFAILED (errors=1)\n"

        with unittest.mock.patch.object(mutation_check, "run_apart",
                                        lambda *a, **k: FakeResult()):
            with self.assertRaises(SystemExit):
                mutation_check.suite_fails()

    def test_a_silent_run_is_not_called_caught_either(self):
        """Третье условие: вывода нет вовсе - значит и судить не по чему."""
        class FakeResult(object):
            returncode = 1
            stdout = b""

        with unittest.mock.patch.object(mutation_check, "run_apart",
                                        lambda *a, **k: FakeResult()):
            with self.assertRaises(SystemExit):
                mutation_check.suite_fails()

    def test_the_word_failedtest_in_a_test_name_does_not_kill_the_tool(self):
        """Примета - полное имя класса загрузчика, а не подстрока.

        Пока искали `"_FailedTest" in output`, тест с таким словом в имени
        или в сообщении убил бы инструмент на первой же мутации: SystemExit
        вместо честного «поймана».
        """
        class FakeResult(object):
            returncode = 1
            stdout = (u"FAIL: test_FailedTest_naming_is_allowed\n"
                      u"Ran 215 tests in 44.0s\n\nFAILED (failures=1)\n"
                      ).encode("utf-8")

        with unittest.mock.patch.object(mutation_check, "run_apart",
                                        lambda *a, **k: FakeResult()):
            self.assertTrue(mutation_check.suite_fails())

    def test_a_hook_mutant_that_is_not_valid_sh_is_a_hard_error(self):
        """Правило про негодный мутант распространяется и на хук.

        Первая редакция проверяла только `.py`, а `--anchors-only` при этом
        обещал, что мутант компилируется. Негодный мутант хука импорт не
        роняет: тесты честно краснеют на его синтаксической ошибке, и
        инструмент печатает «поймана» про ветку, которой не касался. Тот же
        класс лжи, только в другом файле.
        """
        if not mutation_check.find_shell():
            self.skipTest("sh не найден - проверить нечем")
        broken = [("выдуманная мутация", mutation_check.HOOK,
                   'CHECKER="scripts/check_memory_index.py"',
                   "if then fi )(")]
        previous = os.getcwd()
        os.chdir(REPO_DIR)
        try:
            with unittest.mock.patch.object(mutation_check, "MUTATIONS", broken):
                stale = mutation_check.missing_anchors()
        finally:
            os.chdir(previous)
        self.assertEqual(len(stale), 1, stale)
        self.assertIn("не разбирается как sh", stale[0][1])

    def test_without_sh_the_requirement_is_not_silently_skipped(self):
        """`MEMCHECK_REQUIRE_SH` читается, а не только передаётся дальше.

        Докстрока обещала, что в CI пропуск невозможен, а переменная только
        клалась в окружение потомка и здесь не смотрелась - то есть
        `--anchors-only` рапортовал «мутант разбирается» про два десятка
        непроверенных. Починку третьего круга не держал ни один тест.
        """
        broken = [("выдуманная мутация", mutation_check.HOOK,
                   'CHECKER="scripts/check_memory_index.py"',
                   "if then fi )(")]
        previous = os.getcwd()
        os.chdir(REPO_DIR)
        try:
            with unittest.mock.patch.object(mutation_check, "find_shell",
                                            lambda: None), \
                    unittest.mock.patch.dict(os.environ,
                                             {"MEMCHECK_REQUIRE_SH": "1"}), \
                    unittest.mock.patch.object(mutation_check, "MUTATIONS",
                                               broken):
                stale = mutation_check.missing_anchors()
        finally:
            os.chdir(previous)
        self.assertEqual(len(stale), 1, stale)
        self.assertIn("sh не найден", stale[0][1])

    def test_without_sh_and_without_the_variable_we_stay_quiet(self):
        """Вторая половина пары: без требования - молчим.

        На машине без `sh` тесты хука и так пропускаются; требовать большего
        от мутационной проверки не за что.
        """
        broken = [("выдуманная мутация", mutation_check.HOOK,
                   'CHECKER="scripts/check_memory_index.py"',
                   "if then fi )(")]
        previous = os.getcwd()
        os.chdir(REPO_DIR)
        try:
            environment = dict(os.environ)
            environment.pop("MEMCHECK_REQUIRE_SH", None)
            with unittest.mock.patch.object(mutation_check, "find_shell",
                                            lambda: None), \
                    unittest.mock.patch.dict(os.environ, environment,
                                             clear=True), \
                    unittest.mock.patch.object(mutation_check, "MUTATIONS",
                                               broken):
                stale = mutation_check.missing_anchors()
        finally:
            os.chdir(previous)
        self.assertEqual(stale, [], stale)

    def test_a_mutant_that_does_not_compile_is_a_hard_error(self):
        """Правило: негодный мутант - ошибка, как и протухший якорь.

        Проверяем сам сторож, а не список: подсовываем заведомо негодную
        мутацию и требуем, чтобы `missing_anchors` её назвал. Без этого
        правило держалось бы на внимательности того, кто мутацию пишет, -
        а она уже подводила трижды.
        """
        broken = [("выдуманная мутация", mutation_check.LINTER,
                   "def wiki_targets(line):",
                   "def wiki_targets(line)")]
        previous = os.getcwd()
        os.chdir(REPO_DIR)
        try:
            with unittest.mock.patch.object(mutation_check, "MUTATIONS", broken):
                stale = mutation_check.missing_anchors()
        finally:
            os.chdir(previous)
        self.assertEqual(len(stale), 1, stale)
        self.assertIn("не компилируется", stale[0][1])


class TheSuiteRunsApartFromUs(unittest.TestCase):
    """`run_apart` исполняется по-настоящему, а не только подменяется.

    Тесты в классе выше подменяют её целиком. Подмена скрывает всё, о чём
    её не спросили: сама функция вместе с `kill_tree` не выполнялась ни
    разу, хотя обе добавлены ради того, чтобы по таймауту не выживали
    внуки. Здесь запускаются настоящие процессы.
    """

    def test_a_hung_grandchild_dies_with_the_timeout(self):
        """Дерево «потомок → внук»: по таймауту гибнет всё.

        Метку внук ДОПИСЫВАЕТ, а не создаёт один раз. Первая редакция этого
        теста только проверяла, что метка появилась, - и проходила одинаково
        с работающим `kill_tree` и с заглушкой вместо него: единственным
        различием было время, а времени тест не спрашивал. Тест из
        «пустышки по подмене» стал «пустышкой по исполнению», на уровень
        глубже. Теперь спрашиваем оба наблюдаемых признака: сколько заняло и
        растёт ли метка ПОСЛЕ таймаута.
        """
        if os.name == "nt":
            apart = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            apart = {"start_new_session": True}
        marker = os.path.join(tempfile.mkdtemp(prefix="apart-"), "alive.txt")
        self.addCleanup(shutil.rmtree, os.path.dirname(marker), True)
        grandchild = (
            "import time\n"
            "for _ in range(600):\n"
            "    open(%r,'a').write('x')\n"
            "    time.sleep(0.1)\n" % marker)
        child = ("import subprocess,sys;"
                 "subprocess.run([sys.executable,'-c',%r])" % grandchild)
        started = time.time()
        with unittest.mock.patch.object(mutation_check, "SUITE_TIMEOUT", 3):
            with self.assertRaises(subprocess.TimeoutExpired):
                mutation_check.run_apart(
                    [sys.executable, "-c", child], apart,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        spent = time.time() - started
        self.assertTrue(os.path.exists(marker), "внук вообще не стартовал")
        self.assertLess(spent, 20,
                        "прогон занял %.1f с - внук пережил таймаут" % spent)
        was = os.path.getsize(marker)
        time.sleep(1.5)
        self.assertEqual(os.path.getsize(marker), was,
                         "метка растёт после таймаута - внук жив")

    def test_an_interrupt_kills_the_child_too(self):
        """Не только таймаут: любое исключение уносит потомка с собой.

        `subprocess.run`, который эта обёртка заменила, убивает потомка на
        ЛЮБОМ исключении. Первая редакция ловила только таймаут - и на Ctrl+C
        оставляла живой набор и открытый дескриптор. Складывается это
        скверно: своя группа процессов выводит набор из-под Ctrl+C терминала,
        поэтому прерывание убивает только инструмент, а осиротевший набор
        продолжает крутиться на дереве, которое под ним переписывает
        `finally`.
        """
        if os.name == "nt":
            apart = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            apart = {"start_new_session": True}
        seen = {}
        real = subprocess.Popen

        def remember(*args, **kwargs):
            process = real(*args, **kwargs)
            seen["process"] = process
            first = process.communicate

            def interrupted(*a, **k):
                # Прерывание прилетает ровно один раз - на первом ожидании.
                # Уборка внутри обёртки должна успеть довести дело до конца.
                process.communicate = first
                raise KeyboardInterrupt()

            process.communicate = interrupted
            return process

        with unittest.mock.patch.object(mutation_check.subprocess, "Popen",
                                        remember):
            with self.assertRaises(KeyboardInterrupt):
                mutation_check.run_apart(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    apart, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        process = seen.get("process")
        self.assertIsNotNone(process, "потомок не запускался")
        process.wait(timeout=10)
        self.assertIsNotNone(process.returncode, "потомок пережил прерывание")

    def test_a_quick_command_comes_back_with_its_output(self):
        """Вторая половина пары: обычный запуск возвращает вывод и код.

        Иначе первая доказывала бы только, что функция умеет падать.
        """
        if os.name == "nt":
            apart = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            apart = {"start_new_session": True}
        done = mutation_check.run_apart(
            [sys.executable, "-c", "print('privet')"], apart,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"privet", done.stdout)

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
        # LINTER подменяем: восстановление правит только два своих файла, и
        # песочница должна выдать себя за один из них.
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            # Отпечаток передаём: без него срабатывает ветка «поля нет,
            # восстанавливаем как раньше», и сравнение отпечатков не
            # проверялось НИ ОДНИМ тестом - только с отрицательной стороны.
            # Замени условие на «всегда отказывать» - набор остался бы
            # зелёным, а инструмент перестал бы убирать за собой навсегда.
            mutation_check.save_backup(target, "ОРИГИНАЛ\n", "\n", "МУТАЦИЯ\n")
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

    def test_a_file_someone_else_already_fixed_is_left_alone(self):
        """Восстановление возвращает только СВОЮ мутацию.

        Иначе механизм, поставленный охранять целостность дерева, оказывался
        единственным в репозитории, кто способен уничтожить незакоммиченную
        работу: прогон оборвали, человек сам сделал `git checkout`, час
        правил линтер - а следующий запуск молча вернул содержимое до
        обрыва и удалил единственную копию. Слепок лежит в .gitignore,
        поэтому в `git status` его не видно.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            mutation_check.save_backup(target, "ОРИГИНАЛ\n", "\n", "МУТАЦИЯ\n")
            with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("ЧАС РАБОТЫ\n")        # человек уже починил сам
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()

        self.assertFalse(restored)
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ЧАС РАБОТЫ\n", "чужую работу затёрли")
        self.assertTrue(os.path.exists(backup),
                        "слепок удалён - восстановить оригинал больше нечем")
        self.assertIn("НЕ та мутация", printed.getvalue())

    def test_a_backup_pointing_at_a_stranger_is_not_obeyed(self):
        """Путь берётся из файла на диске, а файл может быть чей угодно.

        Инструмент правит ровно два файла - значит и восстанавливать должен
        только их. И слепок при этом НЕ удалять: прежде эта ветка стирала
        единственную копию оригинала и молчала. Сценарий не выдуманный -
        слепок, записанный на Windows, называет `scripts\\check.py`, а на
        Linux `os.path.normpath` обратный слэш не трогает, и путь переставал
        совпадать сам с собой.
        """
        folder, target = self.sandbox()
        stranger = os.path.join(folder, "postoronnii.txt")
        with io.open(stranger, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("ЧУЖОЕ\n")
        backup = os.path.join(folder, ".mutation-backup")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            mutation_check.save_backup(stranger, "ПОДМЕНА\n", "\n",
                                       "МУТАЦИЯ\n")
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()

        self.assertFalse(restored)
        with io.open(stranger, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ЧУЖОЕ\n")
        self.assertTrue(os.path.exists(backup),
                        "слепок удалён - восстановить оригинал больше нечем")
        self.assertIn("постороннего файла", printed.getvalue())

    def test_a_windows_style_path_in_the_backup_is_still_ours(self):
        """Вторая половина: свой файл, записанный с обратным слэшем.

        Прогон оборвали на Windows, следующий запуск - из WSL по тому же
        дереву. Пока сравнение шло через `os.path.normpath`, путь переставал
        совпадать сам с собой, срабатывала ветка «посторонний файл», и
        мутация оставалась в дереве.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        windows_style = target.replace("/", "\\")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            mutation_check.save_backup(windows_style, "ОРИГИНАЛ\n", "\n",
                                       "МУТАЦИЯ\n")
            with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("МУТАЦИЯ\n")
            with redirect_stdout(io.StringIO()):
                restored = mutation_check.restore_interrupted()

        self.assertTrue(restored, "свой же путь принят за посторонний")
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ОРИГИНАЛ\n")

    def test_the_backup_names_the_file_it_does_not_address_it(self):
        """Путь из слепка - это опознание, а не адрес записи.

        Проверка на ВОЗВРАЩАЕМОМ ЗНАЧЕНИИ, а не на эффекте восстановления,
        и это не педантизм: эффект расходится только там, где обратный слэш
        не разделитель, то есть на POSIX. Тест соседа
        (`..._windows_style_path_...`) поймал дефект на ubuntu и на Windows
        проходил при ОБЕИХ реализациях - там писать по строке из слепка так
        же безобидно, как по своей. Этот вход различает их на любой системе.
        """
        folder, target = self.sandbox()
        # Форма входа выбрана так, чтобы она РАСХОДИЛАСЬ с каноническим
        # путём на любой системе. Первая редакция брала
        # `target.replace("/", "\\")` - и на Windows в пути прямых слэшей
        # нет вовсе, замена не меняла ничего, строки совпадали, и тест
        # оставался зелёным на сломанной реализации. Проверено откатом.
        odd_form = os.path.join(folder, ".", os.path.basename(target))
        self.assertNotEqual(odd_form, target, "вход не отличается от канона")
        with unittest.mock.patch.object(mutation_check, "LINTER", target):
            self.assertEqual(mutation_check.resolve_ours(odd_form), target,
                             "вернулась строка из слепка, а не свой путь")
            # Вторая половина пары: посторонний файл своим не становится.
            self.assertIsNone(mutation_check.resolve_ours(
                os.path.join(folder, "postoronnii.py")))

    def test_the_file_is_addressed_by_our_name_not_by_the_backup_string(self):
        """Опознание возвращает своё имя - и восстановление им ПОЛЬЗУЕТСЯ.

        Сосед проверяет возвращаемое значение, а это - обращения к диску, и
        разница между ними не педантизм: убери из `restore_interrupted`
        строку `path = ours` - то есть ровно ту починку, ради которой всё и
        писалось, - и на Windows набор остаётся ЗЕЛЁНЫМ. Ловил её только
        тест про windows-стиль и только на POSIX, то есть половина матрицы
        CI не проверяла половину починки. Здесь смотрим на адрес, а адрес
        от системы не зависит.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        odd_form = os.path.join(folder, ".", os.path.basename(target))
        addressed = []

        real_read = mutation_check.read_source
        real_write = mutation_check.write_atomically

        def spy_read(path):
            addressed.append(path)
            return real_read(path)

        def spy_write(path, text, newline="\n"):
            addressed.append(path)
            return real_write(path, text, newline)

        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            mutation_check.save_backup(odd_form, "ОРИГИНАЛ\n", "\n",
                                       "МУТАЦИЯ\n")
            with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("МУТАЦИЯ\n")
            with unittest.mock.patch.object(
                    mutation_check, "read_source", spy_read), \
                    unittest.mock.patch.object(
                        mutation_check, "write_atomically", spy_write), \
                    redirect_stdout(io.StringIO()):
                mutation_check.restore_interrupted()

        # Слепок пишется до всего этого, поэтому в списке только цель.
        self.assertEqual(addressed, [target, target],
                         "файл адресован строкой из слепка, а не своим именем")

    def test_the_hook_is_restored_too_not_only_the_linter(self):
        """Инструмент правит ДВА файла - восстанавливать обязан оба.

        Все соседние тесты подменяют только LINTER, поэтому вторая половина
        перебора в `resolve_ours` не держалась ничем: сузь его до одного
        файла - и набор останется зелёным, а оборванный прогон по хуку
        оставит мутацию в дереве навсегда.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER",
                                           os.path.join(folder, "drugoi.py")), \
                unittest.mock.patch.object(mutation_check, "HOOK", target):
            mutation_check.save_backup(target, "ОРИГИНАЛ\n", "\n",
                                       "МУТАЦИЯ\n")
            with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("МУТАЦИЯ\n")
            with redirect_stdout(io.StringIO()):
                restored = mutation_check.restore_interrupted()

        self.assertTrue(restored, "слепок хука признан посторонним")
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ОРИГИНАЛ\n")

    def test_a_file_that_vanished_is_restored_not_blamed(self):
        """Файла нет - это ровно тот случай, ради которого слепок и лежит.

        Ветка `current is None` единственная проходит МИМО обоих сторожей
        («совпало с оригиналом» и «не моя мутация») и пишет вслепую, а
        держалась ничем: замени отдельный перехват `FileNotFoundError` на
        общий отказ - и набор остаётся зелёным, а пропавший файл больше
        никогда не восстанавливается.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            mutation_check.save_backup(target, "ОРИГИНАЛ\n", "\n",
                                       "МУТАЦИЯ\n")
            os.remove(target)          # прогон убили между записью и правкой
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()

        self.assertTrue(restored, "пропавший файл не восстановлен")
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ОРИГИНАЛ\n")
        self.assertNotIn("не знаю, что там лежит", printed.getvalue())

    def test_an_empty_name_in_the_backup_is_refused(self):
        """Пустое имя - не «наш файл по умолчанию».

        Отсечка пустого имени стоит перед опознанием, и без неё пустая
        строка уходит в `resolve_ours`, где `abspath("")` даёт текущий
        каталог. Ни один тест этого не держал.
        """
        folder, _target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with io.open(backup, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\tlf\totpechatok\nОРИГИНАЛ\n")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup):
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()

        self.assertFalse(restored)
        self.assertTrue(os.path.exists(backup))
        self.assertIn("имя пустое", printed.getvalue())

    def test_a_backup_without_a_fingerprint_is_refused(self):
        """Старый формат слепка - отказ, а не «сторож пропускается».

        В репозитории было два прежних формата шапки, и сегодняшний разбор
        оба читает как «отпечатка нет». Пока условие сторожа начиналось с
        `left and`, такой слепок ОТКЛЮЧАЛ проверку «в дереве не моя
        мутация» - и час чужой работы затирался содержимым месячной
        давности, с сообщением об успехе.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with io.open(backup, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("%s\tlf\nОРИГИНАЛ\n" % target)
        with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("ЧАС РАБОТЫ\n")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()

        self.assertFalse(restored)
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ЧАС РАБОТЫ\n", "чужую работу затёрли")
        self.assertTrue(os.path.exists(backup))
        self.assertIn("нет отпечатка", printed.getvalue())

    def test_an_unknown_line_ending_in_the_backup_is_refused(self):
        """Незнакомое слово в поле переводов строк - отказ, а не молчаливый LF.

        `NEWLINE_BY_NAME.get(kind, "\n")` чинил бы одно, переписав другое:
        файл с CRLF вернулся бы в дерево целиком в LF - ровно тот ущерб,
        ради которого поле и заведено.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with io.open(backup, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("%s\tнечто\totpechatok\nОРИГИНАЛ\n" % target)
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()

        self.assertFalse(restored)
        self.assertTrue(os.path.exists(backup))
        self.assertIn("незнакомый вид переводов строк", printed.getvalue())

    def test_a_backup_left_behind_stops_the_run(self):
        """Слепок остался - прогон не начинается.

        Возврат `restore_interrupted()` в `main()` отбрасывался, и «не
        трогаю, разберитесь руками» ничего не останавливало: прогон шёл
        дальше, первая же мутация перезаписывала слепок, `finally` его
        удалял, и инструмент выходил с нулём, отрапортовав полный успех.
        Обещание сохранить единственную копию оригинала жило полторы
        секунды.
        """
        folder, _target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with io.open(backup, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("postoronnii.txt\tlf\totpechatok\nЧУЖОЕ\n")
        # LINTER - путь ОТНОСИТЕЛЬНО корня, и main первым делом проверяет,
        # что файл на месте. Без этого тест уходит в ветку «запускать из
        # корня репозитория» и проверяет не то, что написано в имени.
        here = os.getcwd()
        os.chdir(REPO_DIR)
        self.addCleanup(os.chdir, here)
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup):
            with redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()) as complained:
                code = mutation_check.main([])

        self.assertEqual(code, 1, "прогон пошёл поверх чужого слепка")
        self.assertTrue(os.path.exists(backup), "слепок затёрт прогоном")
        self.assertIn("запускать нельзя", complained.getvalue())

    def test_an_interruption_before_the_mutation_landed_is_quiet(self):
        """Обрыв ДО применения мутации - не «кто-то уже поправил».

        Отпечаток кладётся в слепок раньше, чем мутация ложится на диск.
        Поэтому обрыв в этом окне давал ложное обвинение, а обещанный
        «слепок оригинала» затирался первой же следующей мутацией.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            mutation_check.save_backup(target, "ОРИГИНАЛ\n", "\n", "МУТАЦИЯ\n")
            # мутацию записать не успели - на диске оригинал
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()

        self.assertFalse(restored)
        self.assertNotIn("кто-то", printed.getvalue())
        self.assertFalse(os.path.exists(backup), "слепок остался висеть")
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ОРИГИНАЛ\n")

    def test_an_unreadable_target_is_left_alone(self):
        """Не знаем, что на диске - не трогаем.

        Оба сторожа были написаны как «current is not None», поэтому отказ
        чтения проваливался прямо в перезапись: механизм, охраняющий
        целостность дерева, затирал чужую работу, удалял единственную копию
        и рапортовал «восстановлен из слепка». Починку третьего круга не
        держал ни один тест - её можно было молча откатить.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        original = mutation_check.read_source

        def refusing(path):
            if os.path.normcase(path) == os.path.normcase(target):
                raise PermissionError(13, "file busy")
            return original(path)

        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target), \
                unittest.mock.patch.object(mutation_check, "read_source",
                                           refusing):
            mutation_check.save_backup(target, "ОРИГИНАЛ\n", "\n", "МУТАЦИЯ\n")
            with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("ЧАС РАБОТЫ\n")
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()

        self.assertFalse(restored)
        self.assertIn("не знаю, что там лежит", printed.getvalue())
        self.assertTrue(os.path.exists(backup), "слепок удалён")
        with io.open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ЧАС РАБОТЫ\n", "чужую работу затёрли")

    def test_a_corrupt_backup_does_not_crash_the_tool(self):
        """Битый слепок - сообщение, а не трейсбек до первого слова.

        Инструмент читает слепок как UTF-8, а файл на диске может быть каким
        угодно. Прежде `UnicodeDecodeError` вылетал наружу раньше, чем
        инструмент успевал что-либо сказать.
        """
        folder, target = self.sandbox()
        backup = os.path.join(folder, ".mutation-backup")
        with open(backup, "wb") as fh:
            fh.write(b"\xff\xfe\x00\x01 not utf-8 at all")
        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", target):
            with redirect_stdout(io.StringIO()) as printed:
                restored = mutation_check.restore_interrupted()
        self.assertFalse(restored)
        self.assertIn("не читается", printed.getvalue())
        self.assertTrue(os.path.exists(backup), "битый слепок молча удалён")

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

        with unittest.mock.patch.object(mutation_check, "BACKUP", backup), \
                unittest.mock.patch.object(mutation_check, "LINTER", path):
            text, seen = mutation_check.read_source(path)
            mutation_check.save_backup(path, text, seen, "МУТАЦИЯ\n")
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("МУТАЦИЯ\n")          # здесь прогон оборвали
            with redirect_stdout(io.StringIO()):
                mutation_check.restore_interrupted()

        with open(path, "rb") as fh:
            self.assertEqual(fh.read().count(b"\r\n"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
