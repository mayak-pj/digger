# -*- coding: utf-8 -*-
"""События поиска и строка состояния на трёх языках (шаг 1.2, ARCHITECTURE §4.8)."""
import io
import os
import re
import sys
import threading

import pytest

from digger.acquire import events
from digger.acquire.events import (
    Event, EventBus, LANGS, en_plural, load_catalog, render, ru_plural, search_language,
)

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP, "tools"))
search_cli = pytest.importorskip("search_cli", reason="нет tools/ (портативная сборка)")

FORMS = ("вариант", "варианта", "вариантов")


# ---------- склонения ----------

@pytest.mark.parametrize("n, word", [
    (0, "вариантов"), (1, "вариант"), (2, "варианта"), (5, "вариантов"), (11, "вариантов"),
    (21, "вариант"), (22, "варианта"), (111, "вариантов"), (112, "вариантов"), (101, "вариант"), (-3, "варианта"),
])
def test_ru_plural(n, word):
    assert ru_plural(n, *FORMS) == word


@pytest.mark.parametrize("n, word", [
    (0, "matches"), (1, "match"), (2, "matches"), (5, "matches"), (11, "matches"),
    (21, "matches"), (22, "matches"), (111, "matches"),
])
def test_en_plural(n, word):
    assert en_plural(n, "match", "matches") == word


@pytest.mark.parametrize("n, ru, en, zh", [
    (0, "найдено 0 ссылок", "0 links found", "找到 0 条结果"),
    (1, "найдено 1 ссылка", "1 link found", "找到 1 条结果"),
    (2, "найдено 2 ссылки", "2 links found", "找到 2 条结果"),
    (5, "найдено 5 ссылок", "5 links found", "找到 5 条结果"),
    (11, "найдено 11 ссылок", "11 links found", "找到 11 条结果"),
    (21, "найдено 21 ссылка", "21 links found", "找到 21 条结果"),
    (22, "найдено 22 ссылки", "22 links found", "找到 22 条结果"),
    (111, "найдено 111 ссылок", "111 links found", "找到 111 条结果"),
])
def test_counts_in_three_languages(n, ru, en, zh):
    ev = Event("engine.found", {"engine": "DuckDuckGo", "query": "W25Q64JV datasheet pdf", "n": n})
    assert render(ev, "ru").endswith(ru)
    assert render(ev, "en").endswith(en)
    assert render(ev, "zh").endswith(zh)


# ---------- словари ----------

def _keys(lang):
    return {k for k in load_catalog(lang) if not k.startswith("_")}


def test_catalogs_complete():
    """Каждый ключ есть во всех трёх словарях, и все ключи событий §4.8 в них описаны."""
    en, zh, ru = _keys("en"), _keys("zh"), _keys("ru")
    assert en == zh == ru
    assert set(events.KEYS) == en
    for lang in LANGS:
        for key, text in load_catalog(lang).items():
            assert isinstance(text, str) and text.strip(), (lang, key)


def test_catalogs_same_parameters():
    """Перевод не теряет и не выдумывает параметры: имена в {…} одинаковы во всех языках."""
    names = lambda text: set(re.findall(r"\{(\w+)", text))  # noqa: E731
    en, zh, ru = (load_catalog(lang) for lang in LANGS)
    for key in events.KEYS:
        assert names(en[key]) == names(zh[key]) == names(ru[key]), key


def test_catalog_language_really_differs():
    for key in events.KEYS:
        assert re.search(r"[\u4e00-\u9fff]", load_catalog("zh")[key]), key
        assert re.search(r"[а-яё]", load_catalog("ru")[key]), key
        assert not re.search(r"[а-яё\u4e00-\u9fff]", load_catalog("en")[key]), key


# ---------- сборка текста ----------

