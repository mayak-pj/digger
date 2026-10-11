# -*- coding: utf-8 -*-
"""Реестр источников: сборка адаптеров из data/sources.json (ARCHITECTURE §4.2).

Порядок — по списку `levels`, внутри уровня — как в файле. Выключенный источник или уровень пропускается.
Источник с типом адаптера, которого в программе ещё нет, не мешает остальным (список — `Registry.missing`).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Type

from ..core.config import read_json
from .events import I18N_DIR, EventBus
from .sources.base import SourceAdapter, SourceEntry

log = logging.getLogger("digger.acquire.registry")
SOURCES_PATH = os.path.join(os.path.dirname(I18N_DIR), "sources.json")

ADAPTERS: Dict[str, Type[SourceAdapter]] = {}


# модули digger/acquire/sources/: новый адаптер добавляется сюда
BUILTIN = ("google_api", "engine_html", "makers", "catalogs.alldatasheet", "catalogs.datasheet4u", "catalogs.partlist",
           "catalogs.china", "github_api", "engine_queries", "distributors")


def load_builtin() -> None:
    """Подключает встроенные адаптеры (они регистрируются декоратором `@register`)."""
    import importlib
    for name in BUILTIN:
        importlib.import_module("digger.acquire.sources." + name)


def register(cls: Type[SourceAdapter]) -> Type[SourceAdapter]:
    """Декоратор класса адаптера: `@register` — тип берётся из `cls.adapter`."""
    ADAPTERS[cls.adapter] = cls
    return cls


class Registry:
    def __init__(self, data: Dict[str, Any], keys: Optional[Dict[str, Any]] = None, bus: Optional[EventBus] = None,
                 adapters: Optional[Dict[str, Type[SourceAdapter]]] = None):
        self.data = data or {}
        self.keys = keys or {}            # config.json → acquire.api_keys
        self.bus = bus
        if adapters is None:
            load_builtin()
        self._adapters = ADAPTERS if adapters is None else adapters
        self._built: Dict[str, SourceAdapter] = {}      # один экземпляр на источник: общий отдых после капчи
        self.missing: List[str] = []      # id источников, для которых нет класса адаптера
        self.on_failure: Any = None       # (адаптер, исключение, язык): источник не смог обратиться к сайту (§4.11)
        self._levels = [dict(lv) for lv in self.data.get("levels", [])]
        self._entries = [SourceEntry.from_dict(s) for s in self.data.get("sources", [])]
        ids = [e.id for e in self._entries]
        twice = sorted(set(i for i in ids if ids.count(i) > 1))
        if twice:
            raise ValueError("sources.json: повторяется id источника: %s" % ", ".join(twice))

    @classmethod
    def load(cls, path: Optional[str] = None, **kwargs: Any) -> "Registry":
        return cls(read_json(path or SOURCES_PATH), **kwargs)

    def levels(self, include_disabled: bool = False) -> List[Dict[str, Any]]:
        return [lv for lv in self._levels if include_disabled or lv.get("enabled", True)]

    def entries(self, level: Optional[str] = None, include_disabled: bool = False) -> List[SourceEntry]:
        """Источники в порядке уровней; уровень, которого нет в `levels`, идёт последним и считается включённым."""
        order = {lv["id"]: i for i, lv in enumerate(self._levels)}
        off = set(lv["id"] for lv in self._levels if not lv.get("enabled", True))
        out = [e for e in self._entries
               if (level is None or e.level == level)
               and (include_disabled or (e.enabled and e.level not in off))]
        return sorted(out, key=lambda e: order.get(e.level, len(order)))     # сортировка устойчивая

    def _make(self, entry: SourceEntry) -> Optional[SourceAdapter]:
        if entry.id in self._built:
            return self._built[entry.id]
        cls = self._adapters.get(entry.adapter)
        if cls is None:
            if entry.id not in self.missing:
                self.missing.append(entry.id)
                log.warning("источник «%s»: нет адаптера «%s», пропущен", entry.id, entry.adapter)
            return None
        ad = cls(entry, bus=self.bus, key=self.keys.get(entry.needs_key) if entry.needs_key else None)
        ad.registry = self
        self._built[entry.id] = ad
        return ad

    def build(self, level: Optional[str] = None) -> List[SourceAdapter]:
        """Адаптеры включённых источников, готовые к `search()`."""
        return [ad for ad in (self._make(e) for e in self.entries(level)) if ad is not None]

    def engine(self, source_id: str) -> Optional[SourceAdapter]:
        """Включённый источник по id (для `engine_queries.via`); нет или выключен — None."""
        for entry in self._entries:
            if entry.id == source_id and entry.enabled:
                return self._make(entry)
        return None

    def allowed_domains(self) -> List[str]:
        """Домены для белого списка netsafe: включённые источники, сайты производителей, хосты PDF."""
        out: List[str] = []
        for entry in self.entries():
            out += entry.domains
        out += list(self.data.get("maker_sites", {}).values())
        out += self.data.get("pdf_hosts", [])
        return list(dict.fromkeys(d for d in out if d))

    def maker_site(self, manufacturer: str) -> str:
        return self.data.get("maker_sites", {}).get(manufacturer, "")


def legacy_sources(data: Dict[str, Any]) -> Dict[str, Any]:
    """Старый вид sources.json (engines + levels) — нужен только проверке sites в наборе для Win7."""
    if "sources" not in data:
        return data
    entries = [s for s in data["sources"] if s.get("enabled", True) or s.get("adapter") == "engine_html"]
    engines = {s["id"]: {"name": s.get("name", s["id"]), "url": s["url"], "decoder": s.get("decoder", "plain"),
                         "domains": s.get("domains", []), "enabled": s.get("enabled", True)}
               for s in entries if s.get("adapter") == "engine_html"}
    levels = []
    for lv in data.get("levels", []):
        old = dict(lv)
        for s in entries:
            if s.get("level") != lv["id"]:
                continue
            if s.get("adapter") in ("direct_url", "alldatasheet", "datasheet4u", "datasheetarchive", "findchips"):
                old.setdefault("direct", []).append({"name": s.get("name", s["id"]), "url": s["url"],
                                                     "domains": s.get("domains", []), "follow": s.get("follow", 0)})
            elif s.get("adapter") == "engine_queries":
                old["engines"], old["queries"] = list(s.get("via", [])), list(s.get("queries", []))
            elif s.get("adapter") == "github_api":
                old["github_api"] = s["url"]
        if len(old) > len(lv):            # уровни, где есть только поисковики, v1 не нужны
            levels.append(old)
    return {"engines": engines, "levels": levels, "maker_sites": data.get("maker_sites", {}),
            "pdf_hosts": data.get("pdf_hosts", [])}
