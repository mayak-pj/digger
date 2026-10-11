# -*- coding: utf-8 -*-
"""Улики и вердикт (шаг 4.3). PDF — синтетические, из tests/fixtures/make_pdfs.py."""
import json
import os

import pytest

from digger.acquire import decide as D
from digger.acquire import verify as V
from digger.acquire.models import DocFacts, Evidence, PhotoContext, ValidationResult
from tests.fixtures import make_pdfs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRUST = V.SourceTrust(makers=("ti.com", "st.com", "onsemi.com"), catalogs=("alldatasheet.com",), bad=("badpdf.example",),
                      maker_sites={"Atmel": "microchip.com", "Microchip": "microchip.com", "Texas Instruments": "ti.com"})


def ctx(part, maker="", package="", marking=""):
    return PhotoContext(part=part, manufacturer=maker, package=package, marking=marking)


def codes(evidence):
    return {e.code: e.points for e in evidence}


# PDF + контекст фото (+ откуда скачан) → статус, баллы, коды улик
TABLE = [
    ("datasheet", ctx("NE555P", "Texas Instruments", "DIP-8"), "https://www.ti.com/lit/ds/ne555.pdf",
     "confirmed", 100, {"E1": 30, "E2": 15, "E4": 10, "E5": 15, "E6": 10, "E7": 10, "E9": 10}),
    ("datasheet", ctx("NE555P"), "https://files.example.org/ne555.pdf",
     "confirmed", 70, {"E1": 30, "E2": 15, "E4": 10, "E5": 15}),
    ("datasheet", ctx("NE555"), "https://www.alldatasheet.com/x.pdf",
     "probable", 60, {"E1": 30, "E4": 10, "E5": 15, "E9": 5}),
    ("datasheet", ctx("NE555", marking="N555"), "",
     "probable", 65, {"E1": 30, "E4": 10, "E5": 15, "E8": 10}),
    ("datasheet", ctx("NE555P", "STMicroelectronics", "SOIC-14"), "https://www.ti.com/ne555.pdf",
     "needs_user", 55, {"E1": 30, "E2": 15, "E4": 10, "E5": 15, "E6": -15, "E7": -10, "E9": 10}),
    ("family", ctx("STM32F103C8T6", "STMicroelectronics", "LQFP-48"), "https://www.st.com/ds.pdf",
     "confirmed", 90, {"E1": 20, "E2": 15, "E4": 10, "E5": 15, "E6": 10, "E7": 10, "E9": 10}),
    ("family", ctx("STM32F103R8T6", "STMicroelectronics"), "https://www.st.com/ds.pdf",
     "probable", 60, {"E3": 15, "E4": 10, "E5": 15, "E6": 10, "E9": 10}),
    ("app_note", ctx("NE555"), "", "rejected", 10, {"E1": 30, "E5": -20}),
    ("distributor", ctx("NE555P", "Texas Instruments", "DIP-8"), "",
     "rejected", 30, {"E1": 30, "E4": 10, "E5": -30, "E6": 10, "E7": 10}),
    ("foreign", ctx("NE555P"), "https://www.onsemi.com/lm358.pdf",
     "rejected", 0, {"E5": 15, "E9": 10, "E11": -30}),
    ("catalog", ctx("NE555"), "", "rejected", 0, {"E5": -20}),
    ("errata", ctx("STM32F103C8T6"), "https://www.st.com/es.pdf",
     "rejected", 15, {"E3": 15, "E4": 10, "E5": -20, "E9": 10}),
    ("scan", ctx("AT24C02"), "", "needs_user", 10, {"E4": 10, "E12": 0}),
    ("chinese", ctx("GD25Q64CSIG", "GigaDevice", "SOP8", "25Q64CSIG"), "",
     "confirmed", 100, {"E1": 30, "E2": 15, "E4": 10, "E5": 15, "E6": 10, "E7": 10, "E8": 10}),
    ("unrelated", ctx("NE555"), "", "rejected", 0, {}),
]


@pytest.mark.parametrize("name, context, url, status, score, expected", TABLE,
                         ids=["%s-%s" % (row[0], i) for i, row in enumerate(TABLE)])
def test_table(tmp_path, name, context, url, status, score, expected):
    facts, evidence = V.verify_file(make_pdfs.write(name, tmp_path), context, url=url, trust=TRUST)
    verdict = D.decide(evidence)
    got = codes(evidence)
    assert got == expected
    assert (verdict.status, verdict.score) == (status, score)
    assert facts.doc_type
    assert all(len(e.detail) <= 150 for e in evidence)


def test_quote_and_page(tmp_path):
    _, evidence = V.verify_file(make_pdfs.write("datasheet", tmp_path), ctx("NE555P"))
    e1 = [e for e in evidence if e.code == "E1"][0]
    assert e1.page == 1 and "NE555" in e1.detail
    e2 = [e for e in evidence if e.code == "E2"][0]
    assert e2.detail == "NE555P"


