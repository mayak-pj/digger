# -*- coding: utf-8 -*-
"""Запуск Digger: python run.py [фото ...]"""
import os
import sys


def app_dir():
    if getattr(sys, "frozen", False):          # собранный .exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def main():
    d = app_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    os.chdir(d)
    if sys.version_info < (3, 8):
        print("Нужен Python 3.8 или новее")
        return 1
    if "--selftest" in sys.argv:
        import pytest
        extra = sys.argv[sys.argv.index("--selftest") + 1:]     # например -vv --deselect=… (проверка selftest)
        named = any("::" in a for a in extra)                    # названы отдельные тесты — только они
        return int(pytest.main(["-q", "-m", "not live"] + extra + ([] if named else [os.path.join(d, "tests")])))
    from digger.gui.main_window import main as gui_main
    return gui_main(d)


if __name__ == "__main__":
    sys.exit(main())
