# -*- coding: utf-8 -*-
"""Шаг 7.4b: карточка чипа — заголовок (партномер, метка, корпус), фото под размер окна, компоновка окна."""
import os
import threading

import pytest

pytest.importorskip("PyQt5")

from digger.core.models import ChipReport, MemoryVerdict  # noqa: E402
from test_latency import _pump  # noqa: E402
from test_models import _png  # noqa: E402


def _report(path, part, has_memory, package="", pins=0):
    r = ChipReport(image_path=path)
    r.chosen_part = part
    r.memory = MemoryVerdict(has_memory=has_memory)
    r.chip.package, r.chip.pins = package, pins
    return r


def test_photo_view_fits_any_size(qapp):
    from PyQt5.QtGui import QColor, QImage
    from digger.gui.chip_card import PHOTO_PAD, PhotoView
    v = PhotoView()
    assert v.pixmap() is None and v.text() == u"—"
    red = QColor("#c81e1e")
    img = QImage(400, 200, QImage.Format_RGB888)
    img.fill(red)
    v.setImage(img)
    assert v.text() == ""

    def shown():
        pm = v.pixmap()
        return int(round(pm.width() / pm.devicePixelRatio())), int(round(pm.height() / pm.devicePixelRatio()))
    v.resize(800, 600)                                       # широкое место: фото растянуто по ширине
    assert shown() == (800 - 2 * PHOTO_PAD, 400 - PHOTO_PAD)
    v.resize(300, 600)                                       # узкое: уменьшено, пропорции те же
    assert shown() == (300 - 2 * PHOTO_PAD, 150 - PHOTO_PAD)
    v.resize(900, 200)                                       # низкое: ограничивает высота
    assert shown() == (2 * (200 - 2 * PHOTO_PAD), 200 - 2 * PHOTO_PAD)
    centre = v.grab().toImage().pixelColor(450, 100)
    assert abs(centre.red() - red.red()) + abs(centre.green() - red.green()) + abs(centre.blue() - red.blue()) < 30
    v.setText(u"Фото не читается")
    assert v.pixmap() is None and v.text() == u"Фото не читается"
    assert not v.grab().isNull()


def test_state_tag(qapp):
    from digger.gui.chip_card import StateTag
    from digger.gui.models import PHOTO_STATES
    tag = StateTag()
    assert tag.text() == "" and tag.isHidden()               # у нового фото метки нет
    widths = {}
    for state, (title, _color) in PHOTO_STATES.items():
        tag.set_state(state)
        assert tag.state() == state and tag.text() == title and tag.isHidden() == (not title)
        widths[state] = tag.sizeHint().width()
        assert not tag.grab().isNull()
    assert widths["unread"] > widths["memory"] > 0           # ширина — по подписи
    tag.set_state(u"нет такого")
    assert tag.state() == "new"


def test_card_header_follows_photo(window, tmp_path, monkeypatch):
    w, app = window
    assert w.card.title.text() == u"Фото не выбрано" and w.card.tag.text() == "" and w.card.package.text() == ""
    a, b = [os.path.normpath(_png(str(tmp_path / n))) for n in ("a.png", "b.png")]
    w.add_files([a, b])
    _pump(app, lambda: w.list.count() == 2 and w.is_idle())
    assert w.card.title.text() == "a.png" and w.card.tag.state() == "new" and w.card.title.toolTip() == a
    go = threading.Event()

    def analyze(path, progress=None, marking_override="", ocr_mode=""):
        assert go.wait(10)
        if path == a:
            return _report(path, "W25Q64JV", True, "SOP-8", 8), []
        return _report(path, "LM358", False), []
    monkeypatch.setattr(w.pipe, "analyze_image", analyze)
    w.run_selected()
    _pump(app, lambda: w.photos.status(a)[0] == "busy")
    assert w.card.tag.text() == u"распознаю…" and w.card.title.text() == "a.png"
    go.set()
    _pump(app, lambda: w.is_idle() and not w.queue and w.photos.status(b)[0] == "no_memory")
    assert w.current_path() == b                             # окно шло за очередью (шаг 7.4c) — вернёмся к первому
    w.list.setCurrentRow(0)
    assert (w.card.title.text(), w.card.tag.text(), w.card.package.text()) == \
        ("W25Q64JV", u"память", u"SOP-8 · выводов: 8")
    w.list.setCurrentRow(1)
    assert (w.card.title.text(), w.card.tag.text(), w.card.package.text()) == ("LM358", u"без памяти", "")

    w.pkg_box.setEditText("DIP-8")                           # корпус исправили вручную — заголовок это показывает
    w.pins.setValue(0)
    monkeypatch.setattr(w.pipe, "evaluate", lambda r, progress=None: None)
    w.apply_chip()
    _pump(app, w.is_idle)
    assert w.card.package.text() == "DIP-8"

    w.list.selectAll()
    w.remove_selected()
    assert w.card.title.text() == u"Фото не выбрано" and w.card.tag.text() == "" and w.card.package.text() == ""


def test_long_file_name_is_shortened(qapp, tmp_path):
    from digger.gui.chip_card import TITLE_MAX, ChipCard
    from digger.ui import theme as ui_theme
    card = ChipCard(ui_theme.apply_theme(qapp, "light", str(tmp_path / "theme")))
    name = u"очень длинное имя файла с фото микросхемы " * 3 + ".png"
    card.set_header(name, "", "new")
    assert len(card.title.text()) == TITLE_MAX and card.title.text().endswith(".png") and u"…" in card.title.text()
    card.set_header(name, "STM32F103C8T6", "unknown", "LQFP-48", 48)
    assert card.title.text() == "STM32F103C8T6" and card.package.text() == u"LQFP-48 · выводов: 48"
    card.set_header(name, "NE555", "not_found", "", 8)
    assert card.package.text() == u"выводов: 8"


def test_window_layout(window):
    from PyQt5.QtCore import Qt
    from digger.gui.chip_card import FIELDS_WIDTH, PHOTO_ASPECT
    from digger.gui.main_window import CARD_HEIGHT
    w, app = window
    assert [w.tabs.tabText(i) for i in range(4)] == [u"Заключение", u"Документы", u"Почему", u"Журнал"]
    assert w.right.orientation() == Qt.Vertical and w.right.widget(0) is w.card
    lower = w.right.widget(1).layout()                       # под карточкой: место ленты поиска (7.5), затем вкладки
    assert lower.indexOf(w.feed_slot) == 0 and lower.indexOf(w.tabs) == 1
    assert w.feed_slot.layout() is not None and w.feed_slot.isHidden()
    for name in ("img_label", "variant_box", "marking", "ocr_mode", "rerun", "part_box", "pkg_box", "pins"):
        assert getattr(w, name) is getattr(w.card, name)     # прежние имена полей окна действуют
    assert w.marking.maximumHeight() <= 64

    w.show()
    w.resize(1100, 700)
    app.processEvents()
    small, card, tabs = w.img_label.size(), w.card.height(), w.tabs.height()
    w.resize(1700, 1100)
    app.processEvents()
    big = w.img_label.size()
    # шаг 8.3d: область фото не растягивается на пустые поля — прибавка высоты окна уходит вкладкам с заключением
    assert big.width() <= int(w.card.photo_box.height() * PHOTO_ASPECT) + 1 and big.height() >= small.height()
    assert w.card.height() <= max(card, CARD_HEIGHT) and w.tabs.height() >= tabs + 300
    assert FIELDS_WIDTH[0] <= w.card.fields.width() <= FIELDS_WIDTH[1]
