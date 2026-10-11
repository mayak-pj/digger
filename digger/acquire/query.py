# -*- coding: utf-8 -*-
"""План запросов на трёх языках (ARCHITECTURE §4.7).

`base_part` отбрасывает суффиксы корпуса и температуры, `family` даёт шаблон семейства,
`plan_queries` строит запросы en/zh/ru в порядке из конфига. Сети здесь нет.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

DEFAULT_ORDER = ("en", "zh", "ru")

# {part} — основа партномера, {code} — короткий код маркировки SMD
TEMPLATES = {
    "part": {
        "en": ["{part} datasheet pdf"],
        "zh": ["{part} 数据手册", "{part} 规格书 pdf", "{part} 中文资料"],
        "ru": ["{part} даташит", "{part} документация pdf"],
    },
    "smd": {
        "en": ["{code} smd marking code"],
        "zh": ["{code} 丝印"],
        "ru": ["{code} маркировка smd"],
    },
}

# Суффиксы корпуса/упаковки/температуры. Длинные — раньше коротких.
_SUFFIXES = ("SIQ", "SIG", "SNG", "SU", "SN", "DR", "TR", "DT", "PW", "DW", "T6", "T7", "U6")
_SINGLE = ("N", "P", "D")          # снимаются, только если перед ними цифра
_SMD_MAX_LEN = 4


def _core(raw: str) -> str:
    s = re.sub(r"\s+", "", (raw or "").upper())
    s = s.split("/")[0]                      # «/TR», «/NOPB»
    tokens = [t for t in s.split("-") if t]
    if not tokens:
        return ""
    core = tokens[0]
    if len(core) < 3 and len(tokens) > 1:    # «TL-431»: первый кусок слишком короткий
        core += tokens[1]
    return core


def base_part(raw: str) -> str:
    """AT24C02N-10SU-2.7 → AT24C02, W25Q64JVSIQ → W25Q64JV, LM358DR → LM358, PMS150C-U06 → PMS150C."""
    core = _core(raw)
    while True:
        for suf in _SUFFIXES:
            if core.endswith(suf) and len(core) - len(suf) >= 4:
                core = core[:-len(suf)]
                break
        else:
            for suf in _SINGLE:
                if core.endswith(suf) and len(core) > 4 and core[-2].isdigit():
                    core = core[:-1]
                    break
            else:
                return core


def family(raw: str) -> str:
    """STM32F103C8T6 → STM32F103, W25Q64JV → W25Q64; пусто, если отдельного семейства нет."""
    base = base_part(raw)
    fam = re.sub(r"(?<=\d)[A-Z]+\d*$", "", base)
    return fam if len(fam) >= 4 and fam != base else ""


def is_smd_code(raw: str) -> bool:
    """Короткая метка вроде «A6W»: ≤ 4 символа, есть и буква, и цифра."""
    s = re.sub(r"\s+", "", raw or "")
    return 0 < len(s) <= _SMD_MAX_LEN and any(c.isdigit() for c in s) and any(c.isalpha() for c in s)


_PREFIX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "maker_prefixes.json")
_prefix_cache: Dict[str, dict] = {}


def load_prefix_table(path: Optional[str] = None) -> dict:
    """Таблица префиксов производителей (data/maker_prefixes.json); нет файла или он сломан — пустая."""
    path = path or _PREFIX_FILE
    if path not in _prefix_cache:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        _prefix_cache[path] = data if isinstance(data, dict) else {}
    return _prefix_cache[path]


def prefixed_parts(base: str, maker: str = "", table: Optional[dict] = None) -> List[str]:
    """25Q512JV → [W25Q512JV, GD25Q512JV, XM25Q512JV]: возможные полные партномера усечённой маркировки.
    Производитель с фото сужает выбор; не нашёлся в таблице — берутся все варианты ряда (не больше `max_variants`)."""
    table = load_prefix_table() if table is None else table
    base = _compact(base)
    if not base:
        return []
    for rule in table.get("rules", []):
        try:
            hit = re.search(rule["pattern"], base)
        except (re.error, KeyError, TypeError):
            continue
        if not hit:
            continue
        variants = rule.get("variants", [])
        want = (maker or "").strip().lower()
        if want:
            narrowed = [v for v in variants
                        if want == v.get("maker", "").lower() or want in [a.lower() for a in v.get("aliases", [])]]
            variants = narrowed or variants
        limit = int(table.get("max_variants", 3) or 3)
        return [v["prefix"] + base for v in variants[:limit] if v.get("prefix")]
    return []


def maker_by_prefix(part: str, table: Optional[dict] = None) -> str:
    """STM32F103C8T6 → STMicroelectronics: производитель семейства, которое выпускает он один (`families` в
    data/maker_prefixes.json). Ряды, которые делают многие (LM358, 74HC595), — пустая строка."""
    table = load_prefix_table() if table is None else table
    name = _compact(part)
    for rule in table.get("families", []) if name else []:
        try:
            if re.search(rule["pattern"], name):
                return str(rule.get("maker", ""))
        except (re.error, KeyError, TypeError):
            continue
    return ""


@dataclass
class Query:
    lang: str
    text: str
    kind: str            # "part" | "family" | "smd"
    part: str = ""       # партномер / семейство / код из запроса: по нему отсеиваются посторонние ссылки выдачи


_OPERATOR = re.compile(r"\S+:\S*")              # site:…, filetype:…
_WORD = re.compile(r"[A-Z0-9][A-Z0-9._/+-]*")
_SHORT_KEY = 5                                  # короче — искать только отдельным словом


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (text or "").upper())


def relevance_keys(query: object) -> List[str]:
    """Что должно встретиться в ссылке по запросу: партномер и его семейство (усечённая маркировка `25Q512JVFQ`
    в документе пишется `W25Q512JV`). Партномер — `query.part`, иначе первое слово запроса с цифрой.
    Пусто — проверять нечем."""
    part = getattr(query, "part", "") or ""
    if not part:
        text = _OPERATOR.sub(" ", getattr(query, "text", query) or "").upper()
        part = next((w for w in _WORD.findall(text) if any(c.isdigit() for c in w)), "")
    part = _compact(part)
    if not part:
        return []
    fam = family(part) if getattr(query, "kind", "part") != "smd" else ""
    return [part] + ([fam] if fam and fam != part else [])


def mentions(keys: Sequence[str], *texts: str) -> bool:
    """Есть ли хоть один ключ в текстах (заголовок, фрагмент, адрес): без регистра, дефисов и пробелов;
    короткий код — только отдельным словом. Без ключей — да."""
    if not keys:
        return True
    upper = " ".join(t or "" for t in texts).upper()
    solid = _compact(upper)
    for key in keys:
        if len(key) >= _SHORT_KEY:
            if key in solid:
                return True
        elif re.search(r"(?<![A-Z0-9])%s(?![A-Z0-9])" % re.escape(key), upper):
            return True
    return False


def plan_queries(raw: str, langs: Optional[Sequence[str]] = None, smd: Optional[bool] = None,
                 templates: Optional[Dict[str, Dict[str, List[str]]]] = None, maker: str = "",
                 prefixes: Optional[dict] = None) -> List[Query]:
    """Запросы по языкам в порядке `langs`. Короткий код (или smd=True) — запросы маркировки.
    Усечённая маркировка (`25Q512JVFQ`) — ещё по запросу на вариант с префиксом производителя; `maker` с фото сужает его."""
    tpl = templates or TEMPLATES
    order = [l for l in (langs or DEFAULT_ORDER) if l in DEFAULT_ORDER] or list(DEFAULT_ORDER)
    as_smd = is_smd_code(raw) if smd is None else smd
    out: List[Query] = []
    seen = set()

    def add(lang: str, text: str, kind: str, key: str) -> None:
        if (lang, text) not in seen:
            seen.add((lang, text))
            out.append(Query(lang, text, kind, key))

    if as_smd:
        code = re.sub(r"\s+", "", raw).upper()
        for lang in order:
            for t in tpl["smd"].get(lang, []):
                add(lang, t.format(code=code), "smd", code)
        return out
    part = base_part(raw)
    if not part:
        return out
    fam = family(raw)
    full = prefixed_parts(part, maker, prefixes)
    for lang in order:
        for t in tpl["part"].get(lang, []):
            add(lang, t.format(part=part), "part", part)
        if fam:                                    # по одному запросу семейства на язык
            first = tpl["part"].get(lang, [])[:1]
            for t in first:
                add(lang, t.format(part=fam), "family", fam)
        for first in tpl["part"].get(lang, [])[:1]:   # варианты с префиксом производителя
            for p in full:
                add(lang, first.format(part=p), "part", p)
    return out
