# -*- coding: utf-8 -*-
"""Улики проверки документа (ARCHITECTURE §4.4; шаг 4.3).

`collect` — чистая функция: факты документа (`DocFacts`) + что известно о чипе с фото (`PhotoContext`) +
откуда документ скачан → список улик `Evidence` с баллами. `verify_file` — чтение, факты, тип и улики одним
вызовом. Статус по уликам выносит `decide.py`.

Веса — `config.json → acquire.weights` (по умолчанию `DEFAULT_WEIGHTS`, те же числа в config.default.json).
Улика с нулевым весом в список не попадает; E12 (нет текстового слоя) баллов не имеет — она ограничивает итог.
В `detail` — то, что найдено: строка документа с партномером, шаблон семейства, корпус, домен.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import classify as doc_types
from .extract import MANUFACTURERS, DocText, _packages, _parts, covers, facts_from_text, read_document
from .models import DocFacts, Evidence, PhotoContext
from .query import base_part, family
from .rank import _host_matches

FRONT_PAGES = 2               # «первые страницы» для E1
MIN_STEM, MAX_SUFFIX = 5, 3  # основа из документа и буквенный суффикс корпуса/температуры к ней (E1)
INDEPENDENT_DOMAINS = 2       # столько разных доменов с тем же документом нужно для E10

DEFAULT_WEIGHTS: Dict[str, Any] = {
    "E1_front": 30, "E1_any": 20, "E2": 15, "E3": 15, "E4": 10,
    "E5": {doc_types.DATASHEET: 15, doc_types.FAMILY: 15, doc_types.APP_NOTE: -20, doc_types.ERRATA: -20,
           doc_types.REFERENCE_MANUAL: -20, doc_types.CATALOG: -20, doc_types.DISTRIBUTOR: -30},
    "E6_match": 10, "E6_conflict": -15, "E7_match": 10, "E7_conflict": -10, "E8": 10,
    "E9_maker": 10, "E9_catalog": 5, "E9_bad": -10, "E10": 10, "E11": -30,
}

# Корпуса, которые по фото не различить или которые производители называют по-разному.
_PKG_GROUPS = ("SOIC SOP SO ESOP HSOP", "DIP PDIP SPDIP CDIP", "TSSOP HTSSOP", "SSOP QSOP", "MSOP VSSOP",
               "QFN VQFN WQFN UQFN HVQFN VFQFPN UFQFPN", "DFN UDFN WDFN TDFN XDFN SON WSON USON VSON",
               "QFP LQFP TQFP PQFP", "BGA FBGA TFBGA UFBGA VFBGA", "CSP WLCSP")
_PKG_GROUP = {name: group.split()[0] for group in _PKG_GROUPS for name in group.split()}


@dataclass
class SourceTrust:
    """Что известно о доменах: производители, каталоги datasheet, «плохая репутация» (решения пользователя)."""
    makers: Sequence[str] = ()
    catalogs: Sequence[str] = ()
    bad: Sequence[str] = ()
    maker_sites: Dict[str, str] = field(default_factory=dict)     # производитель → сайт (sources.json)

    @classmethod
    def from_sources(cls, data: Mapping[str, Any], bad: Sequence[str] = ()) -> "SourceTrust":
        """Из sources.json: `maker_sites` и домены источников уровней maker и catalog."""
        sites = dict(data.get("maker_sites") or {})
        by_level: Dict[str, List[str]] = {"maker": list(sites.values()), "catalog": []}
        for src in data.get("sources") or []:
            if src.get("level") in by_level:
                by_level[src["level"]].extend(src.get("domains") or [])
        return cls(makers=tuple(dict.fromkeys(by_level["maker"])), catalogs=tuple(dict.fromkeys(by_level["catalog"])),
                   bad=tuple(bad), maker_sites=sites)


def merge_weights(weights: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Веса по умолчанию с изменениями из конфига (в config.json достаточно указать изменённое)."""
    out = dict(DEFAULT_WEIGHTS, E5=dict(DEFAULT_WEIGHTS["E5"]))
    for key, value in (weights or {}).items():
        if key == "E5" and isinstance(value, Mapping):
            out["E5"].update(value)
        else:
            out[key] = value
    return out


