# -*- coding: utf-8 -*-
"""Портативная сборка: поиск Tesseract в папке программы и состав архива (без сети)."""
import io
import os
import sys
import zipfile

import pytest

from digger.recognition.providers.tesseract import find_tesseract

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
bp = pytest.importorskip("build_portable", reason="нет tools/ (портативная сборка)")


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    return path


def test_find_tesseract_bundled(tmp_path):
    exe = _touch(str(tmp_path / "Папка с пробелом" / "tesseract" / "tesseract.exe"))
    app = os.path.dirname(os.path.dirname(exe))
    assert find_tesseract(app) == exe
    # путь из настроек важнее встроенного
    own = _touch(str(tmp_path / "own" / "tesseract.exe"))
    assert find_tesseract(app, own) == own
    # несуществующий путь из настроек — берём встроенный
    assert find_tesseract(app, str(tmp_path / "нет.exe")) == exe


def test_qt_plugins_dir_found():
    # путь к плагинам Qt задаётся явно: из папки с русскими буквами PyQt5 сам его не находит
    pytest.importorskip("PyQt5")
    from digger.gui.main_window import qt_plugins_dir
    assert os.path.isdir(os.path.join(qt_plugins_dir(), "platforms"))


def test_requirements_for_windows():
    reqs = bp.windows_requirements(bp.read_lines(os.path.join(bp.ROOT, "requirements.txt")))
    assert "PyQt5-Qt5==5.15.2" in reqs
    assert not any("5.15.19" in r or ";" in r for r in reqs)
    assert all("==" in r for r in reqs if not r.startswith("certifi"))


def test_program_files_exclude_private():
    files = [rel for _src, rel in bp.program_files()]
    assert "run.py" in files and "config.default.json" in files
    assert any(f.startswith("digger/") for f in files)
    assert any(f.startswith("tests/samples/") for f in files)
    bad = ("my_test/", "my_reports/", ".venv/", ".git/", "tools/", "config.json", "data/library/x")
    assert not any(f.startswith(b) for f in files for b in bad)
    assert not any("__pycache__" in f or f.endswith(".pyc") for f in files)


def test_pth_enables_site_packages():
    lines = bp.PTH.splitlines()
    assert "python38.zip" in lines and "Lib\\site-packages" in lines and "import site" in lines


def test_bat_files_cp866_crlf():
    for name, text in bp.BATS.items():
        data = bp.bat_bytes(text)
        assert b"\r\n" in data and b"\n\n" not in data.replace(b"\r\n", b"")
        data.decode("cp866")
    assert "Digger.bat" in bp.BATS and "Самопроверка.bat" in bp.BATS
    assert "pythonw.exe" in bp.BATS["Digger.bat"]
    assert "--selftest" in bp.BATS["Самопроверка.bat"]


def test_zip_russian_names_cp866(tmp_path):
    src = tmp_path / "src"
    _touch(str(src / "Самопроверка.bat"))
    _touch(str(src / "run.py"))
    out = str(tmp_path / "a.zip")
    bp.make_zip(str(src), out, "Digger")
    with open(out, "rb") as f:
        raw = f.read()
    assert "Digger/Самопроверка.bat".encode("cp866") in raw   # Проводник Win7 читает имена в cp866
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert {i.flag_bits & 0x800 for i in z.infolist()} == {0}   # без флага UTF-8
        assert "Digger/run.py" in z.namelist()


def test_checks_in_build():
    files = [rel for _src, rel in bp.check_files()]
    for need in ("checks/run_checks.py", "checks/sysinfo.py", "checks/selftest.py", "checks/window.py",
                 "checks/ЧТО СДЕЛАТЬ.txt"):
        assert need in files
    assert "Проверка на работе.bat" in bp.BATS
    assert "checks\\run_checks.py" in bp.BATS["Проверка на работе.bat"]


def test_ppocr_libs_come_from_requirements_and_msvc_pinned():
    reqs = dict(x.split("==") for x in bp.windows_requirements(bp.read_lines(os.path.join(bp.ROOT, "requirements.txt")))
                if "==" in x)
    assert reqs["onnxruntime"] == "1.11.1"            # 1.12+ на Win7 не работает
    assert reqs["rapidocr-onnxruntime"] == "1.3.24"
    assert bp.MSVC_WHEEL == "msvc-runtime==14.29.30133"
    assert set(bp.MSVC_DLLS) == {"msvcp140.dll", "vcruntime140_1.dll", "concrt140.dll"}
    assert not hasattr(bp, "CHECK_LIBS")              # отдельных checks/libs больше нет


