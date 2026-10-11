# -*- coding: utf-8 -*-
"""Общие структуры данных, которыми обмениваются модули.

Модули не знают друг о друге — они получают и возвращают только эти объекты.
Поэтому любой модуль можно заменить, если новый возвращает те же структуры.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class ImageVariant:
    """Один вариант обработанного изображения."""
    name: str                 # например "clahe_x3", "otsu_inv_rot90"
    image: Any                # numpy.ndarray (серое или BGR)
    rotation: int = 0         # 0/90/180/270


@dataclass
class OcrLine:
    text: str
    confidence: float         # 0..100
    variant: str              # из какого варианта изображения
    provider: str = ""        # id провайдера, прочитавшего строку


@dataclass
class OcrAttempt:
    """Одна попытка провайдера в цепочке распознавания."""
    provider: str
    title: str = ""
    status: str = ""          # ok | weak | unconfirmed | empty | failed | unavailable | no_consent
    confidence: float = 0.0   # 0..100
    seconds: float = 0.0
    lines: int = 0
    detail: str = ""          # причина для failed / unavailable
    text: str = ""            # что прочитал этот провайдер


@dataclass
class OcrResult:
    lines: List[OcrLine] = field(default_factory=list)
    best_text: str = ""       # итоговая маркировка (несколько строк через \n)
    best_variant: str = ""
    provider: str = ""        # id провайдера распознавания (recognition/), пусто — введено вручную
    provider_title: str = ""
    seconds: float = 0.0      # время провайдера, давшего результат
    confidence: float = 0.0   # 0..100, средняя по строкам итоговой маркировки
    confirmed: bool = False   # партномер из прочитанного подтверждён справочником, каталогом или локальной базой
    total_seconds: float = 0.0  # время всей цепочки
    attempts: List[OcrAttempt] = field(default_factory=list)
    mode: str = ""            # способ, которым просили распознать: auto | compare | id провайдера


@dataclass
class Candidate:
    """Кандидат на партномер."""
    part: str                 # как предлагаем искать, например "STM32F103C8T6"
    score: float              # 0..1, насколько вероятен
    manufacturer: str = ""
    reason: str = ""          # откуда взялся (OCR, замена O->0, справочник...)
    description: str = ""     # из справочника, если есть
    is_marking_code: bool = False  # короткий SMD-код, а не партномер
    confirmed_by: str = ""    # чем подтверждён: справочник | каталог | локальная база; пусто — ничем


@dataclass
class DatasheetHit:
    """Найденный datasheet или ссылка на него."""
    part: str
    title: str
    location: str             # путь к файлу или URL
    source: str               # "local", "alldatasheet", "duckduckgo"...
    level: str                # "local" / "catalog" / "engine" / "forum" / "china" / "marking" / "github"
    score: float = 0.0
    is_local: bool = False
    is_pdf: bool = False
    allowed: bool = True      # домен в белом списке (можно скачивать автоматически)
    note: str = ""


@dataclass
class DownloadResult:
    ok: bool
    path: str = ""
    sha256: str = ""
    size: int = 0
    suspicious: List[str] = field(default_factory=list)
    message: str = ""
    verified_part: bool = False   # партномер найден в тексте PDF


@dataclass
class CheckItem:
    name: str
    status: str               # "ok" / "fail" / "warn" / "unknown"
    detail: str = ""


@dataclass
class Comparison:
    checks: List[CheckItem] = field(default_factory=list)
    score: float = 0.0        # 0..1
    verdict: str = ""         # короткий вывод
    packages_in_datasheet: List[str] = field(default_factory=list)


@dataclass
class MemoryItem:
    kind: str                 # "EEPROM", "Flash", "SRAM", "OTP"...
    size: str = ""            # "2 Kbit", "64 KB"
    evidence: str = ""        # цитата/правило


@dataclass
class MemoryVerdict:
    has_memory: Optional[bool]    # True / False / None (неизвестно)
    items: List[MemoryItem] = field(default_factory=list)
    confidence: float = 0.0
    summary: str = ""


@dataclass
class ChipInfo:
    """Сведения о чипе с фото, которые ввёл пользователь или оценила программа."""
    package: str = ""         # "SOP-8", "QFN-32"... (пусто = неизвестно)
    pins: int = 0             # 0 = неизвестно
    pins_estimated: bool = False
    body_ratio: float = 0.0   # длина/ширина корпуса по фото


@dataclass
class ChipReport:
    image_path: str
    ocr: Optional[OcrResult] = None
    candidates: List[Candidate] = field(default_factory=list)
    chosen_part: str = ""
    chip: ChipInfo = field(default_factory=ChipInfo)
    hits: List[DatasheetHit] = field(default_factory=list)
    datasheet_path: str = ""
    comparison: Optional[Comparison] = None
    memory: Optional[MemoryVerdict] = None
    log: List[str] = field(default_factory=list)
    conclusion: Any = None    # `acquire.conclusion.Conclusion` последнего поиска без подтверждённого документа (§4.11)
    web_searched: bool = False   # по чипу уже искали в интернете (метка «не найдено» вместо «нет в базе»)
    records: List[Any] = field(default_factory=list)   # записи поиска (`AcquisitionRecord`) для вкладок «Документы» и «Почему»

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("records", None)       # записи поиска лежат в базе и паспортах, в отчёт не идут
        return d
