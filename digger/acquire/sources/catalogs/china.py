# -*- coding: utf-8 -*-
"""Китайские каталоги (шаг 2.10): LCSC/szlcsc, Semiee — адаптер `direct_url`.

Страница поиска → ссылки на PDF и страницы деталей на доменах источника (в тексте или адресе есть партномер) →
до `follow` страниц деталей за PDF. Сайты отдают поиск скриптом или закрыты защитой (szlcsc — «WAF拦截页面»,
это капча: домен отдыхает 15 мин, без обхода); тогда адаптер честно отдаёт пустой результат или событие.
elecfans, 21ic, dzsc прямого поиска не имеют — они идут через поисковики (`china_queries` в `data/sources.json`).
Вёрстка синтетическая. Живая проба 2026-10-12 (Mac и рабочий ПК одинаково): szlcsc отвечает кодом 203 и
страницей из одного запутанного скрипта — защита, домен отдыхает; Semiee отдаёт выдачу запросом, которому нужен вход
на сайт («请登录») — источник выключен в `data/sources.json`.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List
from urllib.parse import quote, urljoin, urlsplit

from ...models import Lead
from ...query import base_part
from ...registry import register
from ..base import SourceError
from ..makers import part_of
from .common import _A, CatalogSite, href_of, text_of

log = logging.getLogger("digger.acquire.china")
MAX_DETAILS = 5
_PDF = re.compile(r"\.pdf$", re.I)
_SERVICE = re.compile(r"^/search(?:/|$)", re.I)


def _on(url: str, domains: List[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return urlsplit(url).scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in domains)


def links(page: str, base: str, domains: List[str], wanted: List[str]) -> Any:
    """([(адрес, заголовок)] деталей с партномером, [адреса PDF]) на доменах источника, без повторов."""
    details, pdfs, seen = [], [], set()
    for m in _A.finditer(page):
        url = urljoin(base, href_of(m.group(1))).split("#")[0]
        if url in seen or not _on(url, domains):
            continue
        seen.add(url)
        title = text_of(m.group(2))
        if _PDF.search(urlsplit(url).path):
            pdfs.append(url)
        elif _SERVICE.match(urlsplit(url).path):
            continue                       # `/search/video`, `/search/text` — вкладки выдачи, не карточки
        elif urlsplit(url).path.strip("/") and any(w in (title + " " + url).upper() for w in wanted):
            details.append((url, title))
    return details, pdfs


@register
class DirectUrl(CatalogSite):
    adapter = "direct_url"

    def find(self, query: Any, http: Any) -> List[Lead]:
        part = part_of(query)
        if not part:
            return []
        self.check_rest()
        base, page = self._get(http, self.entry.options["url"].format(part=quote(part)))
        wanted = list({part.upper(), (base_part(part) or part).upper()})
        details, pdfs = links(page, base, self.domains, wanted)
        leads: List[Lead] = []
        seen = set()
        for url in pdfs:
            seen.add(url)
            leads.append(Lead(url=url, title=part, kind="pdf", snippet=base))
        follow = int(self.entry.options.get("follow", 0))
        for url, title in details[:MAX_DETAILS]:
            if follow:
                try:
                    dbase, dpage = self._get(http, url)
                    for pdf in links(dpage, dbase, self.domains, wanted)[1]:
                        if pdf not in seen:
                            seen.add(pdf)
                            leads.append(Lead(url=pdf, title=title or part, kind="pdf", snippet=url))
                except SourceError as e:
                    if e.key == "engine.captcha":
                        raise
                    log.info("%s: деталь %s — %s", self.id, url, e.detail)
            leads.append(Lead(url=url, title=title or part, kind="page"))
        return leads
