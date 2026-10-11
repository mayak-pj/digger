# -*- coding: utf-8 -*-
"""Оркестратор поиска (шаг 6.1, ARCHITECTURE §4.6): уровни, бюджет, остановка, отмена — на fake_http."""
import logging
import os

import pytest

from digger.acquire.events import EventBus
from digger.acquire.models import Lead, PhotoContext
from digger.acquire.orchestrator import Budget, Orchestrator, from_context
from digger.acquire.registry import Registry
from digger.acquire.sources.base import SourceAdapter
from digger.acquire.store import AcquireStore
from digger.acquire.verify import SourceTrust
from digger.core.interfaces import Context
from digger.core.netsafe import SafeHttp
from digger.modules.localdb_sqlite import SQLiteLocalDB
from tests.fakes.fake_http import FakeHttp, FakeResponse
from tests.fixtures import make_pdfs

TRUST = SourceTrust(makers=("ti.com",), catalogs=("alldatasheet.com", "datasheet4u.com"))
TI = "https://www.ti.com/lit/ds/ne555.pdf"
ALL = "https://www.alldatasheet.com/%s.pdf"
D4U = "https://www.datasheet4u.com/ne555.pdf"


class FakeSource(SourceAdapter):
    """Источник из sources.json теста: отдаёт `urls` как ссылки на PDF; `open` — сначала открывает страницу."""
    adapter = "fake"

    def find(self, query, http):
        if self.entry.options.get("open"):
            http.get_html(self.entry.options["open"])
        return [Lead(url=u, kind="pdf", title="NE555 datasheet") for u in self.entry.options.get("urls", [])]


def source(sid, level, *urls, **options):
    return dict(id=sid, adapter="fake", level=level, domains=["%s.example" % sid], urls=list(urls), **options)


@pytest.fixture()
def env(tmp_path):
    cfg = {"paths": {"db": "data/digger.sqlite", "library_dir": "lib"}}
    db = SQLiteLocalDB({}, Context(cfg, str(tmp_path), logging.getLogger("t")))
    with open(make_pdfs.write("datasheet", tmp_path), "rb") as f:
        pdf = f.read()
    return tmp_path, AcquireStore(db), pdf


def build(env, fake, sources, adapter=FakeSource, min_interval=0, **kw):
    tmp_path, store, _ = env
    bus = EventBus()
    net = {"allowed_domains": ["ti.com", "alldatasheet.com", "datasheet4u.com", "a.example"],
           "min_interval_sec": min_interval}
    http = SafeHttp(net, str(tmp_path / "q"), logging.getLogger("t"), transport=fake)
    levels = list(dict.fromkeys(s["level"] for s in sources))
    registry = Registry({"levels": [{"id": lv} for lv in levels], "sources": sources}, bus=bus,
                        adapters={"fake": adapter})
    orch = Orchestrator(registry, http, store, bus=bus, trust=TRUST, sleep=lambda s: None, **kw)
    return orch, bus


def search(orch, **kw):
    return orch.search(PhotoContext(part="NE555P", manufacturer="Texas Instruments", package="DIP-8"), **kw)


def keys(bus):
    return [e.key for e in bus.history()]


def pdf_at(fake, pdf, *urls):
    for url in urls:
        fake.add(url, pdf, content_type="application/pdf")
    return fake


def test_maker_on_first_level_stops_search(env):
    fake = pdf_at(FakeHttp(), env[2], TI, ALL % "a")
    orch, bus = build(env, fake, [source("maker", "maker", TI), source("cat", "catalog", ALL % "a")])
    res = search(orch)
    assert res.status == "confirmed" and res.reason == "confirmed"
    assert os.path.isfile(res.path) and "confirmed" in res.path
    assert (res.queries, res.downloads, res.sources) == (1, 1, 1)
    assert [u for _, u in fake.calls] == [TI]                    # второй уровень не трогали
    got = keys(bus)
    assert got == ["local.search", "local.empty", "site.search", "site.found", "fetch.start", "fetch.done",
                   "quarantine.placed", "validate.ok", "verify.result", "result.confirmed"]
    last = bus.history()[-1]
    assert last.source == "maker" and last.params["queries"] == 1
    assert all(e.level == "maker" for e in bus.history()[2:-1])


def test_second_search_takes_document_from_library(env):
    fake = pdf_at(FakeHttp(), env[2], TI)
    orch, bus = build(env, fake, [source("maker", "maker", TI)])
    first = search(orch)
    calls = len(fake.calls)
    res = search(orch)
    assert res.status == "confirmed" and res.reason == "local" and res.path == first.path
    assert len(fake.calls) == calls
    assert keys(bus)[-3:] == ["local.search", "local.found", "result.confirmed"]


