# -*- coding: utf-8 -*-
"""Снимки окна программы в PNG (ARCHITECTURE §5) — без экрана, для просмотра после шага, меняющего интерфейс.

    python tools/screenshots.py [--out my_reports/screens] [--prefix 7_2] [--themes light,dark] [--no-ocr]

Программа запускается во временной папке (настройки по умолчанию, пустая база): рабочие данные не трогаются.
На каждую тему: главное окно с образцами фото (первое распознано), вкладка «Документы», живая лента поиска
(пример поиска из ARCHITECTURE §4.8, поиск ещё идёт), то же окно тесным и большим, «Настройки», «Расширения».
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:            # встроенный Python (python38._pth) папку скрипта в путь не добавляет
    sys.path.append(HERE)

SAMPLES = ("stm32.png", "at24c02.png", "w25q64_rot.png", "lm358_180.png")


def make_app_dir(theme: str) -> str:
    """Временная папка программы (с русскими буквами в пути) с нужной темой в config.json."""
    app_dir = os.path.join(tempfile.mkdtemp(prefix="digger_shots_"), u"программа")
    os.makedirs(os.path.join(app_dir, "data"))
    shutil.copy(os.path.join(ROOT, "config.default.json"), app_dir)
    shutil.copytree(os.path.join(ROOT, "data", "i18n"), os.path.join(app_dir, "data", "i18n"))
    for fn in os.listdir(os.path.join(ROOT, "data")):
        if fn.endswith(".json"):
            shutil.copy(os.path.join(ROOT, "data", fn), os.path.join(app_dir, "data"))
    with open(os.path.join(app_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"ui": {"theme": theme}, "network": {"offline": True}}, f)
    return app_dir


PHOTO_EXT = (".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".webp")
PHOTOS_DIR = ""             # --photos: свои фото вместо образцов (проверка `screens` на рабочем ПК)
MAX_PHOTOS = 4


def sample_photos():
    if PHOTOS_DIR and os.path.isdir(PHOTOS_DIR):
        found = []
        for d, dirs, files in os.walk(PHOTOS_DIR):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            found += [os.path.join(d, f) for f in sorted(files)         # `._фото.JPG` — спутники с флешки из macOS
                      if not f.startswith(".") and f.lower().endswith(PHOTO_EXT)]
        if found:
            return found[:MAX_PHOTOS]
    d = os.path.join(ROOT, "tests", "samples")
    if not os.path.isdir(d):
        try:
            sys.path.insert(0, os.path.join(ROOT, "tests"))
            import make_samples
            make_samples.main()
        except Exception:       # noqa — в сборке для Win7 нет tests/: окно снимается без фото
            return []
    return [os.path.join(d, n) for n in SAMPLES if os.path.isfile(os.path.join(d, n))]


def demo_search():
    """События примера поиска (`tools/search_cli.py`) с уровнями источников — до подтверждения, оно ещё «идёт»."""
    from digger.acquire.events import Event
    from search_cli import DEMO
    by_kind = {"local": "local", "maker": "maker", "site": "catalog", "market": "russian"}
    by_lang = {"en": "search", "zh": "china", "ru": "russian"}
    events, level = [], ""
    for key, lang, params in DEMO:
        kind = key.split(".")[0]
        if kind in by_kind or kind == "engine":
            level = by_kind.get(kind, by_lang[lang]) if lang == "en" or kind in ("local", "market") else by_lang[lang]
        if kind in ("confirm", "result"):
            level = ""
        events.append(Event(key, dict(params), lang=lang, level=level, source=str(params.get("site") or params.get("engine") or "")))
        if key == "confirm.search":
            break
    return events


def save(widget, path: str) -> None:
    from PyQt5.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    widget.grab().save(buf, "PNG")
    with open(path, "wb") as f:               # путь с русскими буквами: пишем сами, не через Qt
        f.write(bytes(buf.data()))
    print(path)


def analyze_first(w, photos) -> str:
    """Распознать первое фото, которое читается: испорченный файл не срывает снимки. Возвращает его путь."""
    for path in photos:
        try:
            r, variants = w.pipe.analyze_image(path)
        except Exception as e:      # noqa — нечитаемая картинка, сбой распознавания
            print("фото пропущено: %s (%s: %s)" % (path, type(e).__name__, e))
            continue
        w._analyzed((path, r, variants))
        return path
    return ""


def wait_idle(app, w, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if w.is_idle():
            break
        time.sleep(0.01)
    app.processEvents()


def demo_documents(w) -> None:
    """Вкладки «Документы» и «Почему» с тремя примерами записей поиска (выдуманные, не настоящие datasheet)."""
    from digger.acquire.models import AcquisitionRecord, DocFacts, Evidence, Lead, Verdict
    from digger.core.models import ChipReport
    rows = (("confirmed", 92, "ti", "https://www.ti.com/lit/ds/ne555.pdf", [("E1", 30, u"NE555P precision timer", 1),
                                                                            ("E6", 10, u"Texas Instruments", 1)], []),
            ("needs_user", 48, "alldatasheet", "https://www.alldatasheet.com/ne555.pdf",
             [("E1", 30, u"NE555 family", 2), ("E7", -10, u"DIP-8", 1), ("E12", 0, "", 0)], ["package_conflict"]),
            ("rejected", 12, "bing", "https://example.org/other.pdf", [("E11", -30, u"LM358", 1)],
             ["no_part_match"]))
    recs = [AcquisitionRecord(part="NE555P", lead=Lead(url=u, kind="pdf", source_id=src),
                              facts=DocFacts(pages=12, has_text=True, doc_type="datasheet"),
                              verdict=Verdict(status=st, score=sc, reasons=rs,
                                              evidence=[Evidence(code=c, points=p, detail=d, page=pg) for c, p, d, pg in ev]))
            for st, sc, src, u, ev, rs in rows]
    r = next((d["report"] for d in w.items.values() if d["report"]), None)
    if r is None:
        path = next(iter(w.items), None)
        if path is None:
            return
        r = w.items[path]["report"] = ChipReport(image_path=path, chosen_part="NE555P")
        w.list.setCurrentRow(0)
    r.records = recs
    w.show_current()
    w.docs_table.selectRow(1)


def shoot(app, theme: str, out_dir: str, prefix: str, ocr: bool) -> None:
    from digger.gui.dialogs import ExtensionsDialog, SettingsDialog
    from digger.gui.main_window import MainWindow
    app_dir = make_app_dir(theme)
    w = MainWindow(app_dir)
    try:
        w.show()
        photos = sample_photos()
        w.add_files(photos)
        wait_idle(app, w)                                       # фото и миниатюры читаются в фоне
        if ocr and photos and w.ctx.modules["ocr"].is_available():
            analyze_first(w, photos)                            # здесь можно в потоке окна: экрана нет
        wait_idle(app, w)

        def name(part):
            return os.path.join(out_dir, "%s_%s_%s.png" % (prefix, theme, part))
        save(w, name("window"))
        w.tabs.setCurrentIndex(1)
        app.processEvents()
        save(w, name("documents"))
        demo_documents(w)
        w.tabs.setCurrentIndex(1)
        app.processEvents()
        save(w, name("documents_filled"))
        w.tabs.setCurrentIndex(2)
        app.processEvents()
        save(w, name("why"))
        from digger.acquire.conclusion import Conclusion, SiteNote
        c = Conclusion(part="NE555P", sources=14, languages=3, seconds=52)
        c.protected = [SiteNote("alldatasheet.com", "captcha", "https://www.alldatasheet.com/view.jsp?Searchword=NE555P", "search")]
        c.protected += [SiteNote(site, "captcha", "https://%s/search?q=NE555P+datasheet" % site, "search")
                        for site in ("duckduckgo.com", "mojeek.com", "search.brave.com", "baidu.com", "sogou.com")]
        c.blocked = [SiteNote("ti.com", "dns")]
        c.not_whitelisted = [SiteNote("files.example.org", "", "https://files.example.org/ne555.pdf", "page")]
        next(d["report"] for d in w.items.values() if d["report"]).conclusion = c
        w.show_current()
        w.tabs.setCurrentIndex(0)
        app.processEvents()
        save(w, name("conclusion"))
        w.search_feed.begin()
        for event in demo_search():
            w.ctx.bus.publish(event)
        wait_idle(app, w)
        w.search_feed.set_running(True)
        for _ in range(40):                                     # плавная смена текста строки состояния — до конца
            app.processEvents()
            time.sleep(0.01)
        save(w, name("feed"))
        w.search_feed.set_running(False)
        for part, size in (("small", (1000, 640)), ("large", (1920, 1040))):     # раскладка в тесном и в большом окне
            w.resize(*size)
            for _ in range(5):
                app.processEvents()
            save(w, name(part))
        w.resize(1280, 820)
        app.processEvents()
        from digger.gui.dialogs import AdaptersDialog, DiagnosticsDialog
        diag = DiagnosticsDialog([{"ok": True, "category": u"Каталог", "name": "alldatasheet", "detail": "200",
                                   "url": "https://x.example"},
                                  {"ok": False, "category": u"Производитель", "name": "ti.com", "detail": "DNS",
                                   "url": "https://ti.com"}], w)
        ada = AdaptersDialog([{"status": "ok", "level": "catalog", "name": "alldatasheet", "leads": 3, "pdfs": 1, "detail": ""},
                              {"status": "captcha", "level": "web", "name": "bing", "leads": 0, "pdfs": 0, "detail": ""}], w)
        w.tabs.setCurrentIndex(3)                               # «Журнал»
        w.log(u"Пример строки журнала")
        app.processEvents()
        save(w, name("log"))
        inst = w.ext.get("blocked_sites").instance if w.ext and w.ext.get("blocked_sites") else None
        if inst is not None:                                    # вкладка «Сайты без доступа» (7.8) с примером данных
            from digger.acquire.access import AccessLog
            log = AccessLog(w.ctx.modules["local_db"])
            log.record_failure("ti.com", "network_blocked", "NE555P", "https://www.ti.com/x", "maker", 0.4)
            log.record_failure("st.com", "network_blocked", "L7805")
            log.record_ok("st.com")
            inst.access = lambda: log
            inst.view.reload()
            w.tabs.setCurrentWidget(inst.view)
            for _ in range(20):
                app.processEvents()
                time.sleep(0.02)
            save(w, name("blocked_sites"))
        hist = w.ext.get("search_history").instance if w.ext and w.ext.get("search_history") else None
        if hist is not None:                                    # вкладка «История поисков» (7.9)
            for part, st, src in (("NE555P", "confirmed", "local"), ("W25Q64JV", "probable", "winbond.com"),
                                  ("LM358", "not_found", "")):
                w.ctx.bus.emit("result." + st, part=part, source=src, seconds=12)
            hist.view.reload()
            w.tabs.setCurrentWidget(hist.view)
            for _ in range(20):
                app.processEvents()
                time.sleep(0.02)
            save(w, name("history"))
        sst = w.ext.get("search_stats").instance if w.ext and w.ext.get("search_stats") else None
        if sst is not None:                                     # вкладка «Статистика поиска» (7.10)
            from digger.acquire.events import EventBus
            from digger.acquire.stats import SearchStats, StatsRecorder
            stats = SearchStats(w.ctx.modules["local_db"])
            sbus = EventBus()
            rec = StatsRecorder(stats, sbus)
            for part, res, src, lang in (("W25Q64JV", "confirmed", "ddg", "en"), ("W25Q64JV", "not_found", "baidu", "zh"),
                                         ("LM358", "confirmed", "ddg", "en")):
                rec.begin(part)
                sbus.emit("engine.query", lang=lang, source=src, engine=src, query="q")
                sbus.emit("engine.found", lang=lang, source=src, engine=src, query="q", n=2)
                if res == "confirmed":
                    sbus.emit("fetch.done", lang=lang, source=src, file="a.pdf", size=1)
                sbus.emit("result." + res, queries=1, seconds=1)
            sst.stats = lambda: stats
            sst.view.reload()
            w.tabs.setCurrentWidget(sst.view)
            for _ in range(30):
                app.processEvents()
                time.sleep(0.02)
            save(w, name("stats"))
        for part, dlg in (("settings", SettingsDialog(w.ctx, w)), ("extensions", ExtensionsDialog(w.ext, w)),
                          ("diagnostics", diag), ("adapters", ada)):
            dlg.show()
            app.processEvents()
            save(dlg, name(part))
            dlg.close()
    finally:
        w.close()
        shutil.rmtree(os.path.dirname(app_dir), ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=u"Снимки окна Digger")
    ap.add_argument("--out", default=os.path.join(ROOT, "my_reports", "screens"))
    ap.add_argument("--prefix", default="window")
    ap.add_argument("--themes", default="light")
    ap.add_argument("--photos", default="", help=u"папка со своими фото (первые 4) вместо образцов")
    ap.add_argument("--no-ocr", action="store_true", help=u"не распознавать образец (быстрее)")
    args = ap.parse_args(argv)
    global PHOTOS_DIR
    PHOTOS_DIR = args.photos
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtCore import QCoreApplication
    from PyQt5.QtWidgets import QApplication, QMessageBox
    from digger.gui.main_window import install_russian, qt_plugins_dir
    plugins = qt_plugins_dir()
    if plugins:
        QCoreApplication.addLibraryPath(plugins)
    app = QApplication(sys.argv[:1])
    install_russian(app)
    for box in ("warning", "critical", "information"):      # без экрана на вопрос ответить некому
        setattr(QMessageBox, box, staticmethod(lambda *a, **k: 0))
    os.makedirs(args.out, exist_ok=True)
    for theme in [t.strip() for t in args.themes.split(",") if t.strip()]:
        shoot(app, theme, args.out, args.prefix, not args.no_ocr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
