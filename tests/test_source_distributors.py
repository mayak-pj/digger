# -*- coding: utf-8 -*-
"""API дистрибьюторов Mouser и DigiKey (шаг 9.3). Ответы — придуманные, по описанию API."""
import json
import logging
import os

import pytest

from digger.acquire.events import LANGS, EventBus, render
from digger.acquire.query import Query
from digger.acquire.registry import ADAPTERS, Registry
from digger.acquire.sources.distributors import DigikeyApi, MouserApi, NexarApi
from digger.core.netsafe import NetBlocked, SafeHttp
from tests.fakes.fake_http import FakeHttp

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join("sources", "distributors")
JSON = "application/json"
MOUSER_KEY = {"key": "mouser-secret-1"}
MOUSER_URL = "https://api.mouser.com/api/v1/search/partnumber?apiKey=mouser-secret-1"
DIGIKEY_KEY = {"client_id": "dk-client", "client_secret": "dk-secret-2"}
TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_URL = "https://api.digikey.com/products/v4/search/keyword"
NEXAR_KEY = {"client_id": "nx-client", "client_secret": "nx-secret-3"}
NEXAR_TOKEN_URL = "https://identity.nexar.com/connect/token"
NEXAR_URL = "https://api.nexar.com/graphql"
KEYS = {"mouser": MOUSER_KEY, "digikey": DIGIKEY_KEY, "nexar": NEXAR_KEY}


def make(tmp_path, source_id, keys=KEYS):
    bus, events, fake = EventBus(), [], FakeHttp()
    bus.subscribe(events.append)
    reg = Registry.load(os.path.join(APP, "data", "sources.json"), keys=keys, bus=bus)
    http = SafeHttp({"min_interval_sec": 0}, str(tmp_path), logging.getLogger("t"), transport=fake)
    http.add_allowed(reg.allowed_domains())
    return reg.engine(source_id), http, fake, events


def part(text):
    return Query("en", text, "part", part=text)


def test_registered_in_sources_json_and_whitelist():
    reg = Registry.load(os.path.join(APP, "data", "sources.json"), keys=KEYS)
    by = {a.id: a for a in reg.build("world")}
    assert ADAPTERS["mouser_api"] is MouserApi and ADAPTERS["digikey_api"] is DigikeyApi and not reg.missing
    assert by["mouser_api"].available and by["digikey_api"].available
    assert by["mouser_api"].family == "market" and by["mouser_api"].label == "Mouser"
    assert ADAPTERS["nexar_api"] is NexarApi and by["nexar_api"].available and by["nexar_api"].label == "Nexar"
    assert {"api.mouser.com", "api.digikey.com", "api.nexar.com", "identity.nexar.com"} <= set(reg.allowed_domains())
    assert list(by) .index("mouser_api") < list(by).index("world_queries")       # API — раньше поисковиков


def test_default_config_has_empty_keys():
    with open(os.path.join(APP, "config.default.json"), encoding="utf-8") as f:
        keys = json.load(f)["acquire"]["api_keys"]
    assert keys["mouser"] == {"key": ""} and keys["digikey"] == {"client_id": "", "client_secret": ""}
    assert keys["nexar"] == {"client_id": "", "client_secret": ""}


@pytest.mark.parametrize("source_id, keys", [
    ("mouser_api", {}), ("mouser_api", {"mouser": {"key": ""}}),
    ("digikey_api", {}), ("digikey_api", {"digikey": {"client_id": "x", "client_secret": ""}}),
    ("nexar_api", {}), ("nexar_api", {"nexar": {"client_id": "x", "client_secret": ""}})])
def test_no_key_skips_without_request(tmp_path, source_id, keys):
    ad, http, fake, events = make(tmp_path, source_id, keys)
    assert ad.search(part("NE555"), http) == []
    assert fake.calls == [] and [e.key for e in events] == ["engine.no_key"]


