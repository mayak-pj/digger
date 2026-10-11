# -*- coding: utf-8 -*-
"""Шаг 7.5a: данные живой ленты поиска — счётчики и модель истории по уровням (ARCHITECTURE §4.8, §5)."""
import io
import json
import os
import re
import sys

import pytest

pytest.importorskip("PyQt5")

from digger.acquire.events import Event, EventCounters  # noqa: E402

APP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
Q_EN, Q_ZH = "W25Q64JV datasheet pdf", u"W25Q64JV 数据手册"


def _ev(key, lang="en", level="", source="", **params):
    return Event(key, params, lang=lang, level=level, source=source)


def _search():
    """Один поиск: локальная база, каталог, поисковики, китайский магазин со скачиванием, итог."""
    return [
        _ev("local.search", "ru", "local"),
        _ev("local.empty", "ru", "local"),
        _ev("site.search", "en", "catalog", "alldatasheet", site="alldatasheet.com"),
        _ev("site.found", "en", "catalog", "alldatasheet", site="alldatasheet.com", n=1),
        _ev("engine.no_key", "en", "search", "google", engine="Google", query=Q_EN),
        _ev("engine.query", "en", "search", "duckduckgo", engine="DuckDuckGo", query=Q_EN),
        _ev("engine.found", "en", "search", "duckduckgo", engine="DuckDuckGo", query=Q_EN, n=7),
        _ev("engine.query", "zh", "china", "baidu", engine=u"百度", query=Q_ZH),
        _ev("engine.found", "zh", "china", "baidu", engine=u"百度", query=Q_ZH, n=5),
        _ev("fetch.start", "zh", "china", "szlcsc", file="W25Q64JV.pdf", site="szlcsc.com"),
        _ev("fetch.progress", "zh", "china", "szlcsc", file="W25Q64JV.pdf", size=1258291, percent=64),
        _ev("fetch.done", "zh", "china", "szlcsc", file="W25Q64JV.pdf", size=1966080),
        _ev("validate.ok", "zh", "china", "szlcsc", pages=72),
        _ev("engine.captcha", "ru", "russian", "yandex", engine=u"Яндекс", query=u"W25Q64JV даташит", minutes=30),
        _ev("site.search", "en", "catalog", "datasheet4u", site="datasheet4u.com"),
        _ev("site.empty", "en", "catalog", "datasheet4u", site="datasheet4u.com"),
        _ev("result.confirmed", "zh", queries=14, seconds=42),
    ]


def _model(qapp):
    from digger.gui.feed_model import FeedModel
    return FeedModel()


def _rows(m):
    from digger.gui.feed_model import KIND_ROLE
    return [(m.index(i).data(KIND_ROLE), m.index(i).data()) for i in range(m.rowCount())]


# -------------------- счётчики --------------------

def test_counters_and_russian_text():
    c = EventCounters()
    for e in _search()[:-1]:
        c.add(e)
    assert c.counts == {"queries": 4, "found": 13, "fetched": 1, "confirmed": 0}
    assert c.text() == u"4 запроса · найдено 13 · скачано 1 · подтверждено 0"
    c.add(_search()[-1])                                  # оркестратор знает число запросов точнее
    assert c.counts == {"queries": 14, "found": 13, "fetched": 1, "confirmed": 1}
    assert c.text() == u"14 запросов · найдено 13 · скачано 1 · подтверждено 1"
    c.add(_ev("local.found", "ru", "local", n=3))         # своё из базы — не «найдено в интернете»
    c.add(_ev("site.found", site="x.com", n="?"))         # нечисловое значение счёт не ломает
    assert c.counts["found"] == 13
    c.reset()
    assert c.counts == {"queries": 0, "found": 0, "fetched": 0, "confirmed": 0}


