# -*- coding: utf-8 -*-
"""Синтетические PDF для тестов извлечения фактов, типа документа и улик (шаги 4.1–4.4).

Настоящих datasheet здесь нет: текст придуман, PDF собирается pypdf. Латиница пишется шрифтом Helvetica,
строки с китайским или русским текстом — составным шрифтом с таблицей /ToUnicode (в программе просмотра
такие строки могут не отображаться, текст из них извлекается).
Посмотреть файлы: python tests/fixtures/make_pdfs.py ПАПКА  → по одному PDF на документ (в git их не класть).
"""
import io
import os
import sys

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, ByteStringObject, DecodedStreamObject, DictionaryObject, NameObject,
                           NumberObject, TextStringObject)

TO_UNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""


def N(name):
    return NameObject(name)


def _fonts(w):
    latin = DictionaryObject({N("/Type"): N("/Font"), N("/Subtype"): N("/Type1"), N("/BaseFont"): N("/Helvetica")})
    cmap = DecodedStreamObject()
    cmap.set_data(TO_UNICODE)
    info = DictionaryObject({N("/Registry"): TextStringObject("Adobe"), N("/Ordering"): TextStringObject("GB1"),
                             N("/Supplement"): NumberObject(2)})
    cid = DictionaryObject({N("/Type"): N("/Font"), N("/Subtype"): N("/CIDFontType0"),
                            N("/BaseFont"): N("/STSong-Light"), N("/CIDSystemInfo"): info})
    wide = DictionaryObject({N("/Type"): N("/Font"), N("/Subtype"): N("/Type0"), N("/BaseFont"): N("/STSong-Light"),
                             N("/Encoding"): N("/UniGB-UCS2-H"),
                             N("/DescendantFonts"): ArrayObject([w._add_object(cid)]),
                             N("/ToUnicode"): w._add_object(cmap)})
    return DictionaryObject({N("/F1"): w._add_object(latin), N("/F2"): w._add_object(wide)})


def _show(text):
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return "/F2", b"<" + text.encode("utf-16-be").hex().encode("ascii") + b">"
    for ch in (b"\\", b"(", b")"):
        raw = raw.replace(ch, b"\\" + ch)
    return "/F1", b"(" + raw + b")"


def make_pdf(pages, title="", producer="", author=""):
    """pages — список страниц; страница — список строк; строка — текст или (текст, кегль).
    Пустая страница (без строк) — «скан»: текста на ней нет."""
    w = PdfWriter()
    fonts = w._add_object(_fonts(w))
    for lines in pages:
        page = w.add_blank_page(width=595, height=842)
        if not lines:
            continue
        ops = [b"BT 50 800 Td"]
        for line in lines:
            text, size = line if isinstance(line, tuple) else (line, 10)
            font, shown = _show(text)
            ops.append(("%s %d Tf 0 -%d Td " % (font, size, size + 6)).encode("ascii") + shown + b" Tj")
        ops.append(b"ET")
        stream = DecodedStreamObject()
        stream.set_data(b"\n".join(ops))
        page[N("/Resources")] = DictionaryObject({N("/Font"): fonts})
        page[N("/Contents")] = w._add_object(stream)
    w.add_metadata({"/Producer": producer or "pypdf"})
    for key, value in (("/Title", title), ("/Author", author)):
        if value:                                  # pypdf пишет нелатинскую строку без метки UTF-16 — пишем сами
            w._info[N(key)] = ByteStringObject(b"\xfe\xff" + value.encode("utf-16-be"))
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def datasheet():
    """Правильный документ: datasheet на NE555 с таблицей заказных кодов и маркировкой."""
    return make_pdf([
        [("NE555 Precision Timer", 22), "Texas Instruments", "Datasheet   SLFS022 - revised 2014", "",
         "1 Features", "Timing from microseconds to hours, astable or monostable operation",
         "Supply voltage 4.5 V to 16 V, output sink or source up to 200 mA",
         "Available in 8-pin PDIP, SOIC-8 and TSSOP-8 packages",
         "2 Description", "The NE555 is a precision timing circuit producing accurate time delays."],
        ["NE555", "3 Absolute Maximum Ratings", "Supply voltage VCC 18 V", "Output current 225 mA",
         "4 Electrical Characteristics", "Threshold voltage level 10 V typ at VCC = 15 V, 100 nF bypass"],
        ["NE555", "5 Ordering Information", "Orderable part number   Package   Top-side marking",
         "NE555P      PDIP-8    NE555P", "NE555DR     SOIC-8    NE555", "NE555PWR    TSSOP-8   N555",
         "", "6 Mechanical data", "Texas Instruments Incorporated"],
    ], title="NE555 Precision Timer datasheet", producer="Acme PDF Library 9.1")