def test_mouser_gives_datasheet_links(tmp_path):
    ad, http, fake, events = make(tmp_path, "mouser_api")
    fake.add_fixture(MOUSER_URL, os.path.join(FIX, "mouser_ok.json"), content_type=JSON)
    leads = ad.search(part("NE555"), http)
    assert [l.url for l in leads] == ["https://www.ti.com/lit/gpn/ne555",                    # повтор ссылки убран
                                      "https://www.mouser.com/datasheet/2/000/ne555-example.pdf"]   # http → https
    assert leads[0].title == "NE555P Texas Instruments" and "Precision Timer" in leads[0].snippet
    assert all(l.kind == "pdf" and l.source_id == "mouser_api" and l.level == "world" for l in leads)
    assert fake.calls == [("POST", MOUSER_URL)]
    url, headers, body = fake.sent[0]
    assert json.loads(body.decode("utf-8")) == {"SearchByPartRequest": {"mouserPartNumber": "NE555", "partSearchOptions": ""}}
    assert headers["Content-Type"] == JSON
    assert [e.key for e in events] == ["market.search", "market.found"] and events[-1].params["pdfs"] == 2
    assert render(events[-1], "ru").startswith(u"поиск на маркетплейсах: Mouser")


def test_mouser_empty_and_errors(tmp_path):
    ad, http, fake, events = make(tmp_path, "mouser_api")
    fake.add_fixture(MOUSER_URL, os.path.join(FIX, "mouser_empty.json"), content_type=JSON)
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "market.empty"
    fake.add_fixture(MOUSER_URL, os.path.join(FIX, "mouser_bad_key.json"), content_type=JSON)
    assert ad.search(part("NE555"), http) == []
    assert events[-1].key == "engine.error" and "Invalid unique identifier" in events[-1].params["detail"]
    fake.add(MOUSER_URL, json.dumps({"Errors": [{"Message": "Maximum calls per day exceeded."}]}), content_type=JSON)
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "engine.quota"
    for status, key in ((429, "engine.quota"), (403, "engine.quota"), (500, "engine.error")):
        fake.add(MOUSER_URL, "{}", status=status, content_type=JSON)
        assert ad.search(part("NE555"), http) == [] and events[-1].key == key
    fake.add(MOUSER_URL, "<html>не JSON</html>")
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "engine.error"
    fake.add_error(MOUSER_URL, ConnectionError("down"))
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "engine.error"


def test_digikey_token_then_search(tmp_path):
    ad, http, fake, events = make(tmp_path, "digikey_api")
    fake.add_fixture(TOKEN_URL, os.path.join(FIX, "digikey_token.json"), content_type=JSON)
    fake.add_fixture(DIGIKEY_URL, os.path.join(FIX, "digikey_ok.json"), content_type=JSON)
    leads = ad.search(part("W25Q64JV"), http)
    assert [l.url for l in leads] == ["https://www.winbond.com/resource-files/w25q64jv-example.pdf",
                                      "https://media.digikey.com/pdf/Data%20Sheets/Example/W25Q64JV.pdf"]
    assert leads[0].title == "W25Q64JVSSIQ Winbond Electronics" and leads[0].snippet == "IC FLASH 64MBIT SPI/QUAD 8SOIC"
    assert fake.calls == [("POST", TOKEN_URL), ("POST", DIGIKEY_URL)]
    (_, token_headers, token_body), (_, headers, body) = fake.sent
    assert token_headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert token_body == b"client_id=dk-client&client_secret=dk-secret-2&grant_type=client_credentials"
    assert headers["Authorization"] == "Bearer tok-123" and headers["X-DIGIKEY-Client-Id"] == "dk-client"
    assert json.loads(body.decode("utf-8")) == {"Keywords": "W25Q64JV", "Limit": 10, "Offset": 0}
    assert [e.key for e in events] == ["market.search", "market.found"]