def test_part_only_deep_in_document():
    facts = DocFacts(pages=9, has_text=True, parts_found={"NE555": [5, 7]}, doc_type="datasheet")
    assert codes(V.collect(facts, ctx("NE555"))) == {"E1": 20, "E5": 15}


def test_similar_part_is_not_exact():
    """LM3580 — не LM358: ни E1, ни E4."""
    facts = DocFacts(pages=2, has_text=True, heading="LM3580 Datasheet", parts_found={"LM3580": [1]},
                     doc_type="datasheet")
    got = codes(V.collect(facts, ctx("LM358DR")))
    assert got == {"E5": 15, "E11": -30}


def test_unknown_letter_suffix_is_the_same_part():
    facts = DocFacts(pages=9, has_text=True, parts_found={"W25Q64JV": [1], "STM32F103": [1]})
    assert codes(V.collect(facts, ctx("W25Q64JVFQ"))) == {"E1": 30}
    assert codes(V.collect(facts, ctx("W25Q64FVSSIG"))) == {}
    assert codes(V.collect(facts, ctx("STM32F103C8"))) == {}          # C8 — другой кристалл, а не суффикс


def test_document_names_the_part_with_package_suffix():
    """Шаг 8.3h: на фото «AT24C02», документ везде пишет «AT24C02N» — это он же, а не «нет партномера»."""
    facts = DocFacts(pages=9, has_text=True, parts_found={"AT24C02N": [1, 2], "24C02": [1]}, doc_type="datasheet")
    assert codes(V.collect(facts, ctx("AT24C02"))) == {"E1": 30, "E5": 15}
    assert codes(V.collect(facts, ctx("AT24C02D"))) == {"E5": 15}      # на фото другой суффикс — не «точно он»
    assert codes(V.collect(facts, ctx("AT24C0"))) == {"E5": 15}
    facts = DocFacts(pages=9, has_text=True, parts_found={"LM3580": [1], "STM32F103C8": [1]})
    assert codes(V.collect(facts, ctx("LM358"))) == {} and codes(V.collect(facts, ctx("STM32F103"))) == {}


def test_family_name_inside_another_part_is_not_a_title_match():
    """Семейство «AT24» внутри «CAT24C021» — чужой документ: E4 нет, E11 есть."""
    facts = DocFacts(pages=12, has_text=True, heading="CAT24C161/162(16K), CAT24C021/022(2K)",
                     parts_found={"CAT24C021": [1]}, family_patterns=["CAT24C021/022"], doc_type="family")
    assert codes(V.collect(facts, ctx("AT24C02"))) == {"E5": 15, "E11": -30}
    facts = DocFacts(pages=9, has_text=True, heading="AT24Cxx Serial EEPROM", doc_type="family")
    assert codes(V.collect(facts, ctx("AT24C02")))["E4"] == 10          # своё семейство в заголовке — по-прежнему E4


def test_truncated_marking_is_not_another_part():
    """На корпусе «25Q64JVSIQ», в заголовке «W25Q64JV»: это тот же чип, а не чужой документ."""
    facts = DocFacts(pages=9, has_text=True, heading="W25Q64JV", parts_found={"W25Q64JV": [1]}, doc_type="datasheet")
    assert codes(V.collect(facts, ctx("25Q64JVSIQ"))) == {"E4": 10, "E5": 15}


def test_same_company_after_takeover_is_not_a_conflict():
    facts = DocFacts(pages=2, has_text=True, parts_found={"AT24C02": [1]}, manufacturers=["Microchip"])
    assert codes(V.collect(facts, ctx("AT24C02", "Atmel"), trust=TRUST))["E6"] == 10
    assert codes(V.collect(facts, ctx("AT24C02", "atmel")))["E6"] == -15        # без таблицы сайтов — разные
    assert "E6" not in codes(V.collect(DocFacts(has_text=True), ctx("AT24C02", "Atmel")))


@pytest.mark.parametrize("photo, doc, points", [
    ("SOP8", ["SOIC-8", "TSSOP-8"], 10), ("DIP-8", ["PDIP-8"], 10), ("QFN-32", ["VFQFPN-32"], 10),
    ("SOT-23", ["SOT-23-5"], 10), ("SOIC-8", ["TSSOP-8"], None),        # выводов столько же — не противоречие
    ("SOIC-8", ["SOIC-16", "DIP-14"], -10), ("SOIC-8", [], None), ("что-то", ["SOIC-8"], None),
])
def test_package(photo, doc, points):
    got = codes(V.collect(DocFacts(has_text=True, packages=doc), ctx("X1234", package=photo)))
    assert got.get("E7") == points


def test_source_and_independent_copies():
    facts = DocFacts(has_text=True)

    def e(url, same=()):
        return codes(V.collect(facts, ctx("NE555"), url=url, trust=TRUST, same_doc_domains=same))

    assert e("https://www.ti.com/a.pdf") == {"E9": 10}
    assert e("https://pdf1.alldatasheet.com/a.pdf") == {"E9": 5}
    assert e("https://badpdf.example/a.pdf") == {"E9": -10}
    assert e("https://notti.com/a.pdf") == {}
    assert e("", same=["ti.com", "alldatasheet.com"]) == {"E10": 10}
    assert e("", same=["ti.com", "ti.com"]) == {}


