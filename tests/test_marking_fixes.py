# -*- coding: utf-8 -*-
"""Шаг 8.3g: расширение «Замены маркировки» — словарь замен пополняется ручными исправлениями и действует на все
фото; крючки конвейера (API расширений 1.1)."""
import importlib.util
import os

import pytest

from digger.core.models import OcrLine, OcrResult
from digger.core.pipeline import ChipPipeline
from digger.extensions.loader import BUILTIN_DIR, ExtensionManager

from test_extensions import tab_titles, window  # noqa: F401 — фикстура окна

TAB = u"Замены маркировки"


def _module():
    spec = importlib.util.spec_from_file_location(
        "marking_fixes_under_test", os.path.join(BUILTIN_DIR, "marking_fixes", "extension.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOcr:
    """Роль `ocr`: «читает» заранее заданный текст."""

    def __init__(self):
        self.text = ""

    def is_available(self):
        return True

    def recognize(self, variants, progress=None, original=None):
        lines = [OcrLine(text=t, confidence=90.0, variant="fake") for t in self.text.split("\n")]
        return OcrResult(lines=lines, best_text=self.text, provider="fake", confidence=90.0)

    def close(self):
        pass


@pytest.fixture()
def rig(ctx, samples_dir):
    """Конвейер с подставным распознаванием и загруженными встроенными расширениями."""
    ocr = ctx.modules["ocr"] = FakeOcr()
    pipe = ChipPipeline(ctx)
    m = ExtensionManager(ctx, pipeline=pipe, dirs=[BUILTIN_DIR], runner=lambda work, finish: finish(work()))
    m.load()
    info = m.get("marking_fixes")
    assert info is not None and info.state == "active", info and info.error
    photo = os.path.join(samples_dir, "at24c02.png")

    def read(text):
        ocr.text = text
        return pipe.analyze_image(photo)[0]
    yield pipe, m, info.instance, read
    m.close()


def test_token_pairs_and_apply():
    mod = _module()
    assert mod.token_pairs(u"ATMFL\nAT24C02 2135", u"ATMEL\nAT24C02 2135") == [("ATMFL", "ATMEL")]
    assert mod.token_pairs(u"ATMFL 24CO2", u"ATMEL 24C02") == [("ATMFL", "ATMEL"), ("24CO2", "24C02")]
    assert mod.token_pairs(u"AT24C02", u"ATMEL\nAT24C02") == []          # дописанная строка — не замена
    assert mod.token_pairs(u"A B C", u"X C") == []                       # два слова в одно — пары нет
    text, used = mod.apply_fixes(u"ATMFL\nAT24C64  ATMFLX", {"ATMFL": "ATMEL"})
    assert text == u"ATMEL\nAT24C64  ATMFLX" and used == ["ATMFL"]       # только слово целиком, пробелы целы


def test_manual_fix_is_remembered_and_applied_to_next_photos(rig):
    pipe, m, ext, read = rig
    r = read(u"ATMFL\nAT24C02")
    assert r.ocr.best_text == u"ATMFL\nAT24C02"
    pipe.identify(r, u"ATMEL\nAT24C02", [])                              # так делает окно при ручном исправлении
    assert [(e["wrong"], e["fixed"]) for e in ext.entries()] == [("ATMFL", "ATMEL")]
    assert any(u"ATMFL → ATMEL" in s for s in r.log)

    r2 = read(u"ATMFL\nAT24C64")
    assert r2.ocr.best_text == u"ATMEL\nAT24C64" and "24C64" in r2.chosen_part
    assert any(u"замен" in s and u"ATMEL" in s for s in r2.log)
    assert ext.entries()[0]["uses"] == 1


def test_fix_reaches_photos_recognized_earlier(rig):
    pipe, m, ext, read = rig
    first, second = read(u"ATMFL\nAT24C02"), read(u"ATMFL\nW25Q64JV")
    pipe.identify(first, u"ATMEL\nAT24C02", [])
    assert pipe.refresh_marking(second) is True
    assert second.ocr.best_text == u"ATMEL\nW25Q64JV" and "W25Q64" in second.chosen_part
    assert pipe.refresh_marking(second) is False and pipe.refresh_marking(first) is False
    assert len(ext.entries()) == 1                                       # повторное применение словарь не пополняет


def test_risky_replacements_are_not_remembered(rig):
    pipe, m, ext, read = rig
    r = read(u"ATMEL\nAT24C02\n2135")
    pipe.identify(r, u"ATMEL\nAT24C04\n2136", [])    # известная микросхема и код даты — не ошибка чтения «для всех»
    assert ext.entries() == []
    assert any(u"AT24C02" in s and u"не запомнен" in s for s in r.log)
    r = read(u"AB\nAT24C02")
    pipe.identify(r, u"ST\nAT24C02", [])             # слишком короткое слово
    assert ext.entries() == []


def test_reverse_correction_removes_the_rule(rig):
    pipe, m, ext, read = rig
    assert ext.add("HOLTEK", "HOLTEX") == ""
    r = read(u"HOLTEK\nHT24LC02")
    assert r.ocr.best_text.startswith("HOLTEX")
    pipe.identify(r, u"HOLTEK\nHT24LC02", [])        # пользователь вернул как было: замена была лишней
    assert ext.entries() == []
    assert read(u"HOLTEK\nHT24LC02").ocr.best_text.startswith("HOLTEK")


def test_manual_add_validation_and_remove(rig):
    pipe, m, ext, read = rig
    assert ext.add("ATMFL", "ATMFL") and ext.add("", "X") and ext.add("TWO WORDS", "X")
    assert ext.add(" ATMFL ", "ATMEL") == "" and ext.entries()[0]["origin"] == u"вручную"
    ext.remove("ATMFL")
    assert ext.entries() == [] and read(u"ATMFL\nAT24C02").ocr.best_text.startswith("ATMFL")


def test_hooks_are_removed_with_extension(rig):
    pipe, m, ext, read = rig
    ext.add("ATMFL", "ATMEL")
    pipe.marking_filters.append(lambda text: 5)      # чужой фильтр вернул не строку — текст не меняется
    assert len(pipe.marking_filters) == 2 and len(pipe.marking_listeners) == 1
    m.close()
    assert len(pipe.marking_filters) == 1 and pipe.marking_listeners == []
    assert read(u"ATMFL\nAT24C02").ocr.best_text == u"ATMFL\nAT24C02"


def test_failing_filter_disables_only_the_extension(rig):
    pipe, m, ext, read = rig
    ext.add("ATMFL", "ATMEL")
    ext._fixes = 5                                   # порча состояния → ошибка внутри фильтра
    r = read(u"ATMFL\nAT24C02")
    assert r.ocr.best_text == u"ATMFL\nAT24C02" and r.chosen_part
    assert m.get("marking_fixes").state == "failed" and pipe.marking_filters == []


def test_window_tab_shows_dictionary(window):  # noqa: F811
    w, app = window
    assert TAB in tab_titles(w)
    ext = w.ext.get("marking_fixes").instance
    ext.add("ATMFL", "ATMEL")
    ext.view.reload()
    for _ in range(50):
        app.processEvents()
        if ext.view.model.rowCount():
            break
        import time
        time.sleep(0.02)
    assert ext.view.model.rowCount() == 1 and ext.view.model.item(0, 1).text() == "ATMEL"
