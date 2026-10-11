# -*- coding: utf-8 -*-
"""Панель заключения «Не найдено» (ARCHITECTURE §4.11; шаг 7.7): сайты по разделам и три кнопки над выбранным.

Данные — `acquire.conclusion.Conclusion`; панель только показывает и сообщает, что нажали (сигналы с `SiteNote`)."""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableView,
                             QVBoxLayout, QWidget)

COLUMNS = ("Раздел", "Сайт", "Причина", "Ссылка")
MAX_ROWS, MIN_ROWS = 6, 2             # строк таблицы видно без прокрутки: когда места хватает и когда окно тесное


class ConclusionPanel(QWidget):
    open_browser = pyqtSignal(object)     # SiteNote
    add_request = pyqtSignal(object)
    allow = pyqtSignal(object)

    def __init__(self, theme, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("conclusionPanel")
        self.conclusion = None
        self.model = QStandardItemModel(0, len(COLUMNS), self)
        self.model.setHorizontalHeaderLabels(list(COLUMNS))
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.b_open = QPushButton(theme.icon("external-link"), "Открыть в браузере")
        self.b_add = QPushButton(theme.icon("wifi-off"), "Добавить в запрос администраторам")
        self.b_allow = QPushButton(theme.icon("globe"), "Разрешить домен")
        row = QHBoxLayout()
        for b in (self.b_open, self.b_add, self.b_allow):
            row.addWidget(b)
        row.addStretch()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 4)
        for w in (self.title, self.table, self.hint):
            lay.addWidget(w)
        lay.addLayout(row)
        self.b_open.clicked.connect(lambda: self._emit(self.open_browser))
        self.b_add.clicked.connect(lambda: self._emit(self.add_request))
        self.b_allow.clicked.connect(lambda: self._emit(self.allow))
        self.table.selectionModel().currentRowChanged.connect(lambda *_: self._buttons())
        self.set_conclusion(None)

    def preferred_height(self) -> int:
        """Высота панели, при которой таблица видна целиком (до `MAX_ROWS` строк)."""
        return self.minimumSizeHint().height() - self.table.minimumHeight() + self.table.maximumHeight()

    def note(self):
        item = self.model.item(self.table.currentIndex().row(), 0) if self.table.currentIndex().isValid() else None
        return item.data(Qt.UserRole) if item is not None else None

    def _emit(self, signal) -> None:
        n = self.note()
        if n is not None:
            signal.emit(n)

    def _buttons(self) -> None:
        """«Открыть» — когда есть ссылка (закрытый сетью сайт не открыть); «Разрешить» — только для сайтов вне
        белого списка; «В запрос» — для любого выбранного сайта."""
        n = self.note()
        kind = self.model.item(self.table.currentIndex().row(), 0).data(Qt.UserRole + 1) if n else ""
        self.b_open.setEnabled(bool(n and n.url and kind != "blocked"))
        self.b_add.setEnabled(bool(n))
        self.b_allow.setEnabled(kind == "not_whitelisted")

    def set_conclusion(self, c) -> None:
        self.conclusion = c
        self.model.removeRows(0, self.model.rowCount())
        self.setVisible(c is not None)
        if c is None:
            return
        self.title.setText(c.text().split("\n")[0])
        self.hint.setText(c.hint)
        for name, title, n in c.rows():
            cells = [QStandardItem(t) for t in (title.split(" (")[0], n.site,
                                                 "" if name == "not_whitelisted" else n.reason_text,
                                                 "" if name == "blocked" else n.url)]
            cells[0].setData(n, Qt.UserRole)
            cells[0].setData(name, Qt.UserRole + 1)
            self.model.appendRow(cells)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        head, line = self.table.horizontalHeader().height(), self.table.verticalHeader().defaultSectionSize()
        rows = min(self.model.rowCount(), MAX_ROWS)   # таблица по высоте строк; в тесном окне сжимается до MIN_ROWS
        self.table.setMaximumHeight(head + rows * line + 4)
        self.table.setMinimumHeight(head + min(rows, MIN_ROWS) * line + 4)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setVisible(self.model.rowCount() > 0)
        if self.model.rowCount():
            self.table.selectRow(0)
        self._buttons()
