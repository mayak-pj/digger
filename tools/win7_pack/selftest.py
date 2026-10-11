# -*- coding: utf-8 -*-
"""Проверка selftest: `run.py --selftest` встроенным Python, полный вывод — в файл отчёта.

Если самопроверка не завершилась, а оборвалась (код не из кодов pytest: аварийный выход модуля Qt, onnxruntime…),
она повторяется подробно (`-vv`, вывод без буфера, faulthandler): последний начатый тест — виновник. Он
исключается, и остальные тесты прогоняются снова — до `MAX_CRASHES` раз. Так за один выезд известны и все
тесты, обрывающие самопроверку, и итог остальных. Подробный вывод — `selftest_verbose.txt`.

Затем причина обрыва собирается сама (`selftest_crash.txt`, поля `qt_probe` и `crash_runs`):
* проба Qt — пустое окно в отдельном процессе во всех сочетаниях «модуль экрана `windows` / `offscreen`» ×
  «путь к модулям Qt задан явно / не задан», с отладочным выводом Qt в stderr (на Windows он иначе не виден);
* каждый тест-виновник — отдельным процессом в режимах `windows` и `offscreen`, с тем же выводом Qt.
"""
import os
import re
import subprocess
import sys

TIMEOUT = 900
RUN_TIMEOUT = 120         # один тест или одна проба Qt; ошибка Qt может показать окно и ждать «OK»
MAX_CRASHES = 5
_DONE = re.compile(r"\b(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b")
# вывод Qt — в stderr (на Windows сообщение перед аварийным выходом иначе уходит отладчику), поиск модулей — подробно
QT_DEBUG = {"QT_DEBUG_PLUGINS": "1", "QT_FORCE_STDERR_LOGGING": "1", "QT_LOGGING_TO_CONSOLE": "1"}
PLATFORMS = {"windows": {}, "offscreen": {"QT_QPA_PLATFORM": "offscreen", "QT_QPA_FONTDIR": "C:/Windows/Fonts"}}
PROBE = """import os, sys
from PyQt5.QtCore import QCoreApplication, QLibraryInfo, QTimer
import PyQt5
plugins = os.path.join(os.path.dirname(os.path.abspath(PyQt5.__file__)), "Qt5", "plugins")
print("plugins: %r, platforms: %s" % (plugins, os.path.isdir(os.path.join(plugins, "platforms"))))
print("Qt PluginsPath: %r" % QLibraryInfo.location(QLibraryInfo.PluginsPath))
if sys.argv[1] == "explicit":
    QCoreApplication.addLibraryPath(plugins)
print("libraryPaths: %r" % QCoreApplication.libraryPaths())
sys.stdout.flush()
from PyQt5.QtWidgets import QApplication, QWidget
app = QApplication([])
w = QWidget()
w.show()
QTimer.singleShot(200, app.quit)
app.exec_()
print("QT_PROBE_OK platform=%s" % app.platformName())
"""


def _exec(cmd, cwd, env=None, timeout=TIMEOUT):
    """(код, вывод); `env` — полное окружение процесса (по умолчанию — своё)."""
    env = dict(env if env is not None else os.environ, PYTHONUNBUFFERED="1", PYTHONFAULTHANDLER="1")
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return None, (e.stdout or b"").decode("utf-8", "replace") + "\n[таймаут %d с]" % timeout


def _run(ctx, extra=(), env=None, timeout=TIMEOUT):
    return _exec([ctx.python, os.path.join(ctx.app_dir, "run.py"), "--selftest"] + list(extra), ctx.app_dir, env, timeout)


def qt_env(platform):
    """Окружение режима экрана с отладочным выводом Qt. `windows` — модуль по умолчанию: унаследованный
    QT_QPA_PLATFORM снимается, иначе он подменил бы режим."""
    env = dict(os.environ, **QT_DEBUG)
    env.pop("QT_QPA_PLATFORM", None)
    env.update(PLATFORMS[platform])
    return env


def _tail(text, n=3):
    return [s for s in text.splitlines() if s.strip()][-n:]


def crashed(rc):
    """Оборвалась, а не завершилась: коды pytest — 0…5."""
    return rc is not None and rc not in range(6)


