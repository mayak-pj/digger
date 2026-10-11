# -*- coding: utf-8 -*-
"""Кладёт собранную портативную сборку на общую папку для обновления рабочих мест.

    python tools/publish_update.py dist/Digger_portable_win7_x64.zip \\\\server\\share\\digger_update
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from digger import __version__          # noqa: E402
from digger.core import updater          # noqa: E402


def publish(zip_path, share_dir):
    os.makedirs(share_dir, exist_ok=True)
    name = os.path.basename(zip_path)
    tmp = os.path.join(share_dir, name + ".part")
    shutil.copyfile(zip_path, tmp)
    os.replace(tmp, os.path.join(share_dir, name))      # сначала архив, потом version.json: никто не увидит половину
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], universal_newlines=True).strip()
    except Exception:  # noqa
        commit = ""
    return updater.write_manifest(share_dir, name, __version__, commit)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(publish(sys.argv[1], sys.argv[2]))
