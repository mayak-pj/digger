# -*- coding: utf-8 -*-
"""События поиска и их подписи на трёх языках (ARCHITECTURE §4.8).

Этапы сообщают о ходе работы только событиями. Событие хранит ключ сообщения и параметры, а не готовый
текст: текст собирается при показе из словаря `data/i18n/{en,zh,ru}.json`, поэтому одну и ту же строку
можно показать на языке поиска и по-русски (подсказка, журнал).

Шаблон в словаре:
    {name}              значение параметра; True/False → ✔/✘
    {n:link|links}      число и слово в нужной форме (en — 2 формы, ru — 3 формы: 1 / 2 / 5)
    {size:size}         размер в байтах → «1.2 MB» / «1,2 МБ»
В китайском множественного числа нет — счётное слово пишется прямо в шаблоне: `{n} 条结果`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

LANGS = ("en", "zh", "ru")
LANG_LABELS = {"en": "EN", "zh": "中文", "ru": "RU"}
OUTCOMES = ("", "ok", "empty", "found", "fail", "skip")       # "" — этап ещё идёт
I18N_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "i18n")

# Все ключи событий и итог по умолчанию. Новый ключ — сюда и сразу во все три словаря (тест проверяет).
KEYS = {
    "local.search": "", "local.found": "found", "local.empty": "empty",
    "cache.hit": "ok", "cache.negative": "skip",
    "maker.search": "", "maker.found": "found", "maker.empty": "empty",
    "engine.query": "", "engine.found": "found", "engine.empty": "empty",
    "engine.no_key": "skip", "engine.captcha": "skip", "engine.quota": "fail",
    "engine.error": "fail", "engine.offtopic": "fail",
    "site.search": "", "site.found": "found", "site.found_pdf": "found", "site.empty": "empty",
    "site.slow_skip": "skip",
    "market.search": "", "market.found": "found", "market.empty": "empty",
    "crawl.found": "found", "crawl.empty": "empty",
    "fetch.start": "", "fetch.progress": "", "fetch.done": "ok", "fetch.failed": "fail",
    "quarantine.placed": "ok", "manual.start": "",
    "validate.ok": "ok", "validate.scan": "ok", "validate.not_pdf": "fail", "validate.encrypted": "fail",
    "validate.active_content": "fail", "validate.too_big": "fail", "validate.damaged": "fail",
    "verify.result": "ok", "verify.rejected": "fail",
    "confirm.search": "", "confirm.identical": "ok", "confirm.none": "empty",
    "access.site_protected": "fail", "access.network_blocked": "fail", "access.not_whitelisted": "skip",
    "access.transient": "fail", "access.unknown": "fail",
    "search.cancelled": "skip", "search.budget": "skip", "search.limit": "skip",
    "search.order_adaptive": "", "search.order_explore": "",
    "result.confirmed": "ok", "result.probable": "ok", "result.needs_user": "ok",
    "result.rejected": "fail", "result.not_found": "empty",
    "error.internal": "fail",
    "photo.recognized": "ok",       # публикует окно после распознавания фото (для расширений)
    # цепочка распознавания (recognition/manager.py)
    "ocr.start": "", "ocr.next": "", "ocr.ok": "ok", "ocr.weak": "fail", "ocr.unconfirmed": "fail",
    "ocr.empty": "empty", "ocr.failed": "fail", "ocr.unavailable": "skip", "ocr.no_consent": "skip",
    "ocr.fallback": "ok",
}
# итог поиска и ошибки программы показываются по-русски, на каком бы языке ни шёл поиск
ALWAYS_RU = ("result.", "search.", "error.", "photo.", "ocr.")

_ZH_SOURCES = ("baidu", "bing_cn", "sogou", "so360", "lcsc", "szlcsc", "semiee", "taobao", "1688", "aliexpress")
_RU_SOURCES = ("yandex", "chipdip", "promelec", "efo", "ozon")
_ZH_DOMAINS = ("baidu.com", "sogou.com", "so.com", "cn.bing.com", "szlcsc.com", "lcsc.com", "semiee.com", "taobao.com",
               "1688.com", "aliexpress.com", "elecfans.com", "21ic.com", "dzsc.com", "51hei.com", "amobbs.com",
               "icpdf.com", "jlcpcb.com", "mouser.cn")
_RU_DOMAINS = ("yandex.com",)
_ZH_TLD = (".cn", ".中国", ".tw", ".hk")
_RU_TLD = (".ru", ".рф", ".su", ".by")
_CJK = re.compile(u"[㐀-鿿豈-﫿]")
_CYR = re.compile(u"[Ѐ-ӿ]")


# ---------- склонения ----------

def ru_plural(n: int, one: str, few: str, many: str) -> str:
    """1 вариант · 2 варианта · 5 вариантов · 11 вариантов · 21 вариант."""
    n = abs(int(n))
    if n % 100 in (11, 12, 13, 14):
        return many
    if n % 10 == 1:
        return one
    if n % 10 in (2, 3, 4):
        return few
    return many


def en_plural(n: int, one: str, many: str) -> str:
    """1 match · 0/2/21 matches."""
    return one if abs(int(n)) == 1 else many


def _plural(lang: str, n: Any, forms: List[str]) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return forms[-1]
    if lang == "ru" and len(forms) >= 3:
        return ru_plural(n, forms[0], forms[1], forms[2])
    if len(forms) >= 2:
        return en_plural(n, forms[0], forms[-1])
    return forms[0]


def _size(value: Any, catalog: Dict[str, str]) -> str:
    units = (catalog.get("_units") or "B KB MB GB").split()
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    i = 0
    while num >= 1024 and i < len(units) - 1:
        num /= 1024.0
        i += 1
    text = "%d" % num if i == 0 else "%.1f" % num
    return "%s %s" % (text.replace(".", catalog.get("_decimal") or "."), units[i])


# ---------- словари ----------

_catalogs: Dict[str, Dict[str, str]] = {}
_catalogs_lock = threading.Lock()


def load_catalog(lang: str, i18n_dir: Optional[str] = None) -> Dict[str, str]:
    """Словарь языка (читается один раз). Незнакомый язык или испорченный файл → пустой словарь."""
    path = os.path.join(i18n_dir or I18N_DIR, "%s.json" % lang)
    with _catalogs_lock:
        if path not in _catalogs:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    _catalogs[path] = json.load(f)
            except (OSError, ValueError) as e:
                if lang in LANGS:
                    log.error("словарь %s не читается: %s", path, e)
                _catalogs[path] = {}
        return _catalogs[path]


_FIELD = re.compile(r"\{(\w+)(?::([^{}]*))?\}")


def render_key(key: str, params: Optional[Dict[str, Any]] = None, lang: str = "en", i18n_dir: Optional[str] = None) -> str:
    """Текст сообщения. Никогда не падает: нет ключа — сам ключ, нет параметра — `{имя}` остаётся в тексте."""
    params = params or {}
    catalog = load_catalog(lang, i18n_dir)
    if key not in catalog:
        lang, catalog = "en", load_catalog("en", i18n_dir)
    template = catalog.get(key)
    if template is None:
        return key

    def sub(m):
        name, spec = m.group(1), m.group(2)
        if name not in params:
            return "{%s}%s" % (name, " " + spec.split("|")[-1] if spec and "|" in spec else "")
        value = params[name]
        if spec == "size":
            return _size(value, catalog)
        if spec:
            return "%s %s" % (value, _plural(lang, value, spec.split("|")))
        if isinstance(value, bool):
            return u"✔" if value else u"✘"
        return str(value)
    return _FIELD.sub(sub, template)


# ---------- событие ----------

@dataclass
class Event:
    """Одно событие поиска. `lang` — язык текущего поиска (запроса или сайта), `outcome` — итог этапа."""
    key: str
    params: Dict[str, Any] = field(default_factory=dict)
    lang: str = "en"
    level: str = ""           # уровень источника из sources.json
    source: str = ""          # id адаптера или домен
    outcome: Optional[str] = None     # ok|empty|found|fail|skip, "" — идёт; None → по таблице KEYS
    ts: float = 0.0
    seq: int = 0              # порядковый номер в шине

    def __post_init__(self):
        if self.outcome is None:
            self.outcome = KEYS.get(self.key, "")
        if self.outcome not in OUTCOMES:
            raise ValueError("Event.outcome: «%s», допустимо %s" % (self.outcome, "|".join(OUTCOMES[1:])))
        if not self.ts:
            self.ts = time.time()

    @property
    def kind(self) -> str:
        """Тип события — этап: local, engine, site, fetch, validate, verify, confirm, result…"""
        return self.key.split(".", 1)[0]

    @property
    def final(self) -> bool:
        return bool(self.outcome)

    @property
    def display_lang(self) -> str:
        """Язык показа: язык поиска, а для итога и ошибок программы — русский."""
        return "ru" if self.key.startswith(ALWAYS_RU) else self.lang

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})


def render(event: Event, lang: Optional[str] = None, i18n_dir: Optional[str] = None) -> str:
    """Текст события: на языке показа или на заданном (`"ru"` — перевод для подсказки и журнала)."""
    return render_key(event.key, event.params, lang or event.display_lang, i18n_dir)


# ---------- язык по источнику ----------

def _host(domain: str) -> str:
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", (domain or "").strip().lower())
    return re.split(r"[/:?#]", host, 1)[0]


def _in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _domain_language(domain: str) -> str:
    host = _host(domain)
    if not host:
        return ""
    if _in(host, _ZH_DOMAINS) or host.endswith(_ZH_TLD):
        return "zh"
    if _in(host, _RU_DOMAINS) or host.endswith(_RU_TLD):
        return "ru"
    return ""


def text_language(text: str) -> str:
    """Язык по письменности: иероглифы → zh, кириллица → ru, иначе пусто (латиница ничего не говорит)."""
    if _CJK.search(text or ""):
        return "zh"
    if _CYR.search(text or ""):
        return "ru"
    return ""


def search_language(query: str = "", source: Any = None, domain: str = "", default: str = "en") -> str:
    """Язык текущего поиска (§4.8): язык запроса, иначе язык источника (сайта), иначе `default`.

    `source` — id адаптера (`"baidu"`) или его запись из sources.json; поле `lang` в записи решает сразу.
    """
    entry = source if isinstance(source, dict) else {}
    if entry.get("lang") in LANGS:
        return entry["lang"]
    found = text_language(query)
    if found:
        return found
    source_id = str(entry.get("id") or "" if entry else source or "").lower()
    if source_id in _ZH_SOURCES:
        return "zh"
    if source_id in _RU_SOURCES:
        return "ru"
    for d in [domain] + list(entry.get("domains") or []):
        found = _domain_language(d)
        if found:
            return found
    return text_language(str(entry.get("name") or "")) or default


# ---------- счётчики ----------

class EventCounters:
    """Счётчики одного поиска: запросов · найдено · скачано · подтверждено (окно и консольный наблюдатель)."""
    NAMES = ("queries", "found", "fetched", "confirmed")

    def __init__(self):
        self.counts: Dict[str, int] = dict.fromkeys(self.NAMES, 0)

    def reset(self) -> None:
        for name in self.NAMES:
            self.counts[name] = 0

    def add(self, event: Event) -> None:
        c = self.counts
        if event.key in ("engine.query", "site.search", "maker.search", "market.search"):
            c["queries"] += 1
        elif event.outcome == "found" and event.kind != "local":
            try:
                c["found"] += int(event.params.get("n") or 0)
            except (TypeError, ValueError):
                pass
        elif event.key == "fetch.done":
            c["fetched"] += 1
        elif event.kind == "result":
            c["queries"] = event.params.get("queries") or c["queries"]     # оркестратор знает точнее
            c["confirmed"] += int(event.key == "result.confirmed")

    def text(self) -> str:
        c = self.counts
        return u"%d %s · найдено %d · скачано %d · подтверждено %d" % (
            c["queries"], ru_plural(c["queries"], u"запрос", u"запроса", u"запросов"),
            c["found"], c["fetched"], c["confirmed"])


# ---------- шина ----------

class EventBus:
    """Потокобезопасная шина: этапы публикуют из любых потоков, подписчики получают события по порядку.

    Подписчик вызывается в потоке издателя и должен быть быстрым (окно — кладёт в очередь и выходит).
    Ошибка подписчика не останавливает поиск и не мешает остальным.
    """

    def __init__(self, history: int = 2000):
        self._lock = threading.RLock()
        self._subscribers: List[Callable[[Event], None]] = []
        self._history = deque(maxlen=history)
        self._seq = 0

    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Подписаться; возвращает функцию отписки."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    def publish(self, event: Event) -> Event:
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            self._history.append(event)
            for callback in list(self._subscribers):
                try:
                    callback(event)
                except Exception:
                    log.exception("подписчик событий упал на %s", event.key)
        return event

    def emit(self, key: str, lang: str = "en", level: str = "", source: str = "", outcome: Optional[str] = None,
             **params: Any) -> Event:
        """Короткая запись: `bus.emit("engine.found", lang="zh", engine="百度", query=q, n=5)`."""
        return self.publish(Event(key, params, lang=lang, level=level, source=source, outcome=outcome))

    def history(self) -> List[Event]:
        with self._lock:
            return list(self._history)
