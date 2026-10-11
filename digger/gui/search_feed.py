# -*- coding: utf-8 -*-
"""Живая лента поиска (ARCHITECTURE §4.8, §5): строка состояния на языке поиска и история по уровням.

Сверху — метка языка EN/CH/RU, текст последнего события (сменяется плавно), счётчики, «Стоп»; под ней —
история (`FeedModel`): время, значок итога, язык, текст; русский перевод — в подсказке. События приходят
пачками из потока окна (`MainWindow.search_events`); здесь нет ни диска, ни сети.
"""
from __future__ import annotations

from typing import Dict, List

from PyQt5.QtCore import QRectF, QSize, Qt, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListView, QPushButton, QSizePolicy,
                             QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from ..acquire.events import LANG_LABELS, Event, render
from ..ui import theme as ui_theme
from ..ui.theme.tokens import SPACE
from .feed_model import COUNT_ROLE, KIND_ROLE, LANG_ROLE, STATE_ROLE, STATES, TEXT_ROLE, TIME_ROLE, FeedModel
from .photo_list import draw_tag

FADE_MS = 180             # плавная смена текста строки состояния
SLIDE = 6                 # новый текст выезжает снизу на столько точек
ROW_HEIGHT = 24
HISTORY_ROWS = 6          # высота истории — по числу строк, но не больше; остальное — прокруткой
ICON = 14
MUTED_STATES = ("empty", "info", "stale")


def _font(base: QFont, token: str, bold: bool = False) -> QFont:
    f = QFont(base)
    f.setPixelSize(int(ui_theme.current().t[token]))
    f.setBold(bold)
    return f


class LangBadge(QWidget):
    """Метка языка текущего поиска: EN · CH · RU. Ширина постоянная — текст рядом не прыгает."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._text = ""

    def setText(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self):
        fm = QFontMetrics(_font(self.font(), "font_small", True))
        return QSize(max(fm.horizontalAdvance(x) for x in LANG_LABELS.values()) + 2 * SPACE["sm"], fm.height() + 4)

    def paintEvent(self, e):
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_tag(p, QRectF(self.rect()), self._text, ui_theme.current().qcolor("accent"),
                 _font(self.font(), "font_small", True))


class FadeText(QWidget):
    """Одна строка текста; новый текст плавно сменяет прежний (прежний гаснет, новый проявляется и выезжает снизу)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)        # длинный текст сокращается, окно не раздвигает
        self._text = ""
        self._old = ""
        self._t = 1.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(FADE_MS)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._step)

    def setText(self, text: str) -> None:
        if text == self._text:
            return
        self._old, self._text = self._text, text
        self._anim.stop()
        if self.isVisible():
            self._t = 0.0
            self._anim.start()
        else:
            self._t = 1.0
        self.update()

    def text(self) -> str:
        return self._text

    def progress(self) -> float:
        """Доля смены текста: 0 — виден прежний, 1 — новый."""
        return self._t

    def animating(self) -> bool:
        return self._anim.state() == QVariantAnimation.Running

    def _step(self, value) -> None:
        self._t = float(value)
        self.update()

    def sizeHint(self):
        return QSize(200, QFontMetrics(_font(self.font(), "font_body")).height() + SLIDE)

    def minimumSizeHint(self):
        return QSize(40, self.sizeHint().height())

    def paintEvent(self, e):
        p = QPainter(self)
        font = _font(self.font(), "font_body")
        fm = QFontMetrics(font)
        p.setFont(font)
        p.setPen(ui_theme.current().qcolor("text"))
        for text, alpha, shift in ((self._old, 1.0 - self._t, -SLIDE * self._t), (self._text, self._t, SLIDE * (1.0 - self._t))):
            if text and alpha > 0.0:
                p.setOpacity(alpha)
                p.drawText(QRectF(0, shift, self.width(), self.height()), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(text, Qt.ElideRight, self.width()))


