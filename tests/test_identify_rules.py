# -*- coding: utf-8 -*-
"""Выделение партномера из прочитанной маркировки (шаг 8.5): логотип с ошибкой чтения, слова-пометки, коды даты
и партии, партномер в две строки, запись семейства; доля фото `my_test/ocr`, где программа выбрала верный партномер."""
import os
import sys

import pytest

from digger.modules.identify_rules import _is_date_code, maker_of

# шаг 8.5: на 120 вырезках строка совпадает с ответом в 46, но программа с пустой локальной базой выбирала ответ
# только в 22 (18 %; база пользователя, где лежат datasheet этих же чипов, поднимала до 32). После правил — 34 (28 %),
# ещё в 10 выбран полный заказной код, начинающийся с ответа («25Q128FVPG» при ответе «25Q128FV»). Пороги — с запасом.
MIN_MYTEST_CHOSEN_PCT = 26.0
MIN_MYTEST_CHOSEN_OR_FULL_PCT = 34.0


def _parts(ctx, text):
    return [c.part for c in ctx.modules["identifier"].identify(text)]


def test_maker_word_with_ocr_errors():
    """Логотип читается с ошибками («winbong») — это производитель, а не партномер."""
    assert maker_of("WINBOND") == "Winbond"
    assert maker_of("WINBONG") == "Winbond" and maker_of("AINBOND") == "Winbond"
    assert maker_of("GIGODEVICE") == "GigaDevice"
    assert maker_of("BROADCOM7") == "Broadcom" and maker_of("10SPANSION") == "Spansion"   # прилипшая цифра
    assert maker_of("LATTICE") == "Lattice"
    assert maker_of("ST") == "STMicroelectronics" and maker_of("SI") == ""     # короткие — только точно
    assert maker_of("MAX232") == "" and maker_of("ATMEGA8") == "" and maker_of("MICRO") == ""


def test_misread_logo_is_not_a_candidate(ctx):
    cands = ctx.modules["identifier"].identify("winbong\nW25Q32JVSIQ\n2145")
    assert cands[0].part == "W25Q32JVSIQ" and cands[0].manufacturer == "Winbond"
    assert "WINBONG" not in [c.part for c in cands]
    assert _parts(ctx, "LATTICE\nLCMXO2-640HC\nMALAYSIA2231")[0] == "LCMXO2640HC"


def test_note_words_are_not_candidates(ctx):
    """«eMMC», «MEMORY», страна с прилипшим кодом даты — пометки на корпусе."""
    parts = _parts(ctx, "eMMC\nFEMDRW016G-58A42\nALLIANCE MEMORY\nTAIWAN2217")
    assert parts[0] == "FEMDRW016G58A42"
    assert not set(parts) & {"EMMC", "MEMORY", "ALLIANCE", "TAIWAN2217"}


def test_date_code_needs_real_week_and_year():
    assert _is_date_code("2136") and _is_date_code("9952") and _is_date_code("0401") and _is_date_code("202136")
    assert not _is_date_code("1200")      # недели 00 не бывает
    assert not _is_date_code("3006")      # 2030 год ещё не наступил
    assert not _is_date_code("4558")


def test_four_digit_marking_is_a_candidate(ctx):
    """Мелкий корпус: «3007» — маркировка (не код даты), двухсимвольный обрывок её не обгоняет."""
    assert _parts(ctx, "9K\n3007\nB")[0] == "3007"
    assert _parts(ctx, "ATMEL\nAT24C02\n2136")[0] == "AT24C02"       # настоящий код даты кандидатом не становится
    assert _parts(ctx, "NE5532P\nAB2301")[0] == "NE5532P"


def test_marking_shape_breaks_ties(ctx):
    """Без справочника и базы: буквы серии и номер — партномер; точка внутри, буквы и код даты — партия."""
    assert _parts(ctx, "97H2431\nVU2025\nXLB9672")[0] == "XLB9672"
    assert _parts(ctx, "QXT2500\nP03B21.00S-1T")[0] == "QXT2500"
    assert _parts(ctx, "P1PJG3\n54339")[0] == "54339"              # пять цифр — числовая маркировка
    assert _parts(ctx, "TI 52\nDWK")[0] == "DWK"                    # логотип с соседним словом не склеивается
    cands = ctx.modules["identifier"].identify("GigoDevice\nC123074\n25LQ32EWIG")
    assert cands[0].part == "25LQ32EWIG" and cands[0].confirmed_by      # усечённая маркировка: GD25LQ32E


def test_two_line_part_beats_its_first_line(ctx):
    """Партномер разорван переносом: целый важнее начала, даже если начало есть в справочнике."""
    assert _parts(ctx, "SEC 204\nK4B4G16\n46E-BCMA")[0] == "K4B4G1646EBCMA"
    assert _parts(ctx, "STM32F103\nC8T6")[0] == "STM32F103C8T6"


def test_family_shape_prefers_fixed_reading(ctx):
    """«STM32F40SVG»: по записи семейства после буквы серии идут три цифры — значит «S» это «5»."""
    assert _parts(ctx, "STM32F40SVG")[0] == "STM32F405VG"
    assert _parts(ctx, "STM32F405VG")[0] == "STM32F405VG"
    assert _parts(ctx, "STM32G0B1RE")[0] == "STM32G0B1RE"     # буква внутри номера — законная запись


@pytest.mark.mytest
def test_mytest_chosen_part_share(ctx, my_test_dir):
    """Доля вырезок my_test/ocr, где выбранный программой партномер — ответ из имени файла."""
    pytest.importorskip("rapidocr_onnxruntime")
    folder = os.path.join(my_test_dir, "ocr")
    if not os.path.isdir(folder):
        pytest.skip("нет папки my_test/ocr/")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "win7_pack"))
    import ppocr_check as pc
    import program_ocr as po
    items = pc.collect(folder)
    if not items:
        pytest.skip("в my_test/ocr/ нет картинок")
    rows = po.read_photos(ctx, items, pc)
    chosen = 100.0 * sum(1 for r in rows if r["chosen_ok"]) / len(rows)
    full = 100.0 * sum(1 for r in rows if r["chosen_ok"] or r["chosen_full"]) / len(rows)
    print("\nmy_test/ocr: %d фото, выбран ответ — %.1f %%, ответ или полный код с ним в начале — %.1f %%"
          % (len(rows), chosen, full))
    assert chosen >= MIN_MYTEST_CHOSEN_PCT
    assert full >= MIN_MYTEST_CHOSEN_OR_FULL_PCT
