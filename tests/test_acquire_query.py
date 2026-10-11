# -*- coding: utf-8 -*-
"""План запросов на трёх языках (шаг 1.3, ARCHITECTURE §4.7)."""
import pytest

from digger.acquire.query import Query, base_part, family, is_smd_code, mentions, plan_queries, relevance_keys


@pytest.mark.parametrize("raw, base", [
    ("STM32F103C8T6", "STM32F103C8"),
    ("AT24C02N-10SU-2.7", "AT24C02"),
    ("W25Q64JVSIQ", "W25Q64JV"),
    ("LM358DR", "LM358"),
    ("PMS150C-U06", "PMS150C"),
    ("lm358 dr", "LM358"),
    ("LM358/TR", "LM358"),
    ("NE555P", "NE555"),
    ("A6W", "A6W"),
    ("", ""),
])
def test_base_part(raw, base):
    assert base_part(raw) == base


@pytest.mark.parametrize("raw, fam", [
    ("STM32F103C8T6", "STM32F103"), ("W25Q64JVSIQ", "W25Q64"), ("PMS150C-U06", "PMS150"),
    ("LM358DR", ""), ("A6W", ""),
])
def test_family(raw, fam):
    assert family(raw) == fam


def test_is_smd_code():
    assert is_smd_code("A6W") and is_smd_code("1AM")
    assert not is_smd_code("LM358") and not is_smd_code("ABCD") and not is_smd_code("")


def _texts(qs, lang):
    return [q.text for q in qs if q.lang == lang]


def test_plan_stm32():
    qs = plan_queries("STM32F103C8T6")
    assert [q.lang for q in qs][0] == "en"
    assert _texts(qs, "en") == ["STM32F103C8 datasheet pdf", "STM32F103 datasheet pdf"]
    assert "STM32F103C8 数据手册" in _texts(qs, "zh")
    assert "STM32F103C8 даташит" in _texts(qs, "ru")


def test_plan_no_family_for_lm358():
    qs = plan_queries("LM358DR")
    assert all(q.kind == "part" for q in qs)
    assert _texts(qs, "en") == ["LM358 datasheet pdf"]
    assert _texts(qs, "zh") == ["LM358 数据手册", "LM358 规格书 pdf", "LM358 中文资料"]
    assert _texts(qs, "ru") == ["LM358 даташит", "LM358 документация pdf"]


def test_plan_smd_code():
    qs = plan_queries("A6W")
    assert {q.kind for q in qs} == {"smd"}
    assert _texts(qs, "zh") == ["A6W 丝印"]
    assert _texts(qs, "ru") == ["A6W маркировка smd"]


def test_language_order_from_config():
    qs = plan_queries("AT24C02N-10SU-2.7", langs=["ru", "en"])
    langs = []
    for q in qs:
        if not langs or langs[-1] != q.lang:
            langs.append(q.lang)
    assert langs == ["ru", "en"]


def test_unknown_langs_fall_back_to_default():
    assert {q.lang for q in plan_queries("LM358", langs=["xx"])} == {"en", "zh", "ru"}


def test_plan_keeps_part_for_relevance_check():
    qs = plan_queries("STM32F103C8T6")
    assert {q.part for q in qs if q.kind == "part"} == {"STM32F103C8"}
    assert {q.part for q in qs if q.kind == "family"} == {"STM32F103"}
    assert {q.part for q in plan_queries("A6W")} == {"A6W"}


@pytest.mark.parametrize("query, keys", [
    (Query("en", "STM32F103C8 datasheet pdf", "part", "STM32F103C8"), ["STM32F103C8", "STM32F103"]),
    (Query("en", "NE555 datasheet pdf", "part"), ["NE555"]),                   # партномер не задан — берётся из текста
    ("W25Q64JV 数据手册", ["W25Q64JV", "W25Q64"]),
    ("25Q512JVFQ datasheet pdf", ["25Q512JVFQ", "25Q512"]),                    # усечённая маркировка: ряд без суффикса
    ("LM358 filetype:pdf site:21ic.com", ["LM358"]),                           # операторы поисковика — не партномер
    (Query("zh", "A6W 丝印", "smd", "A6W"), ["A6W"]),
    ("даташит микросхемы", []),                                                # проверять нечем — отсева нет
    ("", []),
])
def test_relevance_keys(query, keys):
    assert relevance_keys(query) == keys


