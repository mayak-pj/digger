# -*- coding: utf-8 -*-
"""Модели списка фото и таблицы документов, чтение картинок для фоновых потоков (ARCHITECTURE §5).

Функции `scan_images` и `load_qimage` обращаются к диску (в том числе сетевому) — вызывать только из фона.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PyQt5.QtCore import QAbstractListModel, QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtGui import QBrush, QIcon, QImage, QPixmap

from ..core.utils import imread

IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
LEVEL_RU = {"local": "База", "catalog": "Каталог", "maker": "Производитель", "china": "Китай",
            "forum": "Форум", "marking": "SMD-код", "github": "GitHub"}
THUMB = 72
NAME_ROLE, PART_ROLE, STATE_ROLE = Qt.UserRole + 1, Qt.UserRole + 2, Qt.UserRole + 3
# состояние фото в списке: подпись метки на карточке и её цвет (имя токена темы)
PHOTO_STATES = {"new": (u"", "text_muted"),
                "busy": (u"распознаю…", "accent"),
                "search": (u"ищу…", "accent"),
                "memory": (u"память", "danger"),
                "no_memory": (u"без памяти", "success"),
                "unknown": (u"память: ?", "text_muted"),
                "not_found": (u"не найдено", "warning"),
                "unread": (u"не распознано", "warning")}
THUMB_CACHE = 4000        # столько миниатюр помним, считая убранные из списка фото


# ---------- диск: только из фоновых потоков ----------

def is_photo_name(name: str) -> bool:
    """Имя файла-картинки. Файлы с точкой в начале (`._фото.JPG` — спутники с флешки из macOS) — не фото."""
    return not name.startswith(".") and name.lower().endswith(IMG_EXT)


def scan_images(paths: Iterable[str], cancel: Any = None) -> Tuple[List[str], List[str]]:
    """Файлы-картинки из списка файлов и папок: (найденные пути, недоступные пути)."""
    found: List[str] = []
    errors: List[str] = []
    for p in paths:
        if cancel is not None and cancel.cancelled:
            break
        try:
            if os.path.isdir(p):
                found += [os.path.normpath(os.path.join(p, fn)) for fn in sorted(os.listdir(p))
                          if is_photo_name(fn)]
            elif is_photo_name(os.path.basename(p)):
                if os.path.isfile(p):
                    found.append(os.path.normpath(p))
                else:
                    errors.append(p)
        except OSError:               # сетевой диск отвалился
            errors.append(p)
    return found, errors


def np_to_qimage(img) -> QImage:
    import cv2
    if img.ndim == 2:
        h, w = img.shape
        return QImage(img.data, w, h, img.strides[0], QImage.Format_Grayscale8).copy()
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888).copy()


def load_qimage(path: str, max_w: int, max_h: int) -> Optional[QImage]:
    """Картинка с диска, уменьшенная до max_w × max_h; None — не читается. QImage можно строить вне потока окна."""
    import cv2
    try:
        img = imread(path)
    except Exception:  # noqa — нет файла, не картинка, битые данные
        return None
    h, w = img.shape[:2]
    k = min(float(max_w) / w, float(max_h) / h, 1.0)
    if k < 1.0:
        img = cv2.resize(img, (max(1, int(round(w * k))), max(1, int(round(h * k)))), interpolation=cv2.INTER_AREA)
    return np_to_qimage(img)


# ---------- список фото ----------

class PhotoListModel(QAbstractListModel):
    """Фото в списке: имя, партномер, состояние (`PHOTO_STATES`) и миниатюра.

    Миниатюры приходят позже, пачками, и кэшируются. `DisplayRole` — то же текстом: «имя\nпартномер — метка».
    """

    def __init__(self, placeholder: Optional[QIcon] = None, parent=None):
        super().__init__(parent)
        self._paths: List[str] = []
        self._rows: Dict[str, int] = {}
        self._status: Dict[str, Tuple[str, str]] = {}     # путь → (состояние, партномер)
        self._thumbs: Dict[str, Optional[QIcon]] = {}     # None — фото не читается
        self._placeholder = placeholder or QIcon()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._paths)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._paths):
            return None
        path = self._paths[index.row()]
        state, part = self.status(path)
        if role == Qt.DisplayRole:
            tail = u" — ".join(x for x in (part, PHOTO_STATES[state][0]) if x)
            return os.path.basename(path) + (u"\n" + tail if tail else u"")
        if role == Qt.DecorationRole:
            return self._thumbs.get(path) or self._placeholder
        if role == NAME_ROLE:
            return os.path.basename(path)
        if role == PART_ROLE:
            return part
        if role == STATE_ROLE:
            return state
        if role in (Qt.UserRole, Qt.ToolTipRole):
            return path
        return None

    def paths(self) -> List[str]:
        return list(self._paths)

    def path(self, row: int) -> Optional[str]:
        return self._paths[row] if 0 <= row < len(self._paths) else None

    def row(self, path: str) -> int:
        return self._rows.get(path, -1)

    def add(self, paths: Iterable[str]) -> List[str]:
        """Добавляет новые пути одной вставкой; возвращает то, что действительно добавлено."""
        new: List[str] = []
        seen = set(self._rows)
        for p in paths:
            if p not in seen:
                seen.add(p)
                new.append(p)
        if new:
            first = len(self._paths)
            self.beginInsertRows(QModelIndex(), first, first + len(new) - 1)
            self._paths += new
            self._rows.update((p, first + i) for i, p in enumerate(new))
            self.endInsertRows()
        return new

    def remove(self, paths: Iterable[str]) -> None:
        for row in sorted({self._rows[p] for p in paths if p in self._rows}, reverse=True):
            self.beginRemoveRows(QModelIndex(), row, row)
            self._status.pop(self._paths.pop(row), None)
            self.endRemoveRows()
        self._rows = {p: i for i, p in enumerate(self._paths)}

    def status(self, path: str) -> Tuple[str, str]:
        return self._status.get(path, ("new", ""))

    def set_status(self, path: str, state: str, part: Optional[str] = None) -> None:
        """Состояние фото (ключ `PHOTO_STATES`) и партномер; `part=None` — партномер прежний."""
        if state not in PHOTO_STATES:
            raise KeyError(state)
        new = (state, self.status(path)[1] if part is None else part)
        if path in self._rows and new != self.status(path):
            self._status[path] = new
            idx = self.index(self._rows[path])
            self.dataChanged.emit(idx, idx, [Qt.DisplayRole, PART_ROLE, STATE_ROLE])

    def without_thumbs(self, paths: Iterable[str]) -> List[str]:
        return [p for p in paths if p not in self._thumbs]

    def set_thumbs(self, thumbs: Iterable[Tuple[str, Optional[QImage]]]) -> None:
        """Пачка миниатюр из фона: (путь, QImage или None). Одно обновление вида на пачку."""
        rows = []
        for path, image in thumbs:
            self._thumbs[path] = QIcon(QPixmap.fromImage(image)) if image is not None else None
            if path in self._rows:
                rows.append(self._rows[path])
        if len(self._thumbs) > THUMB_CACHE:
            self._thumbs = {p: t for p, t in self._thumbs.items() if p in self._rows}
        if rows:
            self.dataChanged.emit(self.index(min(rows)), self.index(max(rows)), [Qt.DecorationRole])


# ---------- таблица документов ----------

class HitsModel(QAbstractTableModel):
    """Найденные документы. `colors`: `current` — фон выбранного datasheet, `blocked` — текст «вне белого списка»."""
    HEADERS = ["Где", "Источник", "Название", "Оценка", "PDF", "Адрес", "Примечание"]

    def __init__(self, colors: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._colors = colors
        self._hits: List[Any] = []
        self._current = ""

    def set_hits(self, hits: Iterable[Any], datasheet_path: str = "") -> None:
        self.beginResetModel()
        self._hits = list(hits)
        self._current = datasheet_path or ""
        self.endResetModel()

    def hit(self, row: int):
        return self._hits[row] if 0 <= row < len(self._hits) else None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._hits)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        return self.HEADERS[section] if orientation == Qt.Horizontal else section + 1

    def data(self, index, role=Qt.DisplayRole):
        h = self.hit(index.row()) if index.isValid() else None
        if h is None:
            return None
        blocked = not h.allowed and not h.is_local
        if role == Qt.DisplayRole:
            return (LEVEL_RU.get(h.level, h.level), h.source, h.title, "%d%%" % (h.score * 100),
                    "да" if h.is_pdf else "", h.location,
                    "вне белого списка — только вручную" if blocked else h.note)[index.column()]
        if role == Qt.BackgroundRole and self._current and h.location == self._current:
            return QBrush(self._colors["current"])
        if role == Qt.ForegroundRole and blocked:
            return QBrush(self._colors["blocked"])
        return None
