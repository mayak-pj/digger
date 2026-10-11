# -*- coding: utf-8 -*-
"""Выбор способа распознавания (шаг 7.1c): «Авто» / провайдер / «Сравнить все», окно, настройки, заключение."""
import os
import shutil
import sys
import time

import pytest

from digger.core.config import read_json
from digger.core.models import ChipReport, OcrAttempt, OcrResult
from digger.core.pipeline import ChipPipeline
from digger.recognition.manager import RecognitionManager
from test_recognition import VARIANTS, _manager, _provider

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _three(ctx, monkeypatch, **kw):
    """В цепочке exa и exb, exc — известен, но в цепочку не входит."""
    a, b, c = _provider("exa", "NE555"), _provider("exb", "LM358"), _provider("exc", "QZX7719KW", conf=70.0)
    return _manager(ctx, monkeypatch, chain=["exa", "exb"], exa=a, exb=b, exc=c, **kw), a, b, c


def test_modes_list(ctx, monkeypatch):
    m, _a, _b, _c = _three(ctx, monkeypatch)
    modes = m.modes()
    assert modes[0] == ("auto", u"Авто") and modes[-1] == ("compare", u"Сравнить все")
    assert [x for x, _t in modes[1:3]] == ["exa", "exb"]                    # сначала цепочка по порядку
    titles = dict(modes)
    assert {"exc", "ppocr", "tesseract", "cloud_stub"} <= set(titles)
    assert u"недоступно" in titles["cloud_stub"] and titles["exc"] == "EXC"
    assert m.mode == "auto"


def test_single_provider_mode(ctx, monkeypatch):
    m, a, b, c = _three(ctx, monkeypatch)
    res = m.recognize(VARIANTS, mode="exb")
    assert (res.provider, res.mode, len(res.attempts)) == ("exb", "exb", 1) and not a.calls
    res = m.recognize(VARIANTS, mode="exc")                 # не из цепочки — создаётся при выборе
    assert (res.best_text, res.provider) == ("QZX7719KW", "exc") and len(c.calls) == 1
    assert m.recognize(VARIANTS, mode="нет такого").provider == "exa"       # неизвестный способ → «Авто»


def test_compare_all_runs_every_provider(ctx, monkeypatch):
    m, a, b, c = _three(ctx, monkeypatch)
    res = m.recognize(VARIANTS, mode="compare")
    by = {x.provider: x for x in res.attempts}
    assert (by["exa"].text, by["exb"].text, by["exc"].text) == ("NE555", "LM358", "QZX7719KW")
    assert by["cloud_stub"].status == "unavailable" and len(a.calls) == len(b.calls) == len(c.calls) == 1
    assert (res.provider, res.mode) == ("exa", "compare")   # итог — первый справившийся по порядку
    assert {"NE555", "LM358", "QZX7719KW"} <= {l.text for l in res.lines}


def test_default_mode_from_config(ctx, monkeypatch):
    m, a, b, _c = _three(ctx, monkeypatch)
    ctx.config["recognition"]["mode"] = "exb"
    m = RecognitionManager({}, ctx)
    assert m.mode == "exb" and m.recognize(VARIANTS).provider == "exb"
    ctx.config["recognition"]["mode"] = "удалённый"
    assert RecognitionManager({}, ctx).mode == "auto"


def test_pipeline_passes_mode(ctx, monkeypatch, samples_dir):
    m, a, b, _c = _three(ctx, monkeypatch)
    ctx.modules["ocr"] = m
    r, _v = ChipPipeline(ctx).analyze_image(os.path.join(samples_dir, "stm32.png"), ocr_mode="exb")
    assert (r.ocr.provider, r.chosen_part) == ("exb", "LM358") and not a.calls