def verify_file(path: str, ctx: PhotoContext, url: str = "", trust: Optional[SourceTrust] = None,
                same_doc_domains: Sequence[str] = (), weights: Optional[Mapping[str, Any]] = None,
                progress: Optional[Callable[[int], None]] = None) -> Tuple[DocFacts, List[Evidence]]:
    """Факты (с типом документа) и улики для проверенного PDF. Исключений не бросает.
    `progress(n)` — перед распознаванием скана: сколько страниц будет прочитано."""
    doc = read_document(path, progress=progress)
    facts = facts_from_text(doc, [ctx.part, base_part(ctx.part)])
    facts.doc_type = doc_types.classify(doc, facts)
    return facts, collect(facts, ctx, url, trust, same_doc_domains, weights, doc)


def collect(facts: DocFacts, ctx: PhotoContext, url: str = "", trust: Optional[SourceTrust] = None,
            same_doc_domains: Sequence[str] = (), weights: Optional[Mapping[str, Any]] = None,
            doc: Optional[DocText] = None) -> List[Evidence]:
    """`same_doc_domains` — домены, с которых получен тот же документ (sha256 или отпечаток; `confirm.py`).
    `doc` — прочитанный текст: с ним E1 цитирует строку документа, без него — только партномер."""
    w = merge_weights(weights)
    trust = trust or SourceTrust()
    out: List[Evidence] = []

    def add(code: str, points: Any, detail: str = "", page: int = 0) -> None:
        if points or code == "E12":
            out.append(Evidence(code=code, points=int(points), detail=detail, page=page))

    full, base = _norm(ctx.part), _norm(base_part(ctx.part))
    exact = _exact(facts, full, base) if full else None
    if exact:
        name, page = exact
        add("E1", w["E1_front"] if page <= FRONT_PAGES else w["E1_any"], _quote(doc, page, name), page)
    if full and full != base:
        code = next((c for c in facts.ordering_codes if _norm(c) == full), "")
        if code:
            add("E2", w["E2"], code)
    pattern = next((p for p in facts.family_patterns if covers(p, ctx.part)), "") if full else ""
    if pattern and not exact:
        add("E3", w["E3"], pattern)
    where = facts.heading + "\n" + facts.title
    titled = bool(full) and (_names_in(where, (full, base)) or _names_in(where, (_norm(family(ctx.part)),), prefix=0)
                             or any(p in re.sub(r"\s+", "", where) for p in facts.family_patterns
                                    if covers(p, ctx.part)))
    if titled:
        add("E4", w["E4"], " ".join((facts.heading or facts.title).split()))
    add("E5", w["E5"].get(facts.doc_type, 0), facts.doc_type)
    _maker(add, w, facts, ctx, trust)
    _package(add, w, facts, ctx)
    mark = _norm(ctx.marking)
    if mark and mark in (_norm(c) for c in facts.marking_codes):
        add("E8", w["E8"], mark)
    for key, domains in (("E9_bad", trust.bad), ("E9_maker", trust.makers), ("E9_catalog", trust.catalogs)):
        if url and _host_matches(url, domains):
            add("E9", w[key], (urlsplit(url).hostname or "").lower())
            break
    same = list(dict.fromkeys(d.lower() for d in same_doc_domains if d))
    if len(same) >= INDEPENDENT_DOMAINS:
        add("E10", w["E10"], ", ".join(same))
    others = list(_parts(facts.heading)) or list(_parts(facts.title))
    if full and others and not titled:
        add("E11", w["E11"], ", ".join(dict.fromkeys(others)))
    if not facts.has_text:
        add("E12", 0)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _exact(facts: DocFacts, full: str, base: str) -> Optional[Tuple[str, int]]:
    """Партномер (полный или основа) как записан в документе и первая страница, где он есть.
    Основа — и то, что документ называет сам, если наш партномер длиннее на короткий буквенный суффикс:
    «W25Q512JVFQ» на корпусе — это «W25Q512JV» в документе; «STM32F103C8» и «STM32F103» — разные чипы.
    И наоборот: на фото партномер без суффикса («AT24C02»), документ пишет его с суффиксом корпуса («AT24C02N»)."""
    def same(name: str) -> bool:
        n = _norm(name)
        tail = full[len(n):] if full.startswith(n) else ""
        return (n in (full, base) or (len(n) >= MIN_STEM and tail.isalpha() and len(tail) <= MAX_SUFFIX)
                or (full == base and n != base and _norm(base_part(name)) == base))

    hits = [(pages[0], name) for name, pages in facts.parts_found.items() if pages and same(name)]
    if not hits:
        return None
    page, name = min(hits)
    return name, page