def test_digikey_token_is_reused_until_it_expires(tmp_path):
    ad, http, fake, _ = make(tmp_path, "digikey_api")
    now = [100.0]
    ad._clock = lambda: now[0]
    fake.add_fixture(TOKEN_URL, os.path.join(FIX, "digikey_token.json"), content_type=JSON)
    fake.add_fixture(DIGIKEY_URL, os.path.join(FIX, "digikey_ok.json"), content_type=JSON)
    ad.search(part("W25Q64JV"), http)
    now[0] += 500
    ad.search(part("W25Q64JV"), http)
    assert [u for _, u in fake.calls].count(TOKEN_URL) == 1
    now[0] += 100                                              # 599 с минус запас в минуту — срок вышел
    ad.search(part("W25Q64JV"), http)
    assert [u for _, u in fake.calls].count(TOKEN_URL) == 2


def test_digikey_errors(tmp_path):
    ad, http, fake, events = make(tmp_path, "digikey_api")
    fake.add(TOKEN_URL, json.dumps({"ErrorMessage": "Invalid clientId"}), status=401, content_type=JSON)
    assert ad.search(part("W25Q64JV"), http) == []
    assert events[-1].key == "engine.error" and u"ключ не принят" in events[-1].params["detail"]
    assert fake.calls == [("POST", TOKEN_URL)]
    fake.add(TOKEN_URL, "{}", content_type=JSON)
    assert ad.search(part("W25Q64JV"), http) == [] and events[-1].key == "engine.error"
    fake.add_fixture(TOKEN_URL, os.path.join(FIX, "digikey_token.json"), content_type=JSON)
    fake.add(DIGIKEY_URL, "{}", status=429, content_type=JSON)
    assert ad.search(part("W25Q64JV"), http) == [] and events[-1].key == "engine.quota"
    fake.add(DIGIKEY_URL, "{}", status=401, content_type=JSON)                # пропуск отозван — берётся новый
    before = [u for _, u in fake.calls].count(TOKEN_URL)
    ad.search(part("W25Q64JV"), http)
    fake.add_fixture(DIGIKEY_URL, os.path.join(FIX, "digikey_ok.json"), content_type=JSON)
    assert len(ad.search(part("W25Q64JV"), http)) == 2
    assert [u for _, u in fake.calls].count(TOKEN_URL) == before + 1


def nexar_ready(fake):
    fake.add_fixture(NEXAR_TOKEN_URL, os.path.join(FIX, "nexar_token.json"), content_type=JSON)


def test_nexar_token_then_graphql(tmp_path):
    ad, http, fake, events = make(tmp_path, "nexar_api")
    nexar_ready(fake)
    fake.add_fixture(NEXAR_URL, os.path.join(FIX, "nexar_ok.json"), content_type=JSON)
    leads = ad.search(part("NE555"), http)
    assert [l.url for l in leads] == ["https://www.ti.com/lit/gpn/ne555", "https://octopart-example.net/ne555-revb.pdf"]
    assert leads[0].title == "NE555P Texas Instruments" and leads[0].snippet == "Precision Timer"
    assert all(l.kind == "pdf" and l.source_id == "nexar_api" and l.level == "world" for l in leads)
    assert fake.calls == [("POST", NEXAR_TOKEN_URL), ("POST", NEXAR_URL)]
    (_, token_headers, token_body), (_, headers, body) = fake.sent
    assert token_body == b"client_id=nx-client&client_secret=nx-secret-3&grant_type=client_credentials"
    assert headers["Authorization"] == "Bearer nx-tok-9"
    sent = json.loads(body.decode("utf-8"))
    assert sent["variables"] == {"q": "NE555"} and "supSearchMpn" in sent["query"] and "bestDatasheet" in sent["query"]
    assert [e.key for e in events] == ["market.search", "market.found"]


def test_nexar_token_is_reused(tmp_path):
    ad, http, fake, _ = make(tmp_path, "nexar_api")
    nexar_ready(fake)
    fake.add_fixture(NEXAR_URL, os.path.join(FIX, "nexar_ok.json"), content_type=JSON)
    ad.search(part("NE555"), http)
    ad.search(part("NE555"), http)
    assert [u for _, u in fake.calls].count(NEXAR_TOKEN_URL) == 1