def test_render_examples_from_architecture():
    ev = Event("engine.found", {"engine": "DuckDuckGo", "query": "W25Q64JV datasheet pdf", "n": 7}, lang="en")
    assert render(ev) == 'DuckDuckGo query: "W25Q64JV datasheet pdf" — 7 links found'
    ev = Event("engine.found", {"engine": "百度", "query": "W25Q64JV 数据手册", "n": 5}, lang="zh")
    assert render(ev) == "百度搜索：「W25Q64JV 数据手册」— 找到 5 条结果"
    ev = Event("engine.captcha", {"engine": "Яндекс", "query": "W25Q64JV даташит", "minutes": 30}, lang="ru")
    assert render(ev) == "Яндекс: «W25Q64JV даташит» — капча, пропуск (повтор через 30 мин)"
    ev = Event("fetch.progress", {"file": "W25Q64JV_datasheet.pdf", "size": 1258291, "percent": 64}, lang="zh")
    assert render(ev) == "正在下载 W25Q64JV_datasheet.pdf — 1.2 MB，64%"
    assert render(ev, "ru") == "скачивание W25Q64JV_datasheet.pdf — 1,2 МБ, 64%"
    ev = Event("verify.result", {"part_ok": True, "package": "SOIC-8", "package_ok": True, "maker": "Winbond",
                                 "maker_ok": False, "score": 85}, lang="zh")
    assert render(ev) == "内容核对：型号 ✔ 封装 SOIC-8 ✔ 厂商 Winbond ✘ — 85 分"
    assert render(ev, "en") == "Content check: part ✔ package SOIC-8 ✔ maker Winbond ✘ — 85 points"


def test_result_and_errors_always_russian():
    """Итог поиска и ошибки программы — по-русски, на каком бы языке ни шёл поиск."""
    ev = Event("result.confirmed", {"queries": 14, "seconds": 42}, lang="zh")
    assert ev.display_lang == "ru"
    assert render(ev) == "итог: ПОДТВЕРЖДЁН — сохранён в библиотеку (14 запросов, 42 с)"
    assert Event("error.internal", {"stage": "extract", "error": "boom"}, lang="en").display_lang == "ru"
    assert Event("site.empty", {"site": "alldatasheet.com"}, lang="en").display_lang == "en"


def test_render_never_raises():
    assert render(Event("engine.found", {}, lang="en")) == '{engine} query: "{query}" — {n} links found'
    assert render(Event("no.such.key", {"a": 1}, lang="zh")) == "no.such.key"
    assert render(Event("site.empty", {"site": "x.com"}, lang="xx")) == render(Event("site.empty", {"site": "x.com"}, lang="en"))


def test_event_fields_and_dict():
    ev = Event("site.found", {"site": "lcsc.com", "n": 1}, lang="zh", level="china", source="lcsc")
    assert ev.outcome == "found" and ev.kind == "site" and ev.ts > 0
    assert Event("engine.query", {}).outcome == "" and Event("engine.no_key", {}).outcome == "skip"
    assert Event("site.search", {}, outcome="fail").outcome == "fail"
    with pytest.raises(ValueError):
        Event("site.search", {}, outcome="maybe")
    back = Event.from_dict(ev.to_dict())
    assert back == ev


# ---------- язык по источнику ----------

@pytest.mark.parametrize("kwargs, lang", [
    ({"query": "W25Q64JV datasheet pdf", "source": "duckduckgo"}, "en"),
    ({"query": "W25Q64JV 数据手册"}, "zh"),
    ({"query": "W25Q64JV 规格书 pdf", "source": "bing"}, "zh"),
    ({"query": "W25Q64JV даташит", "source": "duckduckgo"}, "ru"),
    ({"query": "W25Q64JV datasheet", "source": "baidu"}, "zh"),
    ({"source": "so360"}, "zh"),
    ({"source": "bing_cn"}, "zh"),
    ({"source": "yandex"}, "ru"),
    ({"domain": "www.szlcsc.com"}, "zh"),
    ({"domain": "eeworld.com.cn"}, "zh"),
    ({"domain": "chipdip.ru"}, "ru"),
    ({"domain": "https://www.alldatasheet.com/view.jsp?Searchword=NE555"}, "en"),
    ({"domain": "st.com", "source": {"lang": "zh"}}, "zh"),
    ({"source": {"name": "立创商城 LCSC", "domains": ["szlcsc.com", "lcsc.com"]}}, "zh"),
    ({}, "en"),
    ({"default": "ru"}, "ru"),
])
def test_search_language(kwargs, lang):
    assert search_language(**kwargs) == lang


# ---------- шина ----------

def test_bus_delivers_and_unsubscribes():
    bus, got = EventBus(), []
    off = bus.subscribe(got.append)
    ev = bus.emit("site.search", lang="en", site="alldatasheet.com")
    assert got == [ev] and ev.params == {"site": "alldatasheet.com"}
    off()
    bus.emit("site.empty", lang="en", site="alldatasheet.com")
    assert len(got) == 1 and len(bus.history()) == 2


def test_bus_survives_bad_subscriber():
    bus, got = EventBus(), []

    def bad(ev):
        raise RuntimeError("сломанный подписчик")
    bus.subscribe(bad)
    bus.subscribe(got.append)
    bus.emit("local.search")
    assert len(got) == 1


