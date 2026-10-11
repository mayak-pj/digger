# -*- coding: utf-8 -*-
"""Главное окно: перетаскивание фото, распознавание, поиск, заключение."""
from __future__ import annotations

import csv
import datetime
import io
import os
import sys
from collections import OrderedDict

from PyQt5.QtCore import QCoreApplication, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QAbstractItemView, QAction, QApplication, QDockWidget, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                             QPushButton, QSplitter, QTableView, QTabWidget, QTextBrowser, QToolBar,
                             QVBoxLayout, QWidget)

from ..acquire.events import Event
from ..core.config import resolve_path
from ..core.pipeline import ChipPipeline, create_context
from ..core.utils import safe_filename
from ..extensions.loader import ExtensionManager
from ..ui import theme as ui_theme
from .chip_card import ChipCard
from .conclusion_panel import ConclusionPanel
from .dialogs import AdaptersDialog, DiagnosticsDialog, ExtensionsDialog, SettingsDialog
from .docs_model import DocsModel, why_html
from .feed_model import level_names
from .models import IMG_EXT, THUMB, HitsModel, PhotoListModel, load_qimage, np_to_qimage, scan_images
from .photo_list import PhotoList
from .search_feed import SearchFeed
from .worker import Coalescer, ConsentBridge, Job, TaskPool

PREVIEW = (1280, 960)    # фото для карточки читается не крупнее; под размер окна его вписывает `PhotoView`
PREVIEW_CACHE = 16


DEMO_GUIDE = "КАК ПОКАЗАТЬ ПРОГРАММУ.txt"      # имя файла в сборке (`tools/build_portable.py`)
CARD_HEIGHT = 330             # высота карточки чипа при запуске; границу можно двигать
REPORT_MIN_HEIGHT = 170       # столько заключения о памяти видно всегда, даже под таблицей сайтов
WEB_TIP = ("Быстрый поиск datasheet: сначала локальная база, потом интернет (каталоги, сайты производителей,\n"
           "поисковики) — до первого подтверждённого документа")
WEB_ALL_TIP = ("Полный поиск datasheet в интернете: все источники, без остановки на первом найденном документе —\n"
               "дольше; чтобы сравнить варианты или поискать заново, когда документ уже есть в базе")