def test_only_probable_escalates_until_budget(env):
    urls = [ALL % n for n in "abc"]
    fake = pdf_at(FakeHttp(), env[2], *urls)
    sources = [source("s%d" % i, "level%d" % i, url) for i, url in enumerate(urls)]
    orch, bus = build(env, fake, sources, budget=Budget(downloads=2))
    res = search(orch)
    assert res.status == "probable" and res.reason == "budget" and res.downloads == 2
    assert "probable" in res.path and res.best.verdict.reasons == ["unconfirmed"]
    assert [u for _, u in fake.calls] == urls[:2]                # два уровня пройдено, третий — уже нет
    got = keys(bus)
    assert got.count("confirm.none") == 2 and got[-2:] == ["search.limit", "result.probable"]


def test_time_budget(env):
    fake = pdf_at(FakeHttp(), env[2], ALL % "a", ALL % "b")
    ticks = iter(range(0, 100000, 100))
    orch, bus = build(env, fake, [source("s1", "one", ALL % "a"), source("s2", "two", ALL % "b")],
                      budget=Budget(seconds=150), clock=lambda: next(ticks))
    res = search(orch)
    assert res.reason == "budget" and keys(bus)[-2] == "search.budget"
    assert bus.history()[-2].params["seconds"] == 150


def test_same_document_on_independent_sites_is_confirmed(env):
    fake = pdf_at(FakeHttp(), env[2], ALL % "a", D4U)
    orch, bus = build(env, fake, [source("s1", "one", ALL % "a"), source("s2", "two", D4U)])
    res = search(orch)
    assert res.status == "confirmed" and res.downloads == 2
    assert sorted(res.best.sources_agreeing) == ["alldatasheet.com", "datasheet4u.com"]
    assert "confirm.identical" in keys(bus)


def test_everywhere_ignores_stop_on_result(env):
    fake = pdf_at(FakeHttp(), env[2], TI, ALL % "a")
    orch, _ = build(env, fake, [source("maker", "maker", TI), source("cat", "catalog", ALL % "a")])
    res = search(orch, everywhere=True)
    assert res.status == "confirmed" and res.reason == "exhausted" and res.downloads == 2
    assert res.best.lead.source_id == "maker"


def test_all_unreachable_gives_clear_reason(env):
    fake = FakeHttp()
    fake.add_error("https://a.example/find", ConnectionResetError("сброс"))
    fake.routes[ALL % "a"] = FakeResponse(403, b"<title>Just a moment...</title>",
                                          {"Server": "cloudflare", "CF-RAY": "1"})
    sources = [source("a", "one", open="https://a.example/find"), source("s2", "two", ALL % "a"),
               source("s3", "three", "https://files.example.org/ne555.pdf")]
    orch, bus = build(env, fake, sources)
    res = search(orch)
    assert res.status == "not_found" and res.reason == "exhausted" and res.path == "" and res.sources == 3
    by_site = {f["site"]: f for f in res.failures}
    assert by_site["a.example"]["cls"] in ("transient", "network_blocked")
    assert by_site["www.alldatasheet.com"]["cls"] == "site_protected"
    assert by_site["www.alldatasheet.com"]["url"] == ALL % "a"
    assert by_site["files.example.org"]["cls"] == "not_whitelisted"
    got = keys(bus)
    assert "access.site_protected" in got and "access.not_whitelisted" in got and got[-1] == "result.not_found"
    assert bus.history()[-1].params["sources"] == 3
    assert res.downloads == 1                                   # ссылка вне белого списка бюджет не тратит


def test_cancel_in_the_middle(env):
    fake = pdf_at(FakeHttp(), env[2], ALL % "a", ALL % "b")
    orch, bus = build(env, fake, [source("s1", "one", ALL % "a"), source("s2", "two", ALL % "b")])
    unsubscribe = bus.subscribe(lambda e: e.key == "fetch.done" and orch.cancel())
    res = search(orch)
    unsubscribe()
    assert res.status == "cancelled" and res.reason == "cancelled"
    got = keys(bus)
    assert got[-1] == "search.cancelled" and not [k for k in got if k.startswith("result.")]
    assert [u for _, u in fake.calls] == [ALL % "a"]
    assert search(orch).status != "cancelled"                    # следующий поиск отмену не наследует


def test_broken_source_does_not_break_search(env):
    class Broken(FakeSource):
        def find(self, query, http):
            raise RuntimeError("разметка сайта изменилась")

    fake = pdf_at(FakeHttp(), env[2], TI)
    orch, bus = build(env, fake, [source("bad", "one"), source("maker", "maker", TI)])
    orch.registry._built["bad"] = Broken(orch.registry.entries("one")[0], bus=bus)
    res = search(orch)
    assert res.status == "confirmed" and "error.internal" in keys(bus)


