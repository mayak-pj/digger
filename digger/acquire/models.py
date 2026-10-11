# -*- coding: utf-8 -*-
"""Структуры данных подсистемы получения документов (ARCHITECTURE §4.3).

Этапы конвейера (§4.1) обмениваются только этими объектами. Каждый умеет превращаться в dict/JSON и
обратно: так пишутся паспорт документа (§4.9), кэш выдачи и отчёты. `from_dict` пропускает незнакомые
ключи и подставляет значения по умолчанию — паспорт, записанный другой версией программы, читается.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

LEAD_KINDS = ("pdf", "page", "forum", "repo")
VERDICT_STATUSES = ("confirmed", "probable", "rejected", "needs_user")
EVIDENCE_DETAIL_MAX = 150


class Model:
    """Общая сериализация. `_nested`: поле → класс вложенной модели (одна или список)."""
    _nested: Dict[str, Any] = {}
    _required: str = ""       # поле, без которого объект не имеет смысла

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Any:
        data = data or {}
        if cls._required and not data.get(cls._required):
            raise ValueError("%s: нет поля «%s»" % (cls.__name__, cls._required))
        kwargs = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            sub = cls._nested.get(f.name)
            if sub is not None and value is not None:
                value = [sub.from_dict(v) for v in value] if isinstance(value, list) else sub.from_dict(value)
            kwargs[f.name] = value
        return cls(**kwargs)

    @classmethod
    def from_json(cls, text: str) -> Any:
        return cls.from_dict(json.loads(text))


@dataclass
class Lead(Model):
    """Ссылка-кандидат от источника: PDF или страница, где его искать."""
    _required = "url"

    url: str
    title: str = ""
    snippet: str = ""
    source_id: str = ""       # id адаптера: "ddg", "alldatasheet", "manual"...
    level: str = ""           # уровень источника из sources.json
    kind: str = "page"        # pdf | page | forum | repo
    query: str = ""           # запрос, по которому найдена
    language: str = ""        # язык запроса: en | zh | ru
    rank_score: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in LEAD_KINDS:
            raise ValueError("Lead.kind: %r, ожидается одно из %s" % (self.kind, ", ".join(LEAD_KINDS)))


@dataclass
class FetchResult(Model):
    """Итог скачивания в карантин."""
    ok: bool = False
    path_in_quarantine: str = ""
    sha256: str = ""
    size: int = 0
    content_type: str = ""
    final_url: str = ""
    error: str = ""
    failure_class: str = ""   # класс неудачи по §4.11 (site_protected, network_blocked...), пусто при успехе


@dataclass
class ValidationResult(Model):
    """Итог проверки файла из карантина (acquire/validate.py). `ok=False` — жёсткий отказ (§4.4)."""
    ok: bool = False
    reason: str = ""          # not_pdf | too_big | damaged | encrypted | active_content, пусто при успехе
    pages: int = 0
    has_text: bool = False    # False при ok — скан без текстового слоя
    size: int = 0
    active: List[str] = field(default_factory=list)   # найденное активное содержимое: "JavaScript", "Launch"...
    detail: str = ""          # подробность для журнала и паспорта (текст ошибки разбора)


@dataclass
class DocFacts(Model):
    """Что извлечено из документа."""
    pages: int = 0
    has_text: bool = False
    title: str = ""           # заголовок из метаданных PDF
    heading: str = ""         # заголовок первой страницы: строки самым крупным шрифтом
    producer: str = ""
    parts_found: Dict[str, List[int]] = field(default_factory=dict)   # партномер → страницы (с 1)
    family_patterns: List[str] = field(default_factory=list)          # "STM32F103x8"
    packages: List[str] = field(default_factory=list)
    manufacturers: List[str] = field(default_factory=list)
    ordering_codes: List[str] = field(default_factory=list)
    marking_codes: List[str] = field(default_factory=list)
    language: str = ""
    doc_type: str = ""        # datasheet / family / app_note / errata... (acquire/classify.py)
    text_fingerprint: str = ""  # simhash текста, шестнадцатеричная строка
    ocr_pages: List[int] = field(default_factory=list)   # страницы скана, прочитанные распознаванием

    def __post_init__(self) -> None:
        self.parts_found = {str(part): sorted({int(p) for p in pages})
                            for part, pages in (self.parts_found or {}).items()}


@dataclass
class PhotoContext(Model):
    """Что известно о чипе с фото: с этим сверяется документ (acquire/verify.py)."""
    part: str = ""            # партномер как прочитан, с суффиксом, если он есть
    manufacturer: str = ""    # по логотипу или префиксу партномера
    package: str = ""         # "SOIC-8", "SOT-23"
    marking: str = ""         # короткий код маркировки (SMD-код)


@dataclass
class Evidence(Model):
    """Одна улика проверки (§4.4)."""
    _required = "code"

    code: str                 # E1…E12
    points: int = 0
    detail: str = ""          # цитата ≤ 150 символов
    page: int = 0             # страница цитаты, 0 — не привязана к странице

    def __post_init__(self) -> None:
        if len(self.detail) > EVIDENCE_DETAIL_MAX:
            self.detail = self.detail[:EVIDENCE_DETAIL_MAX - 1] + "…"


@dataclass
class Verdict(Model):
    _nested = {"evidence": Evidence}

    status: str = "rejected"  # confirmed | probable | rejected | needs_user
    score: int = 0            # 0–100
    evidence: List[Evidence] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in VERDICT_STATUSES:
            raise ValueError("Verdict.status: %r, ожидается одно из %s"
                             % (self.status, ", ".join(VERDICT_STATUSES)))
        self.score = max(0, min(100, int(self.score)))

    def has(self, code: str) -> bool:
        return any(e.code == code for e in self.evidence)


@dataclass
class AcquisitionRecord(Model):
    """Полная история одного документа: от ссылки до места в библиотеке."""
    _nested = {"lead": Lead, "fetch": FetchResult, "facts": DocFacts, "verdict": Verdict}
    _required = "part"

    part: str
    lead: Optional[Lead] = None
    fetch: Optional[FetchResult] = None
    facts: Optional[DocFacts] = None
    verdict: Optional[Verdict] = None
    sources_agreeing: List[str] = field(default_factory=list)   # независимые домены с тем же документом
    stored_path: str = ""
    started_at: str = ""      # ISO 8601, UTC
    finished_at: str = ""
