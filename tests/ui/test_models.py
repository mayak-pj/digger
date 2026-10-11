# -*- coding: utf-8 -*-
"""Шаг 7.3b: модели списка фото и таблицы документов, фоновые миниатюры, диск — только из фоновых потоков."""
import os
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from digger.core.models import ChipReport, DatasheetHit  # noqa: E402
from test_latency import MAX_LATE_MS, _Lateness, _pump  # noqa: E402

PHOTOS = 500


def _png(path, color=(40, 160, 40)):
    import cv2
    import numpy as np
    img = np.zeros((48, 64, 3), dtype=np.uint8)
    img[:] = color
    ok, buf = cv2.imencode(".png", img)
    assert ok
    buf.tofile(path)                              # путь с русскими буквами — не через cv2.imwrite
    return path


@pytest.fixture()
def photo_dir(tmp_path):
    d = tmp_path / u"фото чипов"
    d.mkdir()
    first = _png(str(d / "chip_000.png"))
    data = open(first, "rb").read()
    for i in range(1, PHOTOS):
        (d / ("chip_%03d.png" % i)).write_bytes(data)
    (d / u"заметки.txt").write_text(u"не фото", encoding="utf-8")
    (d / "._chip_000.png").write_bytes(b"\x00\x05\x16\x07Mac OS X")      # спутник файла с флешки из macOS
    (d / ".hidden.png").write_bytes(data)
    return str(d)


# -------------------- без окна --------------------

def test_scan_images_folders_files_and_unreachable(photo_dir, tmp_path):
    from digger.gui.models import scan_images
    single = _png(str(tmp_path / u"один.PNG"))
    found, errors = scan_images([photo_dir, single, os.path.join(photo_dir, u"заметки.txt"),
                                 str(tmp_path / u"нет такой папки" / "x.png")])
    assert len(found) == PHOTOS + 1 and found[0].endswith("chip_000.png") and found[-1] == os.path.normpath(single)
    assert len(errors) == 1 and u"нет такой папки" in errors[0]
    assert not [p for p in found if os.path.basename(p).startswith(".")]      # служебные файлы macOS — не фото
    assert scan_images([os.path.join(photo_dir, "._chip_000.png")]) == ([], [])
    stop = type("Cancel", (), {"cancelled": True})()
    assert scan_images([photo_dir], stop) == ([], [])


def test_load_qimage_scales_down_and_survives_bad_files(photo_dir, tmp_path):
    from digger.gui.models import load_qimage
    img = load_qimage(os.path.join(photo_dir, "chip_000.png"), 32, 32)
    assert (img.width(), img.height()) == (32, 24)
    assert load_qimage(os.path.join(photo_dir, u"заметки.txt"), 32, 32) is None
    assert load_qimage(str(tmp_path / "missing.png"), 32, 32) is None


