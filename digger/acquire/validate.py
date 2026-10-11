# -*- coding: utf-8 -*-
"""Проверка PDF из карантина (ARCHITECTURE §4.2, §8; шаг 3.2).

Файл читается только pypdf — ничего из него не исполняется. Порядок: размер → сигнатура %PDF- → разбор →
шифрование (пустой пароль допускается) → страницы → активное содержимое → есть ли текстовый слой.
Любой отказ — жёсткий (§4.4): файл остаётся в карантине.

Активное содержимое ищется обходом объектов, а не поиском строк в байтах файла: просматриваются все объекты
из таблицы xref (в том числе лежащие в сжатых потоках /ObjStm и те, на которые никто не ссылается) и всё,
до чего можно дойти от trailer. Имена pypdf раскодирует сам, поэтому `/J#61vaScript` тоже находится.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any, List, Optional, Set, Tuple

from .events import EventBus
from .models import Lead, ValidationResult

log = logging.getLogger("digger.acquire.validate")
logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_MB = 40.0
HEAD_BYTES = 1024             # сигнатура должна быть в начале файла (как в netsafe)
TEXT_PAGES = 5                # сколько первых страниц смотреть в поисках текстового слоя
TEXT_MIN_CHARS = 20
MAX_OBJECTS = 2000000         # защита от файла-«бомбы»: больше объектов не бывает и у справочников на 5000 страниц

# Ключ словаря → название находки. Само наличие ключа — уже активное содержимое.
# Исключение одно — JavaScript с пустым текстом (`_empty_script`, `_empty_js_tree`): исполнять нечего.
JS_TREE_DEPTH, JS_TREE_NODES = 8, 1000     # дерево имён /JavaScript крупнее не разбирается — отказ
ACTIVE_KEYS = {
    "/JS": "JavaScript", "/JavaScript": "JavaScript",
    "/EmbeddedFiles": "EmbeddedFiles", "/EF": "EmbeddedFiles",
    "/XFA": "XFA", "/RichMedia": "RichMedia", "/RichMediaContent": "RichMedia",
    "/Launch": "Launch",
}
# Тип действия (/S) → название находки.
ACTIVE_ACTIONS = {
    "/JavaScript": "JavaScript", "/Launch": "Launch", "/SubmitForm": "SubmitForm", "/ImportData": "ImportData",
    "/RichMediaExecute": "RichMedia", "/Rendition": "Rendition", "/GoToE": "EmbeddedFiles",
}
# Значения /Type и /Subtype, которые сами по себе — активное содержимое.
ACTIVE_TYPES = {"/EmbeddedFile": "EmbeddedFiles", "/RichMedia": "RichMedia", "/FileAttachment": "EmbeddedFiles"}
# Действия, безвредные при открытии документа или страницы: переход внутри документа и команды просмотра.
# Обычная цель перехода (массив) тоже безвредна. Всё остальное в /OpenAction и /AA — отказ.
SAFE_AUTO_ACTIONS = ("/GoTo", "/Named")
AUTO_KEYS = {"/OpenAction": "OpenAction", "/AA": "AA"}


def validate_pdf(path: str, bus: Optional[EventBus] = None, lead: Optional[Lead] = None,
                 max_mb: float = MAX_MB) -> ValidationResult:
    """Проверяет файл из карантина. Не бросает исключений: итог в `ValidationResult`, ход — одним событием."""
    res = _validate(path, max_mb)
    if res.reason:
        log.info("проверка %s: %s %s %s", path, res.reason, ", ".join(res.active), res.detail)
    if bus is not None:
        kw = {}
        if lead is not None:
            kw = dict(lang=lead.language or "en", level=lead.level, source=lead.source_id)
        if res.ok:
            bus.emit("validate.ok" if res.has_text else "validate.scan", pages=res.pages, **kw)
        elif res.reason == "active_content":
            bus.emit("validate.active_content", what=", ".join(res.active), **kw)
        elif res.reason == "too_big":
            bus.emit("validate.too_big", size=res.size, **kw)
        else:
            bus.emit("validate." + res.reason, **kw)
    return res


def _validate(path: str, max_mb: float) -> ValidationResult:
    try:
        size = os.path.getsize(path)
        if size > max_mb * 1024 * 1024:
            return ValidationResult(reason="too_big", size=size)
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return ValidationResult(reason="damaged", detail=_short(e))
    res = ValidationResult(size=len(data))
    if b"%PDF-" not in data[:HEAD_BYTES]:
        res.reason = "not_pdf"
        return res
    try:
        _inspect(data, res)
    except _Encrypted as e:
        res.reason, res.detail = "encrypted", str(e)
    except (Exception, RecursionError) as e:      # noqa: BLE001 — pypdf на битом файле бросает что угодно
        res.reason, res.detail = "damaged", _short(e)
    res.ok = not res.reason
    return res


class _Encrypted(Exception):
    pass


def _short(e: BaseException) -> str:
    return ("%s: %s" % (type(e).__name__, e))[:200]


def _inspect(data: bytes, res: ValidationResult) -> None:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")
        except Exception as e:                    # noqa: BLE001 — нет библиотеки для AES или неизвестный алгоритм
            raise _Encrypted(_short(e))
        if not opened:
            raise _Encrypted("нужен пароль")
    res.pages = len(reader.pages)
    if res.pages == 0:
        res.reason, res.detail = "damaged", "нет страниц"
        return
    reader.pages[0].get_contents()                # первая страница должна читаться
    res.active = find_active_content(reader)
    if res.active:
        res.reason = "active_content"
        return
    res.has_text = _has_text(reader)


def _has_text(reader: Any) -> bool:
    for i in range(min(TEXT_PAGES, len(reader.pages))):
        try:
            text = reader.pages[i].extract_text() or ""
        except (Exception, RecursionError):       # noqa: BLE001 — страница без читаемого текста
            continue
        if len("".join(text.split())) >= TEXT_MIN_CHARS:
            return True
    return False


def _object_ids(reader: Any) -> List[Tuple[int, int]]:
    """Все объекты из таблиц xref: обычные (по поколениям) и лежащие в сжатых потоках объектов."""
    ids = {(num, gen) for gen, table in reader.xref.items() for num in table}
    ids.update((num, 0) for num in reader.xref_objStm)
    return sorted(ids)


def find_active_content(reader: Any) -> List[str]:
    """Названия найденного активного содержимого (пустой список — чисто)."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    found: Set[str] = set()
    seen: Set[Tuple[int, int]] = set()
    ids = _object_ids(reader)
    if len(ids) > MAX_OBJECTS:
        raise ValueError("слишком много объектов: %d" % len(ids))
    stack: List[Any] = [reader.trailer]
    stack.extend(IndirectObject(num, gen, reader) for num, gen in ids)

    def auto_action(value: Any, label: str) -> None:
        """/OpenAction и /AA: безвреден только переход внутри документа."""
        value = _resolve(value)
        if isinstance(value, DictionaryObject):
            if "/S" in value:
                if str(_resolve(value.get("/S"))) not in SAFE_AUTO_ACTIONS:
                    found.add(label)
            else:                                 # словарь /AA: событие → действие
                for k in value:
                    auto_action(value.raw_get(k), label)
        elif value is not None and not isinstance(value, ArrayObject):
            found.add(label)

    while stack:
        obj = stack.pop()
        if isinstance(obj, IndirectObject):
            key = (obj.idnum, obj.generation)
            if key in seen:
                continue
            seen.add(key)
            obj = _resolve(obj)
        if isinstance(obj, DictionaryObject):     # потоки — тоже словари; их данные не раскодируются
            for k in obj:
                name = str(k)
                value = obj.raw_get(k)
                if name == "/JS" and _empty_script(obj) or name == "/JavaScript" and _empty_js_tree(value):
                    pass                          # сценарий без текста (datasheet AT24C02D, шаг 8.3h)
                elif name in ACTIVE_KEYS:
                    found.add(ACTIVE_KEYS[name])
                elif name in AUTO_KEYS:
                    auto_action(value, AUTO_KEYS[name])
                elif name == "/S":
                    kind = ACTIVE_ACTIONS.get(str(_resolve(value)))
                    if kind and not (kind == "JavaScript" and _empty_script(obj)):
                        found.add(kind)
                elif name in ("/Type", "/Subtype"):
                    kind = ACTIVE_TYPES.get(str(_resolve(value)))
                    if kind:
                        found.add(kind)
                if isinstance(value, (IndirectObject, DictionaryObject, ArrayObject)):
                    stack.append(value)
        elif isinstance(obj, ArrayObject):
            for value in list.__iter__(obj):      # без разыменования ссылок
                if isinstance(value, (IndirectObject, DictionaryObject, ArrayObject)):
                    stack.append(value)
    return sorted(found)


