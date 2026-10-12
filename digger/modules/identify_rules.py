# -*- coding: utf-8 -*-
"""Модуль определения партномера по распознанному тексту.

Исправляет типичные ошибки OCR (O↔0, I↔1, S↔5, B↔8, Z↔2, G↔6),
отбрасывает коды даты/партии и логотипы, сверяет варианты со справочником
семейств (data/part_rules.json), каталогом KiCad и локальной базой.
Также грубо оценивает корпус по фото (сколько сторон с выводами, сколько выводов).
"""
from __future__ import annotations

import datetime
import itertools
import re
from typing import Dict, List, Optional

import cv2
import numpy as np

from ..core.interfaces import Identifier
from ..core.models import Candidate, ChipInfo
from ..core.partrules import get_rules
from ..core.utils import norm_part
from ..acquire.query import prefixed_parts
from .enhance_opencv import find_body

TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}
TO_LETTER = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}

MAKER_WORDS = {
    "ST": "STMicroelectronics", "ATMEL": "Atmel", "MICROCHIP": "Microchip", "TI": "Texas Instruments",
    "NXP": "NXP", "WCH": "WCH", "GD": "GigaDevice", "WINBOND": "Winbond", "MXIC": "Macronix",
    "ISSI": "ISSI", "ESPRESSIF": "Espressif", "ONSEMI": "onsemi", "ON": "onsemi", "NEC": "NEC",
    "HOLTEK": "Holtek", "STC": "STC", "PHILIPS": "Philips", "FAIRCHILD": "Fairchild", "INFINEON": "Infineon",
    "MAXIM": "Maxim", "ANALOG": "Analog Devices", "RENESAS": "Renesas", "SAMSUNG": "Samsung",
    "HYNIX": "SK hynix", "MICRON": "Micron", "TOSHIBA": "Toshiba", "CYPRESS": "Cypress", "SIPEX": "Sipex",
    "PADAUK": "Padauk", "MCHP": "Microchip", "GIGADEVICE": "GigaDevice", "NUVOTON": "Nuvoton",
    "SILABS": "Silicon Labs", "FTDI": "FTDI", "PUYA": "Puya",
    "LATTICE": "Lattice", "ASMEDIA": "ASMedia", "BROADCOM": "Broadcom", "SPANSION": "Spansion",
    "ALLIANCE": "Alliance Memory", "REALTEK": "Realtek", "FINTEK": "Fintek", "INTEL": "Intel",
    "XILINX": "Xilinx", "ALTERA": "Altera", "NANYA": "Nanya", "MACRONIX": "Macronix", "KIOXIA": "Kioxia",
    "SANDISK": "SanDisk", "QUALCOMM": "Qualcomm", "MEDIATEK": "MediaTek", "MARVELL": "Marvell",
    "DIODES": "Diodes", "PERICOM": "Pericom", "RICHTEK": "Richtek", "LSI": "LSI", "IOR": "International Rectifier",
    "ROHM": "ROHM",
}
JUNK = {"E3", "E4", "G4", "PB", "ROHS", "CHINA", "TAIWAN", "PHIL", "MALAYSIA", "KOREA", "JAPAN",
        "MADE", "IN", "USA", "THAI", "PHILIPPINES", "MAL", "MEX", "KOR", "CHN", "TWN",
        "EMMC", "MEMORY", "FLASH"}
_FUZZY_MIN = 6       # логотип с ошибкой чтения узнаём только у длинных слов: «WINBONG», но не «5T»


def _is_date_code(t: str) -> bool:
    """YYWW, YWW, YYYYWW: неделя 01–53, год не из будущего («1200» и «3006» — не коды даты)."""
    year_now = datetime.date.today().year
    if re.fullmatch(r"\d{4}", t):
        yy = int(t[:2])
        return 1 <= int(t[2:]) <= 53 and (yy <= year_now % 100 + 1 or yy >= 80)
    if re.fullmatch(r"\d{3}", t):
        return True
    if re.fullmatch(r"(19|20)\d{4}", t):
        return 1 <= int(t[4:]) <= 53 and int(t[:4]) <= year_now + 1
    return False


def _is_lot_code(t: str) -> bool:
    """Одна-две буквы и код даты («AC2242») — так обычно пишут партию, а не партномер."""
    m = re.fullmatch(r"[A-Z]{1,2}(\d{4})", t)
    return bool(m and _is_date_code(m.group(1)))


