# -*- coding: utf-8 -*-
"""Проверка PDF из карантина (шаг 3.2). Все PDF генерируются pypdf — настоящих datasheet в тестах нет."""
import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject,
                           TextStringObject)

from digger.acquire.events import EventBus
from digger.acquire.models import Lead, ValidationResult
from digger.acquire.validate import validate_pdf

TEXT = "NE555 Precision Timer datasheet, 8-pin DIP and SOIC packages"


def N(name):
    return NameObject(name)


def writer(pages=2, text=TEXT):
    w = PdfWriter()
    for _ in range(pages):
        page = w.add_blank_page(width=595, height=842)
        if text:
            font = DictionaryObject({N("/Type"): N("/Font"), N("/Subtype"): N("/Type1"),
                                     N("/BaseFont"): N("/Helvetica")})
            page[N("/Resources")] = DictionaryObject({N("/Font"): DictionaryObject({N("/F1"): w._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(("BT /F1 12 Tf 50 780 Td (%s) Tj ET" % text).encode("latin-1"))
            page[N("/Contents")] = w._add_object(stream)
    return w


def action(kind, **extra):
    d = DictionaryObject({N("/Type"): N("/Action"), N("/S"): N(kind)})
    for k, v in extra.items():
        d[N("/" + k)] = v
    return d


def save(tmp_path, w, name="файл с пробелом.pdf.quarantine", **kw):
    path = tmp_path / name
    with open(str(path), "wb") as f:
        w.write(f, **kw) if kw else w.write(f)
    return str(path)


def check(path, **kw):
    bus = EventBus()
    res = validate_pdf(path, bus=bus, lead=Lead(url="https://www.ti.com/lit/ds/ne555.pdf", language="ru"), **kw)
    events = bus.history()
    assert len(events) == 1 and events[0].lang == "ru"
    return res, events[0]


def test_good_pdf(tmp_path):
    res, ev = check(save(tmp_path, writer(pages=3)))
    assert isinstance(res, ValidationResult)
    assert res.ok and res.reason == "" and res.pages == 3 and res.has_text and res.active == []
    assert ev.key == "validate.ok" and ev.params["pages"] == 3


def test_scan_without_text(tmp_path):
    res, ev = check(save(tmp_path, writer(pages=2, text="")))
    assert res.ok and res.pages == 2 and not res.has_text
    assert ev.key == "validate.scan"


def test_goto_open_action_and_links_are_harmless(tmp_path):
    """Переход на страницу при открытии и ссылки-URI есть в обычных datasheet — это не активное содержимое."""
    w = writer()
    dest = ArrayObject([w.pages[0].indirect_reference, N("/Fit")])
    w._root_object[N("/OpenAction")] = action("/GoTo", D=dest)
    link = DictionaryObject({N("/Type"): N("/Annot"), N("/Subtype"): N("/Link"),
                             N("/Rect"): ArrayObject([NumberObject(0)] * 4),
                             N("/A"): action("/URI", URI=TextStringObject("https://www.ti.com"))})
    w.pages[0][N("/Annots")] = ArrayObject([w._add_object(link)])
    res, ev = check(save(tmp_path, w))
    assert res.ok and res.active == [], res


def test_open_action_destination_array(tmp_path):
    w = writer()
    w._root_object[N("/OpenAction")] = ArrayObject([w.pages[0].indirect_reference, N("/Fit")])
    assert check(save(tmp_path, w))[0].ok


def test_open_action_javascript(tmp_path):
    w = writer()
    w._root_object[N("/OpenAction")] = w._add_object(action("/JavaScript", JS=TextStringObject("app.alert(1)")))
    res, ev = check(save(tmp_path, w))
    assert not res.ok and res.reason == "active_content"
    assert "JavaScript" in res.active and "OpenAction" in res.active
    assert ev.key == "validate.active_content" and "JavaScript" in ev.params["what"]


def test_names_javascript(tmp_path):
    w = writer()
    w.add_js("app.alert('x');")          # /Names → /JavaScript
    res, _ = check(save(tmp_path, w))
    assert res.reason == "active_content" and "JavaScript" in res.active


def js_tree(w, *scripts):
    """/Names → /JavaScript: дерево имён со сценариями (значение `/JS` каждого — как передано)."""
    names = ArrayObject()
    for i, js in enumerate(scripts):
        names.append(TextStringObject("s%d" % i))
        names.append(w._add_object(js if isinstance(js, DictionaryObject) and "/S" in js else action("/JavaScript", JS=js)))
    tree = DictionaryObject({N("/Names"): names})
    w._root_object[N("/Names")] = DictionaryObject({N("/JavaScript"): w._add_object(tree)})
    return tree


def stream(data):
    st = DecodedStreamObject()
    st.set_data(data)
    return st


def test_empty_javascript_is_not_active_content(tmp_path):
    """Шаг 8.3h: datasheet AT24C02D с выезда — в дереве имён один сценарий с пустым текстом. Исполнять нечего."""
    for n, scripts in enumerate(([TextStringObject("")], [TextStringObject(" \n")], [], None)):
        w = writer()
        if scripts is None:
            js_tree(w, w._add_object(stream(b"")))
        else:
            js_tree(w, *scripts)
        res, ev = check(save(tmp_path, w, "empty%d.pdf.quarantine" % n))
        assert res.ok and res.active == [] and res.has_text, (n, res.reason, res.active)


@pytest.mark.parametrize("case", ["text", "mixed", "stream", "no_js", "bare_string", "extra_key", "not_js_action",
                                  "open_action", "deep"])
def test_javascript_that_is_not_provably_empty_is_rejected(tmp_path, case):
    w = writer()
    empty = TextStringObject("")
    if case == "text":
        js_tree(w, TextStringObject("app.alert(1)"))
    elif case == "mixed":
        js_tree(w, empty, TextStringObject("app.alert(1)"))
    elif case == "stream":
        js_tree(w, w._add_object(stream(b"app.alert(1)")))
    elif case == "no_js":                         # действие без текста сценария: непонятно, что это
        js_tree(w, action("/JavaScript"))
    elif case == "bare_string":                   # в дереве не действие, а строка
        js_tree(w, empty)["/Names"][1] = TextStringObject("app.alert(1)")
    elif case == "extra_key":                     # незнакомый ключ в узле дерева
        js_tree(w, empty)[N("/Extra")] = TextStringObject("x")
    elif case == "not_js_action":
        js_tree(w, action("/Launch", F=TextStringObject("calc.exe")))
    elif case == "open_action":                   # автоматическое действие — отказ, даже с пустым сценарием
        w._root_object[N("/OpenAction")] = w._add_object(action("/JavaScript", JS=empty))
    elif case == "deep":                          # слишком глубокое дерево не разбирается
        node = js_tree(w, empty)
        for _ in range(40):
            parent = DictionaryObject({N("/Kids"): ArrayObject([w._add_object(node)])})
            node = parent
        w._root_object[N("/Names")] = DictionaryObject({N("/JavaScript"): w._add_object(node)})
    res, _ = check(save(tmp_path, w))
    assert res.reason == "active_content" and res.active, case


def test_javascript_inside_object_stream(tmp_path):
    """Действие лежит в сжатом потоке объектов: в байтах файла слова JavaScript нет."""
    w = writer()
    w._root_object[N("/OpenAction")] = w._add_object(action("/JavaScript", JS=TextStringObject("app.alert(1)")))
    buf = io.BytesIO()
    w.write(buf)
    packed = _pack_into_object_stream(buf.getvalue())
    assert b"JavaScript" not in packed
    path = tmp_path / "packed.pdf.quarantine"
    path.write_bytes(packed)
    res, _ = check(str(path))
    assert res.reason == "active_content" and "JavaScript" in res.active


def test_hex_escaped_name(tmp_path):
    """/J#61vaScript — то же имя, записанное с кодом символа."""
    w = writer()
    w._root_object[N("/OpenAction")] = w._add_object(action("/JavaScript", JS=TextStringObject("app.alert(1)")))
    buf = io.BytesIO()
    w.write(buf)
    data = buf.getvalue()
    assert data.count(b"/JavaScript") == 1
    data = data.replace(b"/JavaScript", b"/J#61vaScrip#74")       # длина та же — смещения xref не сдвигаются
    path = tmp_path / "hex.pdf.quarantine"
    path.write_bytes(data)
    res, _ = check(str(path))
    assert res.reason == "active_content" and "JavaScript" in res.active


def test_launch_in_annotation(tmp_path):
    w = writer()
    annot = DictionaryObject({N("/Type"): N("/Annot"), N("/Subtype"): N("/Link"),
                              N("/Rect"): ArrayObject([NumberObject(0)] * 4),
                              N("/A"): action("/Launch", F=TextStringObject("cmd.exe"))})
    w.pages[1][N("/Annots")] = ArrayObject([w._add_object(annot)])
    res, ev = check(save(tmp_path, w))
    assert res.reason == "active_content" and res.active == ["Launch"]
    assert ev.params["what"] == "Launch"


def test_unreachable_object_is_checked(tmp_path):
    """Объект, на который никто не ссылается, всё равно просматривается."""
    w = writer()
    w._add_object(action("/Launch", F=TextStringObject("cmd.exe")))
    assert check(save(tmp_path, w))[0].active == ["Launch"]


def test_embedded_file(tmp_path):
    w = writer()
    w.add_attachment("payload.exe", b"MZ\x00\x00")
    res, _ = check(save(tmp_path, w))
    assert res.reason == "active_content" and "EmbeddedFiles" in res.active


def test_additional_actions_and_xfa(tmp_path):
    w = writer()
    w.pages[0][N("/AA")] = DictionaryObject({N("/O"): action("/SubmitForm", F=TextStringObject("https://x.example"))})
    w._root_object[N("/AcroForm")] = DictionaryObject({N("/XFA"): TextStringObject("<xdp/>")})
    res, _ = check(save(tmp_path, w))
    assert res.reason == "active_content"
    assert {"AA", "SubmitForm", "XFA"} <= set(res.active)


def test_encrypted_with_password(tmp_path):
    w = writer()
    w.encrypt("secret", algorithm="RC4-128")
    res, ev = check(save(tmp_path, w))
    assert not res.ok and res.reason == "encrypted"
    assert ev.key == "validate.encrypted"


def test_encrypted_with_empty_user_password_is_read(tmp_path):
    """Запрет печати/копирования (пустой пароль пользователя) — обычное дело для datasheet."""
    w = writer(pages=2)
    w.encrypt("", owner_password="owner", algorithm="RC4-128")
    res, ev = check(save(tmp_path, w))
    assert res.ok and res.pages == 2 and res.has_text, res
    assert ev.key == "validate.ok"


def test_encrypted_does_not_hide_active_content(tmp_path):
    w = writer()
    w.add_js("app.alert(1)")
    w.encrypt("", owner_password="owner", algorithm="RC4-128")
    assert check(save(tmp_path, w))[0].reason == "active_content"


def test_no_pages(tmp_path):
    res, ev = check(save(tmp_path, PdfWriter()))
    assert not res.ok and res.reason == "damaged" and res.pages == 0
    assert ev.key == "validate.damaged"


@pytest.mark.parametrize("data", [b"", b"<html><body>404</body></html>", b"PK\x03\x04" + b"\0" * 2000])
def test_not_pdf(tmp_path, data):
    path = tmp_path / "x.pdf.quarantine"
    path.write_bytes(data)
    res, ev = check(str(path))
    assert not res.ok and res.reason == "not_pdf" and ev.key == "validate.not_pdf"


def test_missing_file(tmp_path):
    res, ev = check(str(tmp_path / "нет такого.pdf"))
    assert not res.ok and res.reason == "damaged" and ev.key == "validate.damaged"


@pytest.mark.parametrize("cut", [0.15, 0.5, 0.9])
def test_truncated(tmp_path, cut):
    buf = io.BytesIO()
    writer(pages=4).write(buf)
    data = buf.getvalue()
    path = tmp_path / "cut.pdf.quarantine"
    path.write_bytes(data[:int(len(data) * cut)])
    res, ev = check(str(path))
    assert not res.ok and res.reason == "damaged" and ev.key == "validate.damaged"


def test_garbage_after_header(tmp_path):
    path = tmp_path / "bad.pdf.quarantine"
    path.write_bytes(b"%PDF-1.4\n" + bytes(range(256)) * 40)
    res, _ = check(str(path))
    assert not res.ok and res.reason == "damaged" and res.detail


def test_too_big(tmp_path):
    path = save(tmp_path, writer())
    res, ev = check(path, max_mb=0.0001)
    assert not res.ok and res.reason == "too_big" and ev.key == "validate.too_big"
    assert ev.params["size"] == res.size > 0


def test_without_bus_and_roundtrip(tmp_path):
    res = validate_pdf(save(tmp_path, writer()))
    assert res.ok
    assert ValidationResult.from_dict(res.to_dict()) == res


def test_event_texts_in_all_languages():
    from digger.acquire.events import KEYS, load_catalog
    assert KEYS["validate.damaged"] == "fail"
    for lang in ("en", "zh", "ru"):
        assert "validate.damaged" in load_catalog(lang)


def _pack_into_object_stream(data):
    """Переписывает PDF так, что все объекты-словари лежат в одном сжатом /ObjStm, а таблица — в /XRef-потоке."""
    import struct
    import zlib

    from pypdf import PdfReader
    from pypdf.generic import StreamObject

    r = PdfReader(io.BytesIO(data))
    ids = sorted(r.xref[0])
    plain, packed = [], []
    for i in ids:
        (plain if isinstance(r.get_object(i), StreamObject) else packed).append(i)
    bodies = []
    for i in packed:
        b = io.BytesIO()
        r.get_object(i).write_to_stream(b)
        bodies.append(b.getvalue())
    head, pos = [], 0
    for i, body in zip(packed, bodies):
        head.append("%d %d" % (i, pos))
        pos += len(body) + 1
    head_bytes = (" ".join(head) + "\n").encode()
    stm_id, xref_id = max(ids) + 1, max(ids) + 2
    payload = zlib.compress(head_bytes + b"\n".join(bodies) + b"\n")
    out = io.BytesIO()
    out.write(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for i in plain:
        offsets[i] = out.tell()
        out.write(b"%d 0 obj\n" % i)
        r.get_object(i).write_to_stream(out)
        out.write(b"\nendobj\n")
    offsets[stm_id] = out.tell()
    out.write(b"%d 0 obj\n<< /Type /ObjStm /N %d /First %d /Length %d /Filter /FlateDecode >>\nstream\n"
              % (stm_id, len(packed), len(head_bytes), len(payload)))
    out.write(payload + b"\nendstream\nendobj\n")
    xref_pos = out.tell()
    rows = [struct.pack(">BIH", 0, 0, 65535)]
    for i in range(1, xref_id + 1):
        if i in packed:
            rows.append(struct.pack(">BIH", 2, stm_id, packed.index(i)))
        elif i == xref_id:
            rows.append(struct.pack(">BIH", 1, xref_pos, 0))
        elif i in offsets:
            rows.append(struct.pack(">BIH", 1, offsets[i], 0))
        else:
            rows.append(struct.pack(">BIH", 0, 0, 0))
    table = zlib.compress(b"".join(rows))
    root = r.trailer.raw_get("/Root")
    out.write(b"%d 0 obj\n<< /Type /XRef /Size %d /W [1 4 2] /Root %d 0 R /Length %d /Filter /FlateDecode >>\nstream\n"
              % (xref_id, xref_id + 1, root.idnum, len(table)))
    out.write(table + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % xref_pos)
    return out.getvalue()
