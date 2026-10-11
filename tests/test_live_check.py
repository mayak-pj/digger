# -*- coding: utf-8 -*-
"""Живая проверка поиска (шаг 6.6): сводка по источникам, отчёт, сбой чипа — на fake_http, без сети."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
live_check = pytest.importorskip("live_check")

from digger.acquire.events import Event  # noqa: E402
from tests.fakes.fake_http import FakeHttp  # noqa: E402
from tests.test_acquire_orchestrator import ALL, TI, build, pdf_at, source  # noqa: E402
from tests.test_acquire_orchestrator import env as _env  # noqa: E402

env = _env             # фикстура оркестраторного теста: БД и PDF во временной папке


def ev(key, source, level="catalog", ts=0.0, **params):
    return Event(key=key, source=source, level=level, params=params, ts=ts or 1.0)


def test_per_source_outcome_leads_time_and_fetches():
    events = [ev("site.search", "a", ts=10.0), ev("site.found", "a", ts=12.5, n=3), ev("fetch.done", "a", ts=13.0),
              ev("fetch.failed", "a", ts=14.0),
              ev("engine.query", "b", ts=10.0), ev("engine.empty", "b", ts=11.0),
              ev("engine.query", "c", ts=10.0), ev("engine.captcha", "c", ts=10.2)]
    got = live_check.per_source(events)
    assert got["a"] == {"level": "catalog", "outcome": "found", "leads": 3, "queries": 1, "fetched": 1, "failed": 1,
                        "seconds": 2.5}
    assert got["b"]["outcome"] == "empty" and got["c"]["outcome"] == "skip" and got["b"]["leads"] == 0


def test_everywhere_asks_all_sources_and_report_has_matrix(env):
    fake = pdf_at(FakeHttp(), env[2], TI, ALL % "a")
    orch, bus = build(env, fake, [source("maker", "maker", TI), source("cat", "catalog", ALL % "a"),
                                  source("empty", "search")])
    rows = live_check.run_chips(orch, bus, ["NE555P", "LM358"], say=lambda s: None)
    assert [r["part"] for r in rows] == ["NE555P", "LM358"]
    first = rows[0]
    assert set(first["sources"]) == {"maker", "cat", "empty"}          # «искать везде» — спрошены все
    assert first["sources"]["maker"]["outcome"] == "found" and first["sources"]["empty"]["outcome"] == "empty"
    text = live_check.md(rows, [], "Чипов 2", ["maker", "cat", "empty", "never"])
    assert "| maker | ✔ (1 /" in text and "| empty | · (0 /" in text and "Ни разу не опрашивались: never" in text
    assert "## Ход поиска" in text and "NE555P" in text


def test_chip_error_does_not_stop_others(env):
    orch, bus = build(env, FakeHttp(), [source("maker", "maker", TI)])
    orig = orch.search

    def search(ctx, everywhere=False):
        if ctx.part == "BAD":
            raise RuntimeError("сломано")
        return orig(ctx, everywhere=everywhere)
    orch.search = search
    rows = live_check.run_chips(orch, bus, ["BAD", "NE555"], say=lambda s: None)
    assert rows[0]["status"] == "error" and "сломано" in rows[0]["error"] and rows[1]["part"] == "NE555"
    assert "error" in live_check.md(rows, [], "x", ["maker"])


def test_deadline_skips_remaining_chips(env):
    orch, bus = build(env, FakeHttp(), [source("maker", "maker", TI)])
    said = []
    rows = live_check.run_chips(orch, bus, ["A", "B"], deadline=1.0, say=said.append)
    assert rows == [] and "A, B" in said[0]


def test_live_check_in_pack_and_build():
    sys.path.insert(0, os.path.join(ROOT, "tools", "win7_pack"))
    import run_checks as rc
    assert "live" in rc.ORDER and rc.ORDER.index("live") > rc.ORDER.index("downloads")
    import build_portable
    assert ("checks/live_check.py") in [dst for _src, dst in build_portable.check_files()]
    import live
    from run_checks import Context
    assert live.run(Context(ROOT + "/tests", ROOT + "/tests"))["status"] == "fail"       # нет data/sources.json


def test_chips_match_trial():
    from digger.acquire import trial
    assert live_check.CHIPS == trial.CHIPS


def test_find_images_skips_dot_files(tmp_path):
    d = tmp_path / u"фото" / "ocr"
    d.mkdir(parents=True)
    for name in ("25Q64FV.JPG", "._25Q64FV.JPG", ".DS_Store", "7972.jpg", u"заметки.txt"):
        (d / name).write_bytes(b"x")
    assert [os.path.basename(p) for p in live_check.find_images(str(tmp_path))] == ["25Q64FV.JPG", "7972.jpg"]
