# -*- coding: utf-8 -*-
"""Шаг 7.5b: живая лента поиска — строка состояния с меткой языка, плавная смена текста, история, «Стоп»."""
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from test_feed_model import _ev, _search  # noqa: E402
from test_latency import _pump  # noqa: E402
from test_models import _png  # noqa: E402
from test_photo_cards import _near, _report  # noqa: E402


def _feed(qapp, theme="light"):
    from digger.gui.search_feed import SearchFeed
    from digger.ui import theme as ui_theme
    feed = SearchFeed(ui_theme.apply_theme(qapp, theme))
    feed.resize(900, 220)
    return feed


# -------------------- плавная смена текста --------------------

def test_status_text_changes_smoothly(qapp):
    from digger.gui.search_feed import FADE_MS, FadeText
    label = FadeText()
    label.resize(400, 24)
    label.setText(u"поиск в локальной базе…")            # виджет не на экране — плавность не нужна
    assert label.text() == u"поиск в локальной базе…" and not label.animating() and label.progress() == 1.0
    label.show()
    seen = []
    started = time.time()
    label.setText("Searching alldatasheet.com…")
    assert label.text() == "Searching alldatasheet.com…" and label.animating() and label.progress() == 0.0

    def step():
        seen.append(label.progress())
        return not label.animating()
    _pump(qapp, step)
    assert time.time() - started >= FADE_MS / 1000.0 * 0.8
    assert any(0.0 < x < 1.0 for x in seen) and seen == sorted(seen) and label.progress() == 1.0
    label.setText("Searching alldatasheet.com…")         # тот же текст — без мигания
    assert not label.animating()
    label.setText(u"百度搜索")
    label.setText(u"正在下载")                             # новый текст посреди смены — смена начинается заново
    assert label.animating() and label.text() == u"正在下载"
    _pump(qapp, lambda: not label.animating())
    assert 100 <= FADE_MS <= 300
    label.close()


# -------------------- лента сама по себе --------------------

def test_feed_shows_language_text_counters_and_history(qapp):
    feed = _feed(qapp)
    assert feed.isHidden() and feed.badge.text() == "" and not feed.b_stop.isEnabled()
    events = _search()
    feed.add_events(events[:6])                           # последнее — запрос DuckDuckGo
    assert not feed.isHidden()
    assert feed.badge.text() == "EN" and feed.line.text().startswith("DuckDuckGo")
    assert u"идёт поиск" in feed.line.toolTip()           # подсказка — русский перевод
    feed.add_events(events[6:9])
    assert feed.badge.text() == u"CH" and feed.line.text().startswith(u"百度搜索")
    assert feed.counters.text() == u"3 запроса · найдено 13 · скачано 0 · подтверждено 0"
    feed.add_events(events[9:])                           # итог поиска — по-русски, на каком бы языке он ни шёл
    assert feed.badge.text() == "RU" and u"ПОДТВЕРЖДЁН" in feed.line.text()
    assert feed.line.toolTip() == ""                      # уже по-русски — перевод не нужен
    assert feed.counters.text() == u"14 запросов · найдено 13 · скачано 1 · подтверждено 1"
    assert feed.view.model() is feed.model and feed.model.rowCount() == 16
    assert not feed.view.isHidden() and feed.b_history.isChecked()
    feed.b_history.click()                                # история сворачивается, строка состояния остаётся
    assert feed.view.isHidden() and not feed.line.isHidden()
    feed.b_history.click()
    assert not feed.view.isHidden()
    feed.add_events([])                                   # пустая пачка ничего не меняет
    assert feed.badge.text() == "RU"
    feed.begin()                                          # новый поиск — лента с чистого листа
    assert feed.model.rowCount() == 0 and feed.line.text() == "" and feed.badge.text() == ""
    assert feed.counters.text() == u"0 запросов · найдено 0 · скачано 0 · подтверждено 0" and feed.counters.isHidden()
    assert feed.view.isHidden()                           # истории нет — и места она не занимает
    feed.add_events(_search()[:2])
    assert not feed.view.verticalScrollBar().isVisible()
    assert not feed.view.isHidden() and feed.view.height() // feed.view.sizeHintForRow(0) == 2
    assert feed.counters.isHidden()                       # запросов ещё не было — считать нечего
    feed.add_events(_search()[2:])
    assert feed.view.height() // feed.view.sizeHintForRow(0) == 6 and not feed.counters.isHidden()


