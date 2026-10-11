# -*- coding: utf-8 -*-
"""Факты документа (ARCHITECTURE §4.2, §4.3; шаг 4.1).

`read_document` читает проверенный PDF (pypdf, ничего не исполняется): текст по страницам, метаданные,
заголовок первой страницы — строки самым крупным шрифтом. `facts_from_text` — чистая функция: из текста
собирает `DocFacts` (партномера по страницам, шаблоны семейств, корпуса, производители, заказные коды,
коды маркировки, язык). `extract_facts` — то и другое; исключений не бросает.
Страницы без текстового слоя (скан) читает `scanocr.py`; какие страницы прочитаны так — `ocr_pages`.

Здесь только «что написано в документе». Подходит ли документ к искомому чипу, решают `classify.py` и
`verify.py`; им же нужен `covers` — покрывает ли шаблон семейства партномер.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import scanocr
from .fingerprint import fingerprint_pages
from .models import DocFacts

log = logging.getLogger("digger.acquire.extract")
logging.getLogger("pypdf").setLevel(logging.ERROR)

HEAD_PAGES = 50               # длинный документ читается не весь: начало…
TAIL_PAGES = 20               # …и конец (таблицы заказных кодов, маркировки и корпусов — в конце)
TEXT_MIN_CHARS = 20           # меньше знаков во всём прочитанном — скан без текстового слоя
MAX_PARTS = 400
MAX_LIST = 100                # предел для списков шаблонов, заказных кодов и кодов маркировки
SECTION_LINES = 40            # сколько строк после заголовка раздела считается таблицей
HEADING_MAX = 200
MAKER_PAGES = 2               # производитель ищется на первых страницах и на последней

_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]+(?:-[A-Z0-9]+(?:\.\d+)?)*(?![A-Za-z0-9])")   # «AT24C02N-10SU-2.7»
_PKG_NAMES = (r"P?DIP|SPDIP|CDIP|SOIC|SOP|SO|SSOP|TSSOP|HTSSOP|MSOP|VSSOP|QSOP|ESOP|HSOP|"
              r"QFN|VQFN|WQFN|UQFN|HVQFN|VFQFPN|UFQFPN|DFN|UDFN|WDFN|TDFN|XDFN|SON|WSON|USON|VSON|"
              r"LQFP|TQFP|PQFP|QFP|LGA|BGA|FBGA|TFBGA|UFBGA|VFBGA|TSOP|PLCC|WLCSP|CSP")
# pypdf нередко рвёт слова пробелами: «SOIC -16», «W25Q512 JVFIQ». «SOIC 300-mil» — ширина, а не число выводов.
_PKG_RE = re.compile(r"(?<![A-Za-z0-9])(%s)\s?-?\s?(\d{1,3})L?(?![A-Za-z0-9])(?!\s?\d?\s?-?\s?mil)" % _PKG_NAMES)
_PKG_PINS_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,3})[-\s](?:pin|lead|ball)s?\s+(%s)(?![A-Za-z0-9])" % _PKG_NAMES)
_PKG_SMALL_RE = re.compile(r"(?<![A-Za-z0-9])(SOT|SC|TO)[-\s]?(\d{1,3})(?:-(\d))?(?![A-Za-z0-9])")  # регистр важен: «to 3 V»
_PKG_TOKEN_RE = re.compile(r"(?:%s|SOT|SC|TO|D2PAK|DPAK)(?:-?\d{1,3}){0,2}L?$" % _PKG_NAMES)
# Не партномера: число с единицей измерения, стандарты и номера документов, выводы и узлы, шестнадцатеричные числа.
_NOISE_RE = re.compile(r"\d+[A-Z]{1,4}$|0X[0-9A-F]+$|00[0-9A-F]+$|[0-9A-F]+0000$|P[A-K]\d{1,2}$|"
                       r"(?:ISO|IEC|JESD|JEP|AEC|MIL|EN|UL|RS|EIA|IPC|ANSI|IEEE|USB|REV|VER|AN|DDR|CISPR|MSL|"
                       r"GPIO|ADC|DAC|UART|USART|TIM|FIG|TABLE|NOTE|PIN)\d+[A-Z]?$")
_FAMILY_X_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,6}\d[A-Z0-9]*(?:x+|XX+)[A-Z0-9]*(?:x+[A-Z0-9]*)*(?![A-Za-z0-9])")
_FAMILY_ENUM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{0,5}\d{2}[A-Z]{1,3}\d{2,4}[A-Z]?"
                             r"(?:\s?[/_]\s?\d{2,4}[A-Z]?){1,10}(?![A-Za-z0-9])")
_ENUM_SPLIT_RE = re.compile(r"([A-Z]{0,5}\d{2}[A-Z]{1,3})(\d{2,4})[A-Z]?((?:[/_]\d{2,4}[A-Z]?)+)$")
_ORDER_RE = re.compile(r"order(?:ing|able)\b|valid part numbers?|订购|订货|選型|选型|"
                       r"информаци\w+ для заказа|обозначени\w+ при заказе", re.I)
_MARK_RE = re.compile(r"marking|top[- ]?mark|丝印|打标|印字|маркировк", re.I)
_MARK_INLINE_RE = re.compile(r"(?:marking(?: code)?|top[- ]?mark|丝印|маркировка)\s*[:：]\s*([A-Z0-9]{2,12})(?![A-Za-z0-9])",
                             re.I)
_UNIT_RE = re.compile(r"\d+(?:V|MV|A|MA|UA|HZ|KHZ|MHZ|NS|US|MS|MM|MIL|K|M|PF|NF|UF|L|LD|PIN|KB|MB|BIT|KBIT|MBIT)$")
_JUNK_TITLE_RE = re.compile(r"untitled|document\d*|без имени|無題|未命名|\d*", re.I)

# Производитель → как он называет себя в документе. Короткие обозначения (ST, TI, ON) не годятся: это и слова.
MANUFACTURERS = {
    "Texas Instruments": ("Texas Instruments", "德州仪器"),
    "STMicroelectronics": ("STMicroelectronics", "意法半导体"),
    "Microchip": ("Microchip", "微芯"),
    "Atmel": ("Atmel",),
    "NXP": ("NXP", "恩智浦"),
    "onsemi": ("onsemi", "ON Semiconductor", "安森美"),
    "Analog Devices": ("Analog Devices", "亚德诺"),
    "Maxim": ("Maxim Integrated",),
    "Infineon": ("Infineon", "英飞凌"),
    "Renesas": ("Renesas", "瑞萨"),
    "Winbond": ("Winbond", "华邦"),
    "GigaDevice": ("GigaDevice", "兆易创新"),
    "Macronix": ("Macronix", "旺宏"),
    "ISSI": ("ISSI", "Integrated Silicon Solution"),
    "Espressif": ("Espressif", "乐鑫"),
    "WCH": ("WCH", "Nanjing Qinheng", "沁恒"),
    "Holtek": ("Holtek", "合泰"),
    "STC": ("STC micro", "STCmicro", "宏晶"),
    "Nuvoton": ("Nuvoton", "新唐"),
    "Puya": ("Puya", "普冉"),
    "Padauk": ("Padauk", "应广"),
    "Giantec": ("Giantec", "聚辰"),
    "Fudan Microelectronics": ("Fudan Micro", "复旦微"),
    "XTX": ("XTX Technology", "芯天下"),
    "Fairchild": ("Fairchild",),
    "Philips": ("Philips",),
    "Toshiba": ("Toshiba", "东芝"),
    "Samsung": ("Samsung", "三星"),
    "Micron": ("Micron Technology",),
    "SK hynix": ("hynix",),
    "Cypress": ("Cypress Semiconductor",),
    "Silicon Labs": ("Silicon Labs", "Silicon Laboratories"),
    "FTDI": ("FTDI", "Future Technology Devices"),
    "ROHM": ("ROHM",),
    "Diodes": ("Diodes Incorporated",),
    "Nexperia": ("Nexperia",),
    "Vishay": ("Vishay",),
    "Миландр": ("Миландр", "Milandr"),
    "Ангстрем": ("Ангстрем",),
    "Интеграл": ("ОАО «ИНТЕГРАЛ»", "ОАО \"ИНТЕГРАЛ\""),
}


def _alias_re(alias: str) -> Any:
    latin = alias.isascii()
    body = re.escape(alias).replace(r"\ ", r"\s+")
    if latin:
        body = r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % body
    short = latin and alias.isupper() and len(alias) <= 5         # «WCH», «ISSI» — только прописными
    return re.compile(body, 0 if short else re.I)


_MAKER_RES = [(name, [_alias_re(a) for a in aliases]) for name, aliases in MANUFACTURERS.items()]


@dataclass
class DocText:
    """Прочитанный документ: то, из чего собираются факты и что нужно `classify.py` и `verify.py`."""
    pages: int = 0                                           # всего страниц в документе
    texts: Dict[int, str] = field(default_factory=dict)     # номер страницы (с 1) → текст; только прочитанные
    title: str = ""                                          # метаданные
    producer: str = ""
    author: str = ""
    heading: str = ""                                        # строки первой страницы самым крупным шрифтом
    ocr_pages: List[int] = field(default_factory=list)      # страницы, текст которых распознан с картинки


def extract_facts(path: str, hints: Sequence[str] = ()) -> DocFacts:
    """Факты документа из карантина или библиотеки. Нечитаемый файл — пустые факты (pages=0)."""
    return facts_from_text(read_document(path), hints)


def read_document(path: str, head_pages: int = HEAD_PAGES, tail_pages: int = TAIL_PAGES,
                  progress: Optional[Callable[[int], None]] = None) -> DocText:
    """`progress(n)` — перед распознаванием скана: сколько страниц будет прочитано (это секунды)."""
    doc = DocText()
    try:
        _read(path, doc, head_pages, tail_pages)
    except (Exception, RecursionError) as e:      # noqa: BLE001 — pypdf на битом файле бросает что угодно
        log.info("не читается %s: %s: %s", path, type(e).__name__, e)
    for n, text in sorted(scanocr.ocr_pages(path, scanocr.scan_pages(doc.texts), progress).items()):
        doc.texts[n] = (doc.texts[n] + "\n" + _clean(text)).strip()
        doc.ocr_pages.append(n)
    return doc


def _read(path: str, doc: DocText, head_pages: int, tail_pages: int) -> None:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        reader.decrypt("")
    total = len(reader.pages)
    try:
        meta = reader.metadata or {}
    except (Exception, RecursionError):           # noqa: BLE001
        meta = {}
    doc.title, doc.producer, doc.author = (_meta(meta, k) for k in ("/Title", "/Producer", "/Author"))
    doc.producer = doc.producer or _meta(meta, "/Creator")
    numbers = list(range(total))
    if total > head_pages + tail_pages:
        numbers = numbers[:head_pages] + numbers[total - tail_pages:]
    for i in numbers:
        chunks: List[Tuple[str, float]] = []

        def visit(text: str, cm: Any, tm: Any, font: Any, size: Any) -> None:
            chunks.append((text, _font_size(size, cm, tm)))

        try:
            text = reader.pages[i].extract_text(visitor_text=visit if i == 0 else None) or ""
        except (Exception, RecursionError) as e:  # noqa: BLE001 — страница без читаемого текста
            log.debug("страница %d не читается: %s", i + 1, e)
            continue
        doc.texts[i + 1] = _clean(text)
        if i == 0:
            doc.heading = _heading(chunks, doc.texts[1])
    doc.pages = total


def _meta(meta: Any, key: str) -> str:
    try:
        value = meta.get(key)
    except (Exception, RecursionError):           # noqa: BLE001
        return ""
    if isinstance(value, bytes):                  # строка без метки кодировки
        value = str(value, "utf-16-be" if value[:1] == b"\x00" else "latin-1", "replace")
    return _clean(str(value or "")).strip()


def _clean(text: str) -> str:
    """Полноширинные знаки, лигатуры и типографские дефисы — к обычным; иначе партномер не узнать."""
    text = unicodedata.normalize("NFKC", text or "").replace("\x00", "")
    return re.sub(r"[‐-―−]", "-", text)


def _font_size(size: Any, cm: Any, tm: Any) -> float:
    try:
        return float(size) * math.hypot(tm[0], tm[1]) * math.hypot(cm[0], cm[1])
    except (TypeError, ValueError, IndexError):
        return 0.0


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return " ".join(line.split())[:HEADING_MAX]
    return ""


def _heading(chunks: List[Tuple[str, float]], page_text: str) -> str:
    """Текст самым крупным шрифтом; если шрифт на странице один — первая непустая строка."""
    sized = [(_clean(t), s) for t, s in chunks if len(t.strip()) >= 3]
    if not sized:
        return _first_line(page_text)
    top = max(s for _, s in sized)
    big = " ".join(" ".join(t.split()) for t, s in sized if s >= top * 0.95)
    if top <= 0 or len(big) > HEADING_MAX or len(big) * 2 > sum(len(t) for t, _ in sized):
        return _first_line(page_text)
    return big


def facts_from_text(doc: DocText, hints: Sequence[str] = ()) -> DocFacts:
    """`hints` — партномера, которые ищутся без учёта регистра (ATmega328P): общий разбор берёт только
    слова из прописных букв и цифр; подсказка находится и разорванной пробелом («W25Q512 JVFIQ»).
    Найденная подсказка попадает в `parts_found` прописными."""
    facts = DocFacts(pages=doc.pages, title=_clean_title(doc.title), producer=doc.producer)
    facts.heading = doc.heading or _first_line(doc.texts.get(1, ""))
    numbers = sorted(doc.texts)
    whole = "\n".join(doc.texts[n] for n in numbers)
    facts.has_text = len("".join(whole.split())) >= TEXT_MIN_CHARS
    facts.ocr_pages = list(doc.ocr_pages)
    if not facts.has_text:
        facts.heading = ""
        return facts
    facts.language = _language(whole)
    facts.text_fingerprint = fingerprint_pages(doc.texts[n] for n in numbers)
    parts: Dict[str, List[int]] = {}
    wanted = [(h, re.compile(r"(?<![A-Z0-9])%s(?![A-Z0-9])" % " ?".join(re.escape(c) for c in h)))
              for h in _unique(re.sub(r"\s+", "", h or "").upper() for h in hints) if h]
    for n in numbers:
        text = doc.texts[n]
        found = list(_parts(text))
        upper = text.upper()
        found.extend(h for h, rx in wanted if rx.search(upper))
        for part in found:
            if part in parts or len(parts) < MAX_PARTS:
                parts.setdefault(part, []).append(n)
        lines = text.splitlines()
        _section(lines, _ORDER_RE, facts.ordering_codes, parts_only=True)
        facts.marking_codes.extend(m.group(1).upper() for m in _MARK_INLINE_RE.finditer(text)
                                   if not _PKG_TOKEN_RE.match(m.group(1).upper()))
        _section(lines, _MARK_RE, facts.marking_codes, parts_only=False)
    facts.parts_found = parts
    facts.__post_init__()
    facts.family_patterns = _unique(_families(whole))[:MAX_LIST]
    facts.packages = _packages(whole)
    facts.ordering_codes = _unique(facts.ordering_codes)[:MAX_LIST]
    facts.marking_codes = _unique(facts.marking_codes)[:MAX_LIST]
    near = [doc.texts[n] for n in numbers[:MAKER_PAGES] + numbers[MAKER_PAGES:][-1:]]
    facts.manufacturers = _manufacturers("\n".join([doc.author, doc.title] + near))
    return facts


def _clean_title(title: str) -> str:
    """Заголовок из метаданных без следов программы: «Microsoft Word - lm358.doc» → «lm358»."""
    t = re.sub(r"^(Microsoft\s+(Office\s+)?Word|Microsoft\s+PowerPoint|PowerPoint\s+Presentation)\s*-?\s*", "",
               " ".join((title or "").split()), flags=re.I)
    t = re.sub(r"\.(docx?|pdf|rtf|pptx?|dvi|indd|fm|qxd)$", "", t, flags=re.I).strip()
    return "" if _JUNK_TITLE_RE.fullmatch(t) else t


def _unique(items: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(items))


def _language(text: str) -> str:
    cjk = cyr = latin = 0
    for ch in text:
        if "一" <= ch <= "鿿":
            cjk += 1
        elif "Ѐ" <= ch <= "ӿ":
            cyr += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    if cjk * 10 > cjk + cyr + latin:              # иероглиф — слово; в таблицах много латиницы
        return "zh"
    if cyr * 3 > cyr + latin:
        return "ru"
    return "en" if latin else ""


def _is_part(token: str) -> bool:
    """Похоже на партномер: 4–24 знака, есть буква и хотя бы две цифры, не единица измерения и не корпус."""
    if not 4 <= len(token) <= 24 or sum(c.isdigit() for c in token) < 2 or not any(c.isalpha() for c in token):
        return False
    return not (_NOISE_RE.match(token) or _PKG_TOKEN_RE.match(token))


def _is_code(token: str) -> bool:
    """Код маркировки в таблице: буквы с цифрами («1AM», «25Q64CSIG»), но не корпус и не величина («8L», «5V»)."""
    return (2 <= len(token) <= 12 and any(c.isalpha() for c in token) and any(c.isdigit() for c in token)
            and not _PKG_TOKEN_RE.match(token) and not _UNIT_RE.match(token))


def _is_tail(token: str) -> bool:
    """Хвост разорванного заказного кода: короткий суффикс («EIQ», «T6»), а не слово, корпус или другой код."""
    return len(token) <= 5 and token.isalnum() and not (_is_part(token) or _PKG_TOKEN_RE.match(token)
                                                        or _UNIT_RE.match(token))


def _parts(text: str) -> Iterable[str]:
    """Партномера из текста. «AT24C02N-10SU» даёт и полную запись, и «AT24C02N»."""
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0)
        head = token.split("-")[0]
        if not _is_part(head):
            continue
        yield head
        if token != head and len(token) <= 24 and not _PKG_TOKEN_RE.match(token):
            yield token


def _section(lines: List[str], heading: Any, out: List[str], parts_only: bool) -> None:
    """Слова из строк раздела: от строки с заголовком до конца страницы, но не дальше SECTION_LINES строк."""
    left = 0
    for line in lines:
        if heading.search(line):
            left = SECTION_LINES + 1
        if left <= 0:
            continue
        left -= 1
        tokens = list(_TOKEN_RE.finditer(line))
        for i, m in enumerate(tokens):
            token = m.group(0)
            if not parts_only:
                if _is_code(token):
                    out.append(token)
            elif _is_part(token.split("-")[0]) and not _PKG_TOKEN_RE.match(token):
                out.append(token if len(token) <= 24 else token.split("-")[0])
                tail = tokens[i + 1] if i + 1 < len(tokens) else None
                if tail is not None and line[m.end():tail.start()] == " " and _is_tail(tail.group(0)):
                    out.append(token + tail.group(0))          # код, разорванный пробелом


def _families(text: str) -> Iterable[str]:
    found = []
    for m in _FAMILY_X_RE.finditer(text):
        token = m.group(0)
        if len(token) >= 6 and sum(c.isdigit() for c in token) >= 2:
            found.append((m.start(), token))
    for m in _FAMILY_ENUM_RE.finditer(text):
        found.append((m.start(), re.sub(r"\s+", "", m.group(0))))
    return [token for _, token in sorted(found)]


def _packages(text: str) -> List[str]:
    found = []
    for m in _PKG_RE.finditer(text):
        if 2 <= int(m.group(2)) <= 1200:
            found.append((m.start(), "%s-%d" % (m.group(1), int(m.group(2)))))
    for m in _PKG_PINS_RE.finditer(text):
        found.append((m.start(), "%s-%d" % (m.group(2), int(m.group(1)))))
    for m in _PKG_SMALL_RE.finditer(text):
        found.append((m.start(), "%s-%s%s" % (m.group(1), m.group(2), "-" + m.group(3) if m.group(3) else "")))
    return _unique(name for _, name in sorted(found))[:MAX_LIST]


def _manufacturers(text: str) -> List[str]:
    """Производители в порядке убывания числа упоминаний."""
    counts = []
    for name, regexes in _MAKER_RES:
        n = sum(len(rx.findall(text)) for rx in regexes)
        if n:
            counts.append((-n, len(counts), name))
    return [name for _, _, name in sorted(counts)]


def covers(pattern: str, part: str) -> bool:
    """Покрывает ли шаблон семейства партномер: «STM32F103x8» → STM32F103C8T6 (x — любой знак, суффикс
    корпуса допустим), «AT24C01A/02/04» → AT24C02N (любой номер из перечисления). Не шаблон — False."""
    p = re.sub(r"[^A-Z0-9]", "", (part or "").upper())
    pattern = re.sub(r"[\s-]+", "", pattern or "")
    if not p or not pattern:
        return False
    m = _ENUM_SPLIT_RE.match(pattern)
    if m:
        sizes = [m.group(2)] + re.findall(r"\d{2,4}", m.group(3))
        return re.match(r"[A-Z]{0,3}?%s(?:%s)(?!\d)" % (re.escape(m.group(1)), "|".join(sizes)), p) is not None
    body = re.sub(r"x+|XX+", lambda x: "[A-Z0-9]{%d}" % len(x.group(0)), pattern)
    if body == pattern or not pattern.isalnum():
        return False
    return re.match(body.upper(), p) is not None
