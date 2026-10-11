# -*- coding: utf-8 -*-
"""Текст сканированных datasheet (шаг 9.2): страницы-картинки читает Tesseract, документ проверяется по уликам."""
import logging

import pytest

from digger.acquire import manual as M
from digger.acquire import scanocr
from digger.acquire.decide import decide
from digger.acquire.events import EventBus, render
from digger.acquire.extract import extract_facts, read_document
from digger.acquire.models import DocFacts, PhotoContext
from digger.acquire.store import AcquireStore
from digger.acquire.verify import SourceTrust, collect, verify_file
from digger.core.interfaces import Context
from digger.modules.localdb_sqlite import SQLiteLocalDB
from digger.modules.pdftext_pypdf import PyPdfText
from tests.fixtures import make_pdfs

PAGES = {1: "NE555 Precision Timer\nTexas Instruments\nDatasheet SLFS022\nAvailable in 8-pin PDIP, SOIC-8 packages",
         2: "NE555\nOrdering Information\nNE555P PDIP-8\nNE555DR SOIC-8"}
CTX = PhotoContext(part="NE555P", manufacturer="Texas Instruments", package="DIP-8")


@pytest.fixture(autouse=True)
def fresh():
    scanocr.configure({"acquire": {"scan_ocr": {"enabled": True}}}, "нет такой папки")
    scanocr.configure({})
    yield
    scanocr.configure({"acquire": {"scan_ocr": {"enabled": True}}}, "нет такой папки")
    scanocr.configure({})


@pytest.fixture()
def fake(monkeypatch):
    """Tesseract подменён: страницы узнаются по порядку обращения, белый лист текста не даёт."""
    calls = []

    def read(image, lang, seconds):
        calls.append((image.size, image.mode, lang))
        dark = image.getextrema()[0] < 128
        return PAGES.get(len(calls), "") if dark else ""

    monkeypatch.setattr(scanocr, "available", lambda: (True, ""))
    monkeypatch.setattr(scanocr, "_tesseract", read)
    return calls


def pdf(tmp_path, name="scan_image"):
    return make_pdfs.write(name, tmp_path)


def test_scan_pages_are_read(tmp_path, fake):
    told = []
    doc = read_document(pdf(tmp_path), progress=told.append)
    assert doc.pages == 3 and doc.ocr_pages == [1, 2] and told == [3]
    assert "NE555 Precision Timer" in doc.texts[1] and "NE555DR" in doc.texts[2] and doc.texts[3] == ""
    assert [c[1:] for c in fake] == [("L", "eng")] * 3 and fake[0][0] == (1654, 2339)


def test_scan_gets_facts_and_evidence(tmp_path, fake):
    facts, evidence = verify_file(pdf(tmp_path), CTX, url="https://www.ti.com/lit/ne555.pdf",
                                  trust=SourceTrust(makers=("ti.com",)))
    codes = {e.code: e.points for e in evidence}
    assert facts.has_text and facts.ocr_pages == [1, 2] and facts.heading == "NE555 Precision Timer"
    assert "NE555P" in facts.parts_found and "Texas Instruments" in facts.manufacturers
    assert codes["E1"] == 30 and codes["E6"] == 10 and "E12" not in codes
    verdict = decide(evidence, ocr=bool(facts.ocr_pages))
    assert verdict.status == "confirmed" and verdict.reasons == ["ocr_text"]


def test_no_tesseract_keeps_old_rule(tmp_path, monkeypatch):
    """Читать нечем — скан, как раньше, уходит пользователю (E12)."""
    monkeypatch.setattr(scanocr, "available", lambda: (False, "нет"))
    facts, evidence = verify_file(pdf(tmp_path), CTX)
    assert not facts.has_text and facts.ocr_pages == []
    verdict = decide(evidence, ocr=bool(facts.ocr_pages))
    assert verdict.status == "needs_user" and verdict.reasons == ["no_text_layer"]


def test_unreadable_scan_keeps_old_rule(tmp_path, monkeypatch):
    def broken(image, lang, seconds):
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(scanocr, "available", lambda: (True, ""))
    monkeypatch.setattr(scanocr, "_tesseract", broken)
    facts = extract_facts(pdf(tmp_path))
    assert facts.pages == 3 and not facts.has_text and "E12" in [e.code for e in collect(facts, CTX)]


def test_disabled_in_config(tmp_path, monkeypatch):
    monkeypatch.setattr(scanocr, "_find", lambda: (True, ""))
    monkeypatch.setattr(scanocr, "_tesseract", lambda *a: PAGES[1])
    scanocr.configure({"acquire": {"scan_ocr": {"enabled": False}}})
    assert scanocr.available() == (False, "выключено в настройках")
    assert read_document(pdf(tmp_path)).ocr_pages == []


def test_text_documents_are_not_touched(tmp_path, fake):
    for name in ("datasheet", "chinese", "scan"):          # «scan» — пустые страницы без картинок
        assert read_document(pdf(tmp_path, name)).ocr_pages == []
    assert fake == []


