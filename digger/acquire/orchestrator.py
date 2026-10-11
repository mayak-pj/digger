# -*- coding: utf-8 -*-
"""Оркестратор поиска документа (ARCHITECTURE §4.6): уровни, языки, бюджет, остановка, отмена.

Один поиск: библиотека → уровни из `sources.json` по порядку. На уровне спрашиваются все источники, новые
ссылки ранжируются, лучшие проходят путь «страница → PDF → карантин → проверка файла → сверка → подтверждение
→ библиотека». Остановка: документ подтверждён (§4.5), исчерпан бюджет, отмена; «искать везде» остановку по
результату пропускает, бюджет — нет. Порядок уровней и источников внутри уровня — по умолчанию из `sources.json`
или адаптивный, по статистике (`adaptive.py`, §4.10); ранжирование ссылок от него не зависит.
Уровни проходятся дважды: сначала сайты (каталоги, производители — по одному запросу), потом поисковики и
запросы через них — бюджет запросов не кончается раньше, чем спрошены сайты. Сайт, не ответивший вовремя
(тайм-аут), до конца сеанса (жизни оркестратора) пропускается: событие `site.slow_skip`, неудача — в заключение.
Ход — только событиями (§4.8). Запросы считаются по событиям шины,
неудачи доступа (`access.*` и капча поисковика, §4.11) собираются в `SearchResult.failures`; если документ не
подтверждён, из них строится заключение (`conclusion.py`): где скачать вручную, что закрыто сетью.
`search()` исключений не бросает: сбой источника или этапа — событие `error.internal`, поиск идёт дальше.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from ..core.config import resolve_path
from ..core.netsafe import NetBlocked, SafeHttp, host_of
from . import confirm as confirm_mod
from . import scanocr
from .conclusion import Conclusion, build_conclusion
from .crawl import crawl
from .decide import decide
from .events import Event, EventBus
from .fetch import fetch_to_quarantine
from .models import AcquisitionRecord, Lead, PhotoContext
from .netdiag import SITE_PROTECTED, TRANSIENT, BlockTracker, failure_class
from .query import DEFAULT_ORDER, Query, base_part, is_smd_code, maker_by_prefix, plan_queries
from .rank import normalize_url, score_lead
from .stats import QUERY_KEYS
from .store import AcquireStore, _now
from .validate import MAX_MB, validate_pdf
from .verify import SourceTrust, verify_file

log = logging.getLogger("digger.acquire.orchestrator")
LEADS_PER_LEVEL = 4       # сколько лучших ссылок уровня проверяется, прежде чем идти на следующий
PDFS_PER_PAGE = 2         # сколько PDF берётся с одной страницы
PARALLEL = 3              # сколько источников уровня спрашивается одновременно (§4.6)
SEARCH_FAMILIES = ("engine", "queries")     # ищут через поисковики: много запросов, поэтому после сайтов
SLOW_REASONS = ("read_timeout", "connect_timeout")     # после такой неудачи сайт пропускается до конца сеанса
_ORDER = {"confirmed": 3, "probable": 2, "needs_user": 1, "rejected": 0}


@dataclass
class Budget:
    """Предел одного поиска (§4.6)."""
    queries: int = 60
    downloads: int = 10
    seconds: float = 180.0


@dataclass
class SearchResult:
    part: str
    status: str = "not_found"     # confirmed | probable | needs_user | rejected | not_found | cancelled
    reason: str = "exhausted"     # почему остановились: local | confirmed | budget | cancelled | exhausted
    path: str = ""                # файл в библиотеке
    best: Optional[AcquisitionRecord] = None
    records: List[AcquisitionRecord] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)   # site, cls, reason, url, source, level
    conclusion: Optional[Conclusion] = None   # §4.11: заключение, когда документ не подтверждён (не при отмене)
    queries: int = 0
    downloads: int = 0
    sources: int = 0
    seconds: float = 0.0


def _site(host: str) -> str:
    """Сайт по имени узла или домену источника: `www.datasheetarchive.com` → `datasheetarchive.com`."""
    return confirm_mod.site_of("//" + host) or host.lower()


def _raw(rec: AcquisitionRecord) -> AcquisitionRecord:
    """Запись с вердиктом до понижения «не подтверждён»: по нему ищется совпадение независимых источников."""
    v = rec.verdict
    if v is None or "unconfirmed" not in v.reasons:
        return rec
    return replace(rec, verdict=replace(v, status="confirmed"))


class Orchestrator:
    def __init__(self, registry: Any, http: Any, store: AcquireStore, bus: Optional[EventBus] = None,
                 trust: Optional[SourceTrust] = None, owners: Iterable[Sequence[str]] = (),
                 budget: Optional[Budget] = None, langs: Optional[Sequence[str]] = None,
                 thresholds: Optional[Mapping[str, Any]] = None, weights: Optional[Mapping[str, Any]] = None,
                 tracker: Optional[BlockTracker] = None, access: Any = None, learner: Any = None,
                 recorder: Any = None, max_mb: float = MAX_MB, clock: Callable[[], float] = time.monotonic,
                 sleep: Optional[Callable[[float], Any]] = None, parallel: int = PARALLEL,
                 adaptive: Any = None) -> None:
        self.registry, self.http, self.store = registry, http, store
        self.bus = bus if bus is not None else EventBus()
        self.trust = trust or SourceTrust()
        self.owners = [list(o) for o in owners]
        self.budget = budget or Budget()
        self.langs = [l for l in (langs or DEFAULT_ORDER) if l in DEFAULT_ORDER] or list(DEFAULT_ORDER)
        self.thresholds, self.weights, self.max_mb = thresholds, weights, max_mb
        self.tracker = tracker or BlockTracker()
        self.access, self.learner, self.recorder = access, learner, recorder   # access.py, learn.py, stats.py
        self.adaptive = adaptive                       # adaptive.py: порядок по статистике; None — по умолчанию
        self.clock = clock
        self.parallel = max(1, int(parallel))
        registry.on_failure = self._failed             # неудача доступа внутри источника (поисковик)
        self._slow: Dict[str, str] = {}                # сайт → причина: не ответил вовремя, до конца сеанса не ждём
        self._cancel = threading.Event()
        self._state = threading.Lock()                 # причина остановки: её проверяют потоки источников
        self.sleep = sleep or self._cancel.wait        # пауза между повторами прерывается отменой

    def cancel(self) -> None:
        """Отмена из другого потока: текущий запрос завершится, новых не будет."""
        self._cancel.set()

    # -------------------- поиск --------------------
    def search(self, ctx: PhotoContext, everywhere: bool = False) -> SearchResult:
        """Полный поиск документа для чипа. `everywhere` — «искать везде»: не останавливаться на подтверждённом."""
        self._cancel.clear()
        if not ctx.manufacturer and maker_by_prefix(ctx.part):       # поиск по одному партномеру: STM32 — это ST
            ctx = replace(ctx, manufacturer=maker_by_prefix(ctx.part), maker_guessed=True)
        self._ctx, self._res = ctx, SearchResult(part=ctx.part or ctx.marking)
        self._asked, self._seen, self._url, self._why = set(), set(), "", ""
        self._skipped = set()                          # медленные сайты, о пропуске которых в этом поиске уже сказано
        self._started = self.clock()
        self._sites = {en.id: en.domains[0] for en in self.registry.entries(include_disabled=True) if en.domains}
        if self.recorder is not None:
            self.recorder.begin(self._res.part, "" if ctx.maker_guessed else ctx.manufacturer)
        unsubscribe = self.bus.subscribe(self._watch)
        try:
            if not (self._local() and not everywhere):
                self._levels(everywhere)
        except Exception as e:       # noqa: BLE001 — поиск не должен ронять программу
            log.exception("поиск %s", self._res.part)
            self._emit("error.internal", "ru", stage="search", error=str(e) or type(e).__name__)
        finally:
            self._finish()
            unsubscribe()
        return self._res

    def _emit(self, key: str, lang: str = "ru", level: str = "", source: str = "", **params: Any) -> None:
        self.bus.emit(key, lang=lang, level=level, source=source, **params)

    def _watch(self, e: Event) -> None:
        """Подписчик на время поиска: счёт запросов и неудачи доступа от всех этапов."""
        if e.key in QUERY_KEYS:
            self._res.queries += 1
            self._asked.add(e.source)
        elif e.key.startswith("access."):
            site, cls = str(e.params.get("site", "")), e.key.split(".", 1)[1]
            url = str(e.params.get("url") or self._url)
            self._res.failures.append({"site": site, "cls": cls, "reason": self.tracker.reason(site), "url": url,
                                       "source": e.source, "level": e.level})
            if self.access is not None:
                self.access.record_failure(site, cls, self._res.part, url, e.level)
            if site and self.tracker.reason(site) in SLOW_REASONS:
                self._slow.setdefault(_site(site), self.tracker.reason(site))
        elif e.key == "engine.captcha":           # поисковик с капчей: человек в браузере искать может
            self._res.failures.append({"site": self._sites.get(e.source, e.source), "cls": SITE_PROTECTED,
                                       "reason": "captcha", "url": "", "source": e.source, "level": e.level})

    def _stop(self) -> bool:
        """Отмена или исчерпан бюджет (событие — один раз)."""
        with self._state:
            if self._why:
                return True
            b, res = self.budget, self._res
            if self._cancel.is_set():
                self._why = "cancelled"
            elif self.clock() - self._started > b.seconds:
                self._why = "budget"
                self._emit("search.budget", seconds=int(b.seconds))
            elif res.queries >= b.queries or res.downloads >= b.downloads:
                self._why = "budget"
                self._emit("search.limit", queries=res.queries, downloads=res.downloads)
            return bool(self._why)

    def _local(self) -> bool:
        """Уровень «локальная база»: подтверждённый документ этого партномера уже в библиотеке."""
        self._emit("local.search", level="local")
        rows = [r for r in self.store.acquisitions(self._res.part)
                if r["status"] == "confirmed" and r["path"] and os.path.isfile(r["path"])]
        if not rows:
            self._emit("local.empty", level="local")
            return False
        self._emit("local.found", level="local", n=len(set(r["path"] for r in rows)))
        self._res.status, self._res.path, self._why = "confirmed", rows[-1]["path"], "local"
        return True

    def _levels(self, everywhere: bool) -> None:
        ctx = self._ctx
        plan = plan_queries(self._res.part, self.langs, maker=ctx.manufacturer)
        order = [lv["id"] for lv in self.registry.levels()]
        base = base_part(ctx.part)
        adapters = {level: self.registry.build(level) for level in order}
        levels = self._order(order, adapters)
        for direct in (True, False):         # сначала сайты, потом поисковики: бюджет не кончается до сайтов
            for level in levels:
                group = [a for a in adapters[level] if (a.family not in SEARCH_FAMILIES) == direct]
                if group and self._round(group, plan, order, base, everywhere):
                    return

    def _round(self, group: List[Any], plan: List[Query], order: List[str], base: str, everywhere: bool) -> bool:
        """Источники одного уровня → лучшие новые ссылки → документы. Истина — поиск пора остановить."""
        ctx = self._ctx
        fresh = []
        for lead in self._ask(group, plan):
            key = normalize_url(lead.url)
            if key not in self._seen:
                self._seen.add(key)
                lead.rank_score = score_lead(lead, ctx.part, order, self.trust.makers, base)
                fresh.append(lead)
        for lead in sorted(fresh, key=lambda x: -x.rank_score)[:LEADS_PER_LEVEL]:
            if self._stop():
                return True
            try:
                confirmed = self._follow(lead)
            except Exception as e:       # noqa: BLE001
                log.exception("ссылка %s", lead.url)
                self._emit("error.internal", "ru", lead.level, lead.source_id, stage="document",
                           error=str(e) or type(e).__name__)
                continue
            if confirmed and not everywhere:
                self._why = "confirmed"
                return True
        return self._stop()

    # -------------------- источники --------------------
    def _order(self, order: List[str], adapters: Dict[str, List[Any]]) -> List[str]:
        """Порядок обхода уровней; адаптивный (§4.10) переставляет и источники внутри уровня."""
        if self.adaptive is None:
            return order
        try:
            found = self.adaptive.plan([(lv, [a.id for a in adapters[lv]]) for lv in order], self._res.part)
        except Exception as e:       # noqa: BLE001 — статистика не должна мешать поиску
            log.exception("адаптивный порядок")
            self._emit("error.internal", "ru", stage="order", error=str(e) or type(e).__name__)
            return order
        if not found.adaptive:
            return order
        self._emit("search.order_adaptive", n=found.searches)
        if found.explored:
            self._emit("search.order_explore", source=found.explored, name=found.explored)
        for level, ids in found.sources.items():
            adapters[level].sort(key=lambda a: ids.index(a.id))
        return found.levels

    def _queries(self, adapter: Any, plan: List[Query]) -> List[Query]:
        """Поисковику — запросы его языка (или всех трёх); сайту — один запрос с партномером."""
        if adapter.family == "engine":
            return [q for q in plan if not adapter.lang or q.lang == adapter.lang]
        raw = self._res.part.strip()
        smd = is_smd_code(raw)
        text = raw if smd or adapter.family == "maker" else base_part(raw) or raw
        query = Query(adapter.lang or self.langs[0], text, "smd" if smd else "part", text)
        query.maker_site = self.registry.maker_site(self._ctx.manufacturer)      # для шаблонов {maker_site}
        return [query]

    def _ask(self, adapters: List[Any], plan: List[Query]) -> List[Lead]:
        """Источники уровня — одновременно, не больше `parallel`; ссылки — в порядке источников, как без потоков.

        Частоту обращений к одному сайту держит `SafeHttp` (1 запрос в 1.5 с), поэтому источники одного домена
        просто ждут своей очереди. Запросы одного источника идут по порядку в его потоке."""
        if self.parallel <= 1 or len(adapters) <= 1:
            return [lead for adapter in adapters for lead in self._discover(adapter, plan)]
        with ThreadPoolExecutor(min(self.parallel, len(adapters)), thread_name_prefix="cf-search") as pool:
            found = list(pool.map(lambda adapter: self._discover(adapter, plan), adapters))
        return [lead for leads in found for lead in leads]

    def _discover(self, adapter: Any, plan: List[Query]) -> List[Lead]:
        leads: List[Lead] = []
        answered = set()              # языки, на которых источник уже дал ссылки
        if adapter.domains and self._slow_site(adapter.domains[0], adapter.level, adapter.id,
                                               adapter.lang or self.langs[0]):
            return leads
        for query in self._queries(adapter, plan):
            if query.lang in answered:
                continue
            if self._stop():
                break
            self._url = ""
            try:
                found = adapter.search(query, self.http)
            except Exception as e:       # noqa: BLE001 — сеть и чужая разметка могут бросить что угодно
                log.info("источник %s: %s", adapter.id, e)
                if not self._failed(adapter, e, query.lang):
                    self._emit("error.internal", "ru", adapter.level, adapter.id, stage=adapter.id,
                               error=str(e) or type(e).__name__)
                break
            if found:
                answered.add(query.lang)
                leads += found
        return leads

    def _failed(self, adapter: Any, exc: BaseException, lang: str) -> bool:
        """Источник не смог обратиться к сайту: событие `access.*` по классу неудачи. Ложь — это не неудача доступа."""
        site = adapter.domains[0] if adapter.domains else adapter.id
        cls = failure_class(exc, self.tracker, site) if isinstance(exc, (NetBlocked, OSError)) else ""
        if cls:
            self._emit("access." + cls, lang, adapter.level, adapter.id, site=site)
        return bool(cls)

    def _slow_site(self, host: str, level: str, source: str, lang: str, url: str = "") -> bool:
        """Сайт уже не ответил вовремя в этом сеансе: не ждём его снова (событие и неудача — раз за поиск)."""
        site = _site(host)
        reason = self._slow.get(site)
        if not reason:
            return False
        with self._state:
            told = site in self._skipped
            self._skipped.add(site)
        if not told:
            self._emit("site.slow_skip", lang, level, source, site=site)
            self._res.failures.append({"site": site, "cls": TRANSIENT, "reason": reason, "url": url,
                                       "source": source, "level": level})
        return True

    # -------------------- документ --------------------
    def _follow(self, lead: Lead) -> bool:
        """Ссылка → PDF (со страницы — до `PDFS_PER_PAGE`) → проверка. Истина — документ подтверждён."""
        self._url = lead.url
        if self._slow_site(host_of(lead.url), lead.level, lead.source_id, lead.language or "en", lead.url):
            return False
        pdfs = crawl(self.http, lead, self.bus, cancelled=self._cancel.is_set, tracker=self.tracker)
        confirmed = False
        for pdf in pdfs[:PDFS_PER_PAGE]:
            if pdf is not lead:
                key = normalize_url(pdf.url)
                if key in self._seen:
                    continue
                self._seen.add(key)
            if self._stop():
                break
            confirmed = self._document(pdf, "" if pdf is lead else pdf.snippet) or confirmed
        return confirmed

    def _document(self, lead: Lead, referer: str) -> bool:
        res, ctx = self._res, self._ctx
        kw = dict(lang=lead.language or "en", level=lead.level, source=lead.source_id)
        site = host_of(lead.url)
        self._url = lead.url
        if self._slow_site(site, lead.level, lead.source_id, kw["lang"], lead.url):
            return False
        if not self.http.is_allowed(lead.url):       # в заключение, бюджет скачиваний не тратится
            self._emit("access.not_whitelisted", site=site, **kw)
            return False
        res.downloads += 1
        rec = AcquisitionRecord(part=res.part, lead=lead, started_at=_now())
        rec.fetch = fetch_to_quarantine(self.http, lead, self.bus, referer=referer, sleep=self.sleep,
                                        tracker=self.tracker)
        self.store.record_attempt(res.part, lead.url, "ok" if rec.fetch.ok else "fail", rec.fetch.error,
                                  lead.source_id)
        if not rec.fetch.ok:
            if rec.fetch.failure_class:
                self._emit("access." + rec.fetch.failure_class, site=site, **kw)
            return False
        if self.access is not None:
            self.access.record_ok(site)
        if self._cancel.is_set() or (self.learner is not None and self.learner.is_rejected(rec.fetch.sha256)):
            return False              # отклонённый пользователем файл обратно в библиотеку не кладётся
        path = rec.fetch.path_in_quarantine
        validation = validate_pdf(path, self.bus, lead, self.max_mb)
        if not validation.ok:
            rec.verdict = decide([], validation)
        else:
            rec.facts, evidence = verify_file(path, ctx, url=rec.fetch.final_url or lead.url, trust=self.trust,
                                              weights=self.weights,
                                              progress=lambda n: self._emit("scan.ocr", pages=n, **kw))
            rec.verdict = decide(evidence, validation, self.thresholds, ocr=bool(rec.facts.ocr_pages))
            self._report(rec, kw)
            if rec.verdict.status == "confirmed":
                found = confirm_mod.confirm(rec, [_raw(r) for r in res.records], self.trust, self.owners)
                if found.rule == confirm_mod.RULE_INDEPENDENT:
                    others = [s for s in found.sites if s != confirm_mod.site_of(lead.url)]
                    self._emit("confirm.identical", site=", ".join(others or found.sites), **kw)
                elif not found.confirmed:
                    self._emit("confirm.none", **kw)
                confirm_mod.apply(rec, found)
        rec.finished_at = _now()
        self.store.save(rec)
        res.records.append(rec)
        return rec.verdict.status == "confirmed"

    def _report(self, rec: AcquisitionRecord, kw: Dict[str, Any]) -> None:
        """Событие сверки: что совпало с фото (улики E1/E3 — партномер, E6 — производитель, E7 — корпус)."""
        v, ctx = rec.verdict, self._ctx
        if v.status == "rejected":
            self._emit("verify.rejected", part=ctx.part, score=v.score, **kw)
            return
        points = {code: sum(e.points for e in v.evidence if e.code == code) for code in ("E6", "E7")}
        self._emit("verify.result", part_ok=v.has("E1") or v.has("E3"), package=ctx.package or "—",
                   package_ok=points["E7"] > 0, maker=ctx.manufacturer or "—", maker_ok=points["E6"] > 0,
                   score=v.score, **kw)

    # -------------------- итог --------------------
    def _finish(self) -> None:
        res = self._res
        res.seconds = self.clock() - self._started
        res.sources = len(self._asked)
        res.reason = self._why or "exhausted"
        if self._cancel.is_set():
            res.status = res.reason = "cancelled"
            self._emit("search.cancelled")
            return
        source = ""
        if res.records:
            res.best = max(res.records, key=lambda r: (_ORDER.get(r.verdict.status, 0), r.verdict.score))
            res.status, res.path = res.best.verdict.status, res.best.stored_path
            source = res.best.lead.source_id
        elif res.reason == "local":
            source = "local"
        if res.status != "confirmed":
            res.conclusion = build_conclusion(res.part, res.status, res.failures, self.registry, res.sources,
                                              len(self.langs), res.seconds)
        self._emit("result." + res.status, source=source, part=res.part, queries=res.queries, seconds=int(round(res.seconds)),
                   sources=res.sources, n=sum(1 for r in res.records if r.verdict.status == "needs_user"))


def from_context(ctx: Any, bus: Optional[EventBus] = None, http: Any = None, db: Any = None) -> Orchestrator:
    """Оркестратор по настройкам программы: `sources.json`, `config.json → acquire`, общая база и библиотека."""
    from .access import AccessLog
    from .adaptive import AdaptiveOrder
    from .learn import Learner
    from .registry import Registry
    from .stats import SearchStats, StatsRecorder
    cfg = ctx.config
    acq = cfg.get("acquire", {})
    scanocr.configure(cfg, ctx.app_dir)
    bus = bus if bus is not None else EventBus()
    registry = Registry.load(os.path.join(ctx.app_dir, "data", "sources.json"), keys=acq.get("api_keys"), bus=bus)
    if http is None:
        http = SafeHttp(dict(cfg.get("network", {})), resolve_path(ctx.app_dir, cfg["paths"]["quarantine_dir"]),
                        ctx.log)
    http.add_allowed(registry.allowed_domains())
    db = db or ctx.module("local_db")
    store = AcquireStore(db)
    learner = Learner(store)
    stats = SearchStats(db)
    return Orchestrator(
        registry, http, store, bus=bus, trust=learner.trust(SourceTrust.from_sources(registry.data)),
        owners=confirm_mod.owners_from_sources(registry.data), budget=Budget(**acq.get("budget", {})),
        langs=acq.get("languages"), thresholds=acq.get("thresholds"), weights=acq.get("weights"),
        access=AccessLog(db), learner=learner, recorder=StatsRecorder(stats, bus),
        adaptive=AdaptiveOrder(stats, **acq.get("adaptive", {})),
        max_mb=float(cfg.get("network", {}).get("max_pdf_mb", MAX_MB)), parallel=int(acq.get("parallel", PARALLEL)))