def test_weights_from_config():
    with open(os.path.join(ROOT, "config.default.json"), encoding="utf-8") as f:
        acquire = json.load(f)["acquire"]
    assert acquire["weights"] == V.DEFAULT_WEIGHTS
    assert acquire["thresholds"] == D.DEFAULT_THRESHOLDS
    facts = DocFacts(pages=1, has_text=True, parts_found={"NE555": [1]}, doc_type="app_note")
    got = codes(V.collect(facts, ctx("NE555"), weights={"E1_front": 40, "E5": {"app_note": -5}}))
    assert got == {"E1": 40, "E5": -5}
    assert V.merge_weights({"E5": {"catalog": 0}})["E5"]["datasheet"] == 15


def ev(**points):
    return [Evidence(code=c, points=p) for c, p in points.items()]


@pytest.mark.parametrize("evidence, status, score, reasons", [
    (ev(E1=30, E2=15, E4=10, E5=15), "confirmed", 70, []),
    (ev(E3=15, E4=10, E5=15, E6=10, E7=10, E9=10), "confirmed", 70, []),
    (ev(E4=10, E5=15, E6=10, E7=10, E8=10, E9=10, E10=10), "probable", 75, ["no_part_match"]),
    (ev(E1=30, E2=15, E4=10, E5=15, E6=10, E7=10, E9=10, E11=-30), "probable", 70, ["other_part_in_heading"]),
    (ev(E1=30, E5=15), "probable", 45, []),
    (ev(E1=30, E4=10), "rejected", 40, ["low_score"]),
    (ev(E5=-20, E11=-30), "rejected", 0, ["low_score", "other_part_in_heading", "no_part_match"]),
    (ev(E1=30, E4=10, E5=15, E6=-15), "needs_user", 40, ["manufacturer_conflict"]),
    (ev(E1=30, E2=15, E4=10, E5=15, E7=-10), "needs_user", 60, ["package_conflict"]),
    (ev(E5=15, E6=-15), "rejected", 0, ["low_score", "no_part_match", "manufacturer_conflict"]),
    (ev(E4=10, E12=0), "needs_user", 10, ["no_text_layer"]),
    (ev(E1=30, E2=15, E3=15, E4=10, E5=15, E6=10, E7=10, E8=10, E9=10, E10=10), "confirmed", 100, []),
])
def test_decide(evidence, status, score, reasons):
    verdict = D.decide(evidence)
    assert (verdict.status, verdict.score) == (status, score)
    assert sorted(verdict.reasons) == sorted(reasons)
    assert verdict.evidence == evidence


def test_hard_reject_and_thresholds():
    bad = ValidationResult(ok=False, reason="active_content")
    verdict = D.decide(ev(E1=30, E2=15, E4=10, E5=15), validation=bad)
    assert (verdict.status, verdict.score, verdict.reasons) == ("rejected", 0, ["hard:active_content"])
    assert D.decide(ev(E1=30, E5=15), validation=ValidationResult(ok=True)).status == "probable"
    assert D.decide(ev(E1=30, E5=15), thresholds={"confirmed": 45}).status == "confirmed"
    assert D.decide(ev(E1=30, E5=15), thresholds={"probable": 50}).status == "rejected"


def test_guessed_maker_matches_but_never_conflicts():
    """Производитель по семейству партномера (не с фото): совпадение — улика, чужое имя в документе — нет."""
    guessed = PhotoContext(part="STM32F103C8T6", manufacturer="STMicroelectronics", maker_guessed=True)
    own = DocFacts(pages=2, has_text=True, parts_found={"STM32F103C8T6": [1]}, manufacturers=["STMicroelectronics"])
    other = DocFacts(pages=2, has_text=True, parts_found={"STM32F103C8T6": [1]}, manufacturers=["Philips"])
    assert codes(V.collect(own, guessed))["E6"] == 10
    assert "E6" not in codes(V.collect(other, guessed))
    assert codes(V.collect(other, ctx("STM32F103C8T6", "STMicroelectronics")))["E6"] == -15


def test_part_in_file_name_title():
    """Заголовок в метаданных — имя файла: «0a-esp8266ex_datasheet_en» называет ESP8266EX (документ Espressif)."""
    facts = DocFacts(pages=31, has_text=True, heading="NOT RECOMMENDED FOR NEW DESIGNS",
                     title="0a-esp8266ex_datasheet_en", parts_found={"ESP8266EX": [1]}, doc_type="datasheet")
    assert codes(V.collect(facts, ctx("ESP8266EX")))["E4"] == 10
    other = DocFacts(pages=9, has_text=True, title="cat24c021_datasheet", parts_found={"AT24C02": [3]})
    assert "E4" not in codes(V.collect(other, ctx("AT24C02")))
