# -*- coding: utf-8 -*-
"""Факты документа (шаг 4.1). PDF — синтетические, из tests/fixtures/make_pdfs.py."""
import os

import pytest

from digger.acquire.extract import DocText, covers, extract_facts, facts_from_text, read_document
from digger.acquire.models import DocFacts
from tests.fixtures import make_pdfs


@pytest.fixture()
def pdf(tmp_path):
    folder = tmp_path / "папка с пробелом"
    folder.mkdir()
    return lambda name: make_pdfs.write(name, folder)


def text_facts(*pages, **kw):
    doc = DocText(pages=len(pages), texts={i + 1: t for i, t in enumerate(pages)},
                  heading=kw.pop("heading", ""), title=kw.pop("title", ""))
    return facts_from_text(doc, **kw)


def test_datasheet(pdf):
    f = extract_facts(pdf("datasheet"))
    assert isinstance(f, DocFacts)
    assert f.pages == 3 and f.has_text and f.language == "en"
    assert f.title == "NE555 Precision Timer datasheet" and f.heading == "NE555 Precision Timer"
    assert f.producer == "Acme PDF Library 9.1"
    assert f.parts_found["NE555"] == [1, 2, 3]
    assert f.parts_found["NE555DR"] == [3] and f.parts_found["NE555P"] == [3]
    assert f.manufacturers == ["Texas Instruments"]
    assert {"PDIP-8", "SOIC-8", "TSSOP-8"} <= set(f.packages)
    assert f.ordering_codes[:2] == ["NE555P", "NE555DR"] and "NE555PWR" in f.ordering_codes
    assert "N555" in f.marking_codes and "NE555" in f.marking_codes
    assert not any(c.startswith(("PDIP", "SOIC", "TSSOP")) for c in f.marking_codes + f.ordering_codes)
    assert f.family_patterns == []
    assert f.doc_type == "" and len(f.text_fingerprint) == 16   # тип — шаг 4.2, отпечаток — 4.4
    assert DocFacts.from_json(f.to_json()) == f


def test_family(pdf):
    f = extract_facts(pdf("family"))
    assert f.family_patterns[:2] == ["STM32F103x8", "STM32F103xB"] and "STM32F103xx" in f.family_patterns
    assert f.heading == "STM32F103x8 STM32F103xB"
    assert f.parts_found["STM32F103C8T6"] == [3]
    assert "STM32F103C8T6" in f.ordering_codes and "STM32F103RBT6" in f.ordering_codes
    assert f.manufacturers == ["STMicroelectronics"]
    assert {"LQFP-48", "LQFP-64", "VFQFPN-36"} <= set(f.packages)
    assert any(covers(p, "STM32F103C8T6") for p in f.family_patterns)


def test_app_note(pdf):
    f = extract_facts(pdf("app_note"))
    assert f.parts_found["NE555"] == [1, 2] and "IRF540N" in f.parts_found
    assert "AN1234" not in f.parts_found                        # номер документа — не партномер
    assert f.heading.startswith("AN1234 Application note")
    assert f.ordering_codes == [] and f.marking_codes == []
    assert "TO-220" in f.packages and f.manufacturers == ["Microchip"]


def test_catalog(pdf):
    f = extract_facts(pdf("catalog"))
    assert f.pages == 5 and len(f.parts_found) == 40
    assert f.parts_found["LM100N"] == [2] and f.parts_found["UA163D"] == [5]
    assert f.title == "Product Selection Guide 2024" and f.manufacturers == ["onsemi"]


def test_foreign(pdf):
    f = extract_facts(pdf("foreign"), hints=["NE555"])
    assert "NE555" not in f.parts_found
    assert f.parts_found["LM358"] == [1, 2] and f.manufacturers == ["onsemi"]
    assert f.title == "LM358 - Dual Operational Amplifier"


def test_scan(pdf):
    f = extract_facts(pdf("scan"))
    assert f.pages == 3 and not f.has_text
    assert f.title == "AT24C02 scan" and f.producer == "ScanSoft 2.0"
    assert f.parts_found == {} and f.language == "" and f.heading == ""


