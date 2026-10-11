# -*- coding: utf-8 -*-
"""Живая проверка поиска (ARCHITECTURE §9): 12 эталонных чипов × все источники → reports/live_<дата>.md.

    python tools/live_check.py [--parts NE555 LM358 ...] [--photos папка] [--out reports] [--minutes 45]

Каждый чип ищется настоящим оркестратором (`from_context`, «искать везде» — опрашиваются все источники, а не только
до первого подтверждения) по настройкам программы. Библиотека и статистика проверки — отдельные, во временной папке
рядом с отчётом: рабочая библиотека не меняется. Фото из папки (`--photos`) проходят весь путь программы: улучшение →
распознавание → партномер → поиск → заключение. Отчёт: итог по чипам, матрица «источник × чип» (найдено/пусто/
неудача/не опрашивался), время по источникам, заключения, ход поиска. Без метки `live` в pytest не запускается.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from digger.acquire.events import Event, EventBus, render  # noqa: E402

CHIPS = ("NE555", "LM358", "AT24C02", "W25Q64JV", "STM32F103C8T6", "GD32F103C8T6", "CH340G", "AMS1117", "ESP8266EX",
         "PMS150C", "STC89C52RC", "74HC595")
QUERY_KINDS = ("engine", "site", "maker", "market", "cache")
RANK = {"found": 4, "ok": 3, "empty": 2, "fail": 1, "skip": 0}        # лучший исход источника за поиск чипа
MARK = {"found": u"✔", "ok": u"✔", "empty": u"·", "fail": u"✘", "skip": u"⤼", "": u"—"}
PHOTO_EXT = (".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".webp")


def per_source(events: Iterable[Event]) -> Dict[str, Dict[str, Any]]:
    """События одного поиска → по источнику: исход, ссылок, запросов, скачано, время (сумма «запрос → ответ»)."""
    out: Dict[str, Dict[str, Any]] = {}
    started: Dict[str, float] = {}
    for e in events:
        if not e.source or e.kind not in QUERY_KINDS + ("fetch", "crawl"):
            continue
        row = out.setdefault(e.source, {"level": e.level, "outcome": "", "leads": 0, "queries": 0, "fetched": 0,
                                        "failed": 0, "seconds": 0.0})
        if e.kind == "fetch":
            row["fetched"] += int(e.key == "fetch.done")
            row["failed"] += int(e.key == "fetch.failed")
            continue
        if e.kind == "crawl":
            continue
        if not e.outcome:
            started[e.source] = e.ts
            row["queries"] += 1
            continue
        if e.source in started:
            row["seconds"] += max(0.0, e.ts - started.pop(e.source))
        try:
            row["leads"] += int(e.params.get("n") or 0) if e.outcome == "found" else 0
        except (TypeError, ValueError):
            pass
        if RANK[e.outcome] >= RANK.get(row["outcome"], -1):
            row["outcome"] = e.outcome
    for row in out.values():
        row["seconds"] = round(row["seconds"], 1)
    return out


def chip_row(res: Any, events: List[Event], registry_ids: Sequence[str], seconds: float) -> Dict[str, Any]:
    sources = per_source(events)
    rows = {"part": res.part, "status": res.status, "reason": res.reason, "queries": res.queries,
            "downloads": res.downloads, "seconds": round(seconds, 1), "path": os.path.basename(res.path or ""),
            "sources": sources, "silent": [s for s in registry_ids if s not in sources],
            "conclusion": res.conclusion.text() if getattr(res, "conclusion", None) else "",
            "history": [u"%s %s" % (time.strftime("%H:%M:%S", time.localtime(e.ts)), render(e))
                        for e in events if e.final], "error": ""}
    return rows


def md(rows: List[Dict[str, Any]], photos: List[Dict[str, Any]], note: str, source_ids: Sequence[str]) -> str:
    lines = ["# Живая проверка поиска", "", note, "",
             "| Чип | Итог | Причина остановки | Запросов | Скачано | Время, с | Файл |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["part"], r["status"], r["reason"], r["queries"], r["downloads"], r["seconds"],
            r["path"] or r["error"][:60].replace("|", "/") or "—"))
    seen = [s for s in source_ids if any(s in r["sources"] for r in rows)]
    if rows:
        lines += ["", "## Источники × чипы", "", u"✔ — ссылки найдены, · — пусто, ✘ — неудача (сеть, защита, разбор), "
                  u"⤼ — пропущен (нет ключа, капча), — — не опрашивался (поиск закончился раньше). "
                  u"В скобках: ссылок / секунд.", "",
                  "| Источник | " + " | ".join(r["part"] for r in rows) + " |", "|---|" + "---|" * len(rows)]
        for sid in source_ids:
            cells = []
            for r in rows:
                s = r["sources"].get(sid)
                cells.append(u"—" if not s else u"%s (%d / %s)" % (MARK[s["outcome"]], s["leads"], s["seconds"]))
            lines.append("| %s | %s |" % (sid, " | ".join(cells)))
        mute = [sid for sid in source_ids if sid not in seen]
        if mute:
            lines += ["", "Ни разу не опрашивались: " + ", ".join(mute)]
    fetched = [(r["part"], sid, s["fetched"], s["failed"]) for r in rows for sid, s in sorted(r["sources"].items())
               if s["fetched"] or s["failed"]]
    if fetched:
        lines += ["", "## Скачивание", "", "| Чип | Источник | Скачано | Не скачано |", "|---|---|---|---|"]
        lines += ["| %s | %s | %d | %d |" % f for f in fetched]
    if photos:
        lines += ["", "## Фото: от распознавания до заключения", "",
                  "| Фото | Прочитано | Партномер | Итог | Документ | Время, с | Ошибка |", "|---|---|---|---|---|---|---|"]
        for p in photos:
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                p["file"], p["text"].replace("|", "/")[:50], p["part"] or "—", p["status"], p["document"] or "—",
                p["seconds"], p["error"][:60].replace("|", "/")))
    lines += ["", "## Заключения при неудаче", ""]
    lines += [u"### %s\n\n%s\n" % (r["part"], r["conclusion"]) for r in rows if r["conclusion"]] or ["Нет."]
    lines += ["", "## Ход поиска", ""]
    for r in rows:
        lines += [u"### %s" % r["part"], "", "```"] + r["history"][:80] + ["```", ""]
    return "\n".join(lines) + "\n"


def make_orchestrator(app_dir: str, work_dir: str, http: Any = None):
    """Оркестратор по настройкам программы, но с библиотекой, базой и карантином в work_dir."""
    from digger.acquire.orchestrator import from_context
    from digger.core.config import load_config, setup_logging
    from digger.core.interfaces import Context
    from digger.modules.localdb_sqlite import SQLiteLocalDB
    cfg = load_config(app_dir)
    paths = dict(cfg.get("paths", {}))
    paths.update(db=os.path.join(work_dir, "live.sqlite"), library_dir=os.path.join(work_dir, "library"),
                 quarantine_dir=os.path.join(work_dir, "quarantine"))
    cfg["paths"] = paths
    ctx = Context(cfg, app_dir, setup_logging(app_dir, cfg))
    db = SQLiteLocalDB({}, ctx)
    bus = EventBus()
    return from_context(ctx, bus, http=http, db=db), bus, db


def run_chips(orch: Any, bus: EventBus, parts: Sequence[str], deadline: float = 0.0,
              say: Callable[[str], Any] = print) -> List[Dict[str, Any]]:
    from digger.acquire.models import PhotoContext
    ids = [en.id for en in orch.registry.entries(include_disabled=True)]
    rows = []
    for part in parts:
        if deadline and time.time() > deadline:
            say("время вышло — остальные чипы пропущены: " + ", ".join(parts[len(rows):]))
            break
        events: List[Event] = []
        unsubscribe = bus.subscribe(events.append)
        t0 = time.time()
        try:
            res = orch.search(PhotoContext(part=part), everywhere=True)
            row = chip_row(res, events, ids, time.time() - t0)
        except BaseException as e:  # noqa — сбой одного чипа не должен ронять остальные
            if isinstance(e, KeyboardInterrupt):
                raise
            row = {"part": part, "status": "error", "reason": "", "queries": 0, "downloads": 0,
                   "seconds": round(time.time() - t0, 1), "path": "", "sources": per_source(events), "silent": [],
                   "conclusion": "", "history": [], "error": "%s: %s" % (type(e).__name__, e),
                   "traceback": traceback.format_exc()}
        finally:
            unsubscribe()
        say("%-14s %-10s запросов %s, скачано %s, %s с" % (part, row["status"], row["queries"], row["downloads"],
                                                          row["seconds"]))
        rows.append(row)
    return rows


def find_images(folder: str) -> List[str]:
    out = []
    for d, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        out += [os.path.join(d, f) for f in sorted(files)                # `._фото.JPG` — спутники с флешки из macOS
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in PHOTO_EXT]
    return out


def run_photos(app_dir: str, orch: Any, photos: Sequence[str], deadline: float = 0.0,
               say: Callable[[str], Any] = print, limit: int = 10) -> List[Dict[str, Any]]:
    """Фото → ChipPipeline.analyze_image → search_web (через оркестратор) → партномер, документ, заключение."""
    from digger.core.pipeline import ChipPipeline, create_context
    rows = []
    if not photos:
        return rows
    prog = create_context(app_dir)
    web = prog.modules.get("web_search")
    if web is not None and hasattr(web, "_orch"):
        web._orch = orch                        # поиск в изолированной библиотеке проверки
    pipe = ChipPipeline(prog)
    for path in list(photos)[:limit]:
        row = {"file": os.path.basename(path), "text": "", "part": "", "status": "", "document": "", "seconds": 0.0,
               "error": "", "log": []}
        t0 = time.time()
        if deadline and t0 > deadline:
            row["error"] = "время вышло"
            rows.append(row)
            continue
        try:
            rep, _variants = pipe.analyze_image(path)
            row["text"] = rep.ocr.best_text.replace("\n", " / ") if rep.ocr else ""
            row["part"] = rep.chosen_part
            if rep.chosen_part:
                pipe.search_web(rep)
            row["document"] = os.path.basename(rep.datasheet_path or "")
            row["status"] = "found" if rep.datasheet_path else ("no_part" if not rep.chosen_part else "not_found")
            row["log"] = list(rep.log)
        except BaseException as e:  # noqa
            if isinstance(e, KeyboardInterrupt):
                raise
            row["status"], row["error"] = "error", "%s: %s" % (type(e).__name__, e)
        row["seconds"] = round(time.time() - t0, 1)
        say("фото %-30s %s %s" % (row["file"][:30], row["status"], row["part"]))
        rows.append(row)
    return rows


def run_live(app_dir: str, work_dir: str, parts: Sequence[str] = CHIPS, photos_dir: str = "", minutes: float = 45.0,
             http: Any = None, say: Callable[[str], Any] = print) -> Dict[str, Any]:
    """Всё вместе: оркестратор → фото → чипы → (rows, photo_rows, текст отчёта)."""
    os.makedirs(work_dir, exist_ok=True)
    deadline = time.time() + minutes * 60
    t0 = time.time()
    orch, bus, db = make_orchestrator(app_dir, work_dir, http)
    try:
        source_ids = [en.id for en in orch.registry.entries(include_disabled=True)]
        photos = find_images(photos_dir) if photos_dir and os.path.isdir(photos_dir) else []
        photo_rows = run_photos(app_dir, orch, photos, deadline, say) if photos else []
        rows = run_chips(orch, bus, list(parts), deadline, say)
    finally:
        db.close()
    stat = {}
    for r in rows:
        stat[r["status"]] = stat.get(r["status"], 0) + 1
    note = u"Дата: %s. Чипов %d из %d: %s. Фото: %d. Всего %.0f мин." % (
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), len(rows), len(parts),
        ", ".join("%s — %d" % kv for kv in sorted(stat.items())) or "—", len(photo_rows), (time.time() - t0) / 60)
    return {"rows": rows, "photos": photo_rows, "stat": stat, "note": note, "sources": source_ids,
            "text": md(rows, photo_rows, note, source_ids)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Живая проверка поиска: эталонные чипы × все источники")
    ap.add_argument("--parts", nargs="*", default=list(CHIPS), help="партномера (по умолчанию 12 эталонных)")
    ap.add_argument("--photos", default="", help="папка с фото: распознавание → поиск → заключение")
    ap.add_argument("--out", default=os.path.join(ROOT, "reports"), help="куда писать live_<дата>.md")
    ap.add_argument("--minutes", type=float, default=45.0, help="общий предел времени")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    logging.getLogger("digger").setLevel(logging.WARNING)
    os.makedirs(args.out, exist_ok=True)
    res = run_live(ROOT, os.path.join(args.out, "live_tmp"), args.parts, args.photos, args.minutes)
    day = datetime.date.today().isoformat()
    path = os.path.join(args.out, "live_%s.md" % day)
    with open(path, "w", encoding="utf-8") as f:
        f.write(res["text"])
    with open(os.path.join(args.out, "live_%s.json" % day), "w", encoding="utf-8") as f:
        json.dump({"rows": res["rows"], "photos": res["photos"]}, f, ensure_ascii=False, indent=2, default=str)
    print("\nОтчёт: " + path)
    return 0 if res["stat"].get("confirmed") else 1


if __name__ == "__main__":
    sys.exit(main())