def test_console_observer_uses_the_same_counters():
    for sub in ("tools", "checks"):                          # в сборке search_cli.py лежит в checks/
        sys.path.insert(0, os.path.join(APP, sub))
    import search_cli
    out = io.StringIO()
    obs = search_cli.ConsoleObserver(out)
    for e in _search():
        obs(e)
    assert obs.counts == {"queries": 14, "found": 13, "fetched": 1, "confirmed": 1}
    obs.close()
    assert out.getvalue().splitlines()[-1] == u"14 запросов · найдено 13 · скачано 1 · подтверждено 1"


# -------------------- история по уровням --------------------

def test_history_is_grouped_by_levels(qapp):
    from digger.gui.feed_model import COUNT_ROLE, KIND_ROLE
    m = _model(qapp)
    m.set_level_names({"catalog": u"1. Сайты-каталоги datasheet", "search": u"Поисковики (английский)",
                       "china": u"3. Китайский интернет"})
    last = m.add(_search())
    groups = [(i, t) for i, (kind, t) in enumerate(_rows(m)) if kind == "group"]
    assert [t for _i, t in groups] == [u"Локальная база", u"Сайты-каталоги datasheet", u"Поисковики (английский)",
                                       u"Китайский интернет", "russian", u"Итог"]      # номера из названий убраны
    assert [m.index(i).data(COUNT_ROLE) for i, _t in groups] == [1, 2, 2, 3, 1, 1]
    # событие встаёт в свой уровень, даже если пришло позже событий других уровней
    catalog = _rows(m)[groups[1][0] + 1:groups[2][0]]
    assert ["alldatasheet.com" in catalog[0][1], "datasheet4u.com" in catalog[1][1]] == [True, True]
    assert m.rowCount() == 6 + 10
    assert m.index(last).data(KIND_ROLE) == "event" and u"ПОДТВЕРЖДЁН" in m.index(last).data()
    assert m.counters.counts["queries"] == 14
    assert m.groups() == [("local", 1), ("catalog", 2), ("search", 2), ("china", 3), ("russian", 1), ("result", 1)]


def test_pending_row_is_replaced_by_its_result(qapp):
    from digger.gui.feed_model import STATE_ROLE
    m = _model(qapp)
    events = _search()
    m.add(events[:1])
    assert m.rowCount() == 2 and m.index(1).data(STATE_ROLE) == "pending"
    assert re.match(u"^\\d\\d:\\d\\d:\\d\\d ⏳ RU поиск в локальной базе…$", m.index(1).data())
    m.add(events[1:2])
    assert m.rowCount() == 2 and m.index(1).data(STATE_ROLE) == "empty"
    m.add(events[9:10])                                   # скачивание: «идёт» → ход → итог, всё одной строкой
    row = m.rowCount() - 1
    assert m.index(row).data(STATE_ROLE) == "pending"
    m.add(events[10:11])
    assert m.rowCount() == row + 1 and "64%" in m.index(row).data() and m.index(row).data(STATE_ROLE) == "pending"
    m.add(events[11:13])
    assert m.rowCount() == row + 2
    assert [m.index(r).data(STATE_ROLE) for r in (row, row + 1)] == ["ok", "ok"]
    # два источника одного уровня идут вперемешку — итог приходит к своей строке
    m.clear()
    m.add([_ev("site.search", "en", "catalog", "a", site="a.com"), _ev("site.search", "en", "catalog", "b", site="b.com"),
           _ev("site.empty", "en", "catalog", "a", site="a.com")])
    assert [m.index(r).data(STATE_ROLE) for r in (1, 2)] == ["empty", "pending"]
    assert "a.com" in m.index(1).data() and "b.com" in m.index(2).data()


