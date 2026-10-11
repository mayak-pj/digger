# -*- coding: utf-8 -*-
"""Интерфейс адаптера источника (ARCHITECTURE §4.2).

Адаптер — один источник из `data/sources.json`: поисковик, каталог, сайт производителя, маркетплейс.
`search()` публикует события хода поиска (§4.8) и зовёт `find()`, который пишет конкретный адаптер.
Сеть — только через переданный `http` (`core/netsafe.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..events import LANGS, EventBus, search_language, text_language
from ..models import Lead

_START = {"engine": "engine.query"}       # остальные семейства: «<семейство>.search»


class SourceError(Exception):
    """Сбой источника из `find()`: `search()` публикует событие `key` (engine.quota, engine.error…) вместо «найдено»."""

    def __init__(self, key: str, detail: str = "", cause: Optional[BaseException] = None, **params: Any):
        super().__init__(detail)
        self.key, self.detail, self.params = key, detail, params       # params — доп. параметры события (minutes…)
        self.cause = cause            # исключение сети или ответ сайта: по нему определяется класс неудачи (§4.11)


@dataclass
class SourceEntry:
    """Запись источника из sources.json. Поля сверх общих (url, decoder, follow, via…) — в `options`."""
    id: str
    adapter: str                  # тип адаптера: ключ в реестре классов
    name: str = ""
    level: str = ""               # уровень поиска (порядок уровней — sources.json → levels)
    lang: str = ""                # en | zh | ru; пусто — язык решает запрос или домен
    domains: List[str] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)     # свои шаблоны запросов; пусто — общие из query.py
    enabled: bool = True
    needs_key: str = ""           # имя ключа в config.json → acquire.api_keys; пусто — ключ не нужен
    options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceEntry":
        data = dict(data or {})
        if not data.get("id") or not data.get("adapter"):
            raise ValueError("sources.json: у источника должны быть id и adapter: %r" % (data,))
        if data.get("lang", "") not in LANGS + ("",):
            raise ValueError("sources.json: источник «%s», язык «%s», допустимо %s"
                             % (data["id"], data["lang"], "|".join(LANGS)))
        own = [n for n in cls.__dataclass_fields__ if n != "options"]
        kwargs = {n: data.pop(n) for n in own if n in data}
        return cls(options=data, **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        out = {n: getattr(self, n) for n in self.__dataclass_fields__ if n != "options"}
        out.update(self.options)
        return out


class SourceAdapter:
    """Базовый адаптер. Наследник задаёт `adapter`, `family`, `kinds` и пишет `find()`."""
    adapter = ""                          # значение поля "adapter" в sources.json
    family = "site"                       # семейство событий: engine | site | maker | market
    kinds: Tuple[str, ...] = ("page",)    # какие Lead.kind даёт источник

    def __init__(self, entry: SourceEntry, bus: Optional[EventBus] = None, key: Any = None):
        self.entry = entry
        self.bus = bus
        self.key = key

    id = property(lambda self: self.entry.id)
    level = property(lambda self: self.entry.level)
    lang = property(lambda self: self.entry.lang)
    domains = property(lambda self: self.entry.domains)
    needs_key = property(lambda self: self.entry.needs_key)

    @property
    def label(self) -> str:
        """Имя в строке состояния: у поисковика — название, у сайта — домен (или `label` из sources.json)."""
        e = self.entry
        if e.options.get("label"):
            return e.options["label"]
        if self.family == "engine":
            return e.name or e.id
        return e.domains[0] if e.domains else e.name or e.id

    @property
    def available(self) -> bool:
        """Ключ не нужен или задан (у составного ключа заполнены все части)."""
        if not self.entry.needs_key:
            return True
        parts = list(self.key.values()) if isinstance(self.key, dict) else [self.key]
        return bool(parts) and all(parts)

    def resting(self) -> int:
        """Сколько минут источнику ещё «отдыхать» после капчи (`_rest_until` по часам `_clock`); 0 — можно спрашивать."""
        until = getattr(self, "_rest_until", 0.0)
        left = until - self._clock() if until else 0.0
        return int(-(-left // 60)) if left > 0 else 0

    def find(self, query: Any, http: Any) -> List[Lead]:
        """Один запрос к источнику. `query` — `Query` или строка; `http` — `SafeHttp`."""
        raise NotImplementedError

    def search(self, query: Any, http: Any) -> List[Lead]:
        """Запрос с событиями: начало → «найдено N» / «не найдено»; без ключа — «нет ключа, пропуск»."""
        text = getattr(query, "text", query) or ""
        source = self.entry.to_dict()
        if self.family == "engine":       # язык строки — язык запроса, у сайта — язык сайта (§4.8)
            lang = getattr(query, "lang", "") or text_language(text) or search_language(source=source)
        else:
            lang = search_language(source=source)
        params = {"engine": self.label, "site": self.label, "query": text}
        if not self.available:
            self._emit("engine.no_key", lang, params)
            return []
        left = self.resting()
        if left:                          # в сеть не ходим — это не запрос: бюджет поиска не тратится
            self._emit("engine.captcha", lang, dict(params, detail="отдых", minutes=left))
            return []
        self._emit(_START.get(self.family, self.family + ".search"), lang, params)
        try:
            leads = list(self.find(query, http) or [])
        except SourceError as e:
            self._emit(e.key, lang, dict(params, detail=e.detail, **e.params))
            hook = getattr(getattr(self, "registry", None), "on_failure", None)
            if hook is not None and e.cause is not None:      # оркестратор классифицирует неудачу доступа
                hook(self, e.cause, lang)
            return []
        for lead in leads:
            lead.source_id = lead.source_id or self.id
            lead.level = lead.level or self.level
            lead.query = lead.query or text
            lead.language = lead.language or lang
        pdfs = sum(1 for lead in leads if lead.kind == "pdf")
        self._emit(self.family + (".found" if leads else ".empty"), lang, dict(params, n=len(leads), pdfs=pdfs))
        return leads

    def _emit(self, key: str, lang: str, params: Dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.emit(key, lang=lang, level=self.level, source=self.id, **params)
