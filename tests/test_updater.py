# -*- coding: utf-8 -*-
"""Шаг 9.5c: обновление портативной сборки с общей папки (без сети, без Windows)."""
import os
import zipfile

import pytest

from digger.core import updater


def make_zip(path, files, root="Digger"):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(root + "/" + name if root else name, data)


def publish(share, version="1.1.0", files=None, name="Digger_portable_win7_x64.zip"):
    os.makedirs(share, exist_ok=True)
    make_zip(os.path.join(share, name), files or {"run.py": b"print(1)", "digger/__init__.py": b"x"})
    return updater.write_manifest(share, name, version, "abc123")


def test_version_compare():
    assert updater.is_newer("1.0.1", "1.0.0") and updater.is_newer("1.10.0", "1.9.9")
    assert not updater.is_newer("1.0.0", "1.0.0") and not updater.is_newer("0.9", "1.0.0")
    assert not updater.is_newer("мусор", "1.0.0")


def test_check_disabled_and_missing(tmp_path):
    assert updater.check("", "1.0.0")["state"] == "off"
    r = updater.check(str(tmp_path / "нет такой"), "1.0.0")
    assert r["state"] == "unreachable" and r["error"]
    (tmp_path / "version.json").write_text("{не json", encoding="utf-8")
    assert updater.check(str(tmp_path), "1.0.0")["state"] == "broken"


def test_check_new_and_current(tmp_path):
    share = str(tmp_path / "Общая папка с пробелом")
    publish(share, "1.1.0")
    r = updater.check(share, "1.0.0")
    assert r["state"] == "new" and r["manifest"]["version"] == "1.1.0"
    assert updater.check(share, "1.1.0")["state"] == "current"


def test_download_verifies_and_extracts(tmp_path):
    share = str(tmp_path / "share")
    m = publish(share)
    staged = updater.download(share, m, str(tmp_path / "app" / "updates"))
    assert open(os.path.join(staged, "run.py"), "rb").read() == b"print(1)"
    assert not [n for n in os.listdir(os.path.dirname(staged)) if n.endswith(".part")]


def test_download_rejects_wrong_hash(tmp_path):
    share = str(tmp_path / "share")
    m = publish(share)
    m["sha256"] = "0" * 64
    with pytest.raises(updater.UpdateError):
        updater.download(share, m, str(tmp_path / "upd"))
    assert not os.path.exists(os.path.join(str(tmp_path / "upd"), "1.1.0"))


def test_download_rejects_path_escape(tmp_path):
    share = str(tmp_path / "share")
    os.makedirs(share)
    make_zip(os.path.join(share, "a.zip"), {"../evil.txt": b"x"})
    m = updater.write_manifest(share, "a.zip", "1.1.0", "c")
    with pytest.raises(updater.UpdateError):
        updater.download(share, m, str(tmp_path / "upd"))
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "upd" / "evil.txt").exists()


def test_download_rejects_package_without_program(tmp_path):
    share = str(tmp_path / "share")
    os.makedirs(share)
    make_zip(os.path.join(share, "a.zip"), {"readme.txt": b"x"})
    m = updater.write_manifest(share, "a.zip", "1.1.0", "c")
    with pytest.raises(updater.UpdateError):
        updater.download(share, m, str(tmp_path / "upd"))


def test_manifest_zip_name_cannot_leave_share(tmp_path):
    share = str(tmp_path / "share")
    m = publish(share)
    m["zip"] = "..\\..\\x.zip"
    with pytest.raises(updater.UpdateError):
        updater.download(share, m, str(tmp_path / "upd"))


def test_apply_script_keeps_user_data(tmp_path):
    app = str(tmp_path / "Рабочая папка" / "Digger")
    staged = str(tmp_path / "Рабочая папка" / "Digger" / "updates" / "1.1.0")
    for d in ("digger", "data", "plugins"):
        os.makedirs(os.path.join(staged, d))
    path = updater.write_apply_script(app, staged, pid=4321)
    text = open(path, "rb").read().decode("cp866")
    assert path.endswith(".bat") and "\r\n" in text
    for keep in ("config.json", "library", "quarantine", "*.sqlite*"):
        assert keep in text
    assert "robocopy" in text and app in text and staged in text and "4321" in text
    assert "/MIR" in text and "Digger.bat" in text and "%%" not in text
    assert "\\tesseract" not in text               # нет в новой сборке — не зеркалим (иначе robocopy даст ошибку)

