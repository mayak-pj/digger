# -*- coding: utf-8 -*-
"""Проверочный набор для рабочего ПК (Win7): запускает проверки по очереди, собирает отчёт_<дата>.zip.

    python checks/run_checks.py [--only имя ...] [--out папка]

Проверка — модуль .py рядом с этим файлом, в котором есть `run(ctx) -> dict`. Ошибка одной проверки не
останавливает остальные. Результат: results.json, отчёт.md, logs/, ЧТО СДЕЛАТЬ.txt — всё в одном zip.
"""
import argparse
import datetime
import importlib.util
import json
import os
import shutil
import sys
import time
import traceback
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
# остальные проверки — по алфавиту после этих
ORDER = ["sysinfo", "selftest", "window", "ppocr_check", "program_ocr", "sites", "adapters", "downloads", "update", "screens", "live"]
SKIP_FILES = {"run_checks", "live_check"}
MAX_LOG = 5 * 1024 * 1024
TODO_FILE = "ЧТО СДЕЛАТЬ.txt"
# сколько обычно идёт проверка, минут (по отчёту выезда 3: 120 фото, 12 эталонных чипов) — подсказка в окне
ABOUT_MINUTES = {"selftest": 10, "ppocr_check": 50, "program_ocr": 15, "sites": 3, "adapters": 5, "downloads": 30, "update": 1,
                 "screens": 3, "live": 45}
NOTES_FILE = "впечатления.txt"      # пользователь пишет впечатления от окна; файл попадает в отчёт


def default_app_dir(here=HERE):
    """В сборке checks/ лежит в папке программы; в исходниках (tools/win7_pack) — на два уровня выше."""
    if os.path.basename(here) == "checks":
        return os.path.dirname(here)
    return os.path.dirname(os.path.dirname(here))


