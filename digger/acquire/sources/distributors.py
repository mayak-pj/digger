# -*- coding: utf-8 -*-
"""API дистрибьюторов: Mouser, DigiKey и Nexar/Octopart (ARCHITECTURE §4.2, шаг 9.3).

Дистрибьютор по партномеру отдаёт карточки товаров, в карточке — ссылка на datasheet; поисковики и разбор
страниц не нужны. Работают только с ключом (`config.json → acquire.api_keys`), без ключа — «нет ключа, пропуск»:

    "mouser":  {"key": "…"}                                  mouser.com → My Account → APIs → Search API
    "digikey": {"client_id": "…", "client_secret": "…"}      developer.digikey.com → Product Information V4
    "nexar":   {"client_id": "…", "client_secret": "…"}      nexar.com → Apps (Octopart, GraphQL)

Ключ уходит только на сайт своего дистрибьютора (`SafeHttp.post_json`, перенаправления запрещены) и не попадает
ни в события, ни в журналы. Результат — ссылки на datasheet (`kind="pdf"`); страницы товаров не берутся.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import quote_plus, urlsplit

from ..models import Lead
from ..registry import register
from .base import SourceAdapter, SourceError

log = logging.getLogger("digger.acquire.distributors")
MAX_LEADS = 5
_QUOTA = ("HTTP 429", "HTTP 403")        # лимит запросов или суточная квота
_DENIED = ("HTTP 401",)                  # ключ не принят


def _norm(text: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def _link(url: Any) -> str:
    """Ссылка на datasheet из карточки: DigiKey отдаёт и «//media.digikey.com/…», http поднимается до https."""
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") and urlsplit(url).hostname else ""


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


class DistributorApi(SourceAdapter):
    """Общее для дистрибьюторов: запрос по партномеру → карточки (партномер, производитель, описание, datasheet)."""
    family = "market"
    kinds = ("pdf",)
    title = ""

    def find(self, query: Any, http: Any) -> List[Lead]:
        part = (getattr(query, "part", "") or getattr(query, "text", query) or "").strip()
        if not part:
            return []
        try:
            cards = list(self.cards(part, http))
        except SourceError:
            raise
        except Exception as e:      # noqa: BLE001 — сеть, код ≥ 400, не JSON: ключ в сообщение не попадает
            detail = str(e) or type(e).__name__
            log.warning("%s API: %s", self.title, detail)
            if detail in _DENIED:
                raise SourceError("engine.error", "ключ не принят (%s)" % detail)
            raise SourceError("engine.quota" if detail in _QUOTA else "engine.error", detail, cause=e)
        want, leads, seen = _norm(part), [], set()
        for mpn, maker, description, datasheet in cards:
            have, url = _norm(mpn), _link(datasheet)
            if not url or url in seen or not have or not (have.startswith(want) or want.startswith(have)):
                continue
            seen.add(url)
            leads.append(Lead(url=url, title=_text("%s %s" % (mpn, maker)), snippet=_text(description), kind="pdf"))
            if len(leads) >= MAX_LEADS:
                break
        return leads

    def cards(self, part: str, http: Any) -> Iterable[Tuple[str, str, str, str]]:
        raise NotImplementedError


@register
class MouserApi(DistributorApi):
    adapter = "mouser_api"
    title = "Mouser"
    URL = "https://api.mouser.com/api/v1/search/partnumber?apiKey={key}"

    def cards(self, part: str, http: Any) -> Iterable[Tuple[str, str, str, str]]:
        url = (self.entry.options.get("url") or self.URL).format(key=quote_plus(str((self.key or {}).get("key", ""))))
        data = http.post_json(url, {"SearchByPartRequest": {"mouserPartNumber": part, "partSearchOptions": ""}})
        data = data if isinstance(data, dict) else {}
        errors = [e for e in data.get("Errors") or [] if isinstance(e, dict)]
        if errors:                  # неверный ключ и лимит Mouser сообщает в теле ответа с кодом 200
            message = _text(errors[0].get("Message") or errors[0].get("Code") or "ошибка")
            limit = re.search(r"too many|limit|quota|exceed", message, re.I)
            raise SourceError("engine.quota" if limit else "engine.error", message[:120])
        for item in (data.get("SearchResults") or {}).get("Parts") or []:
            if isinstance(item, dict):
                yield (item.get("ManufacturerPartNumber", ""), item.get("Manufacturer", ""),
                       item.get("Description", ""), item.get("DataSheetUrl", ""))