def test_from_context_runs_real_sources(ctx, tmp_path):
    """Настоящие источники из sources.json на пустой подменной сети: поиск доходит до конца без ошибок."""
    fake = FakeHttp()
    net = dict(ctx.config["network"], offline=False, min_interval_sec=0)
    http = SafeHttp(net, str(tmp_path / "q"), logging.getLogger("t"), transport=fake)
    bus = EventBus()
    orch = from_context(ctx, bus, http=http)
    assert (orch.budget.queries, orch.budget.downloads, orch.budget.seconds) == (60, 10, 180)
    assert orch.parallel == 3
    assert http.is_allowed("https://www.ti.com/lit/ds/ne555.pdf")
    orch.budget = Budget(queries=25)
    orch.sleep = lambda s: None
    res = orch.search(PhotoContext(part="NE555P", manufacturer="Texas Instruments"))
    got = keys(bus)
    assert res.status == "not_found" and res.reason == "budget" and res.queries >= 25
    assert "error.internal" not in got and got[-1] == "result.not_found"
    assert got.count("search.limit") == 1        # соседние потоки уровня могут досказать своё после события
    assert len(set(e.lang for e in bus.history())) >= 2 and fake.calls
    assert orch.recorder.stats.search_count() == 1               # статистика поиска записана


# -------------------- шаг 6.2: параллельность --------------------

def gauge_source():
    """Источник, который считает, сколько таких же работает одновременно."""
    import threading
    import time

    class Gauge(FakeSource):
        lock, now, peak = threading.Lock(), 0, 0

        def find(self, query, http):
            with Gauge.lock:
                Gauge.now += 1
                Gauge.peak = max(Gauge.peak, Gauge.now)
            time.sleep(0.05)
            with Gauge.lock:
                Gauge.now -= 1
            return FakeSource.find(self, query, http)
    return Gauge


def test_sources_of_level_run_in_parallel_up_to_three(env):
    names = "abcdef"
    gauge = gauge_source()
    fake = FakeHttp()
    orch, bus = build(env, fake, [source("s" + n, "one", ALL % n) for n in names], adapter=gauge)
    assert orch.parallel == 3
    res = search(orch)
    assert 1 < gauge.peak <= 3
    assert (res.queries, res.sources) == (6, 6) and "error.internal" not in keys(bus)
    asked = list(dict.fromkeys(u for _, u in fake.calls))
    assert asked == [ALL % n for n in names[:4]]            # порядок ссылок — порядок источников, не потоков


def test_parallel_one_is_sequential(env):
    gauge = gauge_source()
    orch, _ = build(env, FakeHttp(), [source("s" + n, "one", ALL % n) for n in "abc"], adapter=gauge, parallel=1)
    search(orch)
    assert gauge.peak == 1


def test_parallel_sources_keep_domain_interval(env):
    """Три источника уровня ходят на один сайт одновременно — запросы к нему всё равно через 1.5 с."""
    import threading

    from tests.fakes.fake_http import VirtualTime

    vt, times, lock = VirtualTime(), [], threading.Lock()

    class Timed(FakeHttp):
        def __call__(self, method, url, headers):
            with lock:
                times.append(vt.clock())
            return FakeHttp.__call__(self, method, url, headers)

    fake = Timed()
    sources = []
    for n in "abc":
        fake.add("https://a.example/find?q=" + n, "<html><body>ok</body></html>")
        sources.append(source("s" + n, "one", open="https://a.example/find?q=" + n))
    orch, bus = build(env, fake, sources, min_interval=1.5)
    orch.http._clock, orch.http._sleep = vt.clock, vt.sleep
    search(orch)
    times.sort()
    assert len(times) == 3 and times[0] == vt.start
    assert all(b - a >= 1.5 for a, b in zip(times, times[1:]))


def test_budget_event_once_from_parallel_sources(env):
    orch, bus = build(env, FakeHttp(), [source("s" + n, "one", ALL % n) for n in "abcdef"],
                      budget=Budget(queries=2))
    res = search(orch)
    assert res.reason == "budget" and keys(bus).count("search.limit") == 1


# -------------------- шаг 8.3b: порядок источников и бюджет --------------------

class FakeEngine(FakeSource):
    """Поисковик теста: семейство `engine`, после капчи «отдыхает» (как `engine_html`)."""
    adapter = "engine"
    family = "engine"

    def find(self, query, http):
        from digger.acquire.sources.base import SourceError
        if self.entry.options.get("captcha"):
            self._clock, self._rest_until = (lambda: 0.0), 15 * 60.0
            raise SourceError("engine.captcha", "captcha", minutes=15)
        return super().find(query, http)


