# -*- coding: utf-8 -*-
"""Шаг 7.4a: список фото карточками — состояние фото в модели, отрисовка карточки, метка «в работе»."""
import os
import threading

import pytest

pytest.importorskip("PyQt5")

from digger.core.models import ChipReport, MemoryVerdict  # noqa: E402
from test_latency import _pump  # noqa: E402
from test_models import _png  # noqa: E402


def _report(path, part="", memory=None, datasheet=""):
    r = ChipReport(image_path=path)
    r.chosen_part = part
    r.datasheet_path = datasheet
    if memory is not None:
        r.memory = MemoryVerdict(has_memory={"yes": True, "no": False, "?": None}[memory])
    return r


def _near(image, color, tolerance=40):
    """Сколько точек картинки близки к цвету (текст сглажен — точного совпадения ждать нельзя)."""
    n = 0
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            if abs(c.red() - color.red()) + abs(c.green() - color.green()) + abs(c.blue() - color.blue()) <= tolerance:
                n += 1
    return n


def test_photo_status_in_model(qapp):
    from PyQt5.QtCore import Qt
    from digger.gui.models import NAME_ROLE, PART_ROLE, PHOTO_STATES, STATE_ROLE, PhotoListModel
    m = PhotoListModel()
    a, b = os.path.join("d", "a.png"), os.path.join("d", "b.png")
    m.add([a, b])
    changed = []
    m.dataChanged.connect(lambda first, last, _roles=None: changed.append((first.row(), last.row())))
    idx = m.index(0)
    assert (idx.data(NAME_ROLE), idx.data(PART_ROLE), idx.data(STATE_ROLE)) == ("a.png", "", "new")
    assert idx.data(Qt.DisplayRole) == "a.png" and m.status(a) == ("new", "")
    m.set_status(a, "busy")
    assert idx.data(Qt.DisplayRole) == u"a.png\nраспознаю…" and changed == [(0, 0)]
    m.set_status(a, "memory", "W25Q64JV")
    assert idx.data(Qt.DisplayRole) == u"a.png\nW25Q64JV — память" and idx.data(PART_ROLE) == "W25Q64JV"
    m.set_status(a, "search")                                # партномер не назван — остаётся прежний
    assert m.status(a) == ("search", "W25Q64JV") and idx.data(Qt.DisplayRole) == u"a.png\nW25Q64JV — ищу…"
    m.set_status(a, "search")                                # то же самое — вид не перерисовывается
    assert len(changed) == 3
    m.set_status("чужой.png", "busy")                        # фото уже убрали из списка — не ошибка
    assert m.index(1).data(STATE_ROLE) == "new" and len(changed) == 3
    with pytest.raises(KeyError):
        m.set_status(a, "нет такого")
    m.remove([a])
    assert m.add([a]) == [a] and m.status(a) == ("new", "")  # убрали и добавили снова — как новое
    for title, color in PHOTO_STATES.values():               # у каждой метки есть цвет из токенов темы
        assert color in __import__("digger.ui.theme.tokens", fromlist=["LIGHT"]).LIGHT


def test_cards_are_painted(qapp, tmp_path):
    from PyQt5.QtCore import QItemSelectionModel, QPoint
    from PyQt5.QtGui import QImage, QPixmap, QRegion
    from digger.gui.models import PhotoListModel
    from digger.gui.photo_list import CARD_HEIGHT, PhotoList
    from digger.ui import theme as ui_theme
    theme = ui_theme.apply_theme(qapp, "light", str(tmp_path / "theme"))
    try:
        m = PhotoListModel(theme.icon("cpu", "text_disabled", 72))
        lst = PhotoList(lambda paths: None)
        lst.setModel(m)
        lst.resize(290, 420)
        paths = [os.path.join(u"папка", u"очень_длинное_имя_файла_с_фотографией_микросхемы_%d.png" % i) for i in range(3)]
        m.add(paths)
        qi = QImage(64, 48, QImage.Format_RGB888)
        qi.fill(theme.qcolor("success"))
        m.set_thumbs([(paths[0], qi)])
        r0, r1 = lst.visualRect(m.index(0)), lst.visualRect(m.index(1))
        assert r0.height() == CARD_HEIGHT and r1.top() - r0.top() == CARD_HEIGHT
        assert r0.width() <= lst.viewport().width()          # карточка не шире списка: длинное имя сокращается

        def card(row):
            rect = lst.visualRect(m.index(row))
            pm = QPixmap(rect.size())                        # фон списка прозрачный: просветы между карточками
            pm.fill(theme.qcolor("bg"))                      # без заливки были бы случайными точками
            lst.viewport().render(pm, QPoint(), QRegion(rect))
            return pm.toImage()
        danger, ok = theme.qcolor("danger"), theme.qcolor("success")
        new = card(1)
        assert _near(new, danger) == 0
        m.set_status(paths[1], "memory", "W25Q64JV")
        with_memory = card(1)
        assert with_memory != new and _near(with_memory, danger) > 10        # красная метка «память»
        m.set_status(paths[1], "no_memory", "LM358")
        without = card(1)
        assert _near(without, danger) == 0 and _near(without, ok) > 10       # зелёная «без памяти»
        assert _near(card(0), ok) > 500                                      # миниатюра на карточке
        lst.selectionModel().select(m.index(1), QItemSelectionModel.Select)
        assert card(1) != without                                            # выбранная карточка выделена
        ui_theme.apply_theme(qapp, "dark", str(tmp_path / "theme"))          # цвета берутся из действующей темы
        assert card(2) != new
    finally:
        ui_theme.apply_theme(qapp, "light", str(tmp_path / "theme"))