def test_conclusion_names_the_method(ctx):
    r = ChipReport(image_path="")
    r.ocr = OcrResult(best_text="LM358", provider="tesseract", provider_title="Tesseract", confidence=71.0, seconds=2.4,
                      attempts=[OcrAttempt("ppocr", "PP-OCRv4", "weak", seconds=0.5, confidence=42.0, text="LN35B"),
                                OcrAttempt("mine", u"Свой <способ>", "unavailable", detail=u"нет библиотеки"),
                                OcrAttempt("tesseract", "Tesseract", "ok", seconds=2.4, confidence=71.0, text="LM358")])
    html = ctx.modules["report"].render(r, [])
    assert u"Способ распознавания: <b>Tesseract</b>" in html and u"уверенность 71%" in html
    assert "LN35B" in html and u"низкая уверенность" in html and u"недоступен: нет библиотеки" in html
    assert u"Свой &lt;способ&gt;" in html
    r.ocr = OcrResult(best_text="LM358", best_variant=u"введено вручную")
    assert u"Способ распознавания" not in ctx.modules["report"].render(r, [])


# -------------------- окно --------------------

@pytest.fixture()
def window(tmp_path, monkeypatch):
    if sys.platform != "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    app_dir = tmp_path / u"программа"
    os.makedirs(str(app_dir / "data"))
    shutil.copy(os.path.join(APP, "config.default.json"), str(app_dir))
    shutil.copytree(os.path.join(APP, "data", "i18n"), str(app_dir / "data" / "i18n"))
    shutil.copy(os.path.join(APP, "data", "sources.json"), str(app_dir / "data"))
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: 0))
    from digger.gui.main_window import MainWindow
    w = MainWindow(str(app_dir))
    yield w, app
    w.close()


def _wait_job(w, app):
    """Задача закончилась и её результат дошёл до окна (поток может завершиться раньше доставки done)."""
    for _ in range(1000):
        app.processEvents()
        if w.is_idle() and w.marking.toPlainText():
            break
        time.sleep(0.01)
    app.processEvents()


def test_window_mode_list_and_rerun(window, samples_dir, monkeypatch):
    w, app = window
    modes = [w.ocr_mode.itemData(i) for i in range(w.ocr_mode.count())]
    assert modes[0] == "auto" and modes[-1] == "compare" and modes[1:3] == ["ppocr", "tesseract"]
    assert w.ocr_mode.currentData() == "auto" and w.rerun.isEnabled()
    asked = []

    def analyze(path, progress=None, marking_override="", ocr_mode=""):
        asked.append((os.path.basename(path), ocr_mode))
        r = ChipReport(image_path=path)
        r.ocr = OcrResult(best_text="LM358", provider=ocr_mode, provider_title="Tesseract", confidence=80.0)
        return r, []
    monkeypatch.setattr(w.pipe, "analyze_image", analyze)
    w.recognize_again()                                     # фото не выбрано — ничего не происходит
    w.add_files([os.path.join(samples_dir, "stm32.png")])
    for _ in range(1000):                                   # фото добавляются в фоне
        app.processEvents()
        if w.is_idle() and w.list.count():
            break
        time.sleep(0.01)
    w.ocr_mode.setCurrentIndex(w.ocr_mode.findData("tesseract"))
    w.recognize_again()
    _wait_job(w, app)
    assert asked == [("stm32.png", "tesseract")]
    assert w.marking.toPlainText() == "LM358"
    assert u"Способ распознавания: <b>Tesseract</b>" in w.pipe.render(w.current()["report"], [])
    assert u"Способ распознавания" in w.report_view.toPlainText()


def test_settings_save_default_mode(window):
    w, _app = window
    from digger.gui.dialogs import SettingsDialog
    dlg = SettingsDialog(w.ctx, w)
    assert dlg.ocr_mode.currentData() == "auto"
    dlg.ocr_mode.setCurrentIndex(dlg.ocr_mode.findData("compare"))
    dlg._save()
    assert read_json(os.path.join(w.app_dir, "config.json"))["recognition"]["mode"] == "compare"
    w._load_context()
    assert w.ocr_mode.currentData() == "compare" and w.ctx.modules["ocr"].mode == "compare"
