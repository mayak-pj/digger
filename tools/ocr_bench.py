# -*- coding: utf-8 -*-
"""Сравнение провайдеров распознавания (ARCHITECTURE §6.1) → my_reports/ocr_bench_<дата>.md.

    python tools/ocr_bench.py [my_test/ocr] [--out my_reports] [--limit N] [--params] [--variants]

Каждое фото проходит путь программы (улучшение → менеджер распознавания) цепочкой «Авто» и каждым доступным
провайдером отдельно — встроенным и из `plugins/ocr_*`. Правильный ответ — имя файла или `ответы.csv` рядом
(как в проверке `ppocr_check` набора). Облачные провайдеры не вызываются: фото наружу без согласия не уходит.
Таблица: точность партномера (строка итоговой маркировки совпала с ответом), ответ внутри прочитанной строки,
точность символов, время, стоимость. `--params` добавляет таблицу проверки `ppocr_check`: наборы параметров
PP-OCR и Tesseract на исходном фото и поворотах (долго: каждое фото читается 24 раза). `--variants` — та же
таблица для способов обработки изображения перед PP-OCR (`VARIANTS`): по ней решается, что включать в провайдер.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "tools", "win7_pack")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ppocr_check as pc  # noqa: E402
import program_ocr as po  # noqa: E402

AUTO = "auto"


def create_program(app_dir):
    from digger.core.pipeline import create_context
    return create_context(app_dir)


def _distance(a, b):
    """Расстояние Левенштейна."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def char_accuracy(expected, lines):
    """Доля верных символов ответа в лучшей из строк: 1 − правки / длина ответа, не меньше 0."""
    if not expected:
        return 0.0
    return max([0.0] + [1.0 - float(_distance(expected, pc.norm(t))) / len(expected) for t in lines])


def _summary(pid, title, rows):
    n = len(rows)
    for r in rows:
        r["chars"] = char_accuracy(r["expected"], r["text"].split(" / ") if r["text"] else [])
    return {"id": pid, "title": title, "skipped": "", "rows": rows, "total": n,
            "ok": sum(1 for r in rows if r["ok"]),
            "inside": sum(1 for r in rows if r["ok"] or r["inside"]),
            "chosen": sum(1 for r in rows if r.get("chosen_ok")),
            "chosen_full": sum(1 for r in rows if r.get("chosen_ok") or r.get("chosen_full")),
            "chars": sum(r["chars"] for r in rows) / n if n else 0.0,
            "seconds": sum(r["seconds"] for r in rows),
            "errors": sum(1 for r in rows if r["error"])}


def bench(prog, items, say=None):
    """[{id, title, skipped, rows, total, ok, inside, chars, seconds, errors}] — «Авто», потом провайдеры."""
    ocr = prog.modules["ocr"]

    def read(pid, title):
        done = []

        def step(row):
            done.append(row)
            if say and (len(done) % 10 == 0 or len(done) == len(items)):
                say("%s: %d из %d" % (title, len(done), len(items)))

        return _summary(pid, title, po.read_photos(prog, items, pc, say=step, mode=pid))

    results = [read(AUTO, "Авто (%s)" % " → ".join(ocr.chain))]
    for info in ocr.catalog():
        ok, why = (False, info.error) if info.error else ocr._available(info.id)   # загружает: станут известны вид и название
        title = "%s — %s" % (info.id, info.title or info.id)
        if info.kind == "cloud":
            ok, why = False, "облачный способ — без согласия на отправку фото не вызывался"
        if ok:
            results.append(read(info.id, title))
        else:
            results.append({"id": info.id, "title": title, "skipped": why or "недоступен", "rows": []})
    return results