PHOTO_EXT = (".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".webp")
PHOTO_NAMES = ("фото", "Фото", "ФОТО", "photos", "photo")
PHOTO_ASK = 3             # столько раз окно проверки предложит положить фото, прежде чем идти дальше без них


def photo_places(app_dir):
    """Где ищем фото для проверок распознавания: «фото» рядом с программой, а также в tmp/ и checks/
    (туда папку клали на выезде 2, и проверка её не увидела). Первая — основная, её создаёт сборка."""
    out = []
    for base in (app_dir, os.path.join(app_dir, "tmp"), os.path.join(app_dir, "checks")):
        for name in PHOTO_NAMES:
            p = os.path.join(base, name)
            if os.path.normcase(p) not in [os.path.normcase(x) for x in out]:
                out.append(p)
    return out


def scan_place(path):
    """Что лежит в папке: сколько картинок и какие ещё файлы (по расширениям) — видно, почему фото «не видны»."""
    row = {"path": path, "exists": os.path.isdir(path), "images": 0, "other": {}}
    if not row["exists"]:
        return row
    for _d, dirs, files in os.walk(path):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if fn.startswith(".") or ext == ".txt" or fn.lower() == "ответы.csv":
                continue
            if ext in PHOTO_EXT:
                row["images"] += 1
            else:
                row["other"][ext or "без расширения"] = row["other"].get(ext or "без расширения", 0) + 1
    return row


def find_photos(app_dir):
    """(папка с фото, [что найдено в каждом месте]). Нет нигде — основная папка «фото» рядом с программой."""
    rows = []
    for p in photo_places(app_dir):
        same = any(os.path.isdir(p) and os.path.isdir(r["path"]) and os.path.samefile(p, r["path"]) for r in rows)
        if not same:              # «фото» и «Фото» — одна папка там, где регистр имён не различается
            rows.append(scan_place(p))
    for row in rows:
        if row["images"]:
            return row["path"], rows
    return rows[0]["path"], rows


def photos_text(rows):
    lines = []
    for r in rows:
        if not r["exists"]:
            continue
        other = ", ".join("%s — %d" % kv for kv in sorted(r["other"].items()))
        lines.append("%s: картинок %d%s" % (r["path"], r["images"],
                                            ("; не картинки (не читаются): " + other) if other else ""))
    return lines or ["папки «фото» нет ни в одном из мест: " + "; ".join(r["path"] for r in rows[:1])]


def ask_for_photos(app_dir, ask=input, log=print, opener=None):
    """Перед проверками: фото нет — показать точный путь, открыть папку и подождать, пока их положат.
    Пустой выезд из-за пустой папки больше не повторится. Возвращает число найденных картинок."""
    for attempt in range(PHOTO_ASK + 1):
        path, rows = find_photos(app_dir)
        found = sum(r["images"] for r in rows if r["path"] == path)
        if found:
            log("Фото для проверки распознавания: %d шт. в папке %s" % (found, path))
            return found
        if attempt == PHOTO_ASK:
            break
        os.makedirs(path, exist_ok=True)
        log("")
        log("ВНИМАНИЕ: нет фото для проверки распознавания.")
        for line in photos_text(rows):
            log("  " + line)
        log("Положите снимки чипов (JPG или PNG; правильная маркировка — в имени файла) в папку:")
        log("  " + path)
        try:
            (opener or getattr(os, "startfile", lambda p: None))(path)
        except Exception:  # noqa — не открылась в Проводнике: путь напечатан
            pass
        try:
            ask("Когда положите — нажмите Enter. Проверять без фото — тоже Enter (спрошу %d раз): "
                % (PHOTO_ASK - attempt))
        except EOFError:
            break
    log("Фото не найдены — проверки распознавания будут пропущены.")
    return 0


def duration(seconds):
    """Длительность для окна проверки: «45 с», «12 мин 05 с», «1 ч 38 мин»."""
    s = int(round(seconds))
    if s < 60:
        return "%d с" % s
    if s < 3600:
        return "%d мин %02d с" % (s // 60, s % 60)
    return "%d ч %02d мин" % (s // 3600, s % 3600 // 60)


class Context(object):
    def __init__(self, app_dir, work_dir, name=""):
        self.app_dir = app_dir
        self.work_dir = work_dir           # сюда проверка складывает файлы для отчёта
        self.name = name
        self.python = sys.executable
        self.photos_dir = find_photos(app_dir)[0]
        self.todo_items = []

    def todo(self, text):
        """Попросить пользователя о ручном действии (попадёт в «ЧТО СДЕЛАТЬ.txt» отчёта)."""
        self.todo_items.append(text)


def discover(checks_dir):
    names = sorted(f[:-3] for f in os.listdir(checks_dir)
                   if f.endswith(".py") and not f.startswith("_") and f[:-3] not in SKIP_FILES)
    first = [n for n in ORDER if n in names]
    return first + [n for n in names if n not in first]


def load_module(checks_dir, name):
    spec = importlib.util.spec_from_file_location("check_" + name, os.path.join(checks_dir, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_one(checks_dir, name, app_dir, out_dir):
    work = os.path.join(out_dir, name)
    os.makedirs(work, exist_ok=True)
    ctx = Context(app_dir, work, name)
    t0 = time.time()
    try:
        mod = load_module(checks_dir, name)
        run = getattr(mod, "run", None)
        if not callable(run):
            return None, ctx
        res = run(ctx)
        res = dict(res) if isinstance(res, dict) else {"result": res}
        res.setdefault("status", "ok")
    except BaseException as e:  # noqa — SystemExit/KeyboardInterrupt одной проверки не должны ронять набор
        res = {"status": "error", "error": "%s: %s" % (type(e).__name__, e), "traceback": traceback.format_exc()}
    res["name"] = name
    res["seconds"] = round(time.time() - t0, 2)
    return res, ctx


def run_all(app_dir, checks_dir=HERE, out_dir=None, only=None, log=print):
    out_dir = out_dir or os.path.join(app_dir, "tmp", "report")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    results, todo = [], []
    names = [n for n in discover(checks_dir) if not only or n in only]
    started = time.time()
    for i, name in enumerate(names, 1):
        about = ABOUT_MINUTES.get(name)
        log("== проверка %d из %d: %s%s ..." % (i, len(names), name, " (обычно до %d мин)" % about if about else ""))
        res, ctx = run_one(checks_dir, name, app_dir, out_dir)
        if res is None:
            continue
        log("   %s (%s)%s; всего прошло %s" % (res["status"], duration(res["seconds"]),
                                             ": " + res["error"] if res.get("error") else "",
                                             duration(time.time() - started)))
        results.append(res)
        todo.extend(ctx.todo_items)
    return results, todo


def report_md(results, todo, photos=None):
    lines = ["# Отчёт проверочного набора", "", "Дата: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "",
             "| Проверка | Итог | Время, с | Замечание |", "|---|---|---|---|"]
    for r in results:
        lines.append("| %s | %s | %s | %s |" % (r["name"], r["status"], r["seconds"],
                                                 str(r.get("error", r.get("note", ""))).replace("|", "/")[:120]))
    if photos:
        lines += ["", "## Где искали фото", ""] + ["- " + t for t in photos]
    if todo:
        lines += ["", "## Что сделать вручную", ""] + ["- " + t for t in todo]
    return "\n".join(lines) + "\n"


def free_zip_path(app_dir, today=None):
    day = today or datetime.date.today().isoformat()
    path = os.path.join(app_dir, "отчёт_%s.zip" % day)
    n = 2
    while os.path.exists(path):
        path = os.path.join(app_dir, "отчёт_%s_%d.zip" % (day, n))
        n += 1
    return path


def build_report(app_dir, out_dir, results, todo, checks_dir=HERE):
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(out_dir, "отчёт.md"), "w", encoding="utf-8") as f:
        f.write(report_md(results, todo, photos_text(find_photos(app_dir)[1])))
    todo_text = ""
    static = os.path.join(checks_dir, TODO_FILE)
    if os.path.isfile(static):
        with open(static, encoding="utf-8") as f:
            todo_text = f.read().rstrip() + "\n"
    if todo:
        todo_text += "\nПо итогам проверок:\n" + "".join("- %s\n" % t for t in todo)
    with open(os.path.join(out_dir, TODO_FILE), "w", encoding="utf-8") as f:
        f.write(todo_text)
    path = free_zip_path(app_dir)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for base in (app_dir, os.path.join(app_dir, "checks")):          # «впечатления.txt» рядом с программой
            notes = os.path.join(base, NOTES_FILE)
            if os.path.isfile(notes):
                z.write(notes, NOTES_FILE)
                break
        for d, _dirs, files in os.walk(out_dir):
            for fn in sorted(files):
                p = os.path.join(d, fn)
                z.write(p, os.path.relpath(p, out_dir).replace(os.sep, "/"))
        logs = os.path.join(app_dir, "logs")
        if os.path.isdir(logs):
            for d, _dirs, files in os.walk(logs):
                for fn in sorted(files):
                    p = os.path.join(d, fn)
                    if os.path.getsize(p) <= MAX_LOG:
                        z.write(p, "logs/" + os.path.relpath(p, logs).replace(os.sep, "/"))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="*", help="запустить только эти проверки")
    ap.add_argument("--app-dir", default=None)
    ap.add_argument("--out", default=None, help="временная папка для файлов проверок")
    ap.add_argument("--no-ask", action="store_true", help="не ждать, пока положат фото (CI)")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa
        pass
    app_dir = os.path.abspath(a.app_dir or default_app_dir())
    out_dir = os.path.abspath(a.out) if a.out else os.path.join(app_dir, "tmp", "report")
    if not a.no_ask and sys.stdin is not None and sys.stdin.isatty():
        ask_for_photos(app_dir)
    results, todo = run_all(app_dir, HERE, out_dir, a.only)
    path = build_report(app_dir, out_dir, results, todo, HERE)
    print("\nГотово: %s" % path)
    print("Принесите этот файл домой (положить в my_reports/).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
