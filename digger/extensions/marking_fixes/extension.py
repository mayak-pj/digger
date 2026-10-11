# -*- coding: utf-8 -*-
"""Расширение «Замены маркировки» (шаг 8.3g): словарь «прочитано → заменять на» для распознанной маркировки.

Пополняется ручными исправлениями (слово, которое пользователь заменил в поле маркировки) и вручную на вкладке;
применяется ко всем фото — к новым при распознавании, к уже распознанным сразу после исправления.
Заменяется только слово целиком. Не запоминаются: слова короче MIN_LEN, слова без букв (код даты, партия) и слова,
которые справочник знает как микросхему, — такое исправление относится к одному чипу, а не к ошибке чтения.
"""
from __future__ import annotations

import difflib
import re
import threading
import time

from digger.extensions.api import Extension as BaseExtension

MIN_LEN = 3
COLUMNS = ("Прочитано", "Заменять на", "Применено", "Откуда", "Добавлено")
MIGRATIONS = ["CREATE TABLE {prefix}fixes (wrong TEXT PRIMARY KEY, fixed TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '', "
              "added_at REAL NOT NULL DEFAULT 0, uses INTEGER NOT NULL DEFAULT 0);"]
MANUAL, LEARNED = "вручную", "из исправления"
HINT = ("Слово слева заменяется словом справа в маркировке каждого фото (только слово целиком). Словарь пополняется "
        "сам, когда вы исправляете маркировку в карточке чипа; лишнюю замену удалите здесь.")


def token_pairs(before, after):
    """Слова, заменённые при исправлении: пары (было, стало). Дописанные и удалённые слова — не замены."""
    a, b = before.split(), after.split()
    pairs = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "replace" and i2 - i1 == j2 - j1:
            pairs += zip(a[i1:i2], b[j1:j2])
    return pairs


def apply_fixes(text, fixes):
    """Заменяет слова по словарю, не трогая пробелы и переводы строк. Возвращает (текст, применённые слова)."""
    used = []

    def swap(m):
        word = m.group(0)
        if word in fixes:
            used.append(word)
            return fixes[word]
        return word
    return re.sub(r"\S+", swap, text), used