def family():
    """Datasheet на семейство: партномер задан шаблоном с x, полные коды — в таблице заказа."""
    return make_pdf([
        [("STM32F103x8", 20), ("STM32F103xB", 20), "Medium-density performance line ARM-based 32-bit MCU",
         "with 64 or 128 KB Flash, USB, CAN, 7 timers, 2 ADCs", "Datasheet - production data", "STMicroelectronics",
         "Packages: LQFP48, LQFP64, LQFP100, VFQFPN36"],
        ["STM32F103x8, STM32F103xB", "2 Description", "The STM32F103xx medium-density performance line family",
         "incorporates the high-performance ARM Cortex-M3 32-bit RISC core operating at 72 MHz."],
        ["STM32F103x8, STM32F103xB", "7 Ordering information scheme", "Example: STM32F103C8T6",
         "STM32F103C8T6   LQFP48", "STM32F103CBT6   LQFP48", "STM32F103RBT6   LQFP64"],
    ], title="STM32F103x8 STM32F103xB", producer="Acme Distiller", author="STMicroelectronics")


def app_note():
    """Заметка по применению: NE555 упоминается, но документ не о нём."""
    return make_pdf([
        [("AN1234 Application note", 18), ("LED dimming with a timer IC", 16), "Microchip Technology",
         "Introduction", "This application note shows how to build a PWM dimmer around the NE555 timer.",
         "The circuit drives an IRF540N transistor in a TO-220 package."],
        ["AN1234", "Circuit description", "The NE555 works in astable mode at about 1 kHz.",
         "References", "NE555 datasheet"],
    ], title="AN1234 LED dimming with a timer IC")


def catalog(parts=40):
    """Каталог: десятки разных партномеров, по нескольку на странице."""
    names = ["%s%d%s" % (p, 100 + 7 * i, s) for i in range(parts // 4)
             for p, s in (("LM", "N"), ("TL", "CD"), ("MC", "P"), ("UA", "D"))]
    pages = [[("Product Selection Guide 2024", 20), "Linear and logic products", "Onsemi"]]
    for i in range(0, len(names), 10):
        pages.append(["Selection guide", "Part number   Description   Package"] +
                     ["%s   general purpose device   SOIC-8" % n for n in names[i:i + 10]])
    return make_pdf(pages, title="Product Selection Guide 2024")


def foreign():
    """Чужой документ: datasheet на LM358, искомого NE555 в нём нет."""
    return make_pdf([
        [("LM358 Dual Operational Amplifier", 20), "Datasheet", "ON Semiconductor",
         "Features", "Wide supply range 3 V to 32 V", "Packages: SOIC-8, PDIP-8"],
        ["LM358", "Ordering Information", "Device   Package   Marking",
         "LM358DR2G   SOIC-8   LM358", "LM358NG   PDIP-8   LM358N"],
    ], title="LM358 - Dual Operational Amplifier", producer="Acme PDF Library 9.1")


def scan():
    """Скан: страницы без текстового слоя, от документа есть только заголовок в метаданных."""
    return make_pdf([[], [], []], title="AT24C02 scan", producer="ScanSoft 2.0")


def scan_pdf(pages, title="", size=(1654, 2339), rotate=0):
    """Скан с картинками страниц (A4, 200 dpi): страница — список строк, строка — текст или (текст, кегль в точках
    картинки). Текстового слоя нет. `rotate` — страницы лежат в файле повёрнутыми, поворот записан в /Rotate."""
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader
    images = []
    for lines in pages:
        image = Image.new("L", size, 255)
        draw, y = ImageDraw.Draw(image), 120
        for line in lines:
            text, pt = line if isinstance(line, tuple) else (line, 34)
            draw.text((120, y), text, font=ImageFont.load_default(size=pt), fill=0)
            y += int(pt * 1.6)
        images.append(image.rotate(rotate, expand=True) if rotate else image)
    buf = io.BytesIO()
    images[0].save(buf, "PDF", resolution=200, save_all=True, append_images=images[1:])
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(buf.getvalue())))
    for page in w.pages:
        if rotate:
            page[N("/Rotate")] = NumberObject(rotate)
    w.add_metadata({"/Producer": "ScanSoft 2.0", "/Title": title})
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def scan_image():
    """Скан datasheet на NE555: страницы — картинки; последняя страница пустая (белый лист)."""
    return scan_pdf([
        [("NE555 Precision Timer", 70), "Texas Instruments", "Datasheet SLFS022 revised 2014", "",
         "Timing from microseconds to hours, astable or monostable operation",
         "Available in 8-pin PDIP, SOIC-8 and TSSOP-8 packages"],
        ["NE555", "Ordering Information", "NE555P PDIP-8", "NE555DR SOIC-8", "", "Texas Instruments Incorporated"],
        [],
    ], title="scan0001")


