# -*- coding: utf-8 -*-
"""Китайские каталоги шага 2.10: адаптер direct_url (LCSC/szlcsc, Semiee). Вёрстка синтетическая.

Шаг 8.6a: szlcsc отвечает кодом 203 и страницей из одного запутанного скрипта (ставит cookie и перезагружает
страницу) — это защита сайта, а не «не найдено»; Semiee отдаёт выдачу только после входа — источник выключен."""
import logging
import os


from digger.acquire.events import EventBus
from digger.acquire.query import Query
from digger.acquire.registry import ADAPTERS, Registry
from digger.acquire.sources.base import SourceEntry
from digger.acquire.sources.catalogs.china import DirectUrl, links
from digger.acquire.sources.catalogs.common import is_blocked
from digger.core.netsafe import SafeHttp
from tests.fakes.fake_http import FakeHttp

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
Q = Query("zh", "W25Q64JV datasheet pdf", "part")
SRC = Registry.load(os.path.join(APP, "data", "sources.json"))
SEARCH = "https://so.szlcsc.com/global.html?k=W25Q64JV"
DETAIL = "https://item.szlcsc.com/97521.html"
PDF = "https://atta.szlcsc.com/upload/public/pdf/source/W25Q64JV.pdf"
PAGE = ('<a href="%s">W25Q64JVSSIQ 闪存</a><a href="/about.html">关于</a>'
        '<a href="https://evil.example/W25Q64JV.pdf">x</a><a href="%s">W25Q64JV.pdf</a>')


def make(tmp_path, sid="lcsc", clock=None, **over):
    entry = next(s for s in SRC.entries(include_disabled=True) if s.id == sid).to_dict()
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    fake = FakeHttp()
    http = SafeHttp({"min_interval_sec": 0}, str(tmp_path), logging.getLogger("t"), transport=fake)
    http.add_allowed(["szlcsc.com", "semiee.com", "evil.example"])
    kwargs = {"clock": clock} if clock else {}
    return DirectUrl(SourceEntry.from_dict(dict(entry, **over)), bus=bus, **kwargs), http, fake, events


def test_registered_and_in_sources_json():
    assert ADAPTERS["direct_url"] is DirectUrl
    assert "lcsc" in {a.id for a in SRC.build("china")}
    assert "direct_url" not in SRC.missing


def test_semiee_disabled_with_reason():
    """Выдача Semiee приходит запросом, который без входа отвечает «请登录»: источник выключен, причина записана."""
    assert "semiee" not in {a.id for a in SRC.build("china")}
    entry = next(s for s in SRC.entries(include_disabled=True) if s.id == "semiee")
    assert not entry.enabled and "вход" in entry.options["note"]


def test_links_only_source_domains_and_part():
    details, pdfs = links(PAGE % (DETAIL, PDF), SEARCH, ["szlcsc.com"], ["W25Q64JV"])
    assert details == [(DETAIL, "W25Q64JVSSIQ 闪存")] and pdfs == [PDF]


def test_search_follows_detail_for_pdf(tmp_path):
    ad, http, fake, events = make(tmp_path)
    fake.add(SEARCH, (PAGE % (DETAIL, PDF)).encode("utf-8"))
    fake.add(DETAIL, ('<a href="%s">下载 datasheet</a>' % "/upload/W25Q64JV_rev.pdf").encode("utf-8"))
    leads = ad.search(Q, http)
    assert PDF in [l.url for l in leads if l.kind == "pdf"]
    assert "https://item.szlcsc.com/upload/W25Q64JV_rev.pdf" in [l.url for l in leads if l.kind == "pdf"]
    assert DETAIL in [l.url for l in leads if l.kind == "page"]
    assert [e.key for e in events] == ["site.search", "site.found"]
    assert all(l.source_id == "lcsc" and l.level == "china" for l in leads)


def test_empty_script_page_gives_no_leads(tmp_path):
    ad, http, fake, events = make(tmp_path, "semiee")
    fake.add("https://www.semiee.com/search?searchModel=W25Q64JV", b'<div id="pageData"></div>')
    assert ad.search(Q, http) == []
    assert events[-1].key == "site.empty"


def test_waf_block_is_captcha_and_rests(tmp_path):
    now = [1000.0]
    ad, http, fake, events = make(tmp_path, clock=lambda: now[0])
    fake.add(SEARCH, "<html><title>WAF拦截页面</title></html>".encode("utf-8"), status=403)
    assert ad.search(Q, http) == [] and events[-1].key == "engine.captcha" and events[-1].params["minutes"] == 15
    n = len(fake.calls) if hasattr(fake, "calls") else None
    assert ad.search(Q, http) == [] and events[-1].key == "engine.captcha"
    if n is not None:
        assert len(fake.calls) == n                      # в отдых в сеть не ходим
    now[0] += 16 * 60
    fake.add(SEARCH, (PAGE % (DETAIL, PDF)).encode("utf-8"))
    assert PDF in [l.url for l in ad.search(Q, http) if l.kind == "pdf"]


# Заглушка szlcsc (записана 2026-10-12, скрипт сокращён): слова cookie и reload лежат в таблице строк,
# «document.cookie» в тексте нет.
STUB = ('<!DOCTYPE html><html> <head> <meta charset="UTF-8"> <script> var _xvasu = 1104951776; var _xvtsc = 300; '
        'var _xvpfs = "tws2_"; </script> <script> function a3_0x5716(){var _0x13562e=[\';expires=\',\'call\','
        '\'chain\',\'length\',\'charCodeAt\',\'input\',\'counter\',\'reload\',\'string\',\'test\','
        '\'fromCharCode\',\'cookie\',\'stateObject\',\'constructor\',\'apply\',\'setTime\',\'join\'];'
        'a3_0x5716=function(){return _0x13562e;};return a3_0x5716();}</script> </head><body></body></html>')


def test_script_stub_203_is_protection_not_empty(tmp_path):
    now = [1000.0]
    ad, http, fake, events = make(tmp_path, clock=lambda: now[0])
    fake.add(SEARCH, STUB.encode("utf-8"), status=203)
    assert ad.search(Q, http) == []
    assert events[-1].key == "engine.captcha" and events[-1].params["minutes"] == 15
    calls = len(fake.calls)
    assert ad.search(Q, http) == [] and len(fake.calls) == calls      # в отдых в сеть не ходим


def test_page_with_text_and_203_is_not_protection():
    assert is_blocked(203, STUB)
    assert not is_blocked(200, STUB)                                  # тот же скрипт на обычной странице — не признак
    assert not is_blocked(203, "<html><body><p>cookie policy</p><a href=\"/x\">reload</a>"
                               + "текст страницы " * 50 + "</body></html>")


def test_other_part_empty_query(tmp_path):
    ad, http, fake, events = make(tmp_path)
    assert ad.find("", http) == []


def test_chipdip_product_and_pdf_links():
    """Шаг 2.12: выдача Чип и Дип — карточки с партномером в адресе, PDF на static.chipdip.ru."""
    page = ('<a href="/product/ne555dr-taymer-555-texas-instruments-64173">NE555DR таймер</a>'
            '<a href="/search/video?searchtext=NE555">Видео</a>'
            '<a href="https://static.chipdip.ru/lib2/b/247/DOC087247989.pdf">PDF</a>')
    details, pdfs = links(page, "https://www.chipdip.ru/search", ["chipdip.ru", "static.chipdip.ru"], ["NE555"])
    assert pdfs == ["https://static.chipdip.ru/lib2/b/247/DOC087247989.pdf"]
    assert details[0][0].endswith("-64173")
