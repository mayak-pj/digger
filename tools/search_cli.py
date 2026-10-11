# -*- coding: utf-8 -*-
"""Консольный наблюдатель поиска (ARCHITECTURE §4.8).

Одна строка состояния обновляется на месте (`\\r`) на языке текущего поиска; когда поиск закончен, ниже
печатается история с метками EN/CH/RU и счётчики. Если вывод идёт не в терминал (файл, журнал CI) —
каждое событие печатается отдельной строкой.

    python tools/search_cli.py W25Q64JVSIQ     настоящий поиск: источники, скачивание, проверка, итог
    python tools/search_cli.py NE555P --maker "Texas Instruments" --package DIP-8 --everywhere
    python tools/search_cli.py --demo          показать пример поиска из архитектуры
    python tools/search_cli.py --demo --ru     история с русским переводом каждой строки
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from digger.acquire.events import Event, EventBus, EventCounters, LANG_LABELS, render  # noqa: E402

ICONS = {"": u"⏳", "ok": u"✔", "found": u"✔", "empty": u"·", "fail": u"✘", "skip": u"⤼"}


def display_width(text: str) -> int:
    """Ширина строки в знакоместах консоли: иероглиф занимает два."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def fit(text: str, width: int) -> str:
    """Обрезать до `width` знакомест, чтобы строка состояния не переносилась."""
    if display_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + u"…"


def label(lang: str) -> str:
    text = LANG_LABELS.get(lang, lang.upper()[:4])
    return text + " " * (5 - display_width(text))


