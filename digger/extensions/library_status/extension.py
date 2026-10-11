# -*- coding: utf-8 -*-
"""Расширение «Состояние библиотеки» (шаг 9.5b): общая библиотека доступна / ждут N документов, «Перенести сейчас».
Работа с папками — `AcquireStore` (9.5a); сеть и диски — только в фоне."""
from __future__ import annotations

from digger.acquire.store import AcquireStore
from digger.extensions.api import Extension as BaseExtension


def status_text(st):
    """Состояние библиотеки → (строка, ждёт ли внимания)."""
    if st["available"]:
        head = "Библиотека доступна: %s" % st["library_dir"]
    else:
        head = "Библиотека недоступна: %s (%s)" % (st["library_dir"], st["error"] or "нет доступа")
    if st["pending"]:
        tail = "Ждут переноса: %d. Документы сохранены локально (%s) и ищутся как обычно." % (
            st["pending"], st["pending_dir"])
    else:
        tail = "Ожидающих документов нет."
    return head + "\n" + tail, (not st["available"]) or bool(st["pending"])


def refresh_and_flush(store, flush):
    """Переносит ожидающие документы (если просили) и возвращает свежее состояние и число перенесённых."""
    moved = store.flush_pending() if flush else 0
    st = store.library_status()
    st["moved"] = moved
    return st


class Extension(BaseExtension):
    def setup(self, services):
        self.services = services
        self.view = None

    def store(self):
        return AcquireStore(self.services.ctx.module("local_db"))

    def contribute(self, ui):
        ui.add_tab("Библиотека", self.make_tab)

    def make_tab(self):
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
        ext = self
        w = QWidget()
        w.info = QLabel("Проверяю библиотеку…")
        w.info.setWordWrap(True)
        w.info.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        w.b_refresh = QPushButton("Проверить")
        w.b_flush = QPushButton("Перенести сейчас")
        w.b_flush.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(w.b_refresh)
        row.addWidget(w.b_flush)
        row.addStretch()
        lay = QVBoxLayout(w)
        lay.addWidget(w.info)
        lay.addLayout(row)
        lay.addStretch()

        def done(st):
            text, _ = status_text(st)
            if st.get("moved"):
                text += "\nПеренесено сейчас: %d." % st["moved"]
            w.info.setText(text)
            w.b_flush.setEnabled(bool(st["pending"]))
            w.b_refresh.setEnabled(True)

        def run(flush):
            w.b_refresh.setEnabled(False)
            w.b_flush.setEnabled(False)
            w.info.setText("Работаю с библиотекой…")
            store = ext.store()
            ext.services.run_in_background(lambda: refresh_and_flush(store, flush), done)

        w.b_refresh.clicked.connect(lambda: run(False))
        w.b_flush.clicked.connect(lambda: run(True))
        w.check = lambda: run(False)
        self.view = w
        run(False)
        return w