def test_row_text_time_icon_language_and_russian_tooltip(qapp):
    from digger.gui.feed_model import LANG_ROLE, STATE_ROLE, STATES, TEXT_ROLE, TIME_ROLE
    m = _model(qapp)
    m.add(_search())
    by_text = {m.index(i).data(TEXT_ROLE): m.index(i) for i in range(m.rowCount()) if m.index(i).data(TEXT_ROLE)}
    zh = by_text[u"百度搜索：「W25Q64JV 数据手册」— 找到 5 条结果"]
    assert zh.data(LANG_ROLE) == u"CH" and zh.data(STATE_ROLE) == "found"
    assert re.match(u"^\\d\\d:\\d\\d:\\d\\d$", zh.data(TIME_ROLE))
    assert re.match(u"^\\d\\d:\\d\\d:\\d\\d ✔ CH 百度搜索", zh.data())
    assert zh.data(0x3) == u"百度: «W25Q64JV 数据手册» — найдено 5 ссылок"          # Qt.ToolTipRole — русский перевод
    en = by_text["Google query: \"%s\" — no API key, skipped" % Q_EN]
    assert en.data(LANG_ROLE) == "EN" and en.data(STATE_ROLE) == "skip" and u"нет ключа API" in en.data(0x3)
    ru = next(ix for t, ix in by_text.items() if t.startswith(u"итог: ПОДТВЕРЖДЁН"))   # итог — всегда по-русски
    assert ru.data(LANG_ROLE) == "RU" and ru.data(0x3) == ru.data(TEXT_ROLE)
    for state in ("pending", "ok", "found", "empty", "fail", "skip", "info", "stale"):
        icon, color, glyph = STATES[state]
        assert icon and color and glyph, state
    assert [STATES[s][2] for s in ("pending", "ok", "fail", "skip")] == [u"⏳", u"✔", u"✘", u"⤼"]


def test_recognition_plan_finish_clear_and_limit(qapp):
    from digger.gui import feed_model
    from digger.gui.feed_model import STATE_ROLE
    m = _model(qapp)
    m.add([_ev("ocr.start", "ru", provider="Tesseract"), _ev("ocr.ok", "ru", provider="Tesseract", conf=91),
           _ev("photo.recognized", "ru", part="W25Q64JV", path="a.png"),
           _ev("search.order_adaptive", "ru", n=12),
           _ev("site.search", "en", "catalog", "alldatasheet", site="alldatasheet.com"),
           _ev("quarantine.placed", "en")])               # без уровня — в текущий уровень
    assert m.groups() == [("ocr", 2), ("start", 1), ("catalog", 2)]
    assert [t for k, t in _rows(m) if k == "group"] == [u"Распознавание", u"Поиск", "catalog"]
    assert m.index(4).data(STATE_ROLE) == "info"          # порядок поиска — справка, а не «идёт…»
    resets = []
    m.modelReset.connect(lambda: resets.append(1))
    m.finish()                                            # поиск остановлен: «идёт…» больше не идёт
    assert m.index(6).data(STATE_ROLE) == "stale" and m.index(6).data().split()[1] == u"⤼"
    m.add([_ev("search.cancelled", "ru")])
    assert m.groups()[-1] == ("result", 1) and len(resets) >= 1
    m.clear()
    assert m.rowCount() == 0 and m.groups() == [] and m.counters.counts["queries"] == 0
    for i in range(feed_model.MAX_EVENTS + 50):           # история не растёт без конца: старое уходит
        m.add([_ev("site.empty", "en", "catalog" if i < 30 else "forum", "s%d" % i, site="s%d.com" % i)])
    assert m.groups() == [("forum", feed_model.MAX_EVENTS)]


def test_level_names_from_sources_file(tmp_path):
    from digger.gui.feed_model import level_names
    names = level_names(os.path.join(APP, "data", "sources.json"))
    assert names["catalog"] == u"Сайты-каталоги datasheet" and names["search"] == u"Поисковики (английский)"
    bad = tmp_path / "sources.json"
    bad.write_text(u"{не json", encoding="utf-8")
    assert level_names(str(bad)) == {} and level_names(str(tmp_path / "нет.json")) == {}
    ok = tmp_path / u"источники.json"
    ok.write_text(json.dumps({"levels": [{"id": "x", "name": u"12. Свой уровень"}, {"id": "y"}]}), encoding="utf-8")
    assert level_names(str(ok)) == {"x": u"Свой уровень", "y": "y"}