def test_single_image_page_is_not_a_scan(tmp_path, fake):
    """Обычный документ с одной страницей-картинкой (обложка, чертёж) сканом не считается."""
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for name in ("datasheet", "scan_image"):
        w.append(PdfReader(pdf(tmp_path, name)), pages=(0, 3 if name == "datasheet" else 1))
    path = tmp_path / "mixed.pdf"
    w.write(str(path))
    doc = read_document(str(path))
    assert doc.pages == 4 and doc.ocr_pages == [] and fake == []
    assert scanocr.scan_pages({1: "x" * 50, 2: "", 3: ""}) == [2, 3] and scanocr.scan_pages({}) == []


def test_same_file_is_read_once(tmp_path, fake):
    path = pdf(tmp_path)
    first = read_document(path)
    copy = tmp_path / "копия в библиотеке.pdf"
    copy.write_bytes(open(path, "rb").read())
    again = read_document(str(copy))
    assert len(fake) == 3 and again.texts == first.texts and again.ocr_pages == [1, 2]
    scanocr.configure({})                                    # те же настройки — прочитанное остаётся
    read_document(path)
    assert len(fake) == 3


def test_only_first_and_last_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(scanocr, "available", lambda: (True, ""))
    monkeypatch.setattr(scanocr, "_tesseract", lambda image, lang, seconds: "page text " * 5)
    path = tmp_path / "long.pdf"
    path.write_bytes(make_pdfs.scan_pdf([["A%d" % i] for i in range(9)], size=(600, 800)))
    assert scanocr.choose(range(1, 10)) == [1, 2, 3, 4, 8, 9]
    assert read_document(str(path)).ocr_pages == [1, 2, 3, 4, 8, 9]
    scanocr.configure({"acquire": {"scan_ocr": {"head_pages": 1, "tail_pages": 0}}})
    assert read_document(str(path)).ocr_pages == [1]


def test_time_limit_stops_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(scanocr, "available", lambda: (True, ""))
    monkeypatch.setattr(scanocr, "_tesseract", lambda image, lang, seconds: "page text " * 5)
    scanocr.configure({"acquire": {"scan_ocr": {"total_seconds": 0}}})
    assert read_document(pdf(tmp_path)).ocr_pages == []


def test_image_filter(tmp_path):
    from pypdf import PdfReader
    from pypdf.generic import NameObject, NumberObject
    page = PdfReader(pdf(tmp_path)).pages[0]
    assert scanocr.image_names(page) == ["/image"]
    image = page["/Resources"]["/XObject"]["/image"]
    image[NameObject("/Width")] = NumberObject(60000)        # заявлен огромный размер — не раскодируется
    assert scanocr.image_names(page) == []
    image[NameObject("/Width")] = NumberObject(64)           # значок
    assert scanocr.image_names(page) == []
    assert scanocr.image_names({}) == []


def test_rotated_page_is_turned_upright(tmp_path, fake):
    path = tmp_path / "rotated.pdf"
    path.write_bytes(make_pdfs.scan_pdf([[("NE555", 70)]], rotate=90))
    read_document(str(path))
    assert fake[0][0] == (1654, 2339)


def test_manual_check_reports_scan(tmp_path, fake):
    cfg = {"paths": {"db": "data/digger.sqlite", "library_dir": "lib"}}
    store = AcquireStore(SQLiteLocalDB({}, Context(cfg, str(tmp_path), logging.getLogger("t"))))
    bus, seen = EventBus(), []
    bus.subscribe(seen.append)
    rec = M.check_manual_pdf(pdf(tmp_path), CTX, store, str(tmp_path / "q"), bus=bus, user=True)
    assert rec.facts.ocr_pages == [1, 2] and "ocr_text" in rec.verdict.reasons
    assert rec.verdict.status in ("confirmed", "probable") and rec.stored_path
    event = next(e for e in seen if e.key == "scan.ocr")
    assert event.params["pages"] == 3 and render(event) == u"скан без текста: распознаю 3 страницы"
    assert DocFacts.from_dict(rec.facts.to_dict()).ocr_pages == [1, 2]


def test_pdf_text_module_reads_scan(tmp_path, fake):
    cfg = {"paths": {"db": "data/digger.sqlite", "library_dir": "lib"}}
    module = PyPdfText({}, Context(cfg, str(tmp_path), logging.getLogger("t")))
    text = module.extract(pdf(tmp_path))
    assert "NE555 Precision Timer" in text and "Ordering Information" in text
    assert "Timing from" in module.extract(pdf(tmp_path, "datasheet"))


@pytest.mark.needs_tesseract
def test_real_tesseract_reads_generated_scan(tmp_path):
    assert scanocr.available() == (True, "")
    facts, evidence = verify_file(pdf(tmp_path), CTX)
    assert facts.ocr_pages == [1, 2] and "NE555P" in facts.parts_found and "NE555" in facts.heading
    assert "Texas Instruments" in facts.manufacturers and "E1" in [e.code for e in evidence]