def report_md(results, source, cost="бесплатно"):
    lines = ["# Сравнение провайдеров распознавания", "",
             "Дата: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "Источник: %s. Правильный ответ — имя файла (или `ответы.csv`).  " % source,
             "`Партномер` — строка итоговой маркировки совпала с ответом; `Ответ внутри строки` — совпала или содержит "
             "ответ («25Q128FVPG» при ответе «25Q128FV»); `Символы` — доля верных символов ответа в лучшей строке; "
             "`Выбран` — программа выделила ответ из прочитанного (первый кандидат); `Выбран или полный код` — "
             "ответ или заказной код, начинающийся с ответа.", "",
             "| Провайдер | Фото | Партномер, % | Ответ внутри строки, % | Выбран, % | Выбран или полный код, % | Символы, % "
             "| Время на фото, с | Стоимость | Ошибки |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r["skipped"]:
            lines.append("| %s | — | — | — | — | — | — | — | — | — |" % r["title"])
            continue
        t = r["total"]
        lines.append("| %s | %d | %s | %s | %s | %s | %.1f | %.2f | %s | %d |" % (
            r["title"], t, pc._pct(r["ok"], t), pc._pct(r["inside"], t), pc._pct(r["chosen"], t),
            pc._pct(r["chosen_full"], t), 100.0 * r["chars"],
            r["seconds"] / t if t else 0.0, cost, r["errors"]))
    skipped = [r for r in results if r["skipped"]]
    if skipped:
        lines += ["", "Не участвовали: " + "; ".join("%s — %s" % (r["id"], r["skipped"]) for r in skipped) + "."]
    ran = [r for r in results if not r["skipped"]]
    auto = next((r for r in ran if r["id"] == AUTO), None)
    if auto:
        got = {}                # файл → провайдеры, прочитавшие фото, которое «Авто» не прочитало
        missed = set(row["file"] for row in auto["rows"] if not row["ok"])
        for r in ran:
            for row in r["rows"]:
                if r is not auto and row["ok"] and row["file"] in missed:
                    got.setdefault(row["file"], []).append(r["id"])
        if got:
            lines += ["", "## Способ читает, а «Авто» — нет (%d)" % len(got), "",
                      "| Файл | Ожидалось | Кто прочитал | Прочитано «Авто» |", "|---|---|---|---|"]
            for row in auto["rows"]:
                if row["file"] in got:
                    lines.append("| %s | %s | %s | %s |" % (row["file"], row["expected"], ", ".join(got[row["file"]]),
                                                            _cell(row)))
        bad = [row for row in auto["rows"] if not row["ok"]]
        if bad:
            lines += ["", "## Не прочитано способом `%s` (%d)" % (AUTO, len(bad)), "",
                      "| Файл | Ожидалось | Прочитано | Провайдер | Символы, % |", "|---|---|---|---|---|"]
            for row in bad:
                lines.append("| %s | %s | %s | %s | %.0f |" % (row["file"], row["expected"], _cell(row),
                                                               row["provider"] or "—", 100.0 * row["chars"]))
        lost = [row for row in auto["rows"] if (row["ok"] or row["inside"]) and not row.get("chosen_ok")]
        if lost and any(row.get("chosen") for row in auto["rows"]):     # программа без модуля определения — не считаем
            lines += ["", "## Ответ прочитан, но программа выбрала другое (%d)" % len(lost), "",
                      "| Файл | Ожидалось | Выбрано | Прочитано |", "|---|---|---|---|"]
            for row in lost:
                lines.append("| %s | %s | %s | %s |" % (row["file"], row["expected"], row.get("chosen") or "—",
                                                        _cell(row)))
    return "\n".join(lines) + "\n"


def _resize(img, k):
    import cv2
    return cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC if k > 1 else cv2.INTER_AREA)


def _long_side(side):
    return lambda img: _resize(img, float(side) / max(img.shape[:2]))


def _pad(img):
    """Поля 10 % цветом краёв: у вырезки вплотную к надписи детектор текста теряет крайние буквы."""
    import cv2
    b = max(8, int(0.1 * max(img.shape[:2])))
    return cv2.copyMakeBorder(img, b, b, b, b, cv2.BORDER_REPLICATE)


def _clahe(img):
    import cv2
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    lab[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _gray(img):
    import cv2
    return cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)


def _sharpen(img):
    import cv2
    return cv2.addWeighted(img, 1.6, cv2.GaussianBlur(img, (0, 0), 2.0), -0.6, 0)


VARIANTS = [("orig", lambda img: img), ("pad", _pad), ("x2", lambda img: _resize(img, 2.0)),
            ("long640", _long_side(640)), ("long1280", _long_side(1280)), ("clahe", _clahe), ("gray", _gray),
            ("invert", lambda img: 255 - img), ("sharpen", _sharpen)]


def variant_engines(read, variants=VARIANTS):
    """{имя: f(img) → строки} для `ppocr_check.evaluate`; read — чтение PP-OCR с основными параметрами."""
    def make(fn):
        return lambda img: read(fn(img))
    return dict((name, make(fn)) for name, fn in variants)


def _cell(row):
    return (row["text"] or row["error"] or "—").replace("|", "/")[:80]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", nargs="?", default=os.path.join(ROOT, "my_test", "ocr"),
                    help="папка с фото (подпапки читаются); по умолчанию my_test/ocr")
    ap.add_argument("--out", default=os.path.join(ROOT, "my_reports"))
    ap.add_argument("--limit", type=int, default=0, help="только первые N фото")
    ap.add_argument("--params", action="store_true", help="ещё и таблица наборов параметров PP-OCR (долго)")
    ap.add_argument("--variants", action="store_true", help="ещё и таблица способов обработки фото перед PP-OCR (долго)")
    ap.add_argument("--no-providers", action="store_true", help="без таблицы провайдеров (только --params / --variants)")
    a = ap.parse_args(argv)
    items = pc.collect(a.folder) if os.path.isdir(a.folder) else []
    if a.limit:
        items = items[:a.limit]
    if not items:
        print("В %s нет картинок" % a.folder)
        return 1
    source = "%s (%d фото)" % (a.folder, len(items))
    parts = [] if a.no_providers else [report_md(bench(create_program(ROOT), items, say=print), source)]
    tables = []
    if a.params:
        tables.append(("Наборы параметров PP-OCR", pc.make_engines))
    if a.variants:
        tables.append(("Обработка фото перед PP-OCR", lambda: variant_engines(pc.make_engines(False)["ppocr_main"])))
    for title, engines in tables:
        print("%s: %d фото" % (title, len(items)))
        stats, rows = pc.evaluate(items, engines())
        parts.append(pc.report_md(stats, rows, source).replace("# Бенч распознавания", "# " + title))
    if not parts:
        print("Нечего считать: --no-providers без --params и --variants")
        return 1
    md = "\n".join(parts)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "ocr_bench_%s.md" % datetime.date.today().isoformat())
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print("Отчёт: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
