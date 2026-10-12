# -*- coding: utf-8 -*-
"""tools/ocr_bench.py: таблица точности по всем провайдерам распознавания (ARCHITECTURE §6.1)."""
import os
import shutil
import sys

import pytest

from digger.core.models import OcrLine, OcrResult
from digger.recognition.manager import ProviderInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
ob = pytest.importorskip("ocr_bench", reason="нет tools/ocr_bench.py")

READS = {"auto": "LM358", "good": "LM358", "close": "LM35B", "broken": ""}


class _Ocr(object):
    chain = ["good", "close"]
    error = ""

    def __init__(self):
        self.calls = []

    def catalog(self):
        return [ProviderInfo("good", "Хороший"), ProviderInfo("close", "Почти"),
                ProviderInfo("away", "Нет программы"), ProviderInfo("cloud", "Облако", kind="cloud"),
                ProviderInfo("bad", "Сломанный", error="не загрузился: нет файла")]

    def _available(self, pid):
        return (False, "не установлен") if pid == "away" else (True, "")

    def recognize(self, variants, original=None, progress=None, mode=""):
        self.calls.append(mode)
        text = READS[mode]
        return OcrResult(lines=[OcrLine(text, 90.0, "x")], best_text=text, provider=mode if mode != "auto" else "good")


class _Enh(object):
    def enhance(self, img):
        return [img]


class _Prog(object):
    def __init__(self):
        self.modules = {"ocr": _Ocr(), "enhancer": _Enh()}


def _photos(tmp_path, names=("LM358.png",)):
    for name in names:
        shutil.copy2(os.path.join(ROOT, "tests", "samples", "stm32.png"), str(tmp_path / name))
    return str(tmp_path)


def test_char_accuracy():
    assert ob.char_accuracy("LM358", ["LM358"]) == 1.0
    assert ob.char_accuracy("LM358", ["xx", "LM35B"]) == pytest.approx(0.8)      # лучшая строка, одна замена из пяти
    assert ob.char_accuracy("LM358", ["lm 358 n"]) == pytest.approx(0.8)         # лишняя буква — тоже ошибка
    assert ob.char_accuracy("LM358", []) == 0.0
    assert ob.char_accuracy("LM358", ["QWERTYUIOPASD"]) == 0.0                   # не уходит в минус
    assert ob.char_accuracy("", ["LM358"]) == 0.0


def test_bench_runs_every_local_provider_and_never_cloud(tmp_path):
    prog = _Prog()
    results = ob.bench(prog, ob.pc.collect(_photos(tmp_path)))
    by = {r["id"]: r for r in results}
    assert list(by) == ["auto", "good", "close", "away", "cloud", "bad"]
    assert prog.modules["ocr"].calls == ["auto", "good", "close"]                # облако и недоступные не вызываются
    assert by["good"]["ok"] == 1 and by["good"]["chars"] == pytest.approx(1.0)
    assert by["close"]["ok"] == 0 and by["close"]["chars"] == pytest.approx(0.8)
    assert by["cloud"]["skipped"] and "облачн" in by["cloud"]["skipped"]
    assert by["away"]["skipped"] == "не установлен"
    assert "не загрузился" in by["bad"]["skipped"]
    assert by["auto"]["title"].startswith("Авто")


def test_report_has_table_and_misses(tmp_path):
    results = ob.bench(_Prog(), ob.pc.collect(_photos(tmp_path, ("LM358.png", "NE555.png"))))
    md = ob.report_md(results, "папка (2 фото)")
    assert ("| Провайдер | Фото | Партномер, % | Ответ внутри строки, % | Выбран, % | Выбран или полный код, % | Символы, % "
            "| Время на фото, с | Стоимость | Ошибки |") in md
    assert "| good — Хороший | 2 | 50.0 | 50.0 | 0.0 | 0.0 | 60.0 |" in md
    assert "программа выбрала другое" not in md                                  # в программе-примере нет модуля определения
    assert "| cloud — Облако | — |" in md and "не вызывался" in md
    assert "## Не прочитано способом `auto` (1)" in md and "| NE555.png | NE555 | LM358 |" in md
    assert "«Авто» — нет" not in md                                              # «Авто» читает всё, что читает good


def test_report_shows_what_auto_misses_but_provider_reads(tmp_path):
    class Ocr(_Ocr):
        def recognize(self, variants, original=None, progress=None, mode=""):
            text = {"auto": "", "good": "", "close": "LM358"}[mode]
            return OcrResult(lines=[OcrLine(text, 90.0, "x")] if text else [], best_text=text, provider=mode)

    prog = _Prog()
    prog.modules["ocr"] = Ocr()
    md = ob.report_md(ob.bench(prog, ob.pc.collect(_photos(tmp_path))), "папка")
    assert "## Способ читает, а «Авто» — нет (1)" in md and "| LM358.png | LM358 | close |" in md


def test_main_writes_report_and_respects_limit(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "фото"
    folder.mkdir()
    _photos(folder, ("LM358.png", "NE555.png", "TL431.png"))
    (folder / "ответы.csv").write_text("файл;ответ\nNE555.png;LM358\n", encoding="utf-8")
    monkeypatch.setattr(ob, "create_program", lambda app_dir: _Prog())
    out = tmp_path / "отчёты"
    assert ob.main([str(folder), "--out", str(out), "--limit", "2"]) == 0
    files = os.listdir(str(out))
    assert len(files) == 1 and files[0].startswith("ocr_bench_") and files[0].endswith(".md")
    md = (out / files[0]).read_text(encoding="utf-8")
    assert "| good — Хороший | 2 | 100.0 |" in md                                # ответы.csv важнее имени файла
    assert "Отчёт:" in capsys.readouterr().out


def test_main_without_photos(tmp_path, capsys):
    assert ob.main([str(tmp_path), "--out", str(tmp_path / "o")]) == 1
    assert "нет картинок" in capsys.readouterr().out


def test_variant_engines_feed_processed_image_to_reader():
    import numpy as np
    img = np.full((40, 100, 3), 200, dtype=np.uint8)
    seen = {}
    engines = ob.variant_engines(lambda im: [("%dx%d" % (im.shape[1], im.shape[0]), 0.9)])
    assert list(engines)[0] == "orig" and len(engines) == len(ob.VARIANTS)
    for name, fn in engines.items():
        seen[name] = fn(img)[0][0]
    assert seen["orig"] == "100x40" and seen["x2"] == "200x80" and seen["long640"] == "640x256"
    assert seen["pad"] == "120x60" and seen["clahe"] == "100x40" and seen["invert"] == "100x40"


def test_main_no_providers_needs_a_table(tmp_path, capsys):
    _photos(tmp_path)
    assert ob.main([str(tmp_path), "--out", str(tmp_path / "o"), "--no-providers"]) == 1
    assert "Нечего считать" in capsys.readouterr().out