class Extension(BaseExtension):
    def setup(self, services):
        self.services = services
        self.view = None
        self.dirty = False
        self._lock = threading.RLock()
        self._fixes = None               # словарь в памяти; читается из базы при первом обращении (в фоне)
        self.db = services.db()
        self.db.migrate(MIGRATIONS)
        services.add_marking_filter(self.fix)
        services.on_marking_corrected(self.learn)

    # -------------------- словарь --------------------
    def fixes(self):
        with self._lock:
            if self._fixes is None:
                self._fixes = dict(self.db.execute("SELECT wrong, fixed FROM {prefix}fixes"))
            return self._fixes

    def entries(self):
        rows = self.db.execute("SELECT wrong, fixed, uses, origin, added_at FROM {prefix}fixes ORDER BY added_at, wrong")
        return [dict(zip(("wrong", "fixed", "uses", "origin", "added_at"), row)) for row in rows]

    def add(self, wrong, fixed, origin=MANUAL):
        """Добавляет замену. Возвращает пустую строку или причину отказа."""
        wrong, fixed = wrong.strip(), fixed.strip()
        if not wrong or not fixed or wrong == fixed:
            return "Нужны два разных слова: что прочитано и на что заменять."
        if len(wrong.split()) > 1 or len(fixed.split()) > 1:
            return "Замена действует на одно слово: без пробелов."
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO {prefix}fixes (wrong, fixed, origin, added_at) VALUES (?, ?, ?, ?)",
                            (wrong, fixed, origin, time.time()))
            self.db.execute("DELETE FROM {prefix}fixes WHERE wrong = ?", (fixed,))   # иначе замены пойдут по кругу
            self._fixes = None
        self.dirty = True
        return ""

    def remove(self, wrong):
        with self._lock:
            self.db.execute("DELETE FROM {prefix}fixes WHERE wrong = ?", (wrong,))
            self._fixes = None
        self.dirty = True

    def is_known_part(self, word):
        ident = getattr(self.services.ctx, "modules", {}).get("identifier")
        if ident is None:
            return False
        return any(c.confirmed_by and c.part.upper() == word.upper() for c in ident.identify(word, []))

    # -------------------- крючки конвейера (фоновый поток) --------------------
    def fix(self, text):
        fixed, used = apply_fixes(text, self.fixes())
        for word in used:
            self.db.execute("UPDATE {prefix}fixes SET uses = uses + 1 WHERE wrong = ?", (word,))
        if used:
            self.dirty = True
        return fixed

    def learn(self, before, after):
        notes = []
        for wrong, fixed in token_pairs(before, after):
            if self.fixes().get(fixed) == wrong:         # пользователь вернул слово, которое заменил словарь
                self.remove(fixed)
                notes.append("Замена %s → %s удалена из словаря: вы вернули прежнее слово." % (fixed, wrong))
            elif len(wrong) < MIN_LEN or not any(ch.isalpha() for ch in wrong):
                continue
            elif self.is_known_part(wrong):
                notes.append("Замена %s → %s не запомнена: %s — известная микросхема, исправление действует только "
                             "на это фото." % (wrong, fixed, wrong))
            elif not self.add(wrong, fixed, LEARNED):
                notes.append("Запомнена замена %s → %s: применяется ко всем фото (вкладка «Замены маркировки»)."
                             % (wrong, fixed))
        return "\n".join(notes)

    # -------------------- вкладка --------------------
    def contribute(self, ui):
        ui.add_tab("Замены маркировки", self.make_tab)

    def make_tab(self):
        from PyQt5.QtCore import QTimer, Qt
        from PyQt5.QtGui import QStandardItem, QStandardItemModel
        from PyQt5.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                                     QTableView, QVBoxLayout, QWidget)
        ext = self
        w = QWidget()
        w.model = QStandardItemModel(0, len(COLUMNS), w)
        w.model.setHorizontalHeaderLabels(list(COLUMNS))
        w.table = QTableView()
        w.table.setModel(w.model)
        w.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        w.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        w.table.setSelectionMode(QAbstractItemView.SingleSelection)
        w.table.verticalHeader().hide()
        w.info = QLabel(HINT)
        w.info.setWordWrap(True)
        w.status = QLabel()
        w.wrong, w.fixed = QLineEdit(), QLineEdit()
        w.wrong.setPlaceholderText("Прочитано, например ATMFL")
        w.fixed.setPlaceholderText("Заменять на, например ATMEL")
        w.b_add = QPushButton("Добавить")
        w.b_del = QPushButton("Удалить выбранную")
        w.b_refresh = QPushButton("Обновить")
        row = QHBoxLayout()
        for item in (w.wrong, QLabel("→"), w.fixed, w.b_add, w.b_del, w.b_refresh):
            row.addWidget(item)
        row.addWidget(w.status, 1)
        for edit in (w.wrong, w.fixed):
            edit.setMinimumWidth(230)
        lay = QVBoxLayout(w)
        lay.addWidget(w.info)
        lay.addLayout(row)
        lay.addWidget(w.table, 1)

        def done(entries):
            w.model.removeRows(0, w.model.rowCount())
            for e in entries or []:
                added = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["added_at"]))
                items = [QStandardItem(c) for c in (e["wrong"], e["fixed"], str(e["uses"]), e["origin"], added)]
                items[2].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                w.model.appendRow(items)
            w.table.resizeColumnsToContents()
            w.table.horizontalHeader().setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.Stretch)
            w.b_del.setEnabled(bool(entries))
            if not entries:
                w.status.setText("Словарь пуст.")
            elif w.status.text().startswith("Словарь пуст"):
                w.status.setText("")

        def load():
            ext.dirty = False
            ext.services.run_in_background(ext.entries, done)

        def add():
            wrong, fixed = w.wrong.text(), w.fixed.text()

            def added(error):
                w.status.setText(error or "Добавлено: %s → %s" % (wrong.strip(), fixed.strip()))
                if not error:
                    w.wrong.clear()
                    w.fixed.clear()
                load()
            ext.services.run_in_background(lambda: ext.add(wrong, fixed), added)

        def delete():
            rows = w.table.selectionModel().selectedRows()
            if not rows:
                w.status.setText("Выберите строку таблицы.")
                return
            wrong = w.model.item(rows[0].row(), 0).text()
            w.status.setText("Удалено: %s" % wrong)
            ext.services.run_in_background(lambda: ext.remove(wrong), lambda _r: load())

        w.b_add.clicked.connect(add)
        w.fixed.returnPressed.connect(add)
        w.b_del.clicked.connect(delete)
        w.b_refresh.clicked.connect(load)
        w.timer = QTimer(w)
        w.timer.timeout.connect(lambda: load() if ext.dirty and w.isVisible() else None)
        w.timer.start(1500)
        w.reload = load
        self.view = w
        load()
        return w
