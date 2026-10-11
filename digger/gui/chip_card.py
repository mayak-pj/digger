# -*- coding: utf-8 -*-
"""Карточка чипа (ARCHITECTURE §5): заголовок (партномер, метка, корпус), фото под размер окна, поля маркировки.

Здесь только вид: данные и действия — в `MainWindow`. Цвета и размеры — из действующей темы.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QEvent, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QFontMetrics, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                             QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from ..ui import theme as ui_theme
from ..ui.theme.tokens import SPACE
from .models import PHOTO_STATES, STATE_TIPS
from .photo_list import draw_tag

PACKAGES = ["", "SOP-8", "SOIC-8", "DIP-8", "TSSOP-8", "MSOP-8", "SOT-23-5", "SOT-23-6", "DFN-8", "WSON-8",
            "SOP-14", "SOP-16", "DIP-14", "DIP-16", "DIP-28", "DIP-40", "SSOP-20", "TSSOP-20", "SSOP-28",
            "QFN-20", "QFN-24", "QFN-32", "QFN-48", "LQFP-32", "LQFP-48", "LQFP-64", "LQFP-100", "LQFP-144",
            "TQFP-32", "TQFP-44", "PLCC-32", "PLCC-44", "TSOP-48", "BGA"]
PHOTO_PAD = SPACE["xs"]            # поле между рамкой и фото
FIELDS_WIDTH = (340, 620)          # колонка полей: растёт с окном до этого предела
PHOTO_ASPECT = 1.5                 # область фото не шире полутора своих высот: без пустых полей по бокам снимка
PHOTO_MIN_WIDTH = 280
MARKING_HEIGHT = 64                # три строки маркировки
TITLE_MAX = 48                     # длиннее — имя файла в заголовке сокращается посередине
NO_PHOTO = u"Фото не выбрано"


class PhotoView(QWidget):
    """Фото или вариант обработки: вписывается в отведённое место при любом размере окна."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("imagePreview")
        self.setMinimumSize(240, 160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._image = None           # исходная картинка; под размер окна подгоняется копия
        self._text = u"—"
        self._scaled = None
        self._scaled_for = None

    def setImage(self, image: QImage) -> None:
        self._image, self._text, self._scaled = image, "", None
        self.update()

    def setText(self, text: str) -> None:
        self._image, self._text, self._scaled = None, text, None
        self.update()

    def text(self) -> str:
        return self._text

    def image(self) -> Optional[QImage]:
        """Исходная картинка; None — показан текст."""
        return self._image

    def pixmap(self) -> Optional[QPixmap]:
        """Картинка в том размере, в каком она показана; None — показан текст."""
        if self._image is None:
            return None
        ratio = self.devicePixelRatioF()
        size = QSize(max(1, self.width() - 2 * PHOTO_PAD), max(1, self.height() - 2 * PHOTO_PAD)) * ratio
        if self._scaled is None or self._scaled_for != size:
            self._scaled = QPixmap.fromImage(self._image.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._scaled.setDevicePixelRatio(ratio)
            self._scaled_for = size
        return self._scaled

    def paintEvent(self, e):
        theme = ui_theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(theme.qcolor("border"), 1))
        p.setBrush(theme.qcolor("bg"))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), theme.t["radius_sm"], theme.t["radius_sm"])
        pm = self.pixmap()
        if pm is None:
            p.setPen(theme.qcolor("text_muted"))
            p.drawText(self.rect(), Qt.AlignCenter, self._text)
            return
        w, h = pm.width() / pm.devicePixelRatio(), pm.height() / pm.devicePixelRatio()
        p.drawPixmap(QPointF((self.width() - w) / 2.0, (self.height() - h) / 2.0), pm)


class PhotoBox(QWidget):
    """Колонка «фото + список вариантов»: ширина ограничена высотой, лишняя ширина окна в неё не уходит."""

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.setMaximumWidth(max(PHOTO_MIN_WIDTH, int(self.height() * PHOTO_ASPECT)))


