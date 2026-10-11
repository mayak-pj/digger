# -*- coding: utf-8 -*-
"""Текст сканированного PDF (шаг 9.2): страница без текстового слоя — это картинка, её читает Tesseract.

Страница не рисуется: скан хранит её готовым изображением, и оно достаётся из PDF самим pypdf (§8: PDF читает
только pypdf, из файла ничего не исполняется). Изображение раскодирует Pillow; размер проверяется по словарю
объекта до раскодирования. Страницы из штрихов и шрифтов без текста (кривые) так не читаются — их нет в виде картинки.

`ocr_pages` исключений не бросает: нет Tesseract, незнакомое сжатие (JBIG2), истёк срок — страница остаётся
без текста, и документ, как раньше, уходит пользователю (улика E12). Настройки — `config.json → acquire.scan_ocr`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("digger.acquire.scanocr")

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "lang": "eng",            # языки Tesseract: «eng+rus», если в tessdata положен rus.traineddata
    "head_pages": 4,          # сколько первых страниц без текста читать…
    "tail_pages": 2,          # …и сколько последних (таблицы заказных кодов и корпусов — в конце)
    "page_seconds": 30,       # срок на одну страницу
    "total_seconds": 60,      # после этого срока новые страницы не начинаются
    "max_side": 3000,         # изображение крупнее уменьшается: Tesseract медленнее, а читает не лучше
}
PAGE_MIN_CHARS = 40           # меньше знаков на странице — текста на ней нет (бывает одна строка-штамп сайта)
MIN_PIXELS = 150 * 1000       # мельче — не страница, а значок или логотип
MAX_PIXELS = 60 * 1000 * 1000  # крупнее не раскодируется (A4 при 600 dpi — 35 млн)
MAX_IMAGES = 4                # страница, нарезанная полосами: не больше стольких изображений
CACHE_SIZE = 8

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_state: Dict[str, Any] = {"settings": dict(DEFAULTS), "app_dir": _APP_DIR, "cmd": "", "ready": None, "error": ""}
_cache: "OrderedDict[Tuple[str, Tuple[int, ...]], Dict[int, str]]" = OrderedDict()
_lock = threading.Lock()


def configure(config: Optional[Mapping[str, Any]] = None, app_dir: str = "") -> None:
    """Настройки программы: `acquire.scan_ocr` и путь к tesseract.exe из окна «Настройки»."""
    config = config or {}
    settings = dict(DEFAULTS)
    settings.update((config.get("acquire") or {}).get("scan_ocr") or {})
    new = dict(settings=settings, app_dir=app_dir or _APP_DIR,
               cmd=((config.get("module_settings") or {}).get("ocr") or {}).get("tesseract_cmd", ""))
    with _lock:
        if any(_state[k] != v for k, v in new.items()):       # те же настройки — прочитанное не выбрасывается
            _state.update(new, ready=None, error="")
            _cache.clear()


def available() -> Tuple[bool, str]:
    """Можно ли читать сканы; причина, если нельзя. Tesseract ищется один раз."""
    if not _state["settings"].get("enabled", True):
        return False, "выключено в настройках"
    if _state["ready"] is None:
        _state["ready"], _state["error"] = _find()
    return _state["ready"], _state["error"]


def _find() -> Tuple[bool, str]:
    try:
        import pytesseract
        from ..recognition.providers.tesseract import find_tesseract
        found = find_tesseract(_state["app_dir"], _state["cmd"])
        if found:
            pytesseract.pytesseract.tesseract_cmd = found
        pytesseract.get_tesseract_version()
        return True, ""
    except Exception as e:      # noqa: BLE001 — нет пакета, нет программы, не запускается
        return False, "Tesseract не найден (%s)" % e


def is_textless(text: str) -> bool:
    return len("".join((text or "").split())) < PAGE_MIN_CHARS


def scan_pages(texts: Mapping[int, str]) -> List[int]:
    """Страницы без текста, если документ — скан: таких страниц не меньше половины прочитанных.
    Одиночная страница-картинка в обычном документе (обложка, чертёж) не читается."""
    blank = sorted(n for n, text in texts.items() if is_textless(text))
    return blank if blank and len(blank) * 2 >= len(texts) else []


def choose(numbers: Sequence[int]) -> List[int]:
    """Какие из страниц без текста читать: первые и последние."""
    s = _state["settings"]
    head, tail = max(0, int(s["head_pages"])), max(0, int(s["tail_pages"]))
    numbers = sorted(set(numbers))
    return numbers[:head] + (numbers[head:][-tail:] if tail else [])


def ocr_pages(path: str, numbers: Sequence[int], progress: Optional[Callable[[int], None]] = None) -> Dict[int, str]:
    """Текст страниц-картинок: номер страницы (с 1) → текст. `numbers` — страницы без текстового слоя.
    `progress(n)` вызывается один раз перед чтением: сколько страниц будет прочитано."""
    chosen = choose(numbers)
    if not chosen or not available()[0]:
        return {}
    try:
        key = (_sha256(path), tuple(chosen))
    except OSError as e:
        log.info("скан %s не читается: %s", path, e)
        return {}
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return dict(_cache[key])
    try:
        out = _read(path, chosen, progress)
    except (Exception, RecursionError) as e:      # noqa: BLE001 — pypdf на битом файле бросает что угодно
        log.info("скан %s не читается: %s: %s", path, type(e).__name__, e)
        out = {}
    with _lock:
        _cache[key] = dict(out)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return out


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _read(path: str, chosen: Sequence[int], progress: Optional[Callable[[int], None]]) -> Dict[int, str]:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        reader.decrypt("")
    pages = [(n, reader.pages[n - 1]) for n in chosen if 1 <= n <= len(reader.pages)]
    pages = [(n, page, names) for n, page in pages for names in [image_names(page)] if names]
    if not pages:
        return {}
    if progress is not None:
        progress(len(pages))
    s = _state["settings"]
    deadline = time.monotonic() + float(s["total_seconds"])
    out: Dict[int, str] = {}
    for n, page, names in pages:
        if time.monotonic() >= deadline:
            log.info("скан %s: срок вышел, страница %d и дальше не прочитаны", path, n)
            break
        parts = []
        for name in names:
            try:
                parts.append(_tesseract(_image(page, name, int(s["max_side"])), s["lang"], float(s["page_seconds"])))
            except (Exception, RecursionError) as e:   # noqa: BLE001 — незнакомое сжатие, срок, сбой Tesseract
                log.info("скан %s, страница %d: %s: %s", path, n, type(e).__name__, e)
        text = "\n".join(p for p in parts if p and p.strip())
        if text:
            out[n] = text
    return out


def image_names(page: Any) -> List[str]:
    """Имена изображений страницы, годных для чтения, в порядке записи в PDF (полосы идут сверху вниз)."""
    try:
        xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        names = []
        for name in xobjects:
            obj = xobjects[name].get_object()
            if obj.get("/Subtype") != "/Image":
                continue
            w, h = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
            if min(w, h) >= 100 and MIN_PIXELS <= w * h <= MAX_PIXELS:
                names.append(str(name))
        return names[:MAX_IMAGES]
    except (Exception, RecursionError):            # noqa: BLE001
        return []


def _image(page: Any, name: str, max_side: int) -> Any:
    image = page.images[name].image
    if image is None:
        raise ValueError("изображение не раскодировано")
    image = image.convert("L")
    if max(image.size) > max_side > 0:
        image.thumbnail((max_side, max_side))
    turn = int(page.get("/Rotate", 0) or 0) % 360
    if turn:
        image = image.rotate(-turn, expand=True)   # /Rotate — по часовой стрелке, Pillow — против
    return image


def _tesseract(image: Any, lang: str, seconds: float) -> str:
    import pytesseract
    config = "--psm 3"
    tessdata = os.path.join(_state["app_dir"], "tessdata")
    if os.path.isdir(tessdata):
        config += ' --tessdata-dir "%s"' % tessdata
    return pytesseract.image_to_string(image, lang=lang, config=config, timeout=seconds)
