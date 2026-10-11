# -*- coding: utf-8 -*-
"""Адаптер сайтов производителей (шаг 2.7): шаблоны URL, проверка первых байт, события."""
import logging
import os

from digger.acquire.events import EventBus
from digger.acquire.query import Query
from digger.acquire.registry import ADAPTERS, Registry
from digger.acquire.sources.base import SourceEntry
from digger.acquire.sources.makers import MakerUrl
from digger.core.netsafe import SafeHttp
from tests.fakes.fake_http import FakeHttp

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TI = "https://www.ti.com/lit/ds/symlink/{part_lower}.pdf"
ENTRY = {"id": "maker_ti", "adapter": "maker_url", "name": "Texas Instruments", "level": "maker", "lang": "en",
         "domains": ["ti.com"], "prefixes": ["^(SN|TL|LM)"], "urls": [TI]}
PDF = b"%PDF-1.5\n1 0 obj\n<<>>\nendobj\n"


def make(tmp_path, entry=ENTRY):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    fake = FakeHttp()
    http = SafeHttp({"min_interval_sec": 0}, str(tmp_path), logging.getLogger("t"), transport=fake)
    http.add_allowed(["ti.com", "www.wch-ic.com", "wch-ic.com"])
    return MakerUrl(SourceEntry.from_dict(entry), bus=bus), http, fake, events


def q(text):
    return Query("en", text, "part")


def test_registered_and_in_sources_json():
    reg = Registry.load(os.path.join(APP, "data", "sources.json"))
    built = [a for a in reg.build("maker") if isinstance(a, MakerUrl)]
    assert ADAPTERS["maker_url"] is MakerUrl and "maker_ti" in [a.id for a in built]
    assert "maker_ti" not in reg.missing
    assert "ti.com" in reg.allowed_domains()


def test_pdf_found_with_suffix_stripped(tmp_path):
    ad, http, fake, events = make(tmp_path)
    fake.add("https://www.ti.com/lit/ds/symlink/sn74hc595dr.pdf", PDF, content_type="application/pdf")
    fake.add("https://www.ti.com/lit/ds/symlink/sn74hc595.pdf", PDF, content_type="application/pdf")
    leads = ad.search(q("SN74HC595DR datasheet pdf"), http)
    assert leads and all(l.kind == "pdf" and l.source_id == "maker_ti" and l.level == "maker" for l in leads)
    assert {l.url.rsplit("/", 1)[1] for l in leads} == {"sn74hc595dr.pdf", "sn74hc595.pdf"}
    assert [e.key for e in events] == ["maker.search", "maker.found"]
    assert events[-1].params["pdfs"] == len(leads)


def test_html_instead_of_pdf_rejected(tmp_path):
    ad, http, fake, events = make(tmp_path)
    fake.add("https://www.ti.com/lit/ds/symlink/lm358.pdf", "<html><body>Страница ошибки</body></html>")
    assert ad.search(q("LM358"), http) == []
    assert events[-1].key == "maker.empty"


def test_404_is_empty(tmp_path):
    ad, http, fake, events = make(tmp_path)
    assert ad.search(q("LM358"), http) == []
    assert events[-1].key == "maker.empty"


def test_foreign_part_no_request_no_events(tmp_path):
    ad, http, fake, events = make(tmp_path)
    assert ad.search(q("STM32F103C8 datasheet pdf"), http) == []
    assert fake.calls == [] and events == []


def test_network_failure_reported_as_error(tmp_path):
    ad, http, fake, events = make(tmp_path)
    fake.add_error("https://www.ti.com/lit/ds/symlink/lm358.pdf", OSError("reset"))
    assert ad.search(q("LM358"), http) == []
    assert events[-1].key == "engine.error"


def test_page_template_checks_html(tmp_path):
    entry = {"id": "maker_wch", "adapter": "maker_url", "name": "WCH", "level": "maker", "domains": ["wch-ic.com"],
             "prefixes": ["^CH\\d"], "page_urls": ["https://www.wch-ic.com/products/{part}.html"]}
    ad, http, fake, events = make(tmp_path, entry)
    fake.add("https://www.wch-ic.com/products/CH340G.html", "<!DOCTYPE html><html><title>CH340</title></html>")
    leads = ad.search(q("CH340G datasheet"), http)
    assert [l.kind for l in leads] == ["page"] and leads[0].url.endswith("CH340G.html")


def test_sources_json_addresses_checked_live(tmp_path):
    """Адреса из sources.json, по которым сайты на самом деле отдают PDF (живая проба 2026-10-11, шаг 8.4b)."""
    reg = Registry.load(os.path.join(APP, "data", "sources.json"))
    makers = {a.id: a for a in reg.build("maker") if isinstance(a, MakerUrl)}
    fake = FakeHttp()
    http = SafeHttp({"min_interval_sec": 0}, str(tmp_path), logging.getLogger("t"), transport=fake)
    http.add_allowed(reg.allowed_domains())
    for source, part, url in [
        ("maker_ti", "NE555", "https://www.ti.com/lit/ds/symlink/ne555.pdf"),
        ("maker_espressif", "ESP8266EX",
         "https://www.espressif.com/sites/default/files/documentation/0a-esp8266ex_datasheet_en.pdf"),
        ("maker_winbond", "W25Q64JV", "https://www.winbond.com/hq/search-resource-file.jsp?partNo=W25Q64JV&type=datasheet"),
    ]:
        fake.add(url, PDF, content_type="application/pdf")
        leads = makers[source].search(q(part + " datasheet pdf"), http)
        assert [l.url for l in leads] == [url], source
    assert not makers["maker_winbond"].applies("NE555") and not makers["maker_ti"].applies("W25Q64JV")
