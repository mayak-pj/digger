# -*- coding: utf-8 -*-
"""Проверка window: `run.py --smoke` и снимок главного окна в PNG (window.png в отчёте).

Снимок делает этот же файл, запущенный с `--snap файл.png` в отдельном процессе: падение Qt не роняет набор.
"""
import faulthandler
import os
import subprocess
import sys

TIMEOUT = 90      # при ошибке Qt показывает окно и ждёт «OK» — процесс убиваем по таймауту


def _run(cmd, cwd, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=TIMEOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return None, (e.stdout or b"").decode("utf-8", "replace") + "\n[таймаут %d с]" % TIMEOUT


def run(ctx):
    smoke_rc, smoke_out = _run([ctx.python, os.path.join(ctx.app_dir, "run.py"), "--smoke"], ctx.app_dir)
    png = os.path.join(ctx.work_dir, "window.png")
    snap_rc, snap_out = _run([ctx.python, os.path.abspath(__file__), "--snap", png, ctx.app_dir], ctx.app_dir)
    with open(os.path.join(ctx.work_dir, "window_log.txt"), "w", encoding="utf-8") as f:
        f.write("--smoke: код %s\n%s\n\n--snap: код %s\n%s\n" % (smoke_rc, smoke_out, snap_rc, snap_out))
    ok = smoke_rc == 0 and snap_rc == 0 and os.path.isfile(png)
    return {"status": "ok" if ok else "fail", "smoke_returncode": smoke_rc, "snap_returncode": snap_rc,
            "png": os.path.isfile(png), "snap_info": snap_out.strip().splitlines()[-1:] }


def snap(png, app_dir):
    faulthandler.enable()     # аварийный выход Qt (0xC0000005) оставит в журнале стек всех потоков, а не только код
    sys.path.insert(0, app_dir)
    os.chdir(app_dir)
    from PyQt5.QtCore import QBuffer, QCoreApplication, QIODevice, QTimer
    from PyQt5.QtWidgets import QApplication
    from digger.gui.main_window import MainWindow, qt_plugins_dir
    plugins = qt_plugins_dir()
    if plugins:
        QCoreApplication.addLibraryPath(plugins)
    app = QApplication(sys.argv[:1])
    w = MainWindow(app_dir)
    w.show()
    info = {}

    def grab():
        pm = w.grab()
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        pm.save(buf, "PNG")
        with open(png, "wb") as f:        # путь с русскими буквами: пишем сами, не через Qt
            f.write(bytes(buf.data()))
        scr = app.primaryScreen()
        info.update(window="%dx%d" % (w.width(), w.height()), screen="%dx%d" % (scr.size().width(), scr.size().height()),
                    dpi=scr.logicalDotsPerInch())
        theme = getattr(w, "theme", None)       # шаг 7.2: какие шрифты нашлись, загрузились ли стрелки QSS
        info.update(theme=theme.info if theme else None)
        print("snap: %s" % info)
        app.quit()
    QTimer.singleShot(800, grab)
    return app.exec_()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--snap":
        sys.exit(snap(sys.argv[2], sys.argv[3]))