def test_chinese(pdf):
    f = extract_facts(pdf("chinese"))
    assert f.language == "zh" and f.has_text
    assert f.title == "GD25Q64C 数据手册" and f.heading == "GD25Q64C 数据手册"
    assert f.parts_found["GD25Q64C"] == [1, 2]
    assert f.manufacturers == ["GigaDevice"]
    assert {"SOP-8", "WSON-8", "USON-8"} <= set(f.packages)
    assert f.ordering_codes[0] == "GD25Q64CSIG" and "GD25Q64CWIG" in f.ordering_codes
    assert "25Q64CSIG" in f.marking_codes


def test_read_document(pdf):
    doc = read_document(pdf("datasheet"))
    assert doc.pages == 3 and sorted(doc.texts) == [1, 2, 3]
    assert doc.texts[3].splitlines()[1] == "5 Ordering Information"
    assert doc.title == "NE555 Precision Timer datasheet"


def test_long_document_reads_head_and_tail(tmp_path):
    pages = [["Page %d of the XY1234 manual" % (i + 1)] for i in range(12)]
    path = tmp_path / "long.pdf"
    path.write_bytes(make_pdfs.make_pdf(pages))
    doc = read_document(str(path), head_pages=3, tail_pages=2)
    assert doc.pages == 12 and sorted(doc.texts) == [1, 2, 3, 11, 12]
    assert facts_from_text(doc).parts_found["XY1234"] == [1, 2, 3, 11, 12]