class MainWindow(QMainWindow):
    ext_failed = pyqtSignal(str)      # расширение отключилось (может прийти из фонового потока)
    search_events = pyqtSignal(list)  # события шины пачкой, не чаще ~30 раз в секунду (для ленты поиска)

    def __init__(self, app_dir: str):
        super().__init__()
        self.app_dir = app_dir
        self.items = {}          # path -> {"report", "variants"}
        self.job = None
        self.queue = []
        self.web_queue = []
        self.ctx = None
        self.pipe = None
        self.ext = None          # ExtensionManager
        self.consent = ConsentBridge(self)   # вопрос об отправке фото облачному способу распознавания
        self._ext_widgets = []   # вкладки, панели, пункты меню и кнопки расширений
        self._bg_jobs = []       # короткие фоновые задачи: диск, миниатюры, расширения
        self._previews = OrderedDict()       # путь → готовая картинка для карточки (последние PREVIEW_CACHE)
        self._loading = set()                # фото для карточки, которые сейчас читаются в фоне
        self.follow = False                  # окно идёт за фото, которое сейчас распознаётся (шаг 7.4c)
        self._flip = False                   # идёт «перелистывание»: прежнее фото остаётся, пока не готово новое
        self.pool = TaskPool(parent=self)    # все фоновые задачи окна и расширений
        self.feed = Coalescer(parent=self)   # ход работы и события шины из фоновых потоков — пачками
        self.feed.flushed.connect(self._feed_batch)
        self.thumb_feed = Coalescer(parent=self)      # миниатюры из фона — тоже пачками
        self._bus_unsubscribe = None
        self.ext_failed.connect(self._ext_failed, Qt.QueuedConnection)
        self.setWindowTitle("Digger — поиск datasheet по фото микросхемы")
        self.resize(1280, 820)
        # тема — до построения окна: иконки и цвета берутся из неё; стрелки для QSS — в папке программы
        self.theme = ui_theme.apply_theme(QApplication.instance(), ui_theme.configured_theme(app_dir),
                                          os.path.join(app_dir, "tmp", "theme"))
        self._build_ui()
        self._load_context()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        tb = self.toolbar = QToolBar("Действия")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        size = self.theme.t["icon_size_md"]
        gap = self.theme.t["space_xs"]             # Fusion ставит подпись вплотную к иконке — поле в самой иконке
        tb.setIconSize(QSize(size + gap, size))
        self.addToolBar(tb)
        ico = self.theme.icon

        def act(text, icon, slot, tip=""):
            a = QAction(ico(icon, pad=gap), text, self)
            a.triggered.connect(slot)
            if tip:
                a.setToolTip(tip)
            tb.addAction(a)
            return a
        act("Добавить фото…", "image-plus", self.add_files_dialog)
        self.a_run = act("Распознать", "scan-text", self.run_selected, "Распознать выбранные (или все новые) фото")
        self.a_web = act("Быстрый поиск", "search", lambda: self.web_search(None), WEB_TIP)
        self.a_web_all = act("Полный поиск", "globe", lambda: self.web_search(["all"]), WEB_ALL_TIP)
        self.a_stop = act("Стоп", "square", self.stop_job)
        tb.addSeparator()
        act("Сохранить отчёт", "file-text", self.save_report)
        act("Сводка CSV", "table", self.save_summary)
        act("Настройки", "settings", self.settings, "Тема, папки, сеть, поиск")

        mb = self.menuBar()
        m = mb.addMenu("Файл")
        m.addAction("Добавить фото…", self.add_files_dialog)
        m.addAction("Сохранить отчёт (HTML)…", self.save_report)
        m.addAction("Сводка по всем фото (CSV для Excel)…", self.save_summary)
        m.addSeparator()
        m.addAction("Выход", self.close)
        m = mb.addMenu("База")
        m.addAction("Индексировать папки с datasheet", self.index_db)
        m.addAction("Импорт каталога деталей (KiCad zip/папка, CSV)…", self.import_catalog)
        m.addAction("Добавить свой PDF в базу для текущего чипа…", self.add_own_pdf)
        m.addAction("Статистика базы", self.db_stats)
        m.addAction("Открыть папку библиотеки", lambda: self._open_path(self.ctx and self.ctx.modules["local_db"].library_dir))
        m = mb.addMenu("Сеть")
        m.addAction("Диагностика: какие сайты доступны", self.diagnose)
        m.addAction("Диагностика адаптеров поиска (тест NE555)", self.diagnose_adapters)
        m.addAction("Журнал сетевых обращений", lambda: self._open_path(
            os.path.join(resolve_path(self.app_dir, self.ctx.config["paths"]["log_dir"]), "network_audit.log")))
        m.addAction("Папка карантина", lambda: self._open_path(resolve_path(self.app_dir, self.ctx.config["paths"]["quarantine_dir"])))
        m = mb.addMenu("Расширения")
        m.addAction("Управление расширениями…", self.show_extensions)
        m.addSeparator()
        m = mb.addMenu("Справка")
        m.addAction("Инструкция (README)", lambda: self._open_path(os.path.join(self.app_dir, "README.md")))
        m.addAction("Как показать программу", lambda: self._open_path(demo_guide_path(self.app_dir)))
        m.addAction("Модули", self.show_modules)
        self.menus = {a.text(): a.menu() for a in mb.actions() if a.menu()}

        split = QSplitter(Qt.Horizontal)
        self.setCentralWidget(split)

        # слева — список фото
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(8, 4, 0, 8)
        self.photos = PhotoListModel(self.theme.icon("cpu", "text_disabled", THUMB), self)
        self.thumb_feed.flushed.connect(self.photos.set_thumbs)
        self.list = PhotoList(self.add_files)
        self.list.setModel(self.photos)
        self.list.selectionModel().currentChanged.connect(self.show_current)
        self.list.user_action.connect(self._follow_off)
        ll.addWidget(self.list)
        rm = QPushButton(ico("trash-2"), "Убрать из списка")
        rm.clicked.connect(self.remove_selected)
        self.b_follow = QPushButton(ico("eye"), "Следить")
        self.b_follow.setCheckable(True)
        self.b_follow.setToolTip("Пока идёт распознавание, список и карточка сами переходят к фото, которое сейчас\n"
                                 "проверяется. Выключается, когда вы сами выбираете фото, прокручиваете список\n"
                                 "или правите поля; эта кнопка включает снова.")
        self.b_follow.clicked.connect(self._follow_clicked)
        lb = QHBoxLayout()
        lb.addWidget(rm, 1)
        lb.addWidget(self.b_follow)
        ll.addLayout(lb)
        split.addWidget(left)

        # справа — карточка чипа; её поля доступны и под прежними именами окна
        self.card = ChipCard(self.theme)
        for name in ("img_label", "variant_box", "marking", "ocr_mode", "rerun", "part_box", "pkg_box", "pins"):
            setattr(self, name, getattr(self.card, name))
        self.variant_box.currentIndexChanged.connect(self.show_variant)
        self.card.b_reidentify.clicked.connect(self.reidentify)
        self.rerun.clicked.connect(self.recognize_again)
        self.card.b_part.clicked.connect(self.choose_part)
        self.card.b_apply.clicked.connect(self.apply_chip)
        self.photos.dataChanged.connect(self._update_header)
        self.card.user_edit.connect(self._follow_off)
        self.card.b_web.clicked.connect(lambda: self.web_search(None))
        self.card.b_web_all.clicked.connect(lambda: self.web_search(["all"]))
        self.card.b_next.clicked.connect(self.next_photo)
        self.card.b_web.setToolTip(WEB_TIP)
        self.card.b_web_all.setToolTip(WEB_ALL_TIP)

        self.tabs = QTabWidget()
        self.report_view = QTextBrowser()
        self.report_view.setOpenExternalLinks(False)
        self.conclusion_panel = ConclusionPanel(self.theme)
        self.conclusion_panel.open_browser.connect(lambda n: QDesktopServices.openUrl(QUrl(n.url)))
        self.conclusion_panel.add_request.connect(self.add_site_to_request)
        self.conclusion_panel.allow.connect(self.allow_site)
        concl = QWidget()
        cl = QVBoxLayout(concl)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.concl_split = QSplitter(Qt.Vertical)    # сайты «не найдено» не закрывают заключение о памяти
        self.concl_split.setChildrenCollapsible(False)
        self.concl_split.addWidget(self.conclusion_panel)
        self.concl_split.addWidget(self.report_view)
        self.concl_split.setStretchFactor(0, 0)
        self.concl_split.setStretchFactor(1, 1)
        self.report_view.setMinimumHeight(REPORT_MIN_HEIGHT)
        cl.addWidget(self.concl_split)
        self.tabs.addTab(concl, "Заключение")

        docs = QWidget()
        dl = QVBoxLayout(docs)
        dl.setContentsMargins(8, 8, 8, 8)
        colors = {k: self.theme.qcolor(k) for k in ("success", "warning", "accent", "danger")}
        self.docs_model = DocsModel(colors, self)
        self.docs_table = QTableView()
        self.docs_table.setModel(self.docs_model)
        self.docs_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.docs_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.docs_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.docs_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.docs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.docs_table.selectionModel().currentRowChanged.connect(lambda *_: self._show_why())
        self.docs_table.doubleClicked.connect(lambda _i: self.tabs.setCurrentWidget(self.why_view))
        db_ = QHBoxLayout()
        self.b_accept = QPushButton(self.theme.icon("file-check"), "Подтвердить")
        self.b_reject = QPushButton(self.theme.icon("x"), "Отклонить")
        self.b_find_all = QPushButton(self.theme.icon("globe"), "Найти для всех фото")
        self.b_own_pdf = QPushButton(self.theme.icon("file-text"), "Проверить свой PDF")
        self.b_accept.clicked.connect(lambda: self.decide_doc(True))
        self.b_reject.clicked.connect(lambda: self.decide_doc(False))
        self.b_find_all.clicked.connect(self.find_for_all)
        self.b_own_pdf.clicked.connect(self.check_own_pdf)
        for b in (self.b_accept, self.b_reject, self.b_find_all, self.b_own_pdf):
            db_.addWidget(b)
        db_.addStretch()
        self.hits_model = HitsModel({"current": self.theme.qcolor("accent_soft"),
                                     "blocked": self.theme.qcolor("text_disabled")}, self)
        self.hits = QTableView()
        self.hits.setModel(self.hits_model)
        self.hits.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hits.setSelectionMode(QAbstractItemView.SingleSelection)
        self.hits.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hits.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.hits.doubleClicked.connect(lambda _i: self.use_hit())
        hb = QHBoxLayout()
        for text, icon, slot in (("Использовать как datasheet", "file-check", self.use_hit),
                                 ("Скачать и проверить", "download", self.download_hit),
                                 ("Открыть PDF", "file-text", self.open_hit_pdf),
                                 ("Открыть ссылку в браузере", "external-link", self.open_hit_browser),
                                 ("Копировать адрес", "copy", self.copy_hit)):
            bb = QPushButton(self.theme.icon(icon), text)
            bb.clicked.connect(slot)
            hb.addWidget(bb)
        hb.addStretch()
        top = QWidget()                  # найденные и проверенные документы с решением пользователя
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.docs_table)
        tl.addLayout(db_)
        bottom = QWidget()               # все ссылки поиска: скачать и проверить вручную
        bl_ = QVBoxLayout(bottom)
        bl_.setContentsMargins(0, 0, 0, 0)
        bl_.addWidget(QLabel("Ссылки поиска"))
        bl_.addWidget(self.hits)
        bl_.addLayout(hb)
        docs_split = QSplitter(Qt.Vertical)
        docs_split.setChildrenCollapsible(False)
        docs_split.addWidget(top)
        docs_split.addWidget(bottom)
        docs_split.setStretchFactor(0, 3)        # таблице документов — основное место, ссылкам — остаток
        docs_split.setStretchFactor(1, 1)
        docs_split.setSizes([300, 100])
        self.docs_table.setMinimumHeight(110)
        self.hits.setMinimumHeight(48)
        dl.addWidget(docs_split)
        self.tabs.addTab(docs, "Документы")
        self.why_view = QTextBrowser()
        self.why_view.setOpenExternalLinks(False)
        self.tabs.addTab(self.why_view, "Почему")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.tabs.addTab(self.log_view, "Журнал")

        # под карточкой — живая лента поиска (скрыта, пока нет событий) и вкладки
        lower = QWidget()
        bl = QVBoxLayout(lower)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(gap * 2)
        self.search_feed = self.feed_slot = SearchFeed(self.theme, stop_button=False)    # «Стоп» один — на панели
        self.search_events.connect(self.search_feed.add_events)
        self.search_feed.stop_requested.connect(self.stop_job)
        bl.addWidget(self.search_feed)
        bl.addWidget(self.tabs, 1)
        self.right = QSplitter(Qt.Vertical)      # граница «карточка / вкладки» двигается: фото растёт вместе с карточкой
        self.right.setChildrenCollapsible(False)
        self.right.addWidget(self.card)
        self.right.addWidget(lower)
        self.right.setStretchFactor(0, 0)        # прибавка высоты окна — заключению, а не полям вокруг фото
        self.right.setStretchFactor(1, 1)
        self.right.setSizes([CARD_HEIGHT, 430])
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 8, 8, 8)
        rl.addWidget(self.right)
        split.addWidget(right)
        split.setStretchFactor(1, 1)             # прибавка ширины окна — карточке, не списку фото
        split.setSizes([290, 990])

        self.status = QLabel("Готово")
        self.busy = QProgressBar()
        self.busy.setMaximumWidth(160)
        self.busy.setRange(0, 0)
        self.busy.hide()
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.busy)
        self._set_busy(False)

    # ------------------------------------------------------------ контекст
    def _load_context(self):
        try:
            if self.ext:
                self.ext.close()
            if self.ctx:
                for mod in self.ctx.modules.values():
                    mod.close()
            self.ctx = create_context(self.app_dir)
            self.pipe = ChipPipeline(self.ctx)
            ocr = self.ctx.modules.get("ocr")
            if hasattr(ocr, "consent"):
                ocr.consent.ask = self.consent.ask
            self._fill_ocr_modes(ocr)
            self._load_extensions()
            if self._bus_unsubscribe:
                self._bus_unsubscribe()
            self._bus_unsubscribe = self.ctx.bus.subscribe(self.feed.post)
            sources = os.path.join(self.app_dir, "data", "sources.json")      # названия уровней для истории поиска
            self._bg(lambda progress, cancel: level_names(sources), self.search_feed.set_level_names)
            ocr = self.ctx.modules["ocr"]
            if not ocr.is_available():
                self.log("⚠ " + getattr(ocr, "error", "OCR недоступен"))
                QMessageBox.warning(self, "Распознавание недоступно", getattr(ocr, "error", "") +
                                    "\n\nМаркировку можно вводить вручную. Путь к Tesseract задаётся в «Настройках».")
            st = self.ctx.modules["local_db"].stats()
            self.log("База: %(files)d файлов (скачано %(library)d), каталог деталей: %(catalog)d" % st)
            if st["files"] == 0 and self.ctx.config["paths"].get("scan_roots"):
                self.log("Индекс пуст — запустите «База → Индексировать папки»")
        except Exception as e:  # noqa
            QMessageBox.critical(self, "Ошибка запуска модулей", str(e))

    def _fill_ocr_modes(self, ocr):
        """Список способов распознавания; у модуля OCR без выбора способа список пуст и выключен."""
        self.ocr_mode.clear()
        for mode, title in (ocr.modes() if hasattr(ocr, "modes") else []):
            self.ocr_mode.addItem(title, mode)
        self.ocr_mode.setCurrentIndex(max(0, self.ocr_mode.findData(getattr(ocr, "mode", ""))))
        for w in (self.ocr_mode, self.rerun):
            w.setEnabled(self.ocr_mode.count() > 0)

    def log(self, msg):
        self.feed.flush()            # строки фона, пришедшие раньше, — в журнал раньше
        self._show_lines([(datetime.datetime.now().strftime("%H:%M:%S"), msg)])

    def _progress(self, msg):
        """Ход фоновой задачи; вызывается из её потока."""
        self.feed.post((datetime.datetime.now().strftime("%H:%M:%S"), str(msg)))

    def _feed_batch(self, items):
        events = [x for x in items if isinstance(x, Event)]
        self._show_lines([x for x in items if not isinstance(x, Event)])
        if events:
            self.search_events.emit(events)

    def _show_lines(self, lines):
        if lines:
            self.log_view.appendPlainText("\n".join("%s  %s" % x for x in lines))
            self.status.setText(lines[-1][1][:200])

    def _set_busy(self, busy):
        self.busy.setVisible(busy)
        self.a_stop.setEnabled(busy)
        self.search_feed.set_running(busy)
        for a in (self.a_run, self.a_web, self.a_web_all, self.card.b_web, self.card.b_web_all):
            a.setEnabled(not busy)

    def start_job(self, fn, on_done, *args, **kwargs):
        if self.job and self.job.isRunning():
            QMessageBox.information(self, "Подождите", "Уже выполняется задача. Нажмите «Стоп», чтобы прервать.")
            return False
        job = self.job = Job(fn, *args, **kwargs)
        job.photo = None                 # фото, над которым идёт задача (см. `_working`)
        job.on_progress = self._progress
        job.done.connect(on_done)
        job.failed.connect(self._job_failed)
        job.finished.connect(lambda: self._job_finished(job))
        self._set_busy(True)
        self.pool.start(job)
        return True

    def _job_finished(self, job):
        if self.job is job and not job.isRunning():      # обработчик результата мог запустить следующую
            self.feed.flush()                            # последние события задачи — в ленту до её закрытия
            self._set_busy(False)
        if job.photo and (self.job is job or self.job.photo != job.photo):
            self._mark_item(job.photo)                   # метка «в работе» снята и после ошибки, и после «Стоп»

    def _begin_feed(self):
        """Новая задача распознавания или поиска: лента поиска начинается с чистого листа."""
        self.feed.flush()                                # события прежней задачи — в прежнюю историю
        self.search_feed.begin()

    def _working(self, path, state):
        """Карточка фото показывает, что над ним идёт только что запущенная задача: «распознаю…», «ищу…»."""
        self.job.photo = path
        self.photos.set_status(path, state)

    def _job_failed(self, msg):
        self.log("Ошибка: " + msg.splitlines()[0])
        self.ctx.log.error(msg)
        QMessageBox.warning(self, "Ошибка", msg[:2000])
        self.queue = []

    def _bg(self, fn, on_done=None):
        """Короткая фоновая задача вне очереди «Распознать / Искать»: диск, миниатюры, расширения."""
        job = Job(fn)
        self._bg_jobs.append(job)
        if on_done:
            job.done.connect(on_done)
        job.failed.connect(lambda msg: self.ctx and self.ctx.log.error(msg))
        job.finished.connect(lambda: self._bg_jobs.remove(job))
        self.pool.start(job)

    def is_idle(self) -> bool:
        """Нет ни идущих задач, ни недоставленных в окно событий."""
        job_busy = self.job and (self.job.isRunning() or not self.job.wait(0))    # поток ещё отправляет done/finished
        return not (job_busy or self._bg_jobs
                    or self.feed.pending() or self.thumb_feed.pending())

    def stop_job(self):
        if self.job and self.job.isRunning():
            self.job.cancel.cancel()
            self.queue = []
            self.web_queue = []
            self.log("Останавливаю…")

    # ------------------------------------------------------------ файлы
    def add_files_dialog(self):
        paths = QFileDialog.getOpenFileNames(self, "Фото микросхем", "",
                                             "Изображения (%s)" % " ".join("*" + e for e in IMG_EXT))[0]
        self.add_files(paths)

    def add_files(self, paths):
        """Файлы и папки — в список. Диск (возможно, сетевой) читается в фоне, список пополняется по готовности."""
        paths = [p for p in paths if p]
        if paths:
            self._bg(lambda progress, cancel: scan_images(paths, cancel), self._add_found)

    def _add_found(self, res):
        found, errors = res
        for p in errors:
            self.log("Не найдено или недоступно: %s" % p)
        new = self.photos.add(found)
        for p in new:
            self.items[p] = {"report": None, "variants": []}
        if new:
            self.log("Добавлено фото: %d" % len(new))
            if self.list.currentRow() < 0:
                self.list.setCurrentRow(0)
            self._load_thumbs(new)

    def _load_thumbs(self, paths):
        todo = self.photos.without_thumbs(paths)

        def work(progress, cancel):
            for p in todo:
                if cancel.cancelled:
                    break
                self.thumb_feed.post((p, load_qimage(p, THUMB, THUMB)))
        if todo:
            self._bg(work)

    def remove_selected(self):
        paths = self.list.selected_paths()
        for p in paths:
            self.items.pop(p, None)
        self.photos.remove(paths)

    def current_path(self):
        idx = self.list.currentIndex()
        return idx.data(Qt.UserRole) if idx.isValid() else None

    def current(self):
        p = self.current_path()
        return self.items.get(p) if p else None

    # ------------------------------------------------------------ обработка
    def run_selected(self):
        # выделенные (в т.ч. повторно) + все ещё не распознанные
        sel = self.list.selected_paths()
        sel += [p for p, d in self.items.items() if d["report"] is None and p not in sel]
        if not sel:
            self.log("Нет новых фото. Выделите фото, чтобы распознать повторно.")
            return
        self.queue = list(sel)
        if self.ctx.config.get("ui", {}).get("follow_recognition", True):
            self._set_follow(True)
        self._next_in_queue()

    def _next_in_queue(self):
        if not self.queue:
            return
        if self.job and self.job.isRunning():
            QTimer.singleShot(150, self._next_in_queue)
            return
        path = self.queue.pop(0)
        mode = self.ocr_mode.currentData() or ""

        def work(path, progress, cancel):
            r, variants = self.pipe.analyze_image(path, progress=progress, ocr_mode=mode)
            if (self.ctx.config["pipeline"].get("auto_web_search") and not r.datasheet_path
                    and r.chosen_part and not cancel.cancelled):
                self.pipe.search_web(r, progress=progress, cancel=cancel)
            return path, r, variants

        self._begin_feed()
        if self.start_job(work, self._analyzed, path):
            self._working(path, "busy")
            self._follow_to(path)
            if self.queue:
                self._preload(self.queue[0])             # следующее фото — заранее: смена без пустого кадра

    # ------------------------------------------------------------ живое распознавание
    def _set_follow(self, on):
        self.follow = bool(on)
        self.b_follow.setChecked(self.follow)

    def _follow_off(self):
        """Пользователь сам выбрал фото, прокрутил список или правит поля — его не перебиваем."""
        if self.follow:
            self._set_follow(False)

    def _follow_clicked(self, on):
        self._set_follow(on)
        if on and self.job and self.job.isRunning() and self.job.photo:
            self._follow_to(self.job.photo)

    def _follow_to(self, path):
        """Список и карточка переходят к фото, над которым идёт работа (только смена строки и картинка из кэша)."""
        row = self.photos.row(path) if self.follow else -1
        if row < 0:
            return
        self._flip = True
        try:
            self.list.follow_row(row)
        finally:
            self._flip = False

    def _preload(self, path):
        if path in self._previews or path in self._loading:
            return
        self._loading.add(path)
        self._bg(lambda progress, cancel: (path, load_qimage(path, PREVIEW[0], PREVIEW[1])), self._photo_loaded)

    def recognize_again(self):
        """Текущее фото ещё раз — способом, выбранным в списке."""
        path = self.current_path()
        if not path:
            return
        if self.job and self.job.isRunning():
            QMessageBox.information(self, "Подождите", "Уже выполняется задача. Нажмите «Стоп», чтобы прервать.")
            return
        self.log("Распознаю заново: %s — %s" % (os.path.basename(path), self.ocr_mode.currentText()))
        self.queue = [path]
        self._next_in_queue()

    def _analyzed(self, res):
        path, r, variants = res
        self.items[path] = {"report": r, "variants": variants}
        self._mark_item(path)
        if self.ctx.bus:
            self.ctx.bus.emit("photo.recognized", lang="ru", part=r.chosen_part or "?", path=path)
        if path == self.current_path():
            self.show_current()
        if self.queue:
            QTimer.singleShot(150, self._next_in_queue)
        else:
            self.log("Готово")

    def _mark_item(self, path):
        """Метка на карточке фото — по его отчёту."""
        d = self.items.get(path)
        if not d:
            return
        r = d["report"]
        if r is None:
            self.photos.set_status(path, "new", "")
            return
        mem = r.memory.has_memory if r.memory else None
        if mem is not None:
            state = "memory" if mem else "no_memory"
        elif not r.chosen_part:
            state = "unread"
        else:
            state = "unknown" if r.datasheet_path else "not_found" if r.web_searched else "no_local"
        self.photos.set_status(path, state, r.chosen_part or "")

    def web_search(self, levels):
        d = self.current()
        if not d or not d["report"] or not d["report"].chosen_part:
            QMessageBox.information(self, "Нет партномера", "Сначала распознайте фото или введите партномер.")
            return
        r = d["report"]

        def work(progress, cancel):
            self.pipe.search_web(r, progress=progress, cancel=cancel, levels=levels)
            return r
        if not (self.job and self.job.isRunning()):
            self._begin_feed()
        if self.start_job(work, lambda _r: self._refresh_current()):
            self._working(self.current_path(), "search")

    def reidentify(self):
        d = self.current()
        if not d:
            return
        text = self.marking.toPlainText().strip()
        if not text:
            return
        if d["report"] is None:
            path = self.current_path()

            def work(progress, cancel):
                return path, *self.pipe.analyze_image(path, progress=progress, marking_override=text)
            if self.start_job(work, self._analyzed):
                self._working(path, "busy")
            return
        r = d["report"]

        others = [(p, x["report"]) for p, x in self.items.items() if x["report"] is not None and x["report"] is not r]

        def work2(progress, cancel):
            self.pipe.identify(r, text, [], progress)
            # исправление могло пополнить словарь замен (расширение) — он действует и на уже распознанные фото
            return [p for p, x in others if self.pipe.refresh_marking(
                x, lambda s, name=os.path.basename(p): progress("%s: %s" % (name, s)))]

        def done(changed):
            for p in changed:
                self._mark_item(p)
            self._refresh_current()
        self.start_job(work2, done)

    def choose_part(self):
        d = self.current()
        part = self.part_box.currentText().strip().split("  (")[0]
        if not d or not part:
            return
        if d["report"] is None:
            self.marking.setPlainText(part)
            self.reidentify()
            return
        r = d["report"]

        def work(progress, cancel):
            self.pipe.set_part(r, part, progress)
            return r
        self.start_job(work, lambda _r: self._refresh_current())

    def apply_chip(self):
        d = self.current()
        if not d or not d["report"]:
            return
        r = d["report"]
        r.chip.package = self.pkg_box.currentText().strip()
        r.chip.pins = self.pins.value()
        r.chip.pins_estimated = False

        def work(progress, cancel):
            self.pipe.evaluate(r, progress)
            return r
        self.start_job(work, lambda _r: self._refresh_current())

    def _refresh_current(self):
        p = self.current_path()
        if p:
            self._mark_item(p)
        self.show_current()

    # ------------------------------------------------------------ показ
    def show_current(self, *_):
        d = self.current()
        self._update_header()
        self.variant_box.blockSignals(True)
        self.variant_box.clear()
        if not d:
            self.variant_box.blockSignals(False)
            self.img_label.setText("—")
            return
        self._show_photo(self.current_path())
        self.variant_box.addItem("Исходное фото")
        for v in d["variants"]:
            self.variant_box.addItem("Вариант: " + v.name)
        r = d["report"]
        if r and r.ocr and r.ocr.best_variant:
            best = r.ocr.best_variant.split("_rot")[0]
            idx = self.variant_box.findText("Вариант: " + best)
            if idx >= 0:
                self.variant_box.setCurrentIndex(idx)
                self.show_variant(idx)
        self.variant_box.blockSignals(False)

        self.part_box.clear()
        self.hits_model.set_hits([])
        self.docs_model.set_records(r.records if r else [])
        self.conclusion_panel.set_conclusion(r.conclusion if r else None)
        self._fit_conclusion()
        self._show_why()
        if not r:
            self.marking.setPlainText("")
            self.report_view.setHtml("<p style='color:%s'>Фото ещё не распознано. Нажмите «Распознать».</p>"
                                      % self.theme.color("text_muted"))
            return
        self.marking.setPlainText(r.ocr.best_text if r.ocr else "")
        for i, c in enumerate(r.candidates):
            self.part_box.addItem("%s  (%d%%)" % (c.part, c.score * 100))
            self.part_box.setItemData(i, "%s\n%s" % (c.description, c.reason), Qt.ToolTipRole)
        idx = next((i for i, c in enumerate(r.candidates) if c.part == r.chosen_part), -1)
        if idx >= 0:
            self.part_box.setCurrentIndex(idx)
        else:
            self.part_box.setEditText(r.chosen_part)
        i = self.pkg_box.findText(r.chip.package)
        if i >= 0:
            self.pkg_box.setCurrentIndex(i)
        else:
            self.pkg_box.setEditText(r.chip.package)
        self.pins.setValue(r.chip.pins or 0)
        for box in (self.part_box, self.pkg_box):
            box.lineEdit().setCursorPosition(0)
        self.report_view.setHtml(self.pipe.render(r, d["variants"], self._report_colors()))
        self._fill_hits(r)

    def _fit_conclusion(self):
        """Таблице сайтов — её полная высота, если при этом заключению о памяти остаётся `REPORT_MIN_HEIGHT`."""
        if self.conclusion_panel.isHidden():
            return
        total = self.concl_split.height()
        want = min(self.conclusion_panel.preferred_height(), max(0, total - REPORT_MIN_HEIGHT))
        self.concl_split.setSizes([want, max(1, total - want)])

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._fit_conclusion)       # после раскладки: высота вкладки уже новая

    def _update_header(self, *_):
        """Заголовок карточки — по состоянию выбранного фото в списке и его отчёту."""
        path = self.current_path()
        if not path:
            self.card.set_header("", "", "new")
            return
        state, part = self.photos.status(path)
        r = self.items.get(path, {}).get("report")
        self.card.set_header(os.path.basename(path), part, state, r.chip.package if r else "",
                             r.chip.pins if r else 0, tip=path)

    def show_variant(self, idx):
        d = self.current()
        if not d:
            return
        if idx <= 0:
            self._show_photo(self.current_path())
        elif idx - 1 < len(d["variants"]):
            self.img_label.setImage(np_to_qimage(d["variants"][idx - 1].image))

    def _show_photo(self, path):
        """Исходное фото в карточке: из кэша сразу, иначе читается в фоне и показывается, если фото ещё выбрано."""
        if path in self._previews:
            self._previews.move_to_end(path)
            self.img_label.setImage(self._previews[path])
            return
        if not (self._flip and self.img_label.image() is not None):
            self.img_label.setText("…")
        self._preload(path)

    def _photo_loaded(self, res):
        path, image = res
        self._loading.discard(path)
        if image is not None:
            self._previews[path] = image
            while len(self._previews) > PREVIEW_CACHE:
                self._previews.popitem(last=False)
        if path == self.current_path() and self.variant_box.currentIndex() <= 0:
            if image is None:
                self.img_label.setText("Фото не читается")
            else:
                self.img_label.setImage(image)

    def add_site_to_request(self, note):
        from ..acquire.site_actions import add_to_access_request
        ws, part = self.ctx.modules["web_search"], (self.current() or {}).get("report")
        part = part.chosen_part if part else ""
        self._bg(lambda progress, cancel: add_to_access_request(getattr(ws.orchestrator, "access", None), note, part),
                 lambda ok: self.log("«%s» — в списке для администраторов (Расширение «Сайты без доступа»)" % note.site
                                     if ok else "«%s» не добавлен" % note.site))

    def allow_site(self, note):
        from ..acquire.site_actions import allow_domain
        added = allow_domain(self.ctx.modules["web_search"].http, self.app_dir, note.site)
        self.log("Домен «%s» разрешён; искать заново — кнопкой «Быстрый поиск»" % note.site if added
                 else "Домен «%s» уже разрешён" % note.site)

    def _report_colors(self):
        """Цвета отчёта в окне — из темы (в файл отчёта идёт светлый вариант)."""
        from ..modules.report_html import colors_from_tokens
        return colors_from_tokens(self.theme.t)

    def _fill_hits(self, r):
        self.hits_model.set_hits(r.hits, r.datasheet_path)
        self.hits.resizeColumnToContents(0)
        self.hits.resizeColumnToContents(3)

    def _selected_doc(self):
        d = self.current()
        rec = self.docs_model.record(self.docs_table.currentIndex().row()) if d and d["report"] else None
        return (d["report"], rec) if rec is not None else (None, None)

    def _show_why(self):
        r, rec = self._selected_doc()
        colors = {k: self.theme.color(k) for k in ("success", "warning", "accent", "danger")}
        colors["muted"] = self.theme.color("text_muted")
        self.why_view.setHtml(why_html(rec, colors))

    def _doc_changed(self, r, rec):
        """Решение или проверка закончены: строка, «Почему», заключение и метка фото обновляются."""
        self._refresh_current()              # перечитывает таблицу; выбранная строка сбрасывается
        row = self.docs_model.row_of(rec)
        if row >= 0 and r is (self.current() or {}).get("report"):
            self.docs_table.selectRow(row)

    def decide_doc(self, accept):
        r, rec = self._selected_doc()
        if rec is None:
            QMessageBox.information(self, "Выберите документ", "Выберите документ в таблице.")
            return
        if not accept and QMessageBox.question(self, "Отклонить документ",
                                               "Документ будет убран из библиотеки. Отклонить?") != QMessageBox.Yes:
            return

        def work(progress, cancel):
            return self.pipe.decide_record(r, rec, accept, progress)
        self.start_job(work, lambda res: self._doc_changed(r, res))

    def check_own_pdf(self):
        d = self.current()
        if not d or not d["report"] or not d["report"].chosen_part:
            QMessageBox.information(self, "Нет партномера", "Выберите распознанное фото.")
            return
        p = QFileDialog.getOpenFileName(self, "Ваш PDF datasheet", "", "PDF (*.pdf)")[0]
        if not p:
            return
        r = d["report"]
        self._begin_feed()

        def work(progress, cancel):
            return self.pipe.check_own_pdf(r, p, progress)
        if self.start_job(work, lambda rec: rec is not None and (self._doc_changed(r, rec), self.tabs.setCurrentWidget(self.why_view))):
            self._working(self.current_path(), "search")

    def next_photo(self):
        """Кнопка карточки «Следующее»: выбрать следующее фото списка (на последнем — остаться)."""
        row = self.list.currentIndex().row() + 1
        if 0 < row < self.photos.rowCount():
            self._follow_off()
            self.list.setCurrentIndex(self.photos.index(row))

    def find_for_all(self):
        """Поиск в интернете для каждого распознанного фото с партномером — по очереди, как «Распознать»."""
        paths = [p for p, d in self.items.items() if d["report"] and d["report"].chosen_part]
        if not paths:
            QMessageBox.information(self, "Нет партномеров", "Сначала распознайте фото.")
            return
        self.web_queue = paths
        self._next_web()

    def _next_web(self):
        if not self.web_queue:
            return
        if self.job and self.job.isRunning():
            QTimer.singleShot(150, self._next_web)
            return
        path = self.web_queue.pop(0)
        r = self.items[path]["report"]

        def work(progress, cancel):
            self.pipe.search_web(r, progress=progress, cancel=cancel)
            return path

        self._begin_feed()
        if self.start_job(work, self._web_done):
            self._working(path, "search")
            self._follow_to(path)

    def _web_done(self, path):
        self._mark_item(path)
        if path == self.current_path():
            self.show_current()
        if self.web_queue:
            QTimer.singleShot(150, self._next_web)
        else:
            self.log("Поиск для всех фото закончен")

    def _selected_hit(self):
        d = self.current()
        h = self.hits_model.hit(self.hits.currentIndex().row()) if d and d["report"] else None
        if h is None:
            QMessageBox.information(self, "Выберите строку", "Выберите документ в таблице.")
            return None, None
        return d["report"], h

    def use_hit(self):
        r, h = self._selected_hit()
        if not h:
            return
        if h.is_local:
            r.datasheet_path = h.location
            db = self.ctx.modules["local_db"]
            if hasattr(db, "confirm_part"):
                db.confirm_part(h.location, r.chosen_part)

            def work(progress, cancel):
                self.pipe.evaluate(r, progress)
                return r
            self.start_job(work, lambda _r: (self._refresh_current(), self.tabs.setCurrentIndex(0)))
        else:
            self.download_hit()

    def download_hit(self):
        r, h = self._selected_hit()
        if not h or h.is_local:
            return
        if not h.allowed:
            QMessageBox.information(self, "Сайт вне белого списка",
                                    "Программа не обращается к сайту %s.\n\nМожно открыть ссылку в браузере "
                                    "вручную, скачать PDF и добавить через «База → Добавить свой PDF»." % h.location)
            return

        def work(progress, cancel):
            ok = self.pipe.download_hit(r, h, progress)
            return ok
        self.start_job(work, lambda ok: self._after_download(r, h, ok))

    def _after_download(self, r, h, ok):
        self._refresh_current()
        if ok:
            self.tabs.setCurrentIndex(0)
            return
        if h.note.startswith("не подтверждён"):
            q = QMessageBox.question(self, "Партномер не найден в PDF",
                                     "В скачанном PDF нет «%s». Возможно, это datasheet на семейство или скан.\n\n"
                                     "Всё равно сохранить в базу и использовать?" % r.chosen_part)
            if q == QMessageBox.Yes:
                self.start_job(lambda progress, cancel: self.pipe.download_hit(r, h, progress, force_accept=True),
                               lambda _ok: (self._refresh_current(), self.tabs.setCurrentIndex(0)))

    def open_hit_pdf(self):
        r, h = self._selected_hit()
        if not h:
            return
        if not h.is_local:
            QMessageBox.information(self, "Сначала скачайте", "Откройте PDF после скачивания и проверки.")
            return
        self._open_path(h.location)

    def open_hit_browser(self):
        r, h = self._selected_hit()
        if not h:
            return
        if h.is_local:
            self._open_path(os.path.dirname(h.location))
            return
        if QMessageBox.question(self, "Открыть в браузере",
                                "Ссылка откроется в вашем браузере (вне защиты программы):\n\n%s" % h.location) == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl(h.location))

    def copy_hit(self):
        r, h = self._selected_hit()
        if h:
            QApplication.clipboard().setText(h.location)
            self.log("Скопировано: " + h.location)

    # ------------------------------------------------------------ база и сеть
    def index_db(self):
        roots = self.ctx.config["paths"].get("scan_roots", [])
        if not roots:
            QMessageBox.information(self, "Нет папок", "Добавьте папки с datasheet в «Настройках». "
                                                       "Скачанные файлы будут проиндексированы в любом случае.")
        db = self.ctx.modules["local_db"]
        self.start_job(lambda progress, cancel: db.index(roots, progress, cancel),
                       lambda n: self.log("Проиндексировано файлов: %s" % n))

    def import_catalog(self):
        p = QFileDialog.getOpenFileName(self, "Каталог деталей: архив библиотек KiCad (.zip) или CSV", "",
                                        "KiCad zip / CSV (*.zip *.csv);;Все файлы (*.*)")[0]
        if not p:
            p = QFileDialog.getExistingDirectory(self, "…или папка с библиотеками KiCad (.kicad_sym/.dcm)")
        if not p:
            return
        db = self.ctx.modules["local_db"]
        self.start_job(lambda progress, cancel: db.import_catalog(p, progress),
                       lambda n: self.log("Каталог: импортировано %s деталей" % n))

    def add_own_pdf(self):
        d = self.current()
        if not d or not d["report"] or not d["report"].chosen_part:
            QMessageBox.information(self, "Нет партномера", "Выберите распознанное фото.")
            return
        p = QFileDialog.getOpenFileName(self, "PDF datasheet", "", "PDF (*.pdf)")[0]
        if not p:
            return
        r = d["report"]
        db = self.ctx.modules["local_db"]

        def work(progress, cancel):       # копирование в библиотеку (сетевой диск) — тоже в фоне
            r.datasheet_path = db.add_to_library(p, r.chosen_part, {"source": "manual", "url": p})
            self.pipe.set_part(r, r.chosen_part, progress)
            return r
        self.start_job(work, lambda _r: self._refresh_current())

    def db_stats(self):
        st = self.ctx.modules["local_db"].stats()
        QMessageBox.information(self, "База", "Файлов в индексе: %(files)d\nСкачано программой: %(library)d\n"
                                              "Деталей в каталоге: %(catalog)d\n\nИндекс: %(db)s\nБиблиотека: %(library_dir)s" % st)

    def diagnose(self):
        ws = self.ctx.modules["web_search"]
        self.start_job(lambda progress, cancel: ws.diagnose(progress, cancel),
                       lambda rows: DiagnosticsDialog(rows, self).exec_())

    def diagnose_adapters(self):
        from ..acquire.diagnose import diagnose_adapters
        from ..acquire.registry import Registry
        ws = self.ctx.modules["web_search"]
        path = os.path.join(self.app_dir, "data", "sources.json")
        keys = self.ctx.config.get("acquire", {}).get("api_keys", {})
        ws.http.add_allowed(Registry.load(path).allowed_domains())
        self.start_job(lambda progress, cancel: diagnose_adapters(path, ws.http, keys, progress=progress, cancel=cancel),
                       lambda rows: AdaptersDialog(rows, self).exec_())

    def settings(self):
        dlg = SettingsDialog(self.ctx, self)
        old = ui_theme.configured_theme(self.app_dir)
        if dlg.exec_():
            self._load_context()
            self.log("Настройки сохранены и применены")
            if ui_theme.configured_theme(self.app_dir) != old:
                QMessageBox.information(self, "Тема", "Тема сменится после перезапуска программы.")

    # ------------------------------------------------------------ расширения
    def _load_extensions(self):
        if self.ext:
            self.ext.close()
        self.ext = ExtensionManager(self.ctx, pipeline=self.pipe, runner=self._ext_run,
                                    on_failure=lambda info: self.ext_failed.emit(info.id))
        self.ext.load()
        self._apply_extensions()

    def _ext_run(self, work, finish):
        self._bg(lambda progress, cancel: work(), finish)

    def _ext_failed(self, ext_id):
        info = self.ext.get(ext_id) if self.ext else None
        if info:
            self.log("⚠ Расширение «%s» %s: %s. Программа работает дальше; подробности — в журнале, "
                     "выключить можно в окне «Расширения»." % (info.name, "не загружено" if not info.instance
                                                               else "отключено из-за ошибки", info.error))
            self._apply_extensions()

    def _apply_extensions(self):
        """Перестраивает вклады расширений в окно: вкладки, боковые панели, пункты меню, кнопки."""
        for w in self._ext_widgets:
            if isinstance(w, QAction):
                for holder in w.associatedWidgets():
                    holder.removeAction(w)
            elif isinstance(w, QMenu):
                self.menuBar().removeAction(w.menuAction())
                self.menus.pop(w.title(), None)
            elif isinstance(w, QDockWidget):
                self.removeDockWidget(w)
            else:
                self.tabs.removeTab(self.tabs.indexOf(w))
            w.deleteLater()
        self._ext_widgets = []
        ui = self.ext.ui
        for c in ui.of("tab") + ui.of("side_panel"):
            w = c.target()
            if not isinstance(w, QWidget):
                continue
            if c.kind == "tab":
                self.tabs.addTab(w, c.title)
            else:
                dock = QDockWidget(c.title, self)
                dock.setWidget(w)
                self.addDockWidget(Qt.RightDockWidgetArea, dock)
                w = dock
            self._ext_widgets.append(w)
        for c in ui.of("menu_item") + ui.of("toolbar_button"):
            a = QAction(c.title, self)
            a.triggered.connect(lambda _checked=False, fn=c.target: fn())
            if c.tip:
                a.setToolTip(c.tip)
            holder = self.toolbar
            if c.kind == "menu_item":
                name = c.menu or "Расширения"
                if name not in self.menus:
                    self.menus[name] = self.menuBar().addMenu(name)
                    self._ext_widgets.append(self.menus[name])
                holder = self.menus[name]
            holder.addAction(a)
            self._ext_widgets.append(a)

    def show_extensions(self):
        if self.ext and ExtensionsDialog(self.ext, self).exec_():
            self._load_extensions()
            self.log("Расширения: изменения применены")

    def show_modules(self):
        lines = ["%-11s %s" % (k, v) for k, v in self.ctx.config["modules"].items()]
        QMessageBox.information(self, "Модули", "Модули программы (меняются в config.json → modules):\n\n" + "\n".join(lines))

    # ------------------------------------------------------------ отчёты
    def save_report(self):
        d = self.current()
        if not d or not d["report"]:
            return
        r = d["report"]
        rdir = resolve_path(self.app_dir, self.ctx.config["paths"]["reports_dir"])
        os.makedirs(rdir, exist_ok=True)
        name = "%s_%s.html" % (safe_filename(r.chosen_part or "chip"), datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        p = QFileDialog.getSaveFileName(self, "Сохранить отчёт", os.path.join(rdir, name), "HTML (*.html)")[0]
        if p:
            html = self.pipe.render(r, d["variants"])
            extra = "".join("<h2>%s</h2>\n%s\n" % (c.title, c.target(r)) for c in self.ext.ui.of("report_section"))
            if extra:
                html = html.replace("</body>", extra + "</body>") if "</body>" in html else html + extra
            with io.open(p, "w", encoding="utf-8") as f:
                f.write(html)
            self.log("Отчёт сохранён: " + p)

    def save_summary(self):
        rows = [(p, d["report"]) for p, d in self.items.items() if d["report"]]
        if not rows:
            return
        rdir = resolve_path(self.app_dir, self.ctx.config["paths"]["reports_dir"])
        os.makedirs(rdir, exist_ok=True)
        p = QFileDialog.getSaveFileName(self, "Сводка", os.path.join(rdir, "svodka_%s.csv" %
                                                                     datetime.datetime.now().strftime("%Y%m%d_%H%M")),
                                        "CSV (*.csv)")[0]
        if not p:
            return
        columns = self.ext.ui.of("csv_column")
        with io.open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Фото", "Маркировка", "Партномер", "Datasheet", "Сверка", "Оценка сверки",
                        "Память", "Типы памяти", "Итог по памяти", "Не найдено: где смотреть"] + [c.title for c in columns])
            for path, r in rows:
                mem = r.memory
                w.writerow([path, (r.ocr.best_text if r.ocr else "").replace("\n", " / "), r.chosen_part,
                            r.datasheet_path, r.comparison.verdict if r.comparison else "",
                            "%d%%" % (r.comparison.score * 100) if r.comparison else "",
                            {True: "ДА", False: "НЕТ", None: "?"}[mem.has_memory if mem else None],
                            "; ".join("%s %s" % (i.kind, i.size) for i in (mem.items if mem else [])),
                            mem.summary if mem else "",
                            r.conclusion.csv_cell() if r.conclusion else ""] + [str(c.target(r)) for c in columns])
        self.log("Сводка сохранена: " + p)

    def _open_path(self, p):
        def opened(found):
            if found:
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))
            else:
                self.log("Не найдено: %s" % p)
        self._bg(lambda progress, cancel: bool(p) and os.path.exists(p), opened)   # путь может быть сетевым

    def closeEvent(self, e):
        self.stop_job()
        if self.job:
            self.job.wait(3000)
        for job in list(self._bg_jobs):
            job.cancel.cancel()
        for job in list(self._bg_jobs):
            job.wait(3000)
        if self._bus_unsubscribe:
            self._bus_unsubscribe()
            self._bus_unsubscribe = None
        if self.ext:
            self.ext.close()
        if self.ctx:
            for mod in self.ctx.modules.values():
                mod.close()
        e.accept()