def test_nexar_errors(tmp_path):
    ad, http, fake, events = make(tmp_path, "nexar_api")
    fake.add(NEXAR_TOKEN_URL, json.dumps({"error": "invalid_client"}), status=400, content_type=JSON)
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "engine.error"
    nexar_ready(fake)
    fake.add(NEXAR_URL, json.dumps({"errors": [{"message": "Part limit exceeded for this plan"}]}), content_type=JSON)
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "engine.quota"
    fake.add(NEXAR_URL, json.dumps({"errors": [{"message": "Cannot query field x"}]}), content_type=JSON)
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "engine.error"
    fake.add(NEXAR_URL, json.dumps({"data": {"supSearchMpn": {"results": []}}}), content_type=JSON)
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "market.empty"
    fake.add(NEXAR_URL, "{}", status=429, content_type=JSON)
    assert ad.search(part("NE555"), http) == [] and events[-1].key == "engine.quota"
    fake.add(NEXAR_URL, "{}", status=401, content_type=JSON)                 # пропуск отозван — берётся новый
    before = [u for _, u in fake.calls].count(NEXAR_TOKEN_URL)
    ad.search(part("NE555"), http)
    fake.add_fixture(NEXAR_URL, os.path.join(FIX, "nexar_ok.json"), content_type=JSON)
    assert len(ad.search(part("NE555"), http)) == 2
    assert [u for _, u in fake.calls].count(NEXAR_TOKEN_URL) == before + 1


def test_nexar_secrets_stay_out(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        ad, http, fake, events = make(tmp_path, "nexar_api")
        nexar_ready(fake)
        fake.add(NEXAR_URL, "{}", status=500, content_type=JSON)
        ad.search(part("NE555"), http)
        text = " ".join(render(e, lang) for e in events for lang in LANGS) + repr([e.to_dict() for e in events])
    for secret in ("nx-secret-3", "nx-tok-9"):
        assert secret not in text and secret not in caplog.text


def test_secrets_stay_out_of_events_and_logs(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        for source_id, bad in (("mouser_api", MOUSER_URL), ("digikey_api", DIGIKEY_URL)):
            ad, http, fake, events = make(tmp_path, source_id)
            fake.add_fixture(TOKEN_URL, os.path.join(FIX, "digikey_token.json"), content_type=JSON)
            fake.add(bad, "{}", status=500, content_type=JSON)
            ad.search(part("NE555"), http)
            fake.add_error(bad, ConnectionError("down"))
            ad.search(part("NE555"), http)
            text = " ".join(render(e, lang) for e in events for lang in LANGS) + repr([e.to_dict() for e in events])
            for secret in ("mouser-secret-1", "dk-secret-2", "tok-123"):
                assert secret not in text
    assert "api.mouser.com" in caplog.text and "api.digikey.com" in caplog.text
    for secret in ("mouser-secret-1", "dk-secret-2", "tok-123"):
        assert secret not in caplog.text


def test_api_request_never_follows_redirect(tmp_path):
    """Ключ в теле или заголовках на другой адрес не уходит — даже на разрешённый."""
    fake = FakeHttp()
    http = SafeHttp({"min_interval_sec": 0}, str(tmp_path), logging.getLogger("t"), transport=fake)
    http.add_allowed(["api.digikey.com", "ti.com"])
    fake.add_redirect(TOKEN_URL, "https://www.ti.com/collect")
    with pytest.raises(NetBlocked):
        http.post_json(TOKEN_URL, form={"client_secret": "s"})
    assert fake.calls == [("POST", TOKEN_URL)]
    with pytest.raises(NetBlocked):                              # белый список и HTTPS — как у любого запроса
        http.post_json("https://api.example.org/x", {"a": 1})
    with pytest.raises(NetBlocked):
        http.post_json("http://api.digikey.com/v1/oauth2/token", {"a": 1})
    assert len(fake.calls) == 1
