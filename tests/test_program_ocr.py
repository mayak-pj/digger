# -*- coding: utf-8 -*-
"""Проверка набора program_ocr: фото через менеджер распознавания программы."""
import importlib.util
import os
import shutil
import sys

import pytest

from digger.core.models import OcrLine, OcrResult

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "win7_pack"))
po = pytest.importorskip("program_ocr", reason="нет tools/win7_pack/program_ocr.py")
pc = pytest.importorskip("ppocr_check")
HAS_PPOCR = all(importlib.util.find_spec(m) for m in ("onnxruntime", "rapidocr_onnxruntime"))


class _Ocr(object):
    chain = ["ppocr", "tesseract"]
    error = "ppocr — нет"

    def recognize(self, variants, original=None, progress=None):
        return OcrResult(lines=[OcrLine("LM358", 90.0, "x")], best_text="LM358", provider="tesseract")


class _Enh(object):
    def enhance(self, img):
        return [img]


class _Prog(object):
    modules = {"ocr": _Ocr(), "enhancer": _Enh()}


def test_read_photos_reports_provider_and_match(tmp_path):
    shutil.copy2(os.path.join(ROOT, "tests", "samples", "lm358_180.png"), str(tmp_path / "LM358.png"))
    shutil.copy2(os.path.join(ROOT, "tests", "samples", "stm32.png"), str(tmp_path / "XYZ999.png"))
    rows = po.read_photos(_Prog(), pc.collect(str(tmp_path)), pc)
    by = {r["file"]: r for r in rows}
    assert by["LM358.png"]["ok"] and by["LM358.png"]["provider"] == "tesseract"
    assert not by["XYZ999.png"]["ok"] and not by["XYZ999.png"]["error"]
    md = po.report_md(rows, "ppocr — нет")
    assert "Совпало с ответом: 1 из 2" in md and "tesseract — 2" in md and "Проблемы провайдеров" in md


def test_match_is_judged_by_final_text_not_spare_lines(tmp_path):
    """Отчёт от 2026-10-10: «да» стояло рядом с мусором — ответ был только среди запасных строк другого способа."""
    from digger.core.models import OcrAttempt

    class Ocr(_Ocr):
        def recognize(self, variants, original=None, progress=None):
            return OcrResult(lines=[OcrLine("44H", 61.0, "x"), OcrLine("QZXB", 57.0, "x"), OcrLine("25Q64FVSIG", 90.0, "x")],
                             best_text="44H\n25Q64FVSIG", provider="tesseract",
                             attempts=[OcrAttempt("ppocr", "PP-OCRv4", status="weak", confidence=57.0, text="QZXB"),
                                       OcrAttempt("tesseract", "Tesseract", status="ok", confidence=61.0, text="44H")])

    class Prog(object):
        modules = {"ocr": Ocr(), "enhancer": _Enh()}

    for name in ("QZXB.png", "25Q64FV.png"):
        shutil.copy2(os.path.join(ROOT, "tests", "samples", "stm32.png"), str(tmp_path / name))
    by = {r["file"]: r for r in po.read_photos(Prog(), pc.collect(str(tmp_path)), pc)}
    row = by["QZXB.png"]
    assert not row["ok"] and row["spare"] and not row["inside"]
    assert row["attempts"] == [["ppocr", "weak", 57.0, "QZXB"], ["tesseract", "ok", 61.0, "44H"]]
    assert by["25Q64FV.png"]["inside"] and not by["25Q64FV.png"]["ok"]
    md = po.report_md(list(by.values()), "")
    assert "Совпало с ответом: 0 из 2" in md and "Ответ внутри прочитанной строки: 1" in md
    assert "Ответ только среди запасных строк: 1" in md and "ppocr: weak 57" in md


def test_read_photos_isolates_errors(tmp_path):
    (tmp_path / "ABC.png").write_bytes(b"not an image")
    rows = po.read_photos(_Prog(), pc.collect(str(tmp_path)), pc)
    assert rows[0]["error"] and not rows[0]["ok"]


def test_run_skips_without_photos(tmp_path):
    class Ctx(object):
        app_dir = str(tmp_path)
        photos_dir = str(tmp_path / "фото")
        work_dir = str(tmp_path)

    assert po.run(Ctx())["status"] == "skip"


@pytest.mark.skipif(not HAS_PPOCR, reason="не установлен rapidocr-onnxruntime")
def test_run_with_real_program(tmp_path):
    app = tmp_path / "Моя программа"
    app.mkdir()
    for fn in ("config.default.json",):
        shutil.copy2(os.path.join(ROOT, fn), str(app / fn))
    shutil.copytree(os.path.join(ROOT, "data"), str(app / "data"), ignore=shutil.ignore_patterns("library", "quarantine"))
    (app / "фото").mkdir()
    shutil.copy2(os.path.join(ROOT, "tests", "samples", "lm358_180.png"), str(app / "фото" / "LM358.png"))
    work = tmp_path / "w"
    work.mkdir()

    class Ctx(object):
        app_dir = str(app)
        photos_dir = str(app / "фото")
        work_dir = str(work)

    res = po.run(Ctx())
    assert res["status"] == "ok", res
    assert res["read"] == 1 and "ppocr" in res["providers"]
    assert (work / "program_ocr.md").is_file()


def test_chosen_part_is_counted_separately(tmp_path):
    """Шаг 8.5: кроме прочитанной строки считается партномер, который программа выделила из прочитанного."""
    from digger.core.models import Candidate

    class Ocr(_Ocr):
        def recognize(self, variants, original=None, progress=None):
            return OcrResult(lines=[OcrLine("winbong", 90.0, "x"), OcrLine("25Q64FVSIG", 90.0, "x"), OcrLine("zz", 20.0, "x")],
                             best_text="winbong\n25Q64FVSIG", provider="ppocr")

    class Ident(object):
        seen = []

        def identify(self, text, alternatives=None):
            Ident.seen.append((text, alternatives))
            return [Candidate(part="25Q64FVSIG", score=0.9), Candidate(part="WINBONG", score=0.5)]

    class Prog(object):
        modules = {"ocr": Ocr(), "enhancer": _Enh(), "identifier": Ident()}

    for name in ("25Q64FV.png", "25Q64FVSIG.png", "FVSIG.png"):
        shutil.copy2(os.path.join(ROOT, "tests", "samples", "stm32.png"), str(tmp_path / name))
    by = {r["file"]: r for r in po.read_photos(Prog(), pc.collect(str(tmp_path)), pc)}
    assert Ident.seen[0] == ("winbong\n25Q64FVSIG", ["winbong", "25Q64FVSIG"])     # запасные строки — как в конвейере
    assert by["25Q64FVSIG.png"]["chosen_ok"] and not by["25Q64FVSIG.png"]["chosen_full"]
    assert by["25Q64FV.png"]["chosen_full"] and not by["25Q64FV.png"]["chosen_ok"]  # выбран полный заказной код
    assert not by["FVSIG.png"]["chosen_ok"] and not by["FVSIG.png"]["chosen_full"]
    md = po.report_md(list(by.values()), "")
    assert "Программа выбрала ответ: 1" in md and "полный код, начинающийся с ответа: 1" in md
    assert "| да | 25Q64FVSIG |" in md