class FeedDelegate(QStyledItemDelegate):
    """Строка истории: время, значок итога, язык, текст; заголовок уровня — подпись и линия до края."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icons: Dict[tuple, object] = {}

    def sizeHint(self, option, index):
        return QSize(200, ROW_HEIGHT)

    def _icon(self, theme, state: str):
        key = (theme.name, state)
        if key not in self._icons:
            self._icons[key] = theme.pixmap(STATES[state][0], STATES[state][1], ICON)
        return self._icons[key]

    def paint(self, painter, option, index):
        theme = ui_theme.current()
        pad = SPACE["sm"]
        rect = QRectF(option.rect).adjusted(pad, 0, -pad, 0)
        small, small_bold, body = (_font(option.font, "font_small"), _font(option.font, "font_small", True),
                                   _font(option.font, "font_body"))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if index.data(KIND_ROLE) == "group":
            title = "%s  %d" % (index.data(), index.data(COUNT_ROLE) or 0)
            painter.setFont(small_bold)
            painter.setPen(theme.qcolor("text_muted"))
            painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, title)
            x = rect.left() + QFontMetrics(small_bold).horizontalAdvance(title) + pad
            if x < rect.right():
                painter.setPen(QPen(theme.qcolor("border"), 1))
                y = int(rect.center().y()) + 0.5
                painter.drawLine(int(x), int(y), int(rect.right()), int(y))
            painter.restore()
            return
        if option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(theme.qcolor("surface_alt"))
            painter.drawRoundedRect(QRectF(option.rect).adjusted(2, 1, -2, -1), theme.t["radius_sm"], theme.t["radius_sm"])
        state = index.data(STATE_ROLE) or "info"
        fm = QFontMetrics(small)
        x = rect.left()
        painter.setFont(small)
        painter.setPen(theme.qcolor("text_muted"))
        w = fm.horizontalAdvance("00:00:00")
        painter.drawText(QRectF(x, rect.top(), w, rect.height()), Qt.AlignLeft | Qt.AlignVCenter, index.data(TIME_ROLE) or "")
        x += w + pad
        painter.drawPixmap(int(x), int(rect.center().y() - ICON / 2.0), self._icon(theme, state))
        x += ICON + pad
        w = max(fm.horizontalAdvance(t) for t in LANG_LABELS.values())
        painter.drawText(QRectF(x, rect.top(), w, rect.height()), Qt.AlignLeft | Qt.AlignVCenter, index.data(LANG_ROLE) or "")
        x += w + pad
        painter.setFont(body)
        painter.setPen(theme.qcolor("text_muted" if state in MUTED_STATES else "text"))
        text = QFontMetrics(body).elidedText(index.data(TEXT_ROLE) or "", Qt.ElideRight, int(rect.right() - x))
        painter.drawText(QRectF(x, rect.top(), rect.right() - x, rect.height()), Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.restore()


class SearchFeed(QFrame):
    """Лента поиска под карточкой чипа. Скрыта, пока нет ни одного события."""
    stop_requested = pyqtSignal()

    def __init__(self, theme, parent=None, stop_button: bool = True):
        super().__init__(parent)
        self.setObjectName("searchFeed")
        t = theme.t
        self.model = FeedModel(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(t["space_md"], t["space_sm"], t["space_sm"], t["space_sm"])
        root.setSpacing(t["space_xs"])

        head = QHBoxLayout()
        head.setSpacing(t["space_sm"])
        self.badge = LangBadge()
        self.line = FadeText()
        self.counters = QLabel()
        self.counters.setProperty("muted", True)
        self.counters.setToolTip(u"Запросов к сайтам и поисковикам · найдено ссылок · скачано файлов · подтверждено документов")
        self.b_history = QPushButton(theme.icon("list-checks"), "")
        self.b_history.setProperty("iconOnly", True)
        self.b_history.setCheckable(True)
        self.b_history.setChecked(True)
        self.b_history.setToolTip(u"История поиска по уровням: показать или свернуть")
        self.b_history.toggled.connect(self._show_history)
        self.b_stop = QPushButton(theme.icon("square"), u"Стоп")
        self.b_stop.setToolTip(u"Остановить поиск")
        self.b_stop.clicked.connect(self.stop_requested)
        head.addWidget(self.badge)
        head.addWidget(self.line, 1)
        head.addWidget(self.counters)
        head.addWidget(self.b_history)
        if stop_button:                  # в главном окне «Стоп» один — на панели действий
            head.addWidget(self.b_stop)
        else:
            self.b_stop.setParent(self)
            self.b_stop.hide()
        root.addLayout(head)

        self.view = QListView()
        self.view.setObjectName("feedHistory")
        self.view.setModel(self.model)
        self.view.setItemDelegate(FeedDelegate(self.view))
        self.view.setUniformItemSizes(True)
        self.view.setSelectionMode(QAbstractItemView.NoSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFocusPolicy(Qt.NoFocus)
        self.view.setMouseTracking(True)                     # подсветка строки под мышью; подсказка — перевод
        root.addWidget(self.view)
        self.model.modelReset.connect(self._fit_history)
        self.set_running(False)
        self._show_counters()
        self._fit_history()
        self.hide()

    # ---------- из окна ----------
    def set_level_names(self, names: Dict[str, str]) -> None:
        self.model.set_level_names(names)

    def begin(self) -> None:
        """Новый поиск: история и счётчики — с чистого листа."""
        self.model.clear()
        self.badge.setText("")
        self.line.setText("")
        self.line.setToolTip("")
        self._show_counters()

    def add_events(self, events: List[Event]) -> None:
        """Пачка событий шины: строка состояния показывает последнее, история пополняется одним обновлением."""
        if not events:
            return
        bar = self.view.verticalScrollBar()
        reading = self.view.underMouse()                     # пользователь читает историю — не дёргать её
        pos = bar.value()
        row = self.model.add(events)
        if reading:
            bar.setValue(pos)
        elif row >= 0:
            self.view.scrollTo(self.model.index(row), QAbstractItemView.EnsureVisible)
        last = events[-1]
        self.badge.setText(LANG_LABELS.get(last.display_lang, last.display_lang.upper()[:4]))
        self.line.setText(render(last))
        self.line.setToolTip("" if last.display_lang == "ru" else render(last, "ru"))
        self._show_counters()
        if self.isHidden():
            self.show()

    def set_running(self, running: bool) -> None:
        self.b_stop.setEnabled(bool(running))
        if not running:
            self.model.finish()

    # ---------- своё ----------
    def _show_counters(self) -> None:
        self.counters.setText(self.model.counters.text())
        self.counters.setVisible(any(self.model.counters.counts.values()))     # одно распознавание — считать нечего

    def _show_history(self, on: bool) -> None:
        self._fit_history()

    def _fit_history(self) -> None:
        """Высота истории — по числу строк (до `HISTORY_ROWS`): короткая история не отнимает место у вкладок."""
        rows = self.model.rowCount()
        self.view.setFixedHeight(max(1, min(rows, HISTORY_ROWS)) * ROW_HEIGHT + 2)       # + рамка: без полосы прокрутки
        self.view.setVisible(self.b_history.isChecked() and rows > 0)
