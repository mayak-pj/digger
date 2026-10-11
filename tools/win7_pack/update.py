# -*- coding: utf-8 -*-
"""Проверка update: скрипт обновления портативной сборки (шаг 9.5c) на настоящем robocopy и cmd Windows 7.

Во временной папке с русскими буквами и пробелом строится «рабочая копия» и «новая сборка»; запускается тот же
`.bat`, что и при обновлении. Проверяется: программа заменилась, `config.json`, библиотека, база и карантин
остались, лишние старые файлы программы удалены, журнал robocopy на месте. Данные пользователя не трогаются.
"""
import os
import shutil
import subprocess
import sys



def _put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text)


def run(ctx):
    if os.name != "nt":
        return {"status": "skip", "reason": "только Windows"}
    sys.path.insert(0, ctx.app_dir)
    from digger.core import updater
    base = os.path.join(ctx.work_dir, "проба обновления")
    shutil.rmtree(base, ignore_errors=True)
    app, staged = os.path.join(base, "Рабочая папка", "Digger"), os.path.join(base, "новая", "Digger")
    for rel, text in (("run.py", b"old"), ("digger/old_only.py", b"x"), ("digger/core.py", b"old"),
                      ("config.json", b"{}"), ("data/library/confirmed/NE/a.pdf", b"pdf"),
                      ("data/digger.sqlite", b"db"), ("data/quarantine/q.pdf", b"q"), ("data/sources.json", b"old")):
        _put(os.path.join(app, *rel.split("/")), text)
    for rel, text in (("run.py", b"new"), ("digger/core.py", b"new"), ("data/sources.json", b"new"),
                      ("data/library/.keep", b""), ("Digger.bat", b"@echo off\r\n")):
        _put(os.path.join(staged, *rel.split("/")), text)
    res = {"status": "fail", "robocopy": shutil.which("robocopy") or "не найден"}
    bat = updater.write_apply_script(app, staged, pid=1)           # процесс 1 не существует — ждать нечего
    try:
        p = subprocess.Popen(["cmd.exe", "/c", bat], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        out, _ = p.communicate(timeout=120)
        res["returncode"] = p.returncode
        res["output"] = out.decode("cp866", "replace")[-1500:]
    except Exception as e:  # noqa
        res["error"] = "%s: %s" % (type(e).__name__, e)
        return res

    def read(rel):
        try:
            with open(os.path.join(app, *rel.split("/")), "rb") as f:
                return f.read()
        except OSError:
            return None
    res["checks"] = {
        "программа заменена": read("run.py") == b"new" and read("digger/core.py") == b"new",
        "лишний старый файл удалён": read("digger/old_only.py") is None,
        "данные программы обновлены": read("data/sources.json") == b"new",
        "config.json цел": read("config.json") == b"{}",
        "библиотека цела": read("data/library/confirmed/NE/a.pdf") == b"pdf",
        "база цела": read("data/digger.sqlite") == b"db",
        "карантин цел": read("data/quarantine/q.pdf") == b"q",
        "журнал robocopy есть": os.path.isfile(os.path.join(app, "updates", "обновление.txt")),
        "отметка «готово»": os.path.isfile(os.path.join(app, "updates", "готово.txt")),
    }
    res["status"] = "ok" if all(res["checks"].values()) else "fail"
    return res
