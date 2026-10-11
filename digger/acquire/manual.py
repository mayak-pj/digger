# -*- coding: utf-8 -*-
"""Проверка PDF, скачанного пользователем вручную (ARCHITECTURE §4.8; шаг 5.7).

Файл проходит тот же путь, что и скачанный программой: копия в карантин → проверка файла → извлечение →
улики → вердикт → подтверждение → библиотека (источник `manual`). Исходный файл пользователя не открывается
ничем, кроме чтения, и не меняется. Адрес, откуда пользователь скачал файл, необязателен; если он указан и это
сайт производителя, работает правило подтверждения (а), иначе файл максимум «вероятен» до решения пользователя.
Не бросает исключений: нечитаемый файл — отказ `hard:unreadable`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import confirm as confirm_mod
from .decide import decide
from .events import EventBus
from .models import AcquisitionRecord, FetchResult, Lead, PhotoContext, ValidationResult
from .store import AcquireStore, _now
from .validate import MAX_MB, validate_pdf
from .verify import SourceTrust, verify_file

log = logging.getLogger("digger.acquire.manual")
SOURCE_ID = "manual"


def _to_quarantine(path: str, quarantine_dir: str, max_mb: float) -> FetchResult:
    """Копирует файл в карантин под именем `<sha16>.pdf.quarantine`, как netsafe."""
    size = os.path.getsize(path)
    if size > max_mb * 1048576:
        return FetchResult(ok=False, size=size, error="too_big")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    os.makedirs(quarantine_dir, exist_ok=True)
    dst = os.path.join(quarantine_dir, sha[:16] + ".pdf.quarantine")
    shutil.copyfile(path, dst)
    return FetchResult(ok=True, path_in_quarantine=dst, sha256=sha, size=size, content_type="application/pdf")


def check_manual_pdf(path: str, ctx: PhotoContext, store: AcquireStore, quarantine_dir: str,
                     url: str = "", bus: Optional[EventBus] = None, trust: Optional[SourceTrust] = None,
                     thresholds: Optional[Mapping[str, Any]] = None, weights: Optional[Mapping[str, Any]] = None,
                     others: Iterable[AcquisitionRecord] = (), owners: Iterable[Sequence[str]] = (),
                     user: bool = False, max_mb: float = MAX_MB) -> AcquisitionRecord:
    """Полный путь проверки для файла пользователя. Итог — запись; в библиотеку попадают confirmed/probable."""
    emit = bus.emit if bus is not None else (lambda *a, **k: None)
    lead = Lead(url=url, title=os.path.basename(path), source_id=SOURCE_ID, level=SOURCE_ID, kind="pdf",
                language="ru")
    rec = AcquisitionRecord(part=ctx.part, lead=lead, started_at=_now())
    kw = dict(lang="ru", level=SOURCE_ID, source=SOURCE_ID)
    emit("manual.start", file=lead.title, **kw)
    try:
        rec.fetch = _to_quarantine(path, quarantine_dir, max_mb)
    except OSError as e:
        log.info("свой PDF %s: %s", path, e)
        rec.fetch = FetchResult(ok=False, error=str(e) or type(e).__name__)
    if not rec.fetch.ok:
        reason = "too_big" if rec.fetch.error == "too_big" else "unreadable"
        if reason == "too_big":
            emit("validate.too_big", size=rec.fetch.size, **kw)
        rec.verdict = decide([], ValidationResult(ok=False, reason=reason, size=rec.fetch.size))
        return _finish(rec, store)
    emit("quarantine.placed", **kw)
    qpath = rec.fetch.path_in_quarantine
    validation = validate_pdf(qpath, bus, lead, max_mb)
    if not validation.ok:
        rec.verdict = decide([], validation)
        return _finish(rec, store)
    rec.facts, evidence = verify_file(qpath, ctx, url=url, trust=trust, weights=weights,
                                      progress=lambda n: emit("scan.ocr", pages=n, **kw))
    rec.verdict = decide(evidence, validation, thresholds, ocr=bool(rec.facts.ocr_pages))
    rec = confirm_mod.apply(rec, confirm_mod.confirm(rec, others, trust, owners, user=user))
    return _finish(rec, store)


def _finish(rec: AcquisitionRecord, store: AcquireStore) -> AcquisitionRecord:
    rec.finished_at = _now()
    store.save(rec)
    return rec