def test_bus_many_threads():
    bus, got, order = EventBus(history=10000), [], []
    bus.subscribe(got.append)
    bus.subscribe(lambda ev: order.append(ev.seq))
    threads, per = 8, 250

    def work(t):
        for i in range(per):
            bus.emit("engine.found", lang=LANGS[t % 3], source="t%d" % t, engine="e", query="q", n=i)
    pool = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
    for th in pool: th.start()
    for th in pool: th.join()
    assert len(got) == threads * per == len(bus.history())
    assert order == sorted(order) and len(set(order)) == len(order)      # подписчик видит события по порядку
    for t in range(threads):                                              # внутри потока порядок сохранён
        assert [e.params["n"] for e in got if e.source == "t%d" % t] == list(range(per))


def test_bus_subscribe_inside_callback_does_not_deadlock():
    bus, got = EventBus(), []

    def first(ev):
        if not got:
            bus.subscribe(got.append)
        got.append(ev)
    bus.subscribe(first)
    done = threading.Event()

    def run():
        bus.emit("local.search")
        done.set()
    threading.Thread(target=run, daemon=True).start()
    assert done.wait(5)


# ---------- консольный наблюдатель ----------

class Tty(io.StringIO):
    def isatty(self):
        return True


def _play(bus):
    bus.emit("local.search", lang="ru")
    bus.emit("local.empty", lang="ru")
    bus.emit("engine.query", lang="en", engine="DuckDuckGo", query="W25Q64JV datasheet pdf")
    bus.emit("engine.found", lang="en", engine="DuckDuckGo", query="W25Q64JV datasheet pdf", n=7)
    bus.emit("engine.query", lang="zh", engine="百度", query="W25Q64JV 数据手册")
    bus.emit("engine.found", lang="zh", engine="百度", query="W25Q64JV 数据手册", n=5)
    bus.emit("engine.captcha", lang="ru", engine="Яндекс", query="W25Q64JV даташит", minutes=30)
    bus.emit("result.confirmed", lang="zh", queries=14, seconds=42)


def test_cli_status_line_in_place_and_history():
    out, bus = Tty(), EventBus()
    obs = search_cli.ConsoleObserver(out, width=100)
    bus.subscribe(obs)
    _play(bus)
    live = out.getvalue()
    assert "\n" not in live and live.count("\r") == 8             # одна строка, обновляется на месте
    assert "百度搜索：「W25Q64JV 数据手册」" in live
    obs.close()
    lines = out.getvalue()[len(live):].splitlines()
    history = [ln for ln in lines if ln.strip()]
    assert len(history) == 6                                      # «идёт…» в историю не попадает, только итоги
    assert re.search(r"^\d\d:\d\d:\d\d ✔ EN   DuckDuckGo query: .* — 7 links found$", history[1])
    assert re.search(r"✔ CH +百度搜索", history[2])
    assert re.search(r"⤼ RU   Яндекс: «W25Q64JV даташит» — капча", history[3])
    assert re.search(r"✔ RU   итог: ПОДТВЕРЖДЁН", history[4])
    assert "14" in history[5]                                     # счётчики


def test_cli_status_line_is_padded_to_erase_previous():
    out = Tty()
    obs = search_cli.ConsoleObserver(out, width=60)
    obs(Event("engine.query", {"engine": "百度", "query": "W25Q64JV 数据手册 规格书 中文资料"}, lang="zh"))
    obs(Event("local.search", {}, lang="ru"))
    frames = out.getvalue().split("\r")[1:]
    assert search_cli.display_width(frames[1]) >= search_cli.display_width(frames[0].rstrip())
    long = Event("engine.query", {"engine": "百度", "query": "数据手册" * 40}, lang="zh")
    obs(long)
    assert search_cli.display_width(out.getvalue().split("\r")[-1]) <= 59      # не переносится на вторую строку


def test_cli_plain_output_when_not_a_terminal():
    out, bus = io.StringIO(), EventBus()
    obs = search_cli.ConsoleObserver(out)
    bus.subscribe(obs)
    _play(bus)
    obs.close()
    text = out.getvalue()
    assert "\r" not in text
    assert len([ln for ln in text.splitlines() if ln.strip()]) == 9


def test_cli_demo_runs(capsys):
    assert search_cli.main(["--demo", "--fast"]) == 0
    text = capsys.readouterr().out
    assert "ПОДТВЕРЖДЁН" in text and "CH" in text and "EN" in text
