# -*- coding: utf-8 -*-
"""Шаг 9.5b: расширение «Состояние библиотеки» — текст состояния и вкладка с кнопкой «Перенести сейчас»."""
import importlib.util
import os

import pytest

from digger.extensions.loader import BUILTIN_DIR

pytest.importorskip("PyQt5")


def _mod():
    spec = importlib.util.spec_from_file_location("ls_ext", os.path.join(BUILTIN_DIR, "library_status", "extension.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_status_text():
    m = _mod()
    ok = {"library_dir": "Z:\\lib", "available": True, "error": "", "pending": 0, "pending_dir": "p"}
    text, attention = m.status_text(ok)
    assert "доступна" in text and "нет" in text and not attention
    bad = dict(ok, available=False, error="путь не найден", pending=3)
    text, attention = m.status_text(bad)
    assert "недоступна" in text and "путь не найден" in text and "Ждут переноса: 3" in text and attention


def test_refresh_and_flush():
    m = _mod()

    class Store:
        moved = 0

        def flush_pending(self):
            self.moved = 2
            return 2

        def library_status(self):
            return {"available": True, "pending": 0}
    s = Store()
    assert m.refresh_and_flush(s, False)["moved"] == 0 and s.moved == 0
    assert m.refresh_and_flush(s, True)["moved"] == 2


def test_tab_in_window(window):
    w, app = window
    assert "Библиотека" in [w.tabs.tabText(i) for i in range(w.tabs.count())]
    view = w.ext.get("library_status").instance.view
    assert view.b_flush.text() == "Перенести сейчас" and view.info.text()