def test_install_msvc_dlls_copies_dlls(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "python").mkdir(parents=True)
    calls = []

    def fake_install(target, reqs):
        calls.append((target, reqs))
        for d in ("msvcp140.dll", "vcruntime140_1.dll"):      # concrt140.dll «забыли»
            _touch(os.path.join(target, "msvc_runtime", d))

    monkeypatch.setattr(bp, "install_packages", fake_install)
    missing = bp.install_msvc_dlls(str(app))
    assert missing == ["concrt140.dll"]
    assert (app / "python" / "msvcp140.dll").is_file() and not (app / "tmp_msvc").exists()
    assert len(calls) == 1 and calls[0][1] == [bp.MSVC_WHEEL]


def test_program_ocr_in_build():
    files = [rel for _src, rel in bp.check_files()]
    assert "checks/program_ocr.py" in files and "checks/ppocr_check.py" in files


def test_ci_ppocr_answers_match_samples():
    import ci_ppocr
    for fn in ci_ppocr.ANSWERS:
        assert os.path.isfile(os.path.join(bp.ROOT, "tests", "samples", fn))
    assert ci_ppocr.MIN_READ <= len(ci_ppocr.ANSWERS)


def test_build_ships_photos_folder_and_visit_guide(tmp_path):
    """Выезд 2: папки «фото» в архиве не было, а инструкция была в две строки."""
    bp.make_visit_files(str(tmp_path))
    assert (tmp_path / "фото" / bp.PHOTOS_NOTE).is_file()
    guide = (tmp_path / bp.VISIT_GUIDE).read_text(encoding="utf-8-sig")
    for word in ("ДО ВЫЕЗДА", "my_test/ocr", "фото", "Проверка на работе.bat", "отчёт_", "my_reports", "HEIC"):
        assert word in guide
    bat = bp.BATS["Проверка на работе.bat"]
    assert "%~dp0фото" in bat and "5-15 минут" not in bat
    bp.bat_bytes(bat)        # текст укладывается в кодировку cmd.exe


def test_build_ships_demo_guide_matching_the_program(tmp_path):
    """Шаг 8.3f: инструкция для показа лежит в корне сборки и называет то, что есть в программе."""
    from digger.acquire.decide import DEFAULT_THRESHOLDS
    from digger.acquire.orchestrator import Budget
    from digger.acquire.trial import CHIPS
    bp.make_visit_files(str(tmp_path))
    raw = (tmp_path / bp.DEMO_GUIDE).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw              # Блокнот Win7: BOM и CRLF
    demo = raw.decode("utf-8-sig")
    for chip in CHIPS:
        assert chip in demo
    with open(os.path.join(bp.ROOT, "digger", "gui", "main_window.py"), encoding="utf-8") as f:
        window = f.read()
    for label in (u"Быстрый поиск", u"Полный поиск", u"Сохранить отчёт", u"Сводка CSV", u"Подтвердить", u"Отклонить",
                  u"Проверить свой PDF", u"Открыть PDF", u"Открыть папку библиотеки", u"Не найдено: где смотреть"):
        assert label in demo and label in window, label
    b = Budget()
    assert u"%d запросов, %d скачиваний" % (b.queries, b.downloads) in demo and b.seconds == 180
    assert u"%d баллов" % DEFAULT_THRESHOLDS["confirmed"] in demo and u"от %d" % DEFAULT_THRESHOLDS["probable"] in demo
    for fn in ("stm32.png", "at24c02.png", "w25q64_rot.png", "lm358_180.png"):
        assert fn in demo and os.path.isfile(os.path.join(bp.ROOT, "tests", "samples", fn))
    from digger.gui.main_window import DEMO_GUIDE, demo_guide_path
    assert DEMO_GUIDE == bp.DEMO_GUIDE and u"Как показать программу" in window       # пункт меню «Справка»
    assert demo_guide_path(str(tmp_path)) == str(tmp_path / bp.DEMO_GUIDE)
    assert demo_guide_path(bp.ROOT) == os.path.join(bp.ROOT, "docs", "DEMO.md")