def test_photo_model(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage
    from digger.gui.models import PhotoListModel
    m = PhotoListModel()
    inserted, changed = [], []
    m.rowsInserted.connect(lambda _p, a, b: inserted.append((a, b)))
    m.dataChanged.connect(lambda a, b, _roles=None: changed.append((a.row(), b.row())))
    paths = [os.path.join("d", "p%d.png" % i) for i in range(PHOTOS)]
    assert m.add(paths + paths[:3]) == paths and m.add(paths[:5]) == []
    assert inserted == [(0, PHOTOS - 1)] and m.rowCount() == PHOTOS       # одна вставка, а не 500
    idx = m.index(7)
    assert idx.data(Qt.DisplayRole) == "p7.png" and idx.data(Qt.UserRole) == paths[7] and idx.data(Qt.ToolTipRole) == paths[7]
    assert m.row(paths[7]) == 7 and m.path(7) == paths[7] and m.path(9999) is None
    assert m.without_thumbs(paths[:3]) == paths[:3]
    qi = QImage(8, 8, QImage.Format_RGB888)
    m.set_thumbs([(paths[7], qi), (paths[2], None), ("чужой.png", qi)])
    assert changed == [(2, 7)] and not idx.data(Qt.DecorationRole).isNull()
    assert m.without_thumbs(paths[:8]) == paths[:2] + paths[3:7]            # нечитаемое фото заново не грузим
    m.set_status(paths[7], "no_memory", "NE555")
    assert idx.data(Qt.DisplayRole) == u"p7.png\nNE555 — без памяти"
    m.remove([paths[0], paths[7]])
    assert m.rowCount() == PHOTOS - 2 and m.row(paths[7]) == -1 and m.row(paths[8]) == 6
    assert m.add([paths[7]]) == [paths[7]] and m.without_thumbs([paths[7]]) == []      # миниатюра осталась в кэше


def test_hits_model(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor
    from digger.gui.models import HitsModel
    m = HitsModel({"current": QColor("#aaccff"), "blocked": QColor("#999999")})
    resets = []
    m.modelReset.connect(lambda: resets.append(1))
    hits = [DatasheetHit("NE555", "ne555.pdf", "/lib/ne555.pdf", "local", "local", 0.93, is_local=True, is_pdf=True),
            DatasheetHit("NE555", u"NE555 数据手册", "https://x.example/ne555", "baidu", "china", 0.5, allowed=False,
                         note="найдено")]
    m.set_hits(hits, "/lib/ne555.pdf")
    assert resets == [1] and m.rowCount() == 2 and m.columnCount() == 7
    assert m.headerData(2, Qt.Horizontal, Qt.DisplayRole) == u"Название"
    row = [m.index(0, c).data(Qt.DisplayRole) for c in range(7)]
    assert row[:6] == [u"База", "local", "ne555.pdf", "93%", u"да", "/lib/ne555.pdf"]
    assert m.index(0, 0).data(Qt.BackgroundRole).color().name() == "#aaccff"
    assert m.index(1, 0).data(Qt.DisplayRole) == u"Китай" and u"вне белого списка" in m.index(1, 6).data(Qt.DisplayRole)
    assert m.index(1, 2).data(Qt.ForegroundRole).color().name() == "#999999"
    assert m.index(1, 0).data(Qt.BackgroundRole) is None and m.hit(1) is hits[1] and m.hit(5) is None
    m.set_hits([], "")
    assert m.rowCount() == 0


# -------------------- окно --------------------

def test_window_500_photos_no_disk_in_ui_thread(window, photo_dir, monkeypatch):
    """500 фото из папки: окно не опаздывает, а к диску обращаются только фоновые потоки."""
    w, app = window
    import numpy as np
    touched = []
    main = threading.current_thread()

    def spy(module, name):
        real = getattr(module, name)

        def wrapped(path, *a, **k):
            if photo_dir in str(path):
                touched.append((name, threading.current_thread() is main))
            return real(path, *a, **k)
        monkeypatch.setattr(module, name, wrapped)
    for module, name in ((os, "listdir"), (os.path, "isdir"), (os.path, "exists"), (os.path, "isfile"), (np, "fromfile")):
        spy(module, name)

    late = _Lateness()
    late.start()
    started = time.perf_counter()
    w.add_files([photo_dir])
    assert (time.perf_counter() - started) * 1000 < MAX_LATE_MS             # сама команда не ждёт диск
    _pump(app, lambda: w.list.count() == PHOTOS and w.is_idle(), timeout=60)
    worst = late.stop()
    assert worst <= MAX_LATE_MS, "поток интерфейса опоздал на %.1f мс" % worst
    assert w.photos.without_thumbs(w.photos.paths()) == []                  # все миниатюры построены
    assert len(w.items) == PHOTOS and w.list.currentRow() == 0
    assert w.img_label.pixmap() is not None and not w.img_label.pixmap().isNull()
    names = {n for n, _ui in touched}
    assert "listdir" in names and "fromfile" in names
    assert not [n for n, ui in touched if ui], "диск из потока интерфейса"
    assert u"Добавлено фото: %d" % PHOTOS in w.log_view.toPlainText()

    w.list.selectAll()
    w.remove_selected()
    assert w.list.count() == 0 and not w.items and w.current_path() is None


def test_window_preview_follows_selection(window, tmp_path):
    w, app = window
    a, b = _png(str(tmp_path / "a.png")), _png(str(tmp_path / "b.png"), (200, 30, 30))
    w.add_files([a, b, str(tmp_path / u"нет.png")])
    _pump(app, lambda: w.list.count() == 2 and w.is_idle())
    first = w.img_label.pixmap().toImage().pixelColor(5, 5).name()
    w.list.setCurrentRow(1)
    assert w.current_path() == os.path.normpath(b)
    _pump(app, w.is_idle)
    assert w.img_label.pixmap().toImage().pixelColor(5, 5).name() != first
    w.list.setCurrentRow(0)                              # уже показанное фото берётся из кэша сразу
    assert w.img_label.pixmap().toImage().pixelColor(5, 5).name() == first


def test_window_documents_table_and_open_path(window, tmp_path, monkeypatch):
    w, app = window
    a = _png(str(tmp_path / "a.png"))
    w.add_files([a])
    _pump(app, lambda: w.list.count() == 1 and w.is_idle())
    r = ChipReport(image_path=a)
    r.chosen_part = "NE555"
    r.hits = [DatasheetHit("NE555", "one", "https://a.example/1.pdf", "bing", "engine", 0.8, is_pdf=True),
              DatasheetHit("NE555", "two", "https://b.example/2", "bing", "engine", 0.4)]
    w._analyzed((os.path.normpath(a), r, []))
    assert w.hits.model().rowCount() == 2 and w._selected_hit() == (None, None)
    w.hits.selectRow(1)
    assert w._selected_hit() == (r, r.hits[1])
    assert u"NE555" in w.photos.index(0).data()

    opened = []
    from PyQt5.QtGui import QDesktopServices
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toLocalFile())))
    w._open_path(a)
    w._open_path(str(tmp_path / u"нет такого.pdf"))
    _pump(app, lambda: w.is_idle() and opened and u"Не найдено" in w.log_view.toPlainText())
    assert [os.path.basename(p) for p in opened] == ["a.png"]