def _distance(a: str, b: str) -> int:
    """Расстояние Левенштейна."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def maker_of(tok: str) -> str:
    """Производитель по слову с корпуса; логотип читается с ошибками и с прилипшей цифрой:
    «WINBONG», «GIGODEVICE», «BROADCOM9». Пусто — слово не похоже на имя производителя."""
    if tok in MAKER_WORDS:
        return MAKER_WORDS[tok]
    m = re.fullmatch(r"\d{0,2}([A-Z]{%d,})\d{0,2}" % _FUZZY_MIN, tok)
    if not m:
        return ""
    word = m.group(1)
    for known, maker in MAKER_WORDS.items():
        if len(known) >= _FUZZY_MIN and _distance(word, known) <= (1 if len(known) < 8 else 2):
            return maker
    return ""


def _is_junk(tok: str) -> bool:
    """Пометки на корпусе: страна, RoHS, «eMMC»; страна с прилипшим кодом даты («MALAYSIA2224»)."""
    if tok in JUNK:
        return True
    m = re.fullmatch(r"([A-Z]{5,})\d{1,6}", tok)
    return bool(m and m.group(1) in JUNK)


def contextual_fix(tok: str) -> str:
    """Буква между цифрами → цифра; цифра в начальном буквенном префиксе → буква."""
    chars = list(tok)
    n = len(chars)
    for i, c in enumerate(chars):
        left = chars[i - 1] if i > 0 else ""
        right = chars[i + 1] if i < n - 1 else ""
        if c in TO_DIGIT and (left.isdigit() and (right.isdigit() or right == "")):
            chars[i] = TO_DIGIT[c]
    # цифра внутри начального буквенного префикса: "ST1M32" редкость; "5TM32" → "STM32"
    m = re.match(r"^([0-9A-Z]{1,4}?)([A-Z]{2,})(\d)", "".join(chars))
    s = "".join(chars)
    if m and m.group(1) and all(ch in TO_LETTER for ch in m.group(1) if ch.isdigit()):
        pref = "".join(TO_LETTER.get(ch, ch) if ch.isdigit() else ch for ch in m.group(1))
        s = pref + s[len(m.group(1)):]
    return s


def ambiguity_variants(tok: str, limit: int = 32) -> List[str]:
    positions = [i for i, c in enumerate(tok) if c in TO_DIGIT or c in TO_LETTER]
    positions = positions[:5]
    out = []
    for combo in itertools.product([False, True], repeat=len(positions)):
        chars = list(tok)
        for flip, i in zip(combo, positions):
            if flip:
                c = chars[i]
                chars[i] = TO_DIGIT.get(c) or TO_LETTER.get(c) or c
        out.append("".join(chars))
        if len(out) >= limit:
            break
    return out


def slashed_zero_variants(tok: str, limit: int = 15) -> List[str]:
    """Перечёркнутый ноль OCR читает как 8: варианты с заменой 8→0 (по одной и все сразу)."""
    pos = [i for i, c in enumerate(tok) if c == "8"][:4]
    out = []
    for n in range(1, len(pos) + 1):
        for combo in itertools.combinations(pos, n):
            out.append("".join("0" if i in combo else c for i, c in enumerate(tok)))
    return out[:limit]


class RuleIdentifier(Identifier):
    name = "rules"

    def __init__(self, settings, ctx):
        super().__init__(settings, ctx)
        self.rules = get_rules(ctx.app_dir)

    # -------------------- партномер --------------------
    def _tokens(self, text: str) -> List[List[str]]:
        lines = []
        for line in (text or "").splitlines():
            toks = [re.sub(r"[^A-Z0-9\-./+#]", "", t.upper()) for t in line.split()]
            toks = [t.strip("-./") for t in toks if t.strip("-./")]
            if toks:
                lines.append(toks)
        return lines

    def _lookup_external(self, part: str) -> Optional[Dict]:
        db = self.ctx.module("local_db")
        if db is not None and hasattr(db, "catalog_lookup"):
            try:
                return db.catalog_lookup(part)
            except Exception:  # noqa
                return None
        return None

    def _local_best(self, part: str) -> float:
        db = self.ctx.module("local_db")
        if db is None:
            return 0.0
        try:
            hits = db.search(part, limit=3)
        except Exception:  # noqa
            return 0.0
        return max([h.score for h in hits] or [0.0])

    def identify(self, text: str, alternatives: Optional[List[str]] = None) -> List[Candidate]:
        main = self._tokens(text)
        alt = self._tokens("\n".join(alternatives or []))
        manufacturer_hint = ""
        raw: Dict[str, float] = {}
        dotted = set()          # «CCX526.18», «P03B21.00S»: точка внутри — код партии, в партномерах её не бывает

        def consider(tok: str, base: float, why: str):
            t = norm_part(tok)
            if len(t) < 2 or _is_junk(t) or maker_of(t):
                return
            key = (t, why)
            raw[key] = max(raw.get(key, 0.0), base)
            if re.search(r"[A-Z0-9]\.[A-Z0-9]", tok.upper()):
                dotted.add(t)

        for group, base in ((main, 0.45), (alt, 0.2)):
            for toks in group:
                for t in toks:
                    maker = maker_of(norm_part(t))
                    if maker:
                        manufacturer_hint = manufacturer_hint or maker
                        continue
                    consider(t, base, "OCR")
                # OCR часто разрывает партномер пробелом: "STM32 F103C8T6"
                for a, b in zip(toks, toks[1:]):
                    if maker_of(norm_part(a)) or maker_of(norm_part(b)):
                        continue        # «TI 52»: логотип с соседним словом не склеиваем
                    if len(norm_part(a)) + len(norm_part(b)) <= 20:
                        consider(a + b, base - 0.1, "склейка")
            # Партномер часто печатают в две строки: "STM32F103" / "C8T6"
            for l1, l2 in zip(group, group[1:]):
                a, b = norm_part(l1[-1]), norm_part(l2[0])
                if len(a) >= 4 and re.search(r"\d$", a) and 2 <= len(b) <= 8 and len(a) + len(b) <= 22 \
                        and not _is_date_code(b):
                    consider(a + b, base - 0.1, "склейка двух строк")

        scored: Dict[str, Candidate] = {}
        total_tokens = sum(len(t) for t in main) or 1

        def push(part: str, score: float, reason: str, **kw):
            c = scored.get(part)
            if c is None or c.score < score:
                scored[part] = Candidate(part=part, score=min(1.0, score), reason=reason, **kw)

        for (tok, why), base in raw.items():
            is_digits = tok.isdigit()
            if _is_date_code(tok):
                continue
            shape_tok = self.rules.shape_ok(tok)
            variants = {tok: why}
            fixed = contextual_fix(tok)
            if fixed != tok:
                variants[fixed] = why + ", исправлены похожие символы"
            for v in ambiguity_variants(tok):
                variants.setdefault(v, why + ", замена похожих символов")
            for v in slashed_zero_variants(tok):
                variants.setdefault(v, why + ", перечёркнутый ноль (8→0)")

            for v, reason in variants.items():
                s = base if v == tok else base - (0.05 if v == fixed else 0.15)
                if v != tok and shape_tok is False and self.rules.shape_ok(v):
                    s = base + 0.05     # прочитанное не подходит под запись семейства, а вариант подходит
                    reason += ", по записи семейства"
                has_l = bool(re.search(r"[A-Z]", v))
                has_d = bool(re.search(r"\d", v))
                if has_l and has_d:
                    s += 0.1
                if len(v) >= 5:
                    s += 0.05
                if is_digits and len(v) <= 3:
                    s -= 0.2
                elif is_digits and len(v) <= 5 and not v.startswith("0"):
                    s += 0.2            # 4–5 цифр, не код даты: числовая маркировка мелкого корпуса («4420», «54327»)
                elif len(v) <= 2:
                    s -= 0.15           # две буквы-цифры — чаще обрывок или метка, чем маркировка
                if re.match(r"[A-Z]{1,5}\d", v):
                    s += 0.05           # буквы серии и номер («PCA9548») — обычная запись партномера
                if tok in dotted:
                    s -= 0.1
                info = self.rules.match(v) if has_d else None      # «AGL», «EMMC» без номера — не партномер
                if has_d and has_l and not info and why == "OCR" and v in (tok, fixed):
                    # усечённая маркировка: «25Q64FV» на корпусе — это W25Q64FV
                    info = next((i for i in map(self.rules.match, prefixed_parts(v, manufacturer_hint)) if i), None)
                ext = self._lookup_external(v) if (v == tok or v == fixed or info) else None
                desc, maker = "", manufacturer_hint
                confirmed_by = "справочник" if info else ("каталог" if ext else "")
                if info:
                    s += 0.3
                    desc = info["family"]
                    maker = maker or info["manufacturer"]
                if ext:
                    s += 0.3
                    desc = ext.get("description") or desc
                if v != tok and not info and not ext:
                    continue  # варианты с заменами оставляем только если они что-то значат
                if not info and not ext and _is_lot_code(v):
                    s -= 0.1
                if (v == tok or v == fixed) and self.settings.get("check_local_db", True) and len(v) >= 4:
                    loc = self._local_best(v)
                    if loc >= 0.95:
                        s += 0.25
                        confirmed_by = confirmed_by or "локальная база"
                        reason += ", есть в локальной базе"
                    elif loc >= 0.8:
                        s += 0.15
                        reason += ", семейство есть в локальной базе"
                short = len(v) <= 5 and not info and not ext
                push(v, s, reason, manufacturer=maker, description=desc, confirmed_by=confirmed_by,
                     is_marking_code=short and total_tokens <= 4)

        # Полный партномер точнее его начала: "STM32F103C8T6" важнее "STM32F103"
        vals = list(scored.values())
        for a in vals:
            longer = [b.score for b in vals if b is not a and b.part.startswith(a.part) and b.description
                      and b.score >= a.score - 0.25]
            if a.description and longer:
                a.score = max(0.0, min(a.score - 0.12, max(longer) - 0.01))
        cands = sorted(vals, key=lambda c: (-round(c.score, 2), -len(c.part)))
        # Короткие маркировочные коды — отдельной пометкой
        for c in cands:
            if c.is_marking_code:
                c.description = c.description or "похоже на SMD-код маркировки (не партномер)"
        return cands[: int(self.settings.get("max_candidates", 12))]

    # -------------------- корпус по фото --------------------
    def estimate_chip(self, image) -> ChipInfo:
        """Грубая оценка: соотношение сторон корпуса и число выводов по светлым
        металлическим выводам вокруг тёмного корпуса. Это оценка — пользователь
        может поправить в окне программы."""
        info = ChipInfo()
        try:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
            h, w = gray.shape[:2]
            blur = cv2.GaussianBlur(gray, (5, 5), 0)
            box = find_body(gray)
            if not box:
                return info
            x, y, bw, bh = box
            info.body_ratio = round(max(bw, bh) / float(max(1, min(bw, bh))), 2)

            # порог «металл вывода / плата» считаем только по области вокруг корпуса
            outside = np.ones_like(blur, bool)
            outside[y:y + bh, x:x + bw] = False
            vals = blur[outside]
            if vals.size < 50:
                return info
            t = cv2.threshold(vals.reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0]
            bright = ((blur > t) * 255).astype(np.uint8)
            bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            margin_min = 0.04
            pad = 2
            sides = {
                "top": bright[0:max(0, y - pad), x:x + bw] if y > h * margin_min else None,
                "bottom": bright[y + bh + pad:h, x:x + bw] if (h - y - bh) > h * margin_min else None,
                "left": bright[y:y + bh, 0:max(0, x - pad)] if x > w * margin_min else None,
                "right": bright[y:y + bh, x + bw + pad:w] if (w - x - bw) > w * margin_min else None,
            }
            counts = {}
            for side, strip in sides.items():
                if strip is None or strip.size == 0:
                    continue
                n, _lab, stats, _c = cv2.connectedComponentsWithStats(strip)
                areas = [stats[i, cv2.CC_STAT_AREA] for i in range(1, n)]
                if not areas:
                    continue
                med = float(np.median(areas))
                good = [a for a in areas if 0.35 * med <= a <= 2.5 * med and a > 8]
                if len(good) >= 1:
                    counts[side] = len(good)
            if counts:
                pins = sum(counts.values())
                info.pins = int(pins)
                info.pins_estimated = True
                if len(counts) >= 4 or (len(counts) == 3):
                    info.package = "выводы с 4 сторон (QFP/QFN?)"
                elif set(counts) in ({"top", "bottom"}, {"left", "right"}):
                    info.package = "выводы с 2 сторон (SOP/DIP/SSOP/TSSOP?)"
                else:
                    info.package = "выводы: " + ", ".join(sorted(counts))
        except Exception as e:  # noqa
            self.ctx.log.debug("estimate_chip: %s", e)
        return info