def demo_guide_path(app_dir: str) -> str:
    """Инструкция для показа: в сборке — текст в корне папки программы, в исходниках — `docs/DEMO.md`."""
    built = os.path.join(app_dir, DEMO_GUIDE)
    return built if os.path.isfile(built) else os.path.join(app_dir, "docs", "DEMO.md")


def qt_plugins_dir() -> str:
    """Папка плагинов Qt внутри пакета PyQt5 (пустая строка, если её нет)."""
    import PyQt5
    d = os.path.join(os.path.dirname(os.path.abspath(PyQt5.__file__)), "Qt5", "plugins")
    return d if os.path.isdir(os.path.join(d, "platforms")) else ""


def install_russian(app) -> None:
    """Русские подписи стандартных кнопок Qt (Yes/No → Да/Нет)."""
    from PyQt5.QtCore import QLibraryInfo, QLocale, QTranslator
    tr = QTranslator(app)
    if tr.load(QLocale("ru_RU"), "qtbase", "_", QLibraryInfo.location(QLibraryInfo.TranslationsPath)):
        app.installTranslator(tr)


def main(app_dir: str) -> int:
    # PyQt5 сам сообщает Qt путь к плагинам, но не в Юникоде: из папки с русскими буквами окно
    # не запускается («no Qt platform plugin could be initialized»). Задаём путь явно.
    plugins = qt_plugins_dir()
    if plugins:
        QCoreApplication.addLibraryPath(plugins)
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setApplicationName("Digger")
    install_russian(app)
    w = MainWindow(app_dir)
    w.show()
    args = [a for a in sys.argv[1:] if os.path.exists(a)]
    if args:
        w.add_files(args)
    if "--smoke" in sys.argv:      # проверка сборки: окно открылось — выходим
        QTimer.singleShot(500, app.quit)
    return app.exec_()