def test_feed_stop_button_and_running_state(qapp):
    from digger.gui.feed_model import STATE_ROLE
    feed = _feed(qapp)
    stops = []
    feed.stop_requested.connect(lambda: stops.append(1))
    feed.set_running(True)
    feed.add_events(_search()[:1])
    assert feed.b_stop.isEnabled() and feed.model.index(1).data(STATE_ROLE) == "pending"
    feed.b_stop.click()
    assert stops == [1]
    feed.set_running(False)                               # остановлено: «идёт…» больше не идёт
    assert not feed.b_stop.isEnabled() and feed.model.index(1).data(STATE_ROLE) == "stale"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_feed_is_painted_with_theme_colors(qapp, theme):
    from digger.ui import theme as ui_theme
    feed = _feed(qapp, theme)
    try:
        feed.set_level_names({"catalog": u"1. Сайты-каталоги datasheet", "china": u"3. Китайский интернет"})
        feed.add_events(_search() + [_ev("error.internal", "ru", stage=u"проверка", error="ValueError")])
        feed.show()
        _pump(qapp, lambda: not feed.line.animating())
        t = ui_theme.current()
        image = feed.grab().toImage()
        assert image.pixelColor(image.width() // 2, 3) != t.qcolor("bg")
        head = feed.badge.grab().toImage()
        assert _near(head, t.qcolor("accent")) > 5        # метка языка
        feed.view.scrollToTop()
        qapp.processEvents()
        history = feed.view.viewport().grab().toImage()
        assert _near(history, t.qcolor("success")) > 10   # ✔
        assert _near(history, t.qcolor("text")) > 50      # текст событий
        feed.view.scrollToBottom()
        qapp.processEvents()
        assert _near(feed.view.viewport().grab().toImage(), t.qcolor("danger")) > 5     # ✘
        rows = feed.view.viewport().height() // feed.view.sizeHintForRow(0)
        assert 4 <= rows <= 8                             # история — несколько строк, не половина окна
    finally:
        feed.close()
        ui_theme.apply_theme(qapp, "light")


# -------------------- лента в окне --------------------

def _window_with_part(window, tmp_path):
    w, app = window
    w.resize(1280, 820)
    w.show()
    path = _png(str(tmp_path / u"чип.png"))
    w.add_files([path])
    _pump(app, lambda: w.list.count() == 1 and w.is_idle())
    w._analyzed((w.current_path(), _report(w.current_path(), "W25Q64JV", "?"), []))
    _pump(app, lambda: w.is_idle())
    return w, app


def test_window_feed_follows_search_and_stop_works(window, tmp_path, monkeypatch):
    from digger.gui.feed_model import STATE_ROLE
    w, app = _window_with_part(window, tmp_path)
    feed = w.search_feed
    assert w.feed_slot is feed and feed.model.groups() == [("ocr", 1)]       # «фото распознано»
    gate, state = threading.Event(), {}

    def search_web(r, progress=None, cancel=None, levels=None):
        for e in _search()[:10]:                          # до «скачивание…» — оно останется без итога
            w.ctx.bus.publish(e)
        gate.wait(20)
        state["cancelled"] = cancel.cancelled
    monkeypatch.setattr(w.pipe, "search_web", search_web)

    w.web_search(None)
    _pump(app, lambda: feed.model.rowCount() == 10)
    assert not feed.isHidden() and feed.b_stop.isEnabled()
    assert feed.badge.text() == u"CH" and feed.line.text().startswith(u"正在从 szlcsc.com 下载")
    assert feed.model.groups() == [("local", 1), ("catalog", 1), ("search", 2), ("china", 2)]   # прежняя история убрана
    assert u"Сайты-каталоги datasheet" in [feed.model.index(i).data() for i in range(feed.model.rowCount())]
    assert feed.counters.text().startswith(u"3 запроса · найдено 13")
    assert feed.b_stop.isHidden()                         # в окне «Стоп» один — на панели действий
    w.a_stop.trigger()
    assert w.job.cancel.cancelled
    gate.set()
    _pump(app, lambda: w.is_idle() and not w.job.isRunning())
    assert state == {"cancelled": True} and not feed.b_stop.isEnabled() and not w.a_stop.isEnabled()
    last = feed.model.rowCount() - 1
    assert feed.model.index(last).data(STATE_ROLE) == "stale"
    assert feed.line.text().startswith(u"正在从 szlcsc.com 下载")        # строка состояния — на чём остановились

    def found(r, progress=None, cancel=None, levels=None):
        for e in _search():
            w.ctx.bus.publish(e)
    monkeypatch.setattr(w.pipe, "search_web", found)
    w.web_search(["all"])
    _pump(app, lambda: w.is_idle() and not w.job.isRunning())
    assert feed.model.groups()[-1] == ("result", 1) and feed.model.rowCount() == 16
    assert all(feed.model.index(i).data(STATE_ROLE) not in ("pending", "stale") for i in range(16))
    assert feed.badge.text() == "RU" and u"ПОДТВЕРЖДЁН" in feed.line.text()
    assert feed.counters.text() == u"14 запросов · найдено 13 · скачано 1 · подтверждено 1"


def test_window_feed_restarts_for_each_photo_in_queue(window, tmp_path, monkeypatch):
    w, app = window
    w.show()
    paths = [_png(str(tmp_path / (u"фото_%d.png" % i))) for i in range(2)]
    w.add_files(paths)
    _pump(app, lambda: w.list.count() == 2 and w.is_idle())
    assert w.search_feed.isHidden()                       # пока ничего не искали — ленты нет

    def analyze(path, progress=None, marking_override="", ocr_mode=""):
        w.ctx.bus.emit("ocr.start", lang="ru", provider="Tesseract")
        w.ctx.bus.emit("ocr.ok", lang="ru", provider="Tesseract", conf=90 + paths.index(path))
        return _report(path, "PART%d" % paths.index(path), "no"), []
    monkeypatch.setattr(w.pipe, "analyze_image", analyze)
    w.run_selected()
    _pump(app, lambda: w.is_idle() and not w.queue and all(w.items[p]["report"] for p in paths))
    feed = w.search_feed
    assert not feed.isHidden() and feed.model.groups() == [("ocr", 2)]       # только второе фото: «прочитано» + «распознано»
    assert "91" in feed.model.index(1).data() and u"PART1" in feed.line.text()