def test_broken_and_missing_file(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not a pdf at all")
    for path in (str(bad), os.path.join(str(tmp_path), "нет такого.pdf")):
        f = extract_facts(path)
        assert f.pages == 0 and not f.has_text and f.parts_found == {}


def test_noise_is_not_a_part():
    f = text_facts("Supply 3V3, 100NF capacitor, 16MHZ crystal, RS232 and RS485 lines, ISO9001, IEC61000, JESD22, "
                   "AEC-Q100, SOIC8, TSSOP20, LQFP48, PA10 and PB12 pins, 0X1F00, UL94V, REV12, EN55022, 2014, "
                   "USB20, 000F0000, 3FF0000, see AN2606 and the LM317 regulator")
    assert list(f.parts_found) == ["LM317"]


def test_part_forms():
    f = text_facts("AT24C02N-10SU-2.7 replaces 24C02 and the 74HC595, 1N4148, L7805CV, CH340G, LM35 parts",
                   "PMS150C-U06 and W25Q64JVSIQ; NE555/NE556 dual", hints=["ATmega328P", "w25q64jv"])
    for part in ("AT24C02N-10SU-2.7", "AT24C02N", "24C02", "74HC595", "1N4148", "L7805CV", "CH340G", "LM35",
                 "PMS150C-U06", "PMS150C", "W25Q64JVSIQ", "NE555", "NE556"):
        assert part in f.parts_found, part
    assert "ATMEGA328P" not in f.parts_found and "W25Q64JV" not in f.parts_found   # подсказки нет в тексте


def test_hint_found_in_any_case():
    f = text_facts("The ATmega328P is a low-power CMOS 8-bit microcontroller", "nothing", "ATmega328P-AU",
                   hints=["ATmega328P"])
    assert f.parts_found["ATMEGA328P"] == [1, 3]


def test_slash_enumeration_is_a_family():
    f = text_facts("AT24C01A/02/04/08A/16A two-wire serial EEPROM", "Also 74HC00 / 02 / 04 and LM78XX, 2014/05/12")
    assert f.family_patterns == ["AT24C01A/02/04/08A/16A", "74HC00/02/04", "LM78XX"]


def test_covers():
    assert covers("STM32F103x8", "STM32F103C8T6") and not covers("STM32F103x8", "STM32F103CBT6")
    assert covers("STM32F103xx", "stm32f103cbt6") and not covers("STM32F103xx", "STM32F407VG")
    assert covers("LM78XX", "LM7805") and not covers("LM78XX", "LM317")
    assert covers("AT24C01A/02/04/08A/16A", "AT24C02N-10SU") and covers("AT24C01A/02/04", "AT24C01")
    assert covers("24C01/02/04", "AT24C04") and not covers("24C01/02/04", "AT24C08")
    assert not covers("AT24C01A/02/04", "AT24C021") and not covers("AT24C01A/02/04", "")
    assert not covers("NE555", "NE555")                         # не шаблон


def test_language():
    assert text_facts("Микросхема К155ЛА3 содержит четыре логических элемента 2И-НЕ, корпус DIP-14").language == "ru"
    assert text_facts("The LM358 consists of two independent operational amplifiers").language == "en"
    assert text_facts("12345 67890 --- 000").language == ""


def test_title_cleanup_and_heading_fallback():
    f = text_facts("\n  LM358 Dual Op Amp  \nFeatures", title="Microsoft Word - lm358_rev3.doc")
    assert f.title == "lm358_rev3" and f.heading == "LM358 Dual Op Amp"
    assert text_facts("LM358", title="Untitled").title == ""


def test_marking_inline_and_russian_sections():
    f = text_facts("MMBT3904 NPN transistor. Marking: 1AM\nPackage SOT-23",
                   "Информация для заказа\nК1986ВЕ92QI   LQFP-64\n\nМаркировка\nMDR32F9Q2I")
    assert f.marking_codes[0] == "1AM" and "MDR32F9Q2I" in f.marking_codes
    assert "SOT-23" in f.packages and "LQFP-64" in f.packages
    assert "MMBT3904" not in f.ordering_codes


def test_pin_count_before_package():
    assert {"PDIP-8", "QFN-32"} <= set(text_facts("8-pin PDIP and 32-lead QFN, 3 pins").packages)


def test_words_torn_by_spaces():
    """pypdf рвёт слова пробелами — так выглядит текст настоящих документов."""
    f = text_facts("Packages: SOIC -16 300mil, WSON -8 8x6mm, SOIC 300 -mil, SOIC 30 0-mil, TFBGA -24",
                   "Valid Part Numbers and Top Side Marking\nXY25Q512 JVFIQ 25Q512JVFQ\nXY25Q512JV EIQ 25Q512JVEQ\n"
                   "XY25Q512JVBIQ SOIC-16 TUBE", hints=["XY25Q512JVFIQ"])
    assert f.packages == ["SOIC-16", "WSON-8", "TFBGA-24"]
    assert f.parts_found["XY25Q512JVFIQ"] == [2]
    assert {"XY25Q512JVFIQ", "XY25Q512JVEIQ", "XY25Q512JVBIQ"} <= set(f.ordering_codes)
    assert "25Q512JVFQ" in f.marking_codes and "XY25Q512JVBIQSOIC" not in f.ordering_codes


@pytest.mark.mytest
def test_my_pdfs(my_test_dir):
    """Настоящие datasheet из my_test/: имя файла (партномер) должно находиться в документе."""
    names = [n for n in sorted(os.listdir(my_test_dir)) if n.lower().endswith(".pdf")]
    if not names:
        pytest.skip("в my_test/ нет PDF")
    for name in names:
        part = os.path.splitext(name)[0]
        f = extract_facts(os.path.join(my_test_dir, name), hints=[part])
        assert f.has_text and f.pages > 0 and f.language, name
        assert any(part.upper() in p or p in part.upper() for p in f.parts_found), (name, list(f.parts_found)[:20])


def test_family_written_with_leading_placeholder():
    """Запись семейства в заголовках TI: «xx555 Precision Timers» — это NE555, SA555…; «LMx58» — LM158/258/358."""
    f = facts_from_text(DocText(texts={1: "xx555 Precision Timers\nLMx58, LMx58x Dual Operational Amplifiers"}), [])
    assert f.family_patterns == ["xx555", "LMx58", "LMx58x"]
    assert covers("xx555", "NE555") and covers("xx555", "SA555P") and not covers("xx555", "NE5551")
    assert covers("LMx58", "LM358") and covers("LMx58", "LM258A") and not covers("LMx58", "LM3580")
    assert not covers("xx555", "NE556") and not covers("LMx58", "LM324")
    assert facts_from_text(DocText(texts={1: "max 5x gain, xx55 and axxx5551 are not families"}), []).family_patterns == []