def last_started(text):
    """Тест, на котором оборвался подробный вывод: последняя строка `файл::тест` без итога."""
    lines = [s.strip() for s in text.splitlines() if "::" in s and not s.startswith(" ")]
    if not lines or _DONE.search(lines[-1]):
        return ""
    return lines[-1].split(" ")[0]


def qt_probe(ctx, log):
    """Пустое окно Qt отдельным процессом: модуль экрана × способ задания пути к модулям Qt."""
    script = os.path.join(ctx.work_dir, "qt_probe.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(PROBE)
    out = {}
    for platform in PLATFORMS:
        for path in ("default", "explicit"):
            rc, text = _exec([ctx.python, script, path], ctx.app_dir, qt_env(platform), RUN_TIMEOUT)
            name = "%s/%s" % (platform, path)
            out[name] = {"returncode": rc, "ok": rc == 0 and "QT_PROBE_OK" in text}
            log.write("=== проба Qt %s: код %s\n%s\n\n" % (name, rc, text))
    return out


def crash_runs(ctx, tests, log):
    """Каждый тест-виновник — своим процессом в каждом режиме экрана, с отладочным выводом Qt."""
    out = {}
    for test in tests:
        out[test] = {}
        for platform in PLATFORMS:
            rc, text = _run(ctx, ["-vv", "-s", test], qt_env(platform), RUN_TIMEOUT)
            out[test][platform] = rc
            log.write("=== тест %s, режим %s: код %s\n%s\n\n" % (test, platform, rc, text))
    return out


def verdict(probe, runs):
    """Вывод одной строкой: что именно обрывает процесс."""
    ok = sorted(k for k, v in probe.items() if v["ok"])
    bad = sorted(k for k, v in probe.items() if not v["ok"])
    if not ok:
        qt = "Qt не запускается ни в одном режиме"
    elif not bad:
        qt = "Qt запускается во всех режимах — дело в самих тестах"
    elif all(k.endswith("/explicit") for k in ok):
        qt = "Qt запускается только с явным путём к модулям (папка программы с русскими буквами?)"
    else:
        qt = "Qt запускается в режимах: %s; не запускается: %s" % (", ".join(ok), ", ".join(bad))
    alone = [t for t, r in runs.items() if any(rc in range(6) for rc in r.values())]
    return "%s; по одному не обрываются %d из %d тестов" % (qt, len(alone), len(runs))


def run(ctx):
    rc, text = _run(ctx)
    with open(os.path.join(ctx.work_dir, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    res = {"status": "ok" if rc == 0 else "fail", "returncode": rc, "timeout": rc is None, "tail": _tail(text)}
    if not crashed(rc):
        return res
    culprits = []
    rest_rc, rest_text = rc, text
    with open(os.path.join(ctx.work_dir, "selftest_verbose.txt"), "w", encoding="utf-8") as f:
        for _ in range(MAX_CRASHES + 1):
            extra = ["-vv"] + ["--deselect=" + name for name in culprits]
            rest_rc, rest_text = _run(ctx, extra)
            f.write("=== %s\n%s\n\n" % (" ".join(extra), rest_text))
            name = last_started(rest_text) if crashed(rest_rc) else ""
            if not name or name in culprits:
                break
            culprits.append(name)
    with open(os.path.join(ctx.work_dir, "selftest_crash.txt"), "w", encoding="utf-8") as f:
        f.write("python: %s\nпапка программы: %r\nплатформа: %s\n\n" % (ctx.python, ctx.app_dir, sys.platform))
        probe = qt_probe(ctx, f)
        runs = crash_runs(ctx, culprits, f)
        why = verdict(probe, runs)
        f.write("=== вывод\n%s\n" % why)
    res.update(crashed_tests=culprits, rest_returncode=rest_rc, rest_tail=_tail(rest_text), qt_probe=probe,
               crash_runs=runs, cause=why,
               note="самопроверка оборвалась (код %s) на: %s; остальные тесты — код %s; %s"
                    % (rc, ", ".join(culprits) or "не определено", rest_rc, why))
    return res
