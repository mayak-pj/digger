# -*- coding: utf-8 -*-
"""Проверка program_ocr: фото из `фото/` через настоящий конвейер программы (менеджер распознавания).

Для каждого фото: каким провайдером прочитано, что прочитано, совпало ли с ответом (имя файла / ответы.csv), время.
«Верно» — строка итоговой маркировки совпала с ответом; отдельно считаются фото, где ответ лежит внутри строки
(«25Q128FVPG» при ответе «25Q128FV») и где он есть только среди запасных строк другого способа. Ход цепочки
(способ, исход, уверенность, прочитанное) — в program_ocr.json и в таблице фото, где способов было несколько.
Отчёт — program_ocr.md и program_ocr.json в папке проверки.
"""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def _import_helpers():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import ppocr_check
    return ppocr_check


def read_photos(ctx_prog, items, helper, say=None, mode=""):
    """[{file, expected, provider, text, ok, inside, spare, attempts, seconds}] — через enhancer и менеджер.
    mode — способ распознавания (id провайдера, `auto`); пусто — из настроек программы."""
    kw = {"mode": mode} if mode else {}
    ocr = ctx_prog.modules["ocr"]
    enhancer = ctx_prog.modules["enhancer"]
    rows = []
    for path, expected in items:
        row = {"file": os.path.basename(path), "expected": expected, "provider": "", "text": "", "ok": False,
               "inside": False, "spare": False, "attempts": [], "seconds": 0.0, "error": ""}
        t0 = time.time()
        try:
            img = helper.imread(path)
            if img is None:
                raise ValueError("файл не открылся как картинка")
            res = ocr.recognize(enhancer.enhance(img), original=img, **kw)
            row["provider"] = res.provider
            row["text"] = res.best_text.replace("\n", " / ")
            best = res.best_text.splitlines()
            row["ok"] = any(helper.is_match(t, expected) for t in best)
            row["inside"] = not row["ok"] and any(expected and expected in helper.norm(t) for t in best)
            row["spare"] = not row["ok"] and any(helper.is_match(l.text, expected) for l in res.lines)
            row["attempts"] = [[a.provider, a.status, a.confidence, a.text.replace("\n", " / ")]
                               for a in getattr(res, "attempts", [])]
        except Exception as e:  # noqa — одно фото не должно ронять проверку
            row["error"] = "%s: %s" % (type(e).__name__, e)
        row["seconds"] = round(time.time() - t0, 2)
        rows.append(row)
        if say:
            say(row)
    return rows


def report_md(rows, problems):
    n = len(rows)
    ok = sum(1 for r in rows if r["ok"])
    by = {}
    for r in rows:
        by[r["provider"] or "—"] = by.get(r["provider"] or "—", 0) + 1
    lines = ["# Проверка program_ocr: распознавание программой", "",
             "Совпало с ответом: %d из %d" % (ok, n),
             "Ответ внутри прочитанной строки: %d" % sum(1 for r in rows if r.get("inside")),
             "Ответ только среди запасных строк: %d" % sum(1 for r in rows if r.get("spare")),
             "Провайдеры: " + ", ".join("%s — %d" % kv for kv in sorted(by.items())),
             "Среднее время на фото: %.2f с" % (sum(r["seconds"] for r in rows) / n if n else 0.0)]
    if problems:
        lines += ["", "Проблемы провайдеров: " + problems]
    lines += ["", "| Файл | Ожидалось | Провайдер | Прочитано | Верно | Время, с |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %s | %s | %s | %.2f |" % (
            r["file"], r["expected"], r["provider"] or "—", (r["text"] or r["error"]).replace("|", "/")[:60],
            "да" if r["ok"] else "нет", r["seconds"]))
    several = [r for r in rows if len(r.get("attempts") or []) > 1]
    if several:
        lines += ["", "## Фото, где пробовалось несколько способов (%d)" % len(several), "",
                  "| Файл | Ожидалось | Итог | Ход цепочки |", "|---|---|---|---|"]
        for r in several:
            chain = "; ".join("%s: %s %.0f «%s»" % (a[0], a[1], a[2], a[3][:40]) for a in r["attempts"])
            lines.append("| %s | %s | %s | %s |" % (r["file"], r["expected"], r["provider"] or "—", chain.replace("|", "/")))
    return "\n".join(lines) + "\n"


def run(ctx):
    helper = _import_helpers()
    photos = ctx.photos_dir
    items = helper.collect(photos) if os.path.isdir(photos) else []
    if not items:
        os.makedirs(photos, exist_ok=True)
        return {"status": "skip", "note": "нет картинок в папке %s (см. проверку ppocr_check)" % photos}
    if ctx.app_dir not in sys.path:
        sys.path.insert(0, ctx.app_dir)
    try:
        from digger.core.pipeline import create_context
        prog = create_context(ctx.app_dir)
    except BaseException:  # noqa — полный текст ошибки — в отчёт
        return {"status": "fail", "error": "программа не загрузилась", "traceback": traceback.format_exc()}
    ocr = prog.modules.get("ocr")
    if ocr is None or not ocr.is_available():
        return {"status": "fail", "error": "распознавание недоступно: %s" % getattr(ocr, "error", "нет модуля"),
                "chain": getattr(ocr, "chain", [])}
    rows = read_photos(prog, items, helper)
    problems = getattr(ocr, "error", "")
    with open(os.path.join(ctx.work_dir, "program_ocr.md"), "w", encoding="utf-8") as f:
        f.write(report_md(rows, problems))
    with open(os.path.join(ctx.work_dir, "program_ocr.json"), "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "problems": problems, "chain": ocr.chain}, f, ensure_ascii=False, indent=2)
    ok = sum(1 for r in rows if r["ok"])
    errors = sum(1 for r in rows if r["error"])
    return {"status": "ok" if errors == 0 else "fail", "photos": len(rows), "read": ok, "errors": errors,
            "providers": sorted(set(r["provider"] for r in rows)), "problems": problems,
            "note": "совпало %d из %d" % (ok, len(rows))}
