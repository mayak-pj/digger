# -*- coding: utf-8 -*-
"""Портативная сборка для Windows 7 x64 (без установки и прав администратора).

    python tools/build_portable.py [--out dist] [--no-tesseract] [--no-zip]

Состав папки Digger/: python/ (embeddable 3.8.10 + пакеты в Lib/site-packages, в т.ч. PP-OCR), tesseract/ (распакованный
установщик UB Mannheim, tessdata/eng), программа, Digger.bat, Самопроверка.bat. Пакеты — колёса win_amd64
для cp38 (pip --platform), поэтому сборку можно запустить и на Mac, чтобы проверить состав. Tesseract
распаковывается через 7-Zip (на CI есть; на Mac без 7-Zip — ключ --no-tesseract).
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

from packaging.requirements import Requirement

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "Digger"
ZIP_NAME = "Digger_portable_win7_x64.zip"

PY_URL = "https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-amd64.zip"
PY_SHA256 = "abbe314e9b41603dde0a823b76f5bbbe17b3de3e5ac4ef06b759da5466711271"
# 5.3.0 собран до того, как MSYS2 перестал поддерживать Win7 (2023); более новые сборки на Win7 не проверены
TESS_VERSION = "5.3.0.20221214"
TESS_URL = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-v%s.exe" % TESS_VERSION
TESS_SHA256 = "175a326853f87474132c284072a821dd819c63b707b01879b309f80bd6c0ab1e"

# pytest для «Самопроверки» (с зависимостями — ставится с --no-deps, поэтому перечислено всё)
SELFTEST_REQS = ["pytest==8.3.5", "pluggy==1.5.0", "iniconfig==2.0.0", "exceptiongroup==1.2.2",
                 "tomli==2.0.2", "colorama==0.4.6"]

# DLL Visual C++ рядом с python.exe — из колеса msvc-runtime (14.40+ Win7 не поддерживает)
MSVC_WHEEL = "msvc-runtime==14.29.30133"
MSVC_DLLS = ("msvcp140.dll", "vcruntime140_1.dll", "concrt140.dll")

# окружение цели для маркеров requirements.txt
WIN_ENV = {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt", "platform_machine": "AMD64",
           "python_version": "3.8", "python_full_version": "3.8.10", "implementation_name": "cpython",
           "platform_python_implementation": "CPython"}

INCLUDE = ["run.py", "config.default.json", "README.md", "requirements.txt", "pyproject.toml",
           "digger", "plugins", "data", "tests"]
CHECKS_SRC = os.path.join(ROOT, "tools", "win7_pack")      # исходники проверочного набора -> checks/ в сборке
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_EXT = (".pyc", ".sqlite", ".sqlite-journal")
SKIP_PREFIX = ("data/quarantine/",)

# ._pth: stdlib из zip, пакеты из Lib\site-packages; наличие файла включает изолированный режим
# (реестр и переменные PYTHON* игнорируются)
PTH = "python38.zip\n.\nLib\\site-packages\nimport site\n"

_ENV = ('if not exist tmp mkdir tmp\n'
        'if not exist logs mkdir logs\n'
        'set "TEMP=%~dp0tmp"\n'
        'set "TMP=%~dp0tmp"\n')
BATS = {
    "Digger.bat": (
        '@echo off\n'
        'rem Запуск Digger (портативная версия, без установки)\n'
        'cd /d "%~dp0"\n' + _ENV +
        'start "" "%~dp0python\\pythonw.exe" "%~dp0run.py" %*\n'),
    "Самопроверка.bat": (
        '@echo off\n'
        'rem Самопроверка Digger: тесты программы без интернета\n'
        'cd /d "%~dp0"\n' + _ENV +
        'echo Идёт самопроверка, подождите 1-3 минуты...\n'
        '"%~dp0python\\python.exe" "%~dp0run.py" --selftest > "%~dp0logs\\selftest.txt" 2>&1\n'
        'set RC=%ERRORLEVEL%\n'
        'type "%~dp0logs\\selftest.txt"\n'
        'echo.\n'
        'if "%RC%"=="0" (echo САМОПРОВЕРКА ПРОЙДЕНА) else (echo САМОПРОВЕРКА НЕ ПРОЙДЕНА, код %RC%)\n'
        'echo Отчёт: logs\\selftest.txt\n'
        'pause\n'),
    "Проверка на работе.bat": (
        '@echo off\n'
        'rem Проверочный набор: один запуск -> один файл отчёт_<дата>.zip в этой папке\n'
        'cd /d "%~dp0"\n' + _ENV +
        'echo Проверка идёт до трёх часов: в окне видно, какая часть идёт и сколько прошло.\n'
        'echo Окно программы будет мигать - это нормально.\n'
        'echo Фото чипов для проверки распознавания должны лежать в папке:\n'
        'echo   %~dp0фото\n'
        'echo Не закрывайте это окно до слова "Готово".\n'
        'echo.\n'
        '"%~dp0python\\python.exe" "%~dp0checks\\run_checks.py"\n'
        'echo.\n'
        'type "%~dp0checks\\ЧТО СДЕЛАТЬ.txt"\n'
        'pause\n'),
}


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [s.strip() for s in f if s.strip() and not s.strip().startswith("#")]


def windows_requirements(lines):
    """Строки requirements.txt, применимые к Win7 x64 / Python 3.8.10, без маркеров."""
    out = []
    for line in lines:
        r = Requirement(line)
        if r.marker is None or r.marker.evaluate(WIN_ENV):
            out.append(r.name + str(r.specifier))
    return out


def program_files():
    """[(путь_на_диске, путь_в_сборке)] — только перечисленное в INCLUDE, без кэшей и данных пользователя."""
    out = []
    for item in INCLUDE:
        src = os.path.join(ROOT, item)
        if os.path.isfile(src):
            out.append((src, item))
            continue
        for d, dirs, files in os.walk(src):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
            for fn in sorted(files):
                p = os.path.join(d, fn)
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                if fn.endswith(SKIP_EXT) or rel.startswith(SKIP_PREFIX) or fn == ".DS_Store":
                    continue
                if rel.startswith("data/library/") and fn != ".keep":
                    continue
                out.append((p, rel))
    return out


def check_files():
    """[(путь_на_диске, путь_в_сборке)] — проверочный набор (tools/win7_pack -> checks/)."""
    out = []
    for fn in sorted(os.listdir(CHECKS_SRC)):
        if fn.endswith((".py", ".txt")) and not fn.startswith("test_"):
            out.append((os.path.join(CHECKS_SRC, fn), "checks/" + fn))
    out.append((os.path.join(ROOT, "tools", "live_check.py"), "checks/live_check.py"))     # проверка live
    for fn in ("screenshots.py", "search_cli.py"):                                          # проверка screens
        out.append((os.path.join(ROOT, "tools", fn), "checks/" + fn))
    return out


PHOTOS_DIR = "фото"
PHOTOS_NOTE = "ПОЛОЖИТЕ ФОТО СЮДА.txt"
VISIT_GUIDE = "ИНСТРУКЦИЯ ДЛЯ ВЫЕЗДА.txt"
DEMO_GUIDE = "КАК ПОКАЗАТЬ ПРОГРАММУ.txt"       # docs/DEMO.md: сценарий показа на эталонных чипах (шаг 8.3f)
NOTES_FILE = "впечатления.txt"
NOTES_TEXT = ("Впечатления от программы (Win7). Пишите своими словами, коротко; файл сам попадёт в отчёт.\n"
              "Что смотреть - в «ИНСТРУКЦИЯ ДЛЯ ВЫЕЗДА.txt», раздел «ЧТО ПОСМОТРЕТЬ ГЛАЗАМИ».\n\n"
              "Окно открывается, текст читается, ничего не обрезано:\n\n"
              "Светлая / тёмная тема (Настройки -> тема, после перезапуска):\n\n"
              "Распознавание своих фото (что нашло, что нет):\n\n"
              "Лента поиска, вкладки «Заключение», «Документы», «Журнал»:\n\n"
              "Скорость, зависания, ошибки:\n\n"
              "Остальное:\n")


def make_visit_files(app):
    """Папка «фото» с запиской (в архиве она есть всегда — на выезде 2 её не было) и инструкция в корне сборки."""
    photos = os.path.join(app, PHOTOS_DIR)
    os.makedirs(photos, exist_ok=True)
    with open(os.path.join(photos, PHOTOS_NOTE), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("Сюда кладутся фото чипов для проверки распознавания («Проверка на работе.bat»).\n"
                "Формат: JPG или PNG (HEIC с телефона не читается). Подпапки можно.\n"
                "Имя файла = правильная маркировка, например: W25Q64JVSIQ.jpg\n")
    with open(os.path.join(app, NOTES_FILE), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(NOTES_TEXT)
    with open(os.path.join(CHECKS_SRC, "ЧТО СДЕЛАТЬ.txt"), encoding="utf-8") as f:
        guide = f.read()
    with open(os.path.join(app, VISIT_GUIDE), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(guide)
    with open(os.path.join(ROOT, "docs", "DEMO.md"), encoding="utf-8") as f:
        demo = f.read()
    with open(os.path.join(app, DEMO_GUIDE), "w", encoding="utf-8-sig", newline="\r\n") as f:     # Блокнот Win7
        f.write(demo)


def bat_bytes(text):
    """cmd.exe читает .bat в OEM-кодировке (cp866 на русской Windows), строки — CRLF."""
    return text.replace("\n", "\r\n").encode("cp866")


class _OemZipInfo(zipfile.ZipInfo):
    """Русские имена в cp866 без флага UTF-8: встроенный распаковщик Win7 флаг UTF-8 не понимает."""
    def _encodeFilenameFlags(self):
        try:
            return self.filename.encode("ascii"), self.flag_bits
        except UnicodeEncodeError:
            return self.filename.encode("cp866"), self.flag_bits


def make_zip(src_dir, out_path, arc_root):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for d, dirs, files in os.walk(src_dir):
            dirs.sort()
            for fn in sorted(files):
                p = os.path.join(d, fn)
                arc = arc_root + "/" + os.path.relpath(p, src_dir).replace(os.sep, "/")
                zi = _OemZipInfo.from_file(p, arc)
                zi.compress_type = zipfile.ZIP_DEFLATED
                with open(p, "rb") as f, z.open(zi, "w") as w:
                    shutil.copyfileobj(f, w, 1 << 20)


def fetch(url, sha256, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, url.rsplit("/", 1)[1])
    if not os.path.isfile(path):
        print("Скачиваю", url)
        req = urllib.request.Request(url, headers={"User-Agent": "Digger-build/0.6 (portable build script)"})
        with urllib.request.urlopen(req, timeout=300) as r, open(path + ".part", "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(path + ".part", path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha256:
        os.remove(path)
        raise SystemExit("Неверная контрольная сумма %s: %s" % (url, h.hexdigest()))
    return path


def install_packages(site_dir, reqs):
    """Колёса для Windows x64 / cp38 независимо от машины сборки. pip (CI) или uv (окружение на Mac без pip)."""
    try:
        import pip  # noqa: F401
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--no-compile",
               "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.8", "--implementation", "cp",
               "--abi", "cp38", "--target", site_dir] + reqs
    except ImportError:
        uv = shutil.which("uv")
        if not uv:
            raise SystemExit("Нужен pip или uv")
        cmd = [uv, "pip", "install", "--no-deps", "--only-binary", ":all:", "--python-platform", "x86_64-pc-windows-msvc",
               "--python-version", "3.8", "--target", site_dir] + reqs
    subprocess.check_call(cmd)


def extract_tesseract(installer, dest):
    seven = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if not seven:
        raise SystemExit("Нужен 7-Zip (7z/7zz) для распаковки Tesseract, или ключ --no-tesseract")
    subprocess.check_call([seven, "x", "-y", "-bd", "-o" + dest, installer], stdout=subprocess.DEVNULL)
    for junk in ("$PLUGINSDIR", "$TEMP", "uninstall.exe"):
        p = os.path.join(dest, junk)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.isfile(p):
            os.remove(p)
    for need in ("tesseract.exe", os.path.join("tessdata", "eng.traineddata"), os.path.join("tessdata", "configs", "tsv")):
        if not os.path.isfile(os.path.join(dest, need)):
            raise SystemExit("В распакованном Tesseract нет " + need)


def install_msvc_dlls(app):
    """DLL Visual C++ рядом с python.exe (нужны onnxruntime). Возвращает список DLL, которых не нашлось."""
    tmp = os.path.join(app, "tmp_msvc")
    install_packages(tmp, [MSVC_WHEEL])
    found = {}
    for d, _dirs, files in os.walk(tmp):
        for fn in files:
            if fn.lower() in MSVC_DLLS:
                found.setdefault(fn.lower(), os.path.join(d, fn))
    for fn, src in found.items():
        shutil.copy2(src, os.path.join(app, "python", fn))
    shutil.rmtree(tmp)
    return [d for d in MSVC_DLLS if d not in found]


def git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT).decode().strip()
    except Exception:  # noqa
        return "?"


def build(out_dir, with_tesseract=True, with_zip=True, with_msvc=True):
    cache = os.path.join(out_dir, "cache")
    app = os.path.join(out_dir, NAME)
    if os.path.isdir(app):
        shutil.rmtree(app)
    os.makedirs(app)

    py = os.path.join(app, "python")
    with zipfile.ZipFile(fetch(PY_URL, PY_SHA256, cache)) as z:
        z.extractall(py)
    with open(os.path.join(py, "python38._pth"), "w", newline="\r\n") as f:
        f.write(PTH)
    reqs = windows_requirements(read_lines(os.path.join(ROOT, "requirements.txt"))) + SELFTEST_REQS
    install_packages(os.path.join(py, "Lib", "site-packages"), reqs)

    if with_tesseract:
        extract_tesseract(fetch(TESS_URL, TESS_SHA256, cache), os.path.join(app, "tesseract"))

    missing_dlls = install_msvc_dlls(app) if with_msvc else []
    if missing_dlls:
        print("ВНИМАНИЕ: в колесе %s нет %s" % (MSVC_WHEEL, ", ".join(missing_dlls)))

    for src, rel in program_files() + check_files():
        dst = os.path.join(app, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    for name, text in BATS.items():
        with open(os.path.join(app, name), "wb") as f:
            f.write(bat_bytes(text))
    make_visit_files(app)
    with open(os.path.join(app, "build_info.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("commit: %s\npython: 3.8.10 embeddable amd64\ntesseract: %s\npackages:\n  %s\n"
                % (git_commit(), TESS_VERSION if with_tesseract else "нет", "\n  ".join(reqs)))

    if not with_zip:
        return app
    out = os.path.join(out_dir, ZIP_NAME)
    make_zip(app, out, NAME)
    print("Готово: %s (%.1f МБ)" % (out, os.path.getsize(out) / 1e6))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--no-tesseract", action="store_true", help="без Tesseract (проверка состава на Mac)")
    ap.add_argument("--no-msvc", action="store_true", help="без DLL Visual C++ рядом с python.exe")
    ap.add_argument("--no-zip", action="store_true", help="только папка, без архива")
    a = ap.parse_args()
    build(os.path.abspath(a.out), not a.no_tesseract, not a.no_zip, not a.no_msvc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
