# -*- coding: utf-8 -*-
"""Итоговый статус документа по уликам (ARCHITECTURE §4.4; шаг 4.3).

`decide` — чистая функция: улики (`verify.py`) и итог проверки файла (`validate.py`) → `Verdict`.
Пороги — `config.json → acquire.thresholds`. Причины — короткие ключи (текст для пользователя — дело окна):

- `hard:<причина>` — жёсткий отказ проверки файла (не PDF, активное содержимое…): сразу `rejected`;
- `no_text_layer` — скан, который не удалось прочитать (E12): итог не выше `needs_user`;
- `ocr_text` — текст скана распознан с картинок страниц (шаг 9.2): баллы считаются как обычно, пометка — для
  пользователя (распознанный текст беднее напечатанного);
- `manufacturer_conflict`, `package_conflict` — фото противоречит документу (E6, E7 с минусом); если документ
  при этом о нашем партномере (E1 или E3) — `needs_user`: противоречие показывается, а не прячется;
- `no_part_match` — нет ни E1, ни E3: выше `probable` не подняться;
- `other_part_in_heading` — E11: выше `probable` не подняться;
- `low_score` — баллов меньше порога `probable`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .models import Evidence, ValidationResult, Verdict

DEFAULT_THRESHOLDS: Dict[str, int] = {"confirmed": 70, "probable": 45}
_CONFLICTS = (("E6", "manufacturer_conflict"), ("E7", "package_conflict"))


def decide(evidence: Sequence[Evidence], validation: Optional[ValidationResult] = None,
           thresholds: Optional[Mapping[str, Any]] = None, ocr: bool = False) -> Verdict:
    limits = dict(DEFAULT_THRESHOLDS, **dict(thresholds or {}))
    evidence = list(evidence)
    if validation is not None and not validation.ok:
        return Verdict(status="rejected", score=0, evidence=evidence, reasons=["hard:" + validation.reason])
    points: Dict[str, int] = {}
    for e in evidence:
        points[e.code] = points.get(e.code, 0) + e.points
    score = max(0, min(100, sum(points.values())))
    if "E12" in points:
        return Verdict(status="needs_user", score=score, evidence=evidence, reasons=["no_text_layer"])
    ours = "E1" in points or "E3" in points
    reasons: List[str] = [] if ours else ["no_part_match"]
    if ocr:
        reasons.append("ocr_text")
    if "E11" in points:
        reasons.append("other_part_in_heading")
    conflicts = [reason for code, reason in _CONFLICTS if points.get(code, 0) < 0]
    reasons.extend(conflicts)
    if conflicts and ours:
        status = "needs_user"
    elif score < limits["probable"]:
        status = "rejected"
        reasons.insert(0, "low_score")
    elif score >= limits["confirmed"] and ours and "E11" not in points:
        status = "confirmed"
    else:
        status = "probable"
    return Verdict(status=status, score=score, evidence=evidence, reasons=reasons)