class StateTag(QWidget):
    """Цветная метка состояния — та же, что на карточке фото в списке (`PHOTO_STATES`)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._state = "new"
        self.hide()

    def set_state(self, state: str) -> None:
        self._state = state if state in PHOTO_STATES else "new"
        self.setVisible(bool(self.text()))
        self.updateGeometry()
        self.update()

    def state(self) -> str:
        return self._state

    def text(self) -> str:
        return PHOTO_STATES[self._state][0]

    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(int(ui_theme.current().t["font_small"]))
        return f

    def sizeHint(self):
        fm = QFontMetrics(self._font())
        return QSize(fm.horizontalAdvance(self.text()) + 2 * SPACE["sm"], fm.height() + 4)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_tag(p, QRectF(self.rect()), self.text(), ui_theme.current().qcolor(PHOTO_STATES[self._state][1]), self._font())


class ChipCard(QFrame):
    """Заголовок, фото с выбором варианта обработки и поля: маркировка, способ, партномер, корпус."""
    user_edit = pyqtSignal()         # пользователь сам правит поля или выбирает в списках (а не программа их заполняет)

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.setObjectName("chipCard")
        t = theme.t
        root = QVBoxLayout(self)
        root.setContentsMargins(t["space_md"], t["space_md"], t["space_md"], t["space_md"])
        root.setSpacing(t["space_sm"])

        head = QHBoxLayout()
        head.setSpacing(t["space_sm"])
        self.title = QLabel()
        self.title.setProperty("role", "title")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)     # партномер можно скопировать
        self.tag = StateTag()
        self.package = QLabel()
        self.package.setProperty("muted", True)
        head.addWidget(self.title)
        head.addWidget(self.tag)
        head.addWidget(self.package)
        head.addStretch(1)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(t["space_md"])
        self.photo_box = PhotoBox()
        photo = QVBoxLayout(self.photo_box)
        photo.setContentsMargins(0, 0, 0, 0)
        photo.setSpacing(t["space_sm"])
        self.img_label = PhotoView()
        self.variant_box = QComboBox()
        self.variant_box.setToolTip(u"Исходное фото или вариант обработки, по которому читалась маркировка")
        photo.addWidget(self.img_label, 1)
        photo.addWidget(self.variant_box)
        body.addWidget(self.photo_box, 3)

        self.fields = QWidget()
        self.fields.setMinimumWidth(FIELDS_WIDTH[0])
        self.fields.setMaximumWidth(FIELDS_WIDTH[1])
        col = QVBoxLayout(self.fields)
        col.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(t["space_sm"])
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        def row(*widgets):
            box = QHBoxLayout()
            box.setSpacing(t["space_sm"])
            for i, w in enumerate(widgets):
                box.addWidget(w, 1 if i == 0 else 0)
            return box
        self.marking = QPlainTextEdit()
        self.marking.setFixedHeight(MARKING_HEIGHT)
        self.marking.setPlaceholderText(u"Текст с корпуса, по строкам — можно исправить")
        self.b_reidentify = QPushButton(u"Пересчитать")
        self.b_reidentify.setToolTip(u"Определить партномер заново по этому тексту")
        r = row(self.marking, self.b_reidentify)
        r.setAlignment(self.b_reidentify, Qt.AlignTop)
        form.addRow(u"Маркировка:", r)
        self.ocr_mode = QComboBox()
        self.ocr_mode.setToolTip(u"Способ распознавания: «Авто» — по цепочке до уверенного результата; конкретный способ; "
                                 u"«Сравнить все» — все доступные способы, таблица в заключении")
        self.rerun = QPushButton(theme.icon("refresh-cw"), u"Заново")
        self.rerun.setToolTip(u"Распознать это фото ещё раз выбранным способом")
        form.addRow(u"Способ:", row(self.ocr_mode, self.rerun))
        self.part_box = QComboBox()
        self.part_box.setEditable(True)
        self.part_box.setInsertPolicy(QComboBox.NoInsert)
        self.b_part = QPushButton(u"Выбрать")
        self.b_part.setToolTip(u"Считать чип этим партномером: подобрать datasheet и заключение заново")
        form.addRow(u"Партномер:", row(self.part_box, self.b_part))
        self.pkg_box = QComboBox()
        self.pkg_box.setEditable(True)
        self.pkg_box.addItems(PACKAGES)
        self.pins = QSpinBox()
        self.pins.setRange(0, 2000)
        self.pins.setSpecialValueText("?")
        self.pins.setSuffix(u" выв.")                # подпись внутри поля: место в строке — списку корпусов
        self.pins.setToolTip(u"Число выводов («?» — неизвестно)")
        self.b_apply = QPushButton(u"Применить")
        self.b_apply.setToolTip(u"Сверить с datasheet заново с этим корпусом и числом выводов")
        form.addRow(u"Корпус:", row(self.pkg_box, self.pins, self.b_apply))
        self.b_web = QPushButton(theme.icon("search"), u"В интернете")
        self.b_web_all = QPushButton(theme.icon("globe"), u"Все сайты")
        self.b_next = QPushButton(theme.icon("skip-forward"), u"Далее")
        self.b_next.setToolTip(u"Перейти к следующему фото в списке")
        search = QHBoxLayout()           # кнопки равной ширины: подписи не обрезаются в узкой колонке
        search.setSpacing(t["space_sm"])
        for b in (self.b_web, self.b_web_all, self.b_next):
            search.addWidget(b, 1)
        form.addRow(u"Искать:", search)
        col.addLayout(form)
        col.addStretch(1)
        body.addWidget(self.fields, 2)
        body.addStretch(0)               # фото и поля дошли до своих пределов — остаток справа пустой, без рамки
        root.addLayout(body, 1)
        self.set_header("", "", "new")
        for box in (self.variant_box, self.part_box, self.pkg_box):
            box.activated.connect(lambda _i: self.user_edit.emit())
        for w in (self.marking, self.part_box, self.part_box.lineEdit(), self.pkg_box, self.pkg_box.lineEdit(),
                  self.pins, self.variant_box):
            w.installEventFilter(self)

    def eventFilter(self, obj, e):
        if e.type() in (QEvent.KeyPress, QEvent.Wheel):
            self.user_edit.emit()
        return False

    def set_header(self, name: str, part: str, state: str, package: str = "", pins: int = 0, tip: str = "") -> None:
        """Заголовок: партномер (пока его нет — имя файла), метка состояния, корпус и число выводов."""
        title = part or name or NO_PHOTO
        if len(title) > TITLE_MAX:
            half = (TITLE_MAX - 1) // 2
            title = title[:TITLE_MAX - 1 - half] + u"…" + title[-half:]
        self.title.setText(title)
        self.title.setToolTip(tip)
        self.tag.set_state(state)
        self.tag.setToolTip(STATE_TIPS.get(state, ""))
        self.package.setText(u" · ".join(x for x in (package, u"выводов: %d" % pins if pins else "") if x))