class ConsoleObserver:
    """Подписчик EventBus: `bus.subscribe(ConsoleObserver(sys.stdout))`, в конце поиска — `close()`."""

    def __init__(self, out=None, width=None, translate=False):
        self.out = out or sys.stdout
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        self.width = width
        self.translate = translate        # под каждой строкой истории — русский перевод
        self.history = []
        self.counters = EventCounters()
        self.counts = self.counters.counts
        self._last = 0

    def __call__(self, event: Event) -> None:
        self.counters.add(event)
        if event.final:
            self.history.append(event)
        if not self.tty:
            self._write(self._history_line(event))
            return
        width = (self.width or shutil.get_terminal_size((80, 24)).columns) - 1
        line = fit(label(event.display_lang) + render(event), width)
        pad = max(0, self._last - display_width(line))       # затереть хвост прежней строки (Win7 не знает ESC[K)
        self._last = display_width(line)
        self.out.write("\r" + line + " " * pad)
        self.out.flush()

    def close(self) -> None:
        """Конец поиска: история (в терминале) и счётчики."""
        if self.tty:
            self.out.write("\n")
            for event in self.history:
                self._write(self._history_line(event))
        self._write(self.counters.text())
        self.history, self._last = [], 0

    def _history_line(self, event: Event) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
        line = "%s %s %s%s" % (stamp, ICONS.get(event.outcome, "?"), label(event.display_lang), render(event))
        if self.translate and event.display_lang != "ru":
            line += "\n" + " " * 16 + render(event, "ru")
        return line

    def _write(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()


DEMO = [  # пример одного поиска из ARCHITECTURE §4.8
    ("local.search", "ru", {}),
    ("local.empty", "ru", {}),
    ("maker.search", "en", {"site": "winbond.com"}),
    ("maker.empty", "en", {"site": "winbond.com"}),
    ("engine.no_key", "en", {"engine": "Google", "query": "W25Q64JV datasheet pdf"}),
    ("engine.query", "en", {"engine": "DuckDuckGo", "query": "W25Q64JV datasheet pdf"}),
    ("engine.found", "en", {"engine": "DuckDuckGo", "query": "W25Q64JV datasheet pdf", "n": 7}),
    ("site.search", "en", {"site": "alldatasheet.com"}),
    ("site.found", "en", {"site": "alldatasheet.com", "n": 1}),
    ("site.search", "en", {"site": "datasheetarchive.com"}),
    ("site.found", "en", {"site": "datasheetarchive.com", "n": 2}),
    ("engine.query", "zh", {"engine": u"百度", "query": u"W25Q64JV 数据手册"}),
    ("engine.found", "zh", {"engine": u"百度", "query": u"W25Q64JV 数据手册", "n": 5}),
    ("site.search", "zh", {"site": u"立创商城 szlcsc.com"}),
    ("site.found_pdf", "zh", {"site": u"立创商城 szlcsc.com", "n": 1, "pdfs": 1}),
    ("fetch.start", "zh", {"file": "W25Q64JV_datasheet.pdf", "site": "szlcsc.com"}),
    ("fetch.progress", "zh", {"file": "W25Q64JV_datasheet.pdf", "size": 1258291, "percent": 64}),
    ("fetch.done", "zh", {"file": "W25Q64JV_datasheet.pdf", "size": 1966080}),
    ("quarantine.placed", "zh", {}),
    ("validate.ok", "zh", {"pages": 72}),
    ("verify.result", "zh", {"part_ok": True, "package": "SOIC-8", "package_ok": True, "maker": "Winbond",
                             "maker_ok": True, "score": 85}),
    ("engine.captcha", "ru", {"engine": u"Яндекс", "query": u"W25Q64JV даташит", "minutes": 30}),
    ("market.search", "ru", {"site": "chipdip.ru"}),
    ("market.found", "ru", {"site": "chipdip.ru", "n": 3, "pdfs": 1}),
    ("confirm.search", "en", {}),
    ("confirm.identical", "en", {"site": "lcsc.com"}),
    ("result.confirmed", "en", {"queries": 14, "seconds": 42}),
]


def demo(out, pause: float, translate: bool) -> None:
    bus = EventBus()
    observer = ConsoleObserver(out, translate=translate)
    bus.subscribe(observer)
    for key, lang, params in DEMO:
        bus.emit(key, lang=lang, **params)
        if pause:
            time.sleep(pause)
    observer.close()


def live(part: str, maker: str, package: str, everywhere: bool, out, translate: bool) -> int:
    """Полный поиск по настройкам программы; Ctrl+C — отмена поиска, а не обрыв программы."""
    import signal

    from digger.acquire.models import PhotoContext
    from digger.acquire.orchestrator import from_context
    from digger.core.config import load_config, setup_logging
    from digger.core.interfaces import Context
    from digger.modules.localdb_sqlite import SQLiteLocalDB

    cfg = load_config(ROOT)
    ctx = Context(cfg, ROOT, setup_logging(ROOT, cfg))
    db = SQLiteLocalDB({}, ctx)
    bus = EventBus()
    observer = ConsoleObserver(out, translate=translate)
    bus.subscribe(observer)
    orch = from_context(ctx, bus, db=db)
    previous = signal.signal(signal.SIGINT, lambda *a: orch.cancel())
    try:
        res = orch.search(PhotoContext(part=part, manufacturer=maker, package=package), everywhere=everywhere)
    finally:
        signal.signal(signal.SIGINT, previous)
        observer.close()
        db.close()
    if res.path:
        out.write(u"файл: %s\n" % res.path)
    if res.conclusion is not None:            # заключение при неудаче (§4.11)
        out.write(u"\n" + res.conclusion.text() + u"\n")
    return 0 if res.status in ("confirmed", "probable") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Консольный наблюдатель поиска Digger")
    ap.add_argument("part", nargs="?", help="партномер или код маркировки: настоящий поиск")
    ap.add_argument("--maker", default="", help="производитель, если известен")
    ap.add_argument("--package", default="", help="корпус, если известен (SOIC-8)")
    ap.add_argument("--everywhere", action="store_true", help="искать везде: не останавливаться на подтверждённом")
    ap.add_argument("--demo", action="store_true", help="показать пример поиска (без сети)")
    ap.add_argument("--fast", action="store_true", help="без пауз между событиями")
    ap.add_argument("--ru", action="store_true", help="под каждой строкой истории — русский перевод")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):          # консоль Win7 (cp866) не знает иероглифов — не падать
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    if args.demo:
        demo(sys.stdout, 0.0 if args.fast else 0.5, args.ru)
        return 0
    if not args.part:
        ap.print_help()
        return 0
    return live(args.part, args.maker, args.package, args.everywhere, sys.stdout, args.ru)


if __name__ == "__main__":
    sys.exit(main())