def _quote(doc: Optional[DocText], page: int, name: str) -> str:
    for line in (doc.texts.get(page, "") if doc else "").splitlines():
        if name in line.upper():
            return " ".join(line.split())
    return name


def _names_in(text: str, names: Sequence[str], prefix: int = 3) -> bool:
    """Есть ли в тексте одно из имён отдельным словом; суффикс допустим (NE555P), другая цифра — нет (LM3580).
    Перед именем допустим префикс производителя: маркировка на корпусе бывает усечённой (25Q64JV — это W25Q64JV).
    Для имени семейства префикс не допускается (`prefix=0`): «AT24» внутри «CAT24C021» — чужой чип."""
    squeezed = re.sub(r"[^A-Z0-9\s]", "", text.upper())
    return any(re.search(r"(?<![A-Z0-9])[A-Z]{0,%d}%s(?!\d)" % (prefix, re.escape(n)), squeezed)
               for n in names if len(n) >= 4)


def _canon_maker(name: str) -> str:
    low = " ".join((name or "").split()).lower()
    for canon, aliases in MANUFACTURERS.items():
        if low == canon.lower() or low in (a.lower() for a in aliases):
            return canon
    return low


def _maker(add: Any, w: Mapping[str, Any], facts: DocFacts, ctx: PhotoContext, trust: SourceTrust) -> None:
    """E6. Одна компания под разными именами (Atmel → Microchip) узнаётся по общему сайту в `maker_sites`."""
    if not ctx.manufacturer or not facts.manufacturers:
        return
    wanted = _canon_maker(ctx.manufacturer)
    sites = {_canon_maker(k): v for k, v in trust.maker_sites.items()}
    for name in facts.manufacturers:
        if _canon_maker(name) == wanted or (sites.get(wanted) and sites.get(wanted) == sites.get(_canon_maker(name))):
            add("E6", w["E6_match"], name)
            return
    if not ctx.maker_guessed:     # догадка по семейству фото не заменяет: чужое имя в документе — не противоречие
        add("E6", w["E6_conflict"], ", ".join(facts.manufacturers))


def _pkg_key(name: str) -> Tuple[str, int]:
    """«VFQFPN-32» → («QFN», 32); «SOT-23-5» → («SOT-23», 5); число выводов 0 — неизвестно."""
    head, _, rest = name.partition("-")
    numbers = [int(n) for n in rest.split("-") if n.isdigit()]
    if head in ("SOT", "SC", "TO"):
        return "%s-%s" % (head, numbers[0] if numbers else ""), numbers[1] if len(numbers) > 1 else 0
    return _PKG_GROUP.get(head, head), numbers[0] if numbers else 0


def _package(add: Any, w: Mapping[str, Any], facts: DocFacts, ctx: PhotoContext) -> None:
    """E7. Совпадение — тот же род корпуса и число выводов. Противоречие — в документе нет ни одного корпуса
    с таким числом выводов: SOIC-8 и TSSOP-8 по фото легко спутать, 8 и 16 выводов — нет."""
    photo = [_pkg_key(p) for p in _packages((ctx.package or "").upper())][:1]
    known = [(p, _pkg_key(p)) for p in facts.packages]
    if not photo or not known:
        return
    group, pins = photo[0]
    for name, (g, n) in known:
        if g == group and (n == pins or not n or not pins):
            add("E7", w["E7_match"], name)
            return
    if pins and all(n and n != pins for _, (_, n) in known):
        add("E7", w["E7_conflict"], ", ".join(name for name, _ in known))