class OAuthApi(DistributorApi):
    """Дистрибьютор с пропуском OAuth2 (client credentials): пропуск живёт минуты и хранится только в памяти."""
    TOKEN_URL = ""
    _clock = staticmethod(time.monotonic)

    def _token(self, http: Any) -> str:
        """Пропуск по client_id/client_secret (живёт минуты): хранится в памяти до истечения срока."""
        token, until = getattr(self, "_cached", ("", 0.0))
        if token and self._clock() < until:
            return token
        key = self.key or {}
        data = http.post_json(self.entry.options.get("token_url") or self.TOKEN_URL,
                              form={"client_id": key.get("client_id", ""), "client_secret": key.get("client_secret", ""),
                                    "grant_type": "client_credentials"})
        token = str((data or {}).get("access_token") or "") if isinstance(data, dict) else ""
        if not token:
            raise SourceError("engine.error", "пропуск не выдан")
        try:
            seconds = float(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        self._cached = (token, self._clock() + max(0.0, seconds - 60))
        return token


@register
class DigikeyApi(OAuthApi):
    adapter = "digikey_api"
    title = "DigiKey"
    URL = "https://api.digikey.com/products/v4/search/keyword"
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"

    def cards(self, part: str, http: Any) -> Iterable[Tuple[str, str, str, str]]:
        key = self.key or {}
        headers: Dict[str, str] = {"X-DIGIKEY-Client-Id": str(key.get("client_id", "")),
                                   "Authorization": "Bearer " + self._token(http)}
        try:
            data = http.post_json(self.entry.options.get("url") or self.URL, {"Keywords": part, "Limit": 10, "Offset": 0},
                                  headers=headers)
        except Exception as e:      # noqa: BLE001
            if str(e) in _DENIED:   # пропуск отозван раньше срока — в следующий раз будет запрошен новый
                self._cached = ("", 0.0)
            raise
        for item in (data.get("Products") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            maker, text = item.get("Manufacturer") or {}, item.get("Description") or {}
            yield (item.get("ManufacturerProductNumber", ""), maker.get("Name", "") if isinstance(maker, dict) else maker,
                   (text.get("ProductDescription") or text.get("DetailedDescription", "")) if isinstance(text, dict) else text,
                   item.get("DatasheetUrl", ""))


@register
class NexarApi(OAuthApi):
    """Nexar (Octopart): GraphQL `supSearchMpn`; ссылка на datasheet — `bestDatasheet` и документы-datasheet."""
    adapter = "nexar_api"
    title = "Nexar"
    URL = "https://api.nexar.com/graphql"
    TOKEN_URL = "https://identity.nexar.com/connect/token"
    QUERY = ("query Mpn($q: String!) { supSearchMpn(q: $q, limit: 10) { results { part { mpn "
             "manufacturer { name } shortDescription bestDatasheet { url } "
             "documentCollections { name documents { name url } } } } } }")

    def cards(self, part: str, http: Any) -> Iterable[Tuple[str, str, str, str]]:
        headers = {"Authorization": "Bearer " + self._token(http)}
        try:
            data = http.post_json(self.entry.options.get("url") or self.URL,
                                  {"query": self.QUERY, "variables": {"q": part}}, headers=headers)
        except Exception as e:      # noqa: BLE001
            if str(e) in _DENIED:
                self._cached = ("", 0.0)
            raise
        data = data if isinstance(data, dict) else {}
        errors = [e for e in data.get("errors") or [] if isinstance(e, dict)]
        if errors:                  # GraphQL отвечает кодом 200 и пишет ошибку в тело
            message = _text(errors[0].get("message") or "ошибка")
            limit = re.search(r"too many|limit|quota|exceed|rate", message, re.I)
            raise SourceError("engine.quota" if limit else "engine.error", message[:120])
        found = ((data.get("data") or {}).get("supSearchMpn") or {}).get("results") or []
        for item in found:
            card = item.get("part") if isinstance(item, dict) else None
            if not isinstance(card, dict):
                continue
            maker, best = card.get("manufacturer") or {}, card.get("bestDatasheet") or {}
            links = [best.get("url", "") if isinstance(best, dict) else ""]
            for group in card.get("documentCollections") or []:
                if isinstance(group, dict) and "datasheet" in str(group.get("name", "")).lower():
                    links += [d.get("url", "") for d in group.get("documents") or [] if isinstance(d, dict)]
            for link in links:
                if link:
                    yield (card.get("mpn", ""), maker.get("name", "") if isinstance(maker, dict) else "",
                           card.get("shortDescription", ""), link)