@pytest.mark.parametrize("keys, texts, hit", [
    (["NE555"], ("NE555 Precision Timer", "", ""), True),
    (["NE555"], ("Timer", "the ne-555 is a classic", ""), True),               # дефис и регистр не мешают
    (["NE555"], ("PDF", "Download", "https://example.org/files/ne555.pdf"), True),
    (["NE555"], ("Sign in", "Sign in to the assistant", "https://example.org/"), False),
    (["25Q512JVFQ", "25Q512"], ("W25Q512JV 3V 512M-bit serial flash", "", ""), True),
    (["A6W"], ("A6W SMD marking: BAS16W", "", ""), True),
    (["A6W"], ("Sea6water pumps", "", "https://example.org/sea6water"), False), # короткий код — только отдельным словом
    ([], ("что угодно", "", ""), True),
])
def test_mentions(keys, texts, hit):
    assert mentions(keys, *texts) is hit


def _parts(qs, lang="en"):
    return [q.part for q in qs if q.lang == lang]


def test_prefix_variants_truncated_marking():
    qs = plan_queries("25Q512JVFQ", langs=["en"])
    assert _parts(qs) == ["25Q512JVFQ", "25Q512", "W25Q512JVFQ", "GD25Q512JVFQ", "XM25Q512JVFQ"]
    assert "W25Q512JVFQ datasheet pdf" in _texts(qs, "en")
    assert relevance_keys(qs[2]) == ["W25Q512JVFQ", "W25Q512"]


def test_prefix_variants_all_languages_and_maker_narrows():
    assert [q.text for q in plan_queries("25Q64", langs=["zh"]) if q.text.startswith("W")] == [
        "W25Q64 数据手册"]
    qs = plan_queries("25Q64", langs=["en"], maker="GigaDevice")
    assert _parts(qs) == ["25Q64", "GD25Q64"]
    assert _parts(plan_queries("25Q64", langs=["en"], maker="Winbond"))[-1] == "W25Q64"
    assert _parts(plan_queries("25Q64", langs=["en"], maker="Unknown"))[-1] == "XM25Q64"   # не нашли — все варианты


def test_prefix_variants_24c02():
    assert _parts(plan_queries("24C02", langs=["en"])) == ["24C02", "AT24C02", "M24C02", "CAT24C02"]


def test_no_prefix_when_already_full_or_unknown():
    assert _parts(plan_queries("W25Q64JVSIQ", langs=["en"])) == ["W25Q64JV", "W25Q64"]
    assert _parts(plan_queries("NE555", langs=["en"])) == ["NE555"]
    assert _parts(plan_queries("A6W", langs=["en"])) == ["A6W"]
    assert _parts(plan_queries("25Q64", langs=["en"], prefixes={})) == ["25Q64"]


@pytest.mark.parametrize("part, maker", [
    ("STM32F103C8T6", "STMicroelectronics"), ("GD32F103C8T6", "GigaDevice"), ("W25Q64JV", "Winbond"),
    ("CH340G", "WCH"), ("ESP8266EX", "Espressif"), ("STC89C52RC", "STC"), ("PMS150C", "Padauk"),
    ("AT24C02", "Microchip"), ("stm32f103c8t6", "STMicroelectronics"),
    ("NE555", ""), ("LM358", ""), ("74HC595", ""), ("AMS1117", ""), ("25Q64JV", ""), ("", ""),
])
def test_maker_by_prefix(part, maker):
    """Производитель известен по партномеру только у семейств, которые выпускает он один."""
    from digger.acquire.query import maker_by_prefix
    assert maker_by_prefix(part) == maker


def test_maker_by_prefix_survives_broken_table():
    from digger.acquire.query import maker_by_prefix
    table = {"families": [{"pattern": "("}, {"maker": "X"}, {"pattern": "^AB", "maker": "Acme"}]}
    assert maker_by_prefix("AB12", table) == "Acme" and maker_by_prefix("AB12", {}) == ""