def build_mixed(env, fake, sources, **kw):
    orch, bus = build(env, fake, sources, **kw)
    orch.registry._adapters = {"fake": FakeSource, "engine": FakeEngine}
    return orch, bus


def test_sites_of_all_levels_are_asked_before_engines(env):
    fake = pdf_at(FakeHttp(), env[2], ALL % "a")
    sources = [dict(source("eng", "catalog", ALL % "x"), adapter="engine"), source("cat", "catalog"),
               source("late", "russian", ALL % "a")]
    orch, bus = build_mixed(env, fake, sources, parallel=1, langs=["en"])
    res = search(orch)
    asked = [e.source for e in bus.history() if e.key in ("site.search", "engine.query")]
    assert asked == ["cat", "late", "eng"]                   # сайт последнего уровня — раньше поисковика первого
    assert res.status == "probable" and res.best.lead.source_id == "late"
    assert [u for _, u in fake.calls][0] == ALL % "a"        # и его документ скачан раньше, чем ссылка поисковика


def test_resting_engine_does_not_spend_query_budget(env):
    fake = pdf_at(FakeHttp(), env[2], ALL % "a")
    sources = [dict(source("eng", "search"), adapter="engine", captcha=True), source("cat", "catalog", ALL % "a")]
    orch, bus = build_mixed(env, fake, sources, parallel=1, langs=["en"])
    first = search(orch)
    assert first.queries == 2                                 # сайт и поисковик, получивший капчу
    res = search(orch, everywhere=True)                       # поисковик отдыхает: в сеть не ходит
    mine = [e for e in bus.history() if e.source == "eng"][-1]
    assert mine.key == "engine.captcha" and mine.params["minutes"] == 15
    assert res.queries == 1 and res.sources == 1
    assert [f["reason"] for f in res.failures if f["source"] == "eng"] == ["captcha"]


def test_site_with_timeout_is_skipped_until_end_of_session(env):
    import socket
    fake = pdf_at(FakeHttp(), env[2], ALL % "a")
    fake.add_error("https://a.example/find", socket.timeout("timed out"))
    sources = [source("a", "one", open="https://a.example/find"), source("s2", "two", ALL % "a")]
    orch, bus = build(env, fake, sources)
    first = search(orch, everywhere=True)
    assert [f["reason"] for f in first.failures] == ["read_timeout"]
    calls = len([u for _, u in fake.calls if "a.example" in u])
    res = search(orch, everywhere=True)
    assert len([u for _, u in fake.calls if "a.example" in u]) == calls        # к сайту больше не обращались
    skips = [e for e in bus.history() if e.key == "site.slow_skip"]
    assert len(skips) == 1 and skips[0].params["site"] == "a.example" and skips[0].source == "a"
    assert res.queries == 1 and res.status == "probable"      # второй источник спрошен и дал документ
    assert [(f["site"], f["cls"], f["reason"]) for f in res.failures] == [("a.example", "transient", "read_timeout")]


def test_lead_to_slow_site_is_not_followed(env):
    fake = pdf_at(FakeHttp(), env[2], ALL % "a")
    orch, bus = build(env, fake, [source("s1", "one", "https://www.alldatasheet.com/slow.pdf", ALL % "a")])
    orch._slow["alldatasheet.com"] = "read_timeout"
    res = search(orch)
    assert res.downloads == 0 and fake.calls == [] and res.status == "not_found"
    assert keys(bus).count("site.slow_skip") == 1             # один раз за поиск, а не на каждую ссылку
    assert res.conclusion is not None


def test_maker_is_taken_from_part_family_when_photo_gave_none(env):
    """Поиск по одному партномеру: STM32 — это ST; названный производитель и общие ряды (NE555) не трогаются."""
    orch, _ = build(env, FakeHttp(), [source("a", "catalog")])
    ctx = PhotoContext(part="STM32F103C8T6")
    orch.search(ctx)
    assert (orch._ctx.manufacturer, orch._ctx.maker_guessed) == ("STMicroelectronics", True)
    assert ctx.manufacturer == ""                                   # запись вызывающего не меняется
    orch.search(PhotoContext(part="STM32F103C8T6", manufacturer="GigaDevice"))
    assert (orch._ctx.manufacturer, orch._ctx.maker_guessed) == ("GigaDevice", False)
    orch.search(PhotoContext(part="NE555"))
    assert (orch._ctx.manufacturer, orch._ctx.maker_guessed) == ("", False)