def test_window_marks_photo_in_work(window, tmp_path, monkeypatch):
    w, app = window
    a, b = [os.path.normpath(_png(str(tmp_path / n))) for n in ("a.png", "b.png")]
    w.add_files([a, b])
    _pump(app, lambda: w.list.count() == 2 and w.is_idle())
    go = threading.Event()

    def analyze(path, progress=None, marking_override="", ocr_mode=""):
        assert go.wait(10)
        if path == a:
            return _report(path, "W25Q64JV", "yes"), []
        return _report(path, "LM358", "no"), []
    monkeypatch.setattr(w.pipe, "analyze_image", analyze)
    w.run_selected()
    _pump(app, lambda: w.photos.status(a)[0] == "busy")
    assert w.photos.status(b) == ("new", "")                 # очередь до него ещё не дошла
    go.set()
    _pump(app, lambda: w.is_idle() and not w.queue and w.photos.status(b)[0] != "busy")
    assert w.photos.status(a) == ("memory", "W25Q64JV") and w.photos.status(b) == ("no_memory", "LM358")

    stop = threading.Event()

    def search(r, progress=None, cancel=None, levels=None):
        assert stop.wait(10)
    monkeypatch.setattr(w.pipe, "search_web", search)
    w.list.setCurrentRow(1)
    w.web_search(None)
    _pump(app, lambda: w.photos.status(b) == ("search", "LM358"))
    stop.set()
    _pump(app, lambda: w.is_idle() and w.photos.status(b) == ("no_memory", "LM358"))


def test_window_states_from_report_and_after_failure(window, tmp_path, monkeypatch):
    w, app = window
    paths = [os.path.normpath(_png(str(tmp_path / ("p%d.png" % i)))) for i in range(4)]
    w.add_files(paths)
    _pump(app, lambda: w.list.count() == 4 and w.is_idle())
    w._analyzed((paths[0], _report(paths[0], "NE555", "?"), []))                       # документа нет
    w._analyzed((paths[1], _report(paths[1], "NE555", "?", datasheet="ne555.pdf"), []))  # документ есть, вывода нет
    w._analyzed((paths[2], _report(paths[2]), []))                                     # маркировка не прочитана
    assert [w.photos.status(p)[0] for p in paths] == ["no_local", "unknown", "unread", "new"]
    w.list.setCurrentRow(0)                        # «нет в базе»: в интернете ещё не искали — подсказка об этом
    assert w.card.tag.text() == u"нет в базе" and u"ещё не искали" in w.card.tag.toolTip()
    w.items[paths[0]]["report"].web_searched = True
    w._mark_item(paths[0])                         # после поиска в интернете без результата — «не найдено»
    assert w.photos.status(paths[0])[0] == "not_found" and w.card.tag.text() == u"не найдено"

    def broken(path, progress=None, marking_override="", ocr_mode=""):
        raise RuntimeError(u"сбой распознавания")
    monkeypatch.setattr(w.pipe, "analyze_image", broken)
    w.list.setCurrentRow(3)
    w.run_selected()
    _pump(app, lambda: w.is_idle() and u"сбой распознавания" in w.log_view.toPlainText())
    assert w.photos.status(paths[3]) == ("new", "")          # метка «распознаю…» не осталась висеть


def test_one_settings_one_stop_and_search_buttons_in_card(window, tmp_path, monkeypatch):
    """Шаг 8.3c: «Настройки» и «Стоп» — по одному; кнопки поиска есть в карточке, «Следующее» листает список."""
    w, app = window
    texts = [a.text() for a in w.toolbar.actions()]
    menu = [a.text() for top in w.menuBar().actions() for a in ([top] + (top.menu().actions() if top.menu() else []))]
    assert texts.count(u"Настройки") == 1 and not [t for t in menu if t.startswith(u"Настройки")]
    assert texts.count(u"Стоп") == 1 and w.search_feed.b_stop.isHidden()
    assert u"Искать везде" not in texts and u"Быстрый поиск" in texts and u"Полный поиск" in texts
    assert (w.card.b_web.text(), w.card.b_web_all.text()) == (u"Быстрый", u"Полный")
    assert u"локальная база" in w.a_web.toolTip() and w.card.b_web.toolTip() == w.a_web.toolTip()

    a, b = [os.path.normpath(_png(str(tmp_path / ("n%d.png" % i)))) for i in range(2)]
    w.add_files([a, b])
    _pump(app, lambda: w.list.count() == 2 and w.is_idle())
    w._analyzed((a, _report(a, "NE555", "?"), []))
    w._analyzed((b, _report(b, "LM358", "?"), []))
    asked = []

    def search(r, progress=None, cancel=None, levels=None):
        asked.append((r.chosen_part, levels))
        r.web_searched = True
    monkeypatch.setattr(w.pipe, "search_web", search)
    w.list.setCurrentRow(0)
    w.card.b_web.click()
    _pump(app, lambda: w.is_idle() and len(asked) == 1)
    assert w.photos.status(a)[0] == "not_found" and w.card.b_web.isEnabled()
    w.card.b_next.click()
    assert w.current_path() == b
    w.card.b_web_all.click()
    _pump(app, lambda: w.is_idle() and len(asked) == 2)
    assert asked == [("NE555", None), ("LM358", ["all"])]
    w.card.b_next.click()                          # последнее фото: остаёмся на нём
    assert w.current_path() == b