def chinese():
    """Китайский datasheet: текст на китайском, партномера и корпуса латиницей."""
    return make_pdf([
        [("GD25Q64C 数据手册", 20), "兆易创新科技股份有限公司", "64M-bit 串行闪存",
         "特性", "工作电压 2.7V 至 3.6V", "封装 SOP8 208mil, WSON8, USON8",
         "概述", "GD25Q64C 是一款串行接口的闪存芯片，支持标准 SPI 以及双线、四线模式。"],
        ["GD25Q64C", "订购信息", "型号   封装   丝印", "GD25Q64CSIG   SOP8   25Q64CSIG", "GD25Q64CWIG   WSON8   25Q64CWIG"],
    ], title="GD25Q64C 数据手册", producer="WPS Office")


def errata():
    """Список ошибок кристалла: о том же чипе, но не datasheet."""
    return make_pdf([
        [("STM32F103x8 Errata sheet", 20), "STM32F103x8 and STM32F103xB device limitations", "STMicroelectronics",
         "Silicon identification", "This errata sheet applies to revision B of the devices.",
         "For the electrical characteristics refer to the STM32F103x8 datasheet."],
        ["STM32F103x8", "2.1 Voltage glitch on ADC input 0", "Description", "Workaround: none."],
    ], title="STM32F103x8 errata sheet", author="STMicroelectronics")


def reference_manual():
    """Справочное руководство по семейству: регистры и периферия, электрических параметров нет."""
    return make_pdf([
        [("RM0008", 22), ("Reference manual", 22), "STM32F101xx, STM32F103xx advanced ARM-based 32-bit MCUs",
         "Introduction", "This reference manual targets application developers.",
         "For ordering information and electrical characteristics please refer to the datasheets."],
        ["RM0008", "1 Documentation conventions", "2 Memory and bus architecture", "3 CRC calculation unit"],
    ], title="RM0008 Reference manual", author="STMicroelectronics")


def product_brief():
    """Краткое описание изделия: одна страница без таблиц параметров."""
    return make_pdf([
        [("ESP32-C3 Product Brief", 20), "Espressif Systems",
         "Low-power 2.4 GHz Wi-Fi and Bluetooth LE system on chip", "Features", "RISC-V single-core processor",
         "Applications: smart home, industrial automation", "For details see the full datasheet."],
    ], title="ESP32-C3 Product Brief")


def distributor():
    """Страница магазина, сохранённая в PDF: цена, наличие, корзина."""
    return make_pdf([
        ["Mouser Electronics", ("NE555P Texas Instruments", 14), "Timers and Support Products",
         "In Stock: 12 450", "Unit Price: 0.52", "Quantity   Price break", "1   0.52", "100   0.31",
         "Add to Cart", "Datasheet   NE555 datasheet (PDF)", "Package: PDIP-8"],
    ], title="NE555P Texas Instruments | Mouser")


def unrelated():
    """Посторонний документ: микросхем в нём нет."""
    return make_pdf([
        [("Annual report 2023", 20), "Letter to shareholders", "Revenue grew by 4 percent over the previous year.",
         "The board proposes a dividend at the general meeting in May."],
    ], title="Annual report 2023")


DOCS = {"datasheet": datasheet, "family": family, "app_note": app_note, "catalog": catalog, "foreign": foreign,
        "scan": scan, "chinese": chinese, "errata": errata, "reference_manual": reference_manual,
        "product_brief": product_brief, "distributor": distributor, "unrelated": unrelated}
SCANS = {"scan_image": scan_image}                # сканы с картинками страниц: читаются только распознаванием


def write(name, folder):
    """Записывает документ `name` в папку и возвращает путь."""
    path = os.path.join(str(folder), name + ".pdf")
    with open(path, "wb") as f:
        f.write((DOCS.get(name) or SCANS[name])())
    return path


def main(folder=None):
    if not folder:
        import tempfile
        folder = tempfile.mkdtemp(prefix="digger_pdfs_")
    os.makedirs(folder, exist_ok=True)
    for name in DOCS:
        print(write(name, folder))


if __name__ == "__main__":
    main(*sys.argv[1:2])
