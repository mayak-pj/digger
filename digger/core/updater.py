# -*- coding: utf-8 -*-
"""Обновление портативной сборки с общей папки (шаг 9.5c, ARCHITECTURE §9).

В общей папке (`update.share_dir`) лежат `version.json` и архив сборки (`tools/publish_update.py`). Программа сверяет
версию, копирует архив в `updates/`, проверяет sha256 и распаковывает; замену делает `.bat`, который запускается
после выхода программы и копирует новые файлы поверх, не трогая `config.json`, библиотеку, карантин, базу и журналы.
Здесь нет Qt и нет сети: общая папка — обычный путь (в том числе UNC). Права администратора не нужны.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from typing import Any, Dict, Tuple

MANIFEST = "version.json"
ROOT_IN_ZIP = "Digger"


# данные и плагины копируются поверх (без удаления); библиотека, карантин и база остаются как есть
_Q = ' /R:20 /W:1 /NFL /NDL /NJH /NJS'
DATA_STEPS = {
    "data": 'robocopy "%(src)s\\data" "%(app)s\\data" /E' + _Q + ' /XD library quarantine /XF *.sqlite* >> "%(log)s"\r\n'
            'if errorlevel 8 set FAIL=1\r\n',
    "plugins": 'robocopy "%(src)s\\plugins" "%(app)s\\plugins" /E' + _Q + ' >> "%(log)s"\r\n'
               'if errorlevel 8 set FAIL=1\r\n',
}


class UpdateError(Exception):
    pass


def parse_version(text: Any) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(text).strip().split("."))
    except ValueError:
        return ()


def is_newer(remote: Any, local: Any) -> bool:
    r, loc = parse_version(remote), parse_version(local)
    return bool(r) and bool(loc) and r > loc


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(share_dir: str, zip_name: str, version: str, commit: str = "") -> Dict[str, Any]:
    """Пишет `version.json` рядом с архивом (его вызывает `tools/publish_update.py`)."""
    manifest = {"version": version, "zip": zip_name, "sha256": _sha256(os.path.join(share_dir, zip_name)),
                "commit": commit}
    tmp = os.path.join(share_dir, MANIFEST + ".part")
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(share_dir, MANIFEST))
    return manifest


def check(share_dir: str, local_version: str) -> Dict[str, Any]:
    """{'state': off | unreachable | broken | current | new, 'manifest', 'error'}. Может быть медленным — в фоне."""
    if not share_dir:
        return {"state": "off", "manifest": None, "error": ""}
    path = os.path.join(share_dir, MANIFEST)
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except OSError as e:
        return {"state": "unreachable", "manifest": None, "error": str(e)}
    except ValueError as e:
        return {"state": "broken", "manifest": None, "error": "version.json не читается: %s" % e}
    if not isinstance(manifest, dict) or not parse_version(manifest.get("version")) or not manifest.get("zip") \
            or len(str(manifest.get("sha256", ""))) != 64:
        return {"state": "broken", "manifest": None, "error": "в version.json нет версии, архива или sha256"}
    state = "new" if is_newer(manifest["version"], local_version) else "current"
    return {"state": state, "manifest": manifest, "error": ""}


def download(share_dir: str, manifest: Dict[str, Any], updates_dir: str) -> str:
    """Копирует архив, проверяет sha256, безопасно распаковывает; возвращает папку с новой сборкой."""
    name = str(manifest["zip"])
    if name != os.path.basename(name.replace("\\", "/")) or name in ("", ".", ".."):
        raise UpdateError("имя архива в version.json указывает за пределы общей папки")
    os.makedirs(updates_dir, exist_ok=True)
    local = os.path.join(updates_dir, name)
    tmp = local + ".part"
    try:
        shutil.copyfile(os.path.join(share_dir, name), tmp)
        if _sha256(tmp) != str(manifest["sha256"]).lower():
            raise UpdateError("контрольная сумма архива не совпала: файл повреждён или заменён")
        os.replace(tmp, local)
    except OSError as e:
        raise UpdateError("архив не скопирован: %s" % e)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    target = os.path.join(updates_dir, str(manifest["version"]))
    if os.path.isdir(target):
        shutil.rmtree(target)
    try:
        _extract(local, target)
    except (zipfile.BadZipFile, OSError, UpdateError) as e:
        shutil.rmtree(target, ignore_errors=True)
        raise e if isinstance(e, UpdateError) else UpdateError("архив не распакован: %s" % e)
    staged = os.path.join(target, ROOT_IN_ZIP)
    if not (os.path.isfile(os.path.join(staged, "run.py")) and os.path.isdir(os.path.join(staged, "digger"))):
        shutil.rmtree(target, ignore_errors=True)
        raise UpdateError("в архиве нет программы (run.py и папки digger)")
    return staged


def _extract(zip_path: str, target: str) -> None:
    root = os.path.realpath(target)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            dst = os.path.realpath(os.path.join(root, *info.filename.replace("\\", "/").split("/")))
            if dst != root and not dst.startswith(root + os.sep):
                raise UpdateError("в архиве путь за пределами папки: %s" % info.filename)
        z.extractall(root)


def write_apply_script(app_dir: str, staged: str, pid: int) -> str:
    """`.bat`, который ждёт выхода программы, копирует новые файлы поверх и запускает программу заново."""
    app, src = app_dir.rstrip("\\/"), staged.rstrip("\\/")
    log = os.path.join(app, "updates", "обновление.txt")
    mirror = "".join('robocopy "%s\\%s" "%s\\%s" /MIR /R:20 /W:1 /NFL /NDL /NJH /NJS >> "%s"\r\n'
                     % (src, d, app, d, log) + 'if errorlevel 8 set FAIL=1\r\n'
                     for d in ("python", "tesseract", "digger", "tests", "checks")
                     if os.path.isdir(os.path.join(src, d)))          # в сборке без Tesseract нет папки tesseract
    subst = {"pid": pid, "src": src, "app": app, "log": log}
    data = "".join(DATA_STEPS[d] for d in ("data", "plugins") if os.path.isdir(os.path.join(src, d))) % subst
    text = ('@echo off\r\n'
            'rem Обновление Digger: заменяет программу, не трогая данные пользователя\r\n'
            'set FAIL=\r\n'
            'echo Ждём закрытия программы...\r\n'
            ':wait\r\n'
            'tasklist /FI "PID eq %(pid)d" 2>nul | find "%(pid)d" >nul && (ping -n 2 127.0.0.1 >nul & goto wait)\r\n'
            + mirror.replace('%', '%%') +
            # данные: копируем поверх, но не трогаем библиотеку, карантин и базу
            data.replace('%', '%%') +
            # файлы в корне (run.py, .bat, README…); config.json пользователя в сборке не лежит и не затрагивается
            'robocopy "%(src)s" "%(app)s" /R:20 /W:1 /NFL /NDL /NJH /NJS /XF config.json >> "%(log)s"\r\n'
            'if errorlevel 8 set FAIL=1\r\n'
            'if defined FAIL (echo ОБНОВЛЕНИЕ НЕ ЗАВЕРШЕНО, см. %(log)s & pause & exit /b 1)\r\n'
            'echo Обновление выполнено > "%(app)s\\updates\\готово.txt"\r\n'
            'cd /d "%(app)s"\r\n'
            'start "" "%(app)s\\Digger.bat"\r\n') % subst
    path = os.path.join(app, "updates", "применить_обновление.bat")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text.encode("cp866", "replace"))
    return path
