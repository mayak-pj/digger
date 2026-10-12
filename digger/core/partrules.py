# -*- coding: utf-8 -*-
"""Справочник семейств микросхем (data/part_rules.json)."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .config import read_json
from .utils import norm_part

SIZE_93C = {"46": "1 Kbit", "56": "2 Kbit", "66": "4 Kbit", "76": "8 Kbit", "86": "16 Kbit"}
SIZE_27C_SPECIAL = {"010": "1 Mbit", "020": "2 Mbit", "040": "4 Mbit", "080": "8 Mbit",
                    "160": "16 Mbit", "322": "32 Mbit", "801": "8 Mbit", "1001": "1 Mbit",
                    "2001": "2 Mbit", "4001": "4 Mbit"}


def _fmt_kbit(n: int) -> str:
    if n >= 1024 and n % 1024 == 0:
        return "%d Mbit (%d КБ)" % (n // 1024, n * 128 // 1024)
    kb = n / 8.0
    kb_s = ("%d байт" % int(n * 128)) if kb < 1 else ("%g КБ" % kb)
    return "%d Kbit (%s)" % (n, kb_s)


def size_from_rule(kind: str, raw: str) -> str:
    if not raw:
        return ""
    raw = raw.lstrip("0") if kind in ("kbit", "mbit") else raw
    try:
        if kind == "kbit":
            n = int(raw)
            if n == 1025:  # 24C1025 и подобные
                n = 1024
            return _fmt_kbit(n)
        if kind == "mbit":
            n = int(raw)
            return "%d Mbit (%s)" % (n, ("%d МБ" % (n // 8)) if n >= 8 else ("%d КБ" % (n * 128)))
        if kind == "93c":
            return SIZE_93C.get(raw, "")
        if kind == "27c":
            if raw in SIZE_27C_SPECIAL:
                return SIZE_27C_SPECIAL[raw]
            return _fmt_kbit(int(raw))
    except ValueError:
        return ""
    return ""


class PartRules:
    def __init__(self, app_dir: str) -> None:
        path = os.path.join(app_dir, "data", "part_rules.json")
        self.rules: List[Dict[str, Any]] = []
        self.shapes: List[Any] = []
        if os.path.exists(path):
            data = read_json(path)
            for r in data.get("rules", []):
                try:
                    r["_re"] = re.compile(r["pattern"])
                    self.rules.append(r)
                except re.error:
                    pass
            for r in data.get("shapes", []):
                try:
                    self.shapes.append((re.compile(r["prefix"]), re.compile(r["shape"])))
                except (re.error, KeyError):
                    pass

    def shape_ok(self, part: str) -> Optional[bool]:
        """Подходит ли партномер под запись своего семейства (`shapes`); None — запись семейства не задана."""
        p = norm_part(part)
        for prefix, shape in self.shapes:
            if prefix.search(p):
                return bool(shape.search(p))
        return None

    def match(self, part: str) -> Optional[Dict[str, Any]]:
        p = norm_part(part)
        if not p:
            return None
        for r in self.rules:
            m = r["_re"].search(p)
            if m:
                mem = r.get("memory")
                memory = None
                if mem is not None:
                    memory = []
                    for item in mem:
                        size = item.get("size", "")
                        if size in ("kbit", "mbit", "93c", "27c"):
                            raw = m.groupdict().get("size") or ""
                            size = size_from_rule(size, raw)
                        memory.append({"kind": item.get("kind", ""), "size": size})
                return {"family": r.get("family", ""), "manufacturer": r.get("manufacturer", ""),
                        "memory": memory, "pattern": r["pattern"]}
        return None


_cache: Dict[str, PartRules] = {}


def get_rules(app_dir: str) -> PartRules:
    if app_dir not in _cache:
        _cache[app_dir] = PartRules(app_dir)
    return _cache[app_dir]
