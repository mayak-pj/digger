# -*- coding: utf-8 -*-
"""Проверка PP-OCR (RapidOCR) и бенч точности: правильный ответ — имя файла.

В наборе для Win7: `run(ctx)` берёт картинки из `фото/` рядом с программой.
Дома на Mac:  python tools/win7_pack/ppocr_check.py my_test/ocr  →  my_reports/ocr_bench_<дата>.md
"""
import argparse
import contextlib
import csv
import datetime
import json
import os
import re
import sys
import time
import traceback
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_EXT = (".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".webp")
ROTATIONS = (0, 90, 180, 270)
CYR = re.compile("[Ѐ-ӿ]")
# (имя, text_score, det_box_thresh, det_unclip_ratio); None — параметры библиотеки по умолчанию
PARAM_SETS = [
    ("main", 0.3, 0.3, 1.8),
    ("default", None, None, None),
    ("loose", 0.2, 0.2, 2.0),
    ("strict", 0.4, 0.4, 1.6),
]


def norm(s):
    """Без пробелов, знаков и регистра; русские буквы отбрасываются."""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def expected_from_name(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    return norm(" ".join(w for w in stem.split() if not CYR.search(w)))


def is_match(line, expected):
    return bool(expected) and norm(line) == expected


def _read_answers(folder):
    path = os.path.join(folder, "ответы.csv")
    answers = {}
    if not os.path.isfile(path):
        return answers
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    delim = ";" if text.count(";") >= text.count(",") else ","
    for i, row in enumerate(csv.reader(text.splitlines(), delimiter=delim)):
        if len(row) < 2 or (i == 0 and norm(row[0]) in ("ФАЙЛ", "FILE", "")):
            continue
        answers[row[0].strip().replace("\\", "/")] = norm(row[1])
    return answers


def collect(folder):
    """[(путь, ожидаемая строка)] — папка с подпапками; ответы.csv важнее имени файла."""
    answers = _read_answers(folder)
    items = []
    for d, dirs, files in os.walk(folder):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for fn in sorted(files):
            if fn.startswith(".") or not fn.lower().endswith(IMG_EXT):
                continue
            p = os.path.join(d, fn)
            rel = os.path.relpath(p, folder).replace(os.sep, "/")
            exp = answers.get(rel, answers.get(fn))
            items.append((p, exp if exp else expected_from_name(fn)))
    return items


def imread(path):
    import cv2
    import numpy as np
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def rotate(img, deg):
    import cv2
    code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(deg)
    return img if code is None else cv2.rotate(img, code)


def _alnum_len(s):
    return len(re.findall(r"[A-Za-z0-9]", s))


def evaluate(items, engines, rotations=ROTATIONS, loader=imread, progress=None):
    """engines: {имя: f(img) -> [(текст, уверенность 0..1)]}.
    Итог по варианту: rot0 — верно на исходном фото; any — верно при каком-то повороте;
    auto — верно при повороте с лучшей общей уверенностью (так будет работать программа)."""
    stats = {n: {"total": 0, "rot0": 0, "any": 0, "auto": 0, "seconds": 0.0, "errors": 0} for n in engines}
    rows = []
    for idx, (path, expected) in enumerate(items):
        img = loader(path)
        row = {"file": os.path.basename(path), "expected": expected, "variants": {}}
        for name, fn in engines.items():
            st = stats[name]
            st["total"] += 1
            per_rot = []
            err = None
            t0 = time.time()
            for rot in rotations:
                try:
                    lines = [(str(t), float(c)) for t, c in fn(rotate(img, rot) if img is not None else None)]
                except Exception as e:  # noqa — одна картинка не должна ронять бенч
                    lines, err = [], "%s: %s" % (type(e).__name__, e)
                per_rot.append((rot, lines))
            st["seconds"] += time.time() - t0
            if err:
                st["errors"] += 1
            oks = [(rot, any(is_match(t, expected) for t, _c in lines)) for rot, lines in per_rot]
            weight = [sum(c * _alnum_len(t) for t, c in lines) for _rot, lines in per_rot]
            auto = max(range(len(per_rot)), key=lambda i: weight[i])
            st["rot0"] += int(oks[0][1])
            st["any"] += int(any(ok for _r, ok in oks))
            st["auto"] += int(oks[auto][1])
            shown = auto if not oks[0][1] else 0
            row["variants"][name] = {"ok": oks[auto][1], "ok0": oks[0][1], "rot": per_rot[shown][0],
                                     "lines": [t for t, _c in per_rot[shown][1]]}
            if err:
                row["variants"][name]["error"] = err
        rows.append(row)
        if progress:
            progress(idx + 1, len(items))
    return stats, rows


def _pct(n, total):
    return "%.1f" % (100.0 * n / total) if total else "—"


def report_md(stats, rows, source):
    lines = ["# Бенч распознавания", "", "Дата: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "Источник: %s. Правильный ответ — имя файла (или `ответы.csv`).  " % source,
             "`rot0` — читается на исходном фото; `auto` — при выбранном повороте; `any` — хоть при каком-то (потолок).", "",
             "| Вариант | Фото | rot0, % | auto, % | any, % | Время на фото, с | Ошибки |", "|---|---|---|---|---|---|---|"]
    for name, s in stats.items():
        t = s["total"]
        lines.append("| %s | %d | %s | %s | %s | %.2f | %d |" % (
            name, t, _pct(s["rot0"], t), _pct(s["auto"], t), _pct(s["any"], t),
            s["seconds"] / t if t else 0.0, s["errors"]))
    best = next(iter(stats), None)
    bad = [r for r in rows if best and not r["variants"][best]["ok"]]
    if bad:
        lines += ["", "## Не прочитано вариантом `%s` (%d)" % (best, len(bad)), "",
                  "| Файл | Ожидалось | Прочитано | Поворот |", "|---|---|---|---|"]
        for r in bad:
            v = r["variants"][best]
            lines.append("| %s | %s | %s | %d |" % (r["file"], r["expected"], " / ".join(v["lines"]).replace("|", "/")[:80], v["rot"]))
    return "\n".join(lines) + "\n"


def make_engines(with_tesseract=True):
    """Вариант на каждый набор параметров PP-OCR + (для сравнения) Tesseract."""
    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()
    engines = {}

    def ppocr(score, box, unclip):
        def f(img):
            kw = {}
            if score is not None:
                kw = {"text_score": score, "box_thresh": box, "unclip_ratio": unclip}
            res, _t = ocr(img, **kw)
            return [(r[1], r[2]) for r in (res or [])]
        return f

    for name, score, box, unclip in PARAM_SETS:
        engines["ppocr_" + name] = ppocr(score, box, unclip)
    if with_tesseract:
        t = _tesseract_engine()
        if t:
            engines.update(t)
    return engines


def _tesseract_engine():
    try:
        import pytesseract
    except ImportError:
        return None
    app = os.path.dirname(os.path.dirname(HERE)) if os.path.basename(HERE) != "checks" else os.path.dirname(HERE)
    for c in (os.path.join(app, "tesseract", "tesseract.exe"),
              os.path.join(os.path.dirname(HERE), "tesseract", "tesseract.exe")):
        if os.path.isfile(c):
            pytesseract.pytesseract.tesseract_cmd = c
            break
    try:
        pytesseract.get_tesseract_version()
    except Exception:  # noqa
        return None
    wl = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-./+#"

    def make(psm):
        def f(img):
            d = pytesseract.image_to_data(img, lang="eng", output_type=pytesseract.Output.DICT,
                                          config="--oem 1 --psm %d -c tessedit_char_whitelist=%s" % (psm, wl))
            lines = {}
            for i, w in enumerate(d["text"]):
                w = (w or "").strip()
                try:
                    conf = float(d["conf"][i])
                except (TypeError, ValueError):
                    conf = -1
                if w and conf >= 0:
                    key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
                    lines.setdefault(key, []).append((w, conf))
            return [(" ".join(w for w, _c in v), sum(c for _w, c in v) / len(v) / 100.0) for v in lines.values()]
        return f

    return {"tesseract_psm6": make(6), "tesseract_psm11": make(11)}


@contextlib.contextmanager
def native_stderr_to(path):
    """Сообщения библиотек на C++ (onnxruntime при загрузке: нет `onnxruntime_providers_shared.dll`, «Windows 10
    and above» — на Win7 безвредно) — в файл отчёта, а не в окно проверки, где их принимают за ошибку."""
    try:
        sys.stderr.flush()
        saved = os.dup(2)
        target = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    except (OSError, ValueError, AttributeError):       # stderr закрыт или подменён — работаем как раньше
        yield
        return
    try:
        os.dup2(target, 2)
        yield
    finally:
        try:
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        os.dup2(saved, 2)
        os.close(saved)
        os.close(target)


def libs_report():
    """Диагностика окружения PP-OCR: библиотеки программы (site-packages), DLL Visual C++ рядом с python.exe."""
    pydir = os.path.dirname(os.path.abspath(sys.executable))
    rep = {"dll": {d: os.path.isfile(os.path.join(pydir, d)) for d in ("msvcp140.dll", "vcruntime140_1.dll", "concrt140.dll")}}
    try:
        import onnxruntime
        rep["onnxruntime"] = onnxruntime.__version__
        rep["providers"] = onnxruntime.get_available_providers()
        rep["onnxruntime_file"] = onnxruntime.__file__
    except BaseException:  # noqa — полный текст ошибки загрузки — в отчёт
        rep["onnxruntime_error"] = traceback.format_exc()
    return rep


def run(ctx):
    """Проверка набора: PP-OCR на фото из `фото/`."""
    noise = os.path.join(ctx.work_dir, "onnxruntime_stderr.txt")
    with native_stderr_to(noise), warnings.catch_warnings():
        warnings.simplefilter("ignore")            # «Unsupported Windows version (7)» — тоже не в окно
        diag = libs_report()
    with open(os.path.join(ctx.work_dir, "ppocr_libs.json"), "w", encoding="utf-8") as f:
        json.dump(diag, f, ensure_ascii=False, indent=2)
    photos = ctx.photos_dir
    if not os.path.isdir(photos) or not collect(photos):
        os.makedirs(photos, exist_ok=True)
        ctx.todo("Нет фото для проверки распознавания. Положите снимки чипов (JPG или PNG, лучше вырезки с одной "
                 "строкой маркировки, файл назван этой строкой) в папку %s и запустите проверку ещё раз." % photos)
        return {"status": "skip", "note": "нет картинок в папке %s" % photos, "libs": diag, "photos_dir": photos}
    try:
        with native_stderr_to(noise):
            engines = make_engines()
    except BaseException:  # noqa — полный текст ошибки загрузки — в отчёт
        tb = traceback.format_exc()
        with open(os.path.join(ctx.work_dir, "ppocr_load_error.txt"), "w", encoding="utf-8") as f:
            f.write(tb)
        return {"status": "fail", "error": "PP-OCR не загрузился", "traceback": tb, "libs": diag}
    items = collect(photos)
    stats, rows = evaluate(items, engines)
    md = report_md(stats, rows, "фото/ (%d)" % len(items))
    with open(os.path.join(ctx.work_dir, "ocr_bench.md"), "w", encoding="utf-8") as f:
        f.write(md)
    main = stats.get("ppocr_main", {})
    return {"status": "ok" if main.get("errors", 1) == 0 else "fail", "photos": len(items), "stats": stats, "libs": diag,
            "note": "main auto %s%%" % _pct(main.get("auto", 0), main.get("total", 0))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", help="папка с фото (подпапки читаются)")
    ap.add_argument("--out", default="my_reports")
    ap.add_argument("--limit", type=int, default=0, help="только первые N фото")
    ap.add_argument("--no-tesseract", action="store_true")
    a = ap.parse_args(argv)
    items = collect(a.folder)
    if a.limit:
        items = items[:a.limit]
    if not items:
        print("В %s нет картинок" % a.folder)
        return 1
    engines = make_engines(not a.no_tesseract)

    def progress(i, n):
        print("\r%d/%d" % (i, n), end="", flush=True)

    stats, rows = evaluate(items, engines, progress=progress)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "ocr_bench_%s.md" % datetime.date.today().isoformat())
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_md(stats, rows, "%s (%d фото)" % (a.folder, len(items))))
    print("\nОтчёт: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