def _empty_script(action: Any) -> bool:
    """Действие с ключом `/JS`, текст которого пуст: строка из одних пробелов или поток без байтов (сырые данные,
    без раскодирования). Всё, чего не удалось прочитать или понять, пустым не считается."""
    from pypdf.generic import ByteStringObject, DictionaryObject, StreamObject, TextStringObject

    action = _resolve(action)
    if not isinstance(action, DictionaryObject) or "/JS" not in action:
        return False
    js = _resolve(action.raw_get("/JS"))
    if isinstance(js, StreamObject):
        data = getattr(js, "_data", None)
        return isinstance(data, bytes) and not data.strip()
    if isinstance(js, (TextStringObject, ByteStringObject)):
        return not js.strip()
    return False


def _empty_js_tree(root: Any) -> bool:
    """Дерево имён `/Names → /JavaScript`, в котором нет ни одного сценария с текстом. Узел — только из ключей
    `/Names`, `/Kids`, `/Limits`; значение имени — только действие JavaScript с пустым `/JS`. Иначе — False."""
    from pypdf.generic import ArrayObject, DictionaryObject

    todo, nodes = [(root, 0)], 0
    while todo:
        node, depth = todo.pop()
        node = _resolve(node)
        nodes += 1
        if not isinstance(node, DictionaryObject) or depth > JS_TREE_DEPTH or nodes > JS_TREE_NODES:
            return False
        if any(str(k) not in ("/Names", "/Kids", "/Limits") for k in node):
            return False
        names, kids = _resolve(node.raw_get("/Names")) if "/Names" in node else ArrayObject(), \
            _resolve(node.raw_get("/Kids")) if "/Kids" in node else ArrayObject()
        if not isinstance(names, ArrayObject) or not isinstance(kids, ArrayObject) or len(names) % 2:
            return False
        for value in list(list.__iter__(names))[1::2]:
            action = _resolve(value)
            if not (isinstance(action, DictionaryObject) and str(_resolve(action.get("/S"))) == "/JavaScript"
                    and _empty_script(action)):
                return False
        todo.extend((kid, depth + 1) for kid in list.__iter__(kids))
    return True


def _resolve(obj: Any) -> Any:
    """Объект по ссылке; нечитаемый или отсутствующий объект — None (как его видит и программа просмотра)."""
    from pypdf.generic import IndirectObject

    for _ in range(20):                           # цепочка ссылок конечной длины
        if not isinstance(obj, IndirectObject):
            return obj
        try:
            obj = obj.pdf.get_object(obj)
        except (Exception, RecursionError):       # noqa: BLE001
            return None
    return None
