# -*- coding: utf-8 -*-
"""Шаг 9.5c: проверка обновления в окне."""
import pytest

pytest.importorskip("PyQt5")


def test_window_update_flow(window, monkeypatch, tmp_path):
    from PyQt5.QtWidgets import QMessageBox
    w, app = window
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    w.check_update()
    assert "не задана" in shown[-1]
    w._update_checked({"state": "current", "manifest": None, "error": ""}, "x", False)
    assert "последняя" in shown[-1]
    w._update_checked({"state": "unreachable", "manifest": None, "error": "сеть"}, "x", False)
    assert "недоступна" in shown[-1] and "сеть" in shown[-1]
    w._update_checked({"state": "unreachable", "manifest": None, "error": "сеть"}, "x", True)
    assert len(shown) == 3                                       # при запуске — молча
