# -*- coding: utf-8 -*-
"""Общее для адаптеров каталогов (шаги 2.8–2.9): разбор ссылок, Cloudflare как капча, отдых домена."""
from __future__ import annotations

import html as htmllib
import logging
import re
import time
from typing import Any, Callable, List, Tuple
from urllib.parse import urljoin, urlsplit

from ..base import SourceAdapter, SourceError

log = logging.getLogger("digger.acquire.catalogs")
REST_MINUTES = 15
_A = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.S | re.I)
_SRC = re.compile(r"<(?:iframe|embed|object)\b[^>]*?\b(?:src|data)=[\"']([^\"']+)[\"']", re.S | re.I)
_ATTR = r"\b%s=[\"']([^\"']*)[\"']"
_TAG = re.compile(r"<[^>]+>")
_CHALLENGE_MARKS = ("just a moment", "challenge-error-text", "_cf_chl_opt", "cf-challenge")


def is_blocked(status: int, page: str) -> bool:
    """Страница проверки Cloudflare вместо сайта. Одного адреса challenges.cloudflare.com мало: обычная страница
    подключает оттуда виджет для формы входа (FindChips)."""
    low = page[:200000].lower()
    if status == 203 and len(page) < 20000 and "document.cookie" in low and "reload" in low:
        return True               # LCSC: «203» со скриптом, ставящим куку и перезагружающим страницу (защита сайта)
    if "waf拦截页面" in low or (status in (403, 503) and "cloudflare" in low):
        return True
    return "challenge" in low and any(mark in low for mark in _CHALLENGE_MARKS)


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(_TAG.sub("", fragment))).strip()


def href_of(attrs: str) -> str:
    m = re.search(_ATTR % "href", attrs, re.I)
    return htmllib.unescape(m.group(1)).strip() if m else ""


def pdf_urls(page: str, base: str, path_re: "re.Pattern[str]") -> List[str]:
    """Адреса PDF со страницы: a/iframe/embed/object, путь подходит под `path_re`; повторы убраны."""
    found = [href_of(m.group(1)) for m in _A.finditer(page)] + [htmllib.unescape(m) for m in _SRC.findall(page)]
    out: List[str] = []
    for raw in found:
        url = urljoin(base, raw)
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and path_re.search(parts.path) and url not in out:
            out.append(url)
    return out


class CatalogSite(SourceAdapter):
    """Каталог-сайт: `_get` отдаёт страницу или бросает SourceError; капча → домен отдыхает."""
    family = "site"
    kinds = ("pdf", "page")

    def __init__(self, *args: Any, clock: Callable[[], float] = time.time, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._clock = clock
        self._rest_until = 0.0

    def _get(self, http: Any, url: str) -> Tuple[str, str]:
        """(адрес после переадресаций, текст страницы); капча — SourceError, сбой сети — тоже."""
        try:
            timeout = self.entry.options.get("timeout")
            reply = http.fetch(url, timeout=float(timeout)) if timeout else http.fetch(url)
        except Exception as e:
            detail = str(e) or type(e).__name__
            log.warning("%s: %s", self.id, detail)
            raise SourceError("engine.error", detail, cause=e)
        page = reply["body"].decode("utf-8", errors="replace")
        if is_blocked(reply["status"], page):
            self._rest_until = self._clock() + REST_MINUTES * 60
            raise SourceError("engine.captcha", "Cloudflare", minutes=REST_MINUTES)
        if reply["status"] >= 400:
            raise SourceError("engine.quota" if reply["status"] == 429 else "engine.error", "HTTP %d" % reply["status"])
        return reply["url"], page

    def check_rest(self) -> None:
        left = self._rest_until - self._clock()
        if left > 0:
            raise SourceError("engine.captcha", "отдых", minutes=int(-(-left // 60)))
