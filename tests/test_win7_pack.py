# -*- coding: utf-8 -*-
"""Проверочный набор для Win7 (tools/win7_pack): запуск проверок, изоляция ошибок, состав отчёта."""
import json
import os
import subprocess
import sys
import zipfile

import pytest

PACK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "win7_pack")
sys.path.insert(0, PACK)
rc = pytest.importorskip("run_checks", reason="нет tools/win7_pack")


def _checks(tmp_path):
    d = tmp_path / "checks"
    d.mkdir()
    (d / "good.py").write_text("def run(ctx):\n    ctx.todo('сделать А')\n    return {'x': 1}\n", encoding="utf-8")
    (d / "bad.py").write_text("def run(ctx):\n    raise RuntimeError('сломано')\n", encoding="utf-8")
    (d / "exits.py").write_text("import sys\ndef run(ctx):\n    sys.exit(3)\n", encoding="utf-8")
    (d / "helper.py").write_text("X = 1\n", encoding="utf-8")        # без run — не проверка
    (d / "_private.py").write_text("def run(ctx):\n    return {}\n", encoding="utf-8")
    (d / "ЧТО СДЕЛАТЬ.txt").write_text("Статичный список\n", encoding="utf-8")
    return str(d)


def test_errors_are_isolated(tmp_path):
    checks = _checks(tmp_path)
    app = tmp_path / "Моя программа"
    app.mkdir()
    results, todo = rc.run_all(str(app), checks, str(tmp_path / "out"), log=lambda *_: None)
    by = {r["name"]: r for r in results}
    assert set(by) == {"bad", "exits", "good"}
    assert by["good"]["status"] == "ok" and by["good"]["x"] == 1
    assert by["bad"]["status"] == "error" and "сломано" in by["bad"]["traceback"]
    assert by["exits"]["status"] == "error"
    assert todo == ["сделать А"]


def test_report_zip(tmp_path):
    checks = _checks(tmp_path)
    app = tmp_path / "app"
    (app / "logs").mkdir(parents=True)
    (app / "logs" / "x.log").write_text("лог", encoding="utf-8")
    out = str(tmp_path / "out")
    results, todo = rc.run_all(str(app), checks, out, log=lambda *_: None)
    path = rc.build_report(str(app), out, results, todo, checks)
    assert os.path.basename(path).startswith("отчёт_") and path.endswith(".zip")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert {"results.json", "отчёт.md", "ЧТО СДЕЛАТЬ.txt", "logs/x.log"} <= set(names)
        assert len(json.loads(z.read("results.json").decode("utf-8"))) == 3
        todo_text = z.read("ЧТО СДЕЛАТЬ.txt").decode("utf-8")
        assert "Статичный список" in todo_text and "сделать А" in todo_text
    # второй запуск в тот же день не затирает первый
    assert rc.build_report(str(app), out, results, todo, checks) != path


def test_only_and_order(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    for n in ("zzz", "window", "sysinfo", "aaa"):
        (d / (n + ".py")).write_text("def run(ctx):\n    return {}\n", encoding="utf-8")
    assert rc.discover(str(d)) == ["sysinfo", "window", "aaa", "zzz"]
    results, _ = rc.run_all(str(tmp_path), str(d), str(tmp_path / "o"), only=["aaa"], log=lambda *_: None)
    assert [r["name"] for r in results] == ["aaa"]


def test_default_app_dir():
    assert rc.default_app_dir("/x/Digger/checks") == "/x/Digger"
    assert rc.default_app_dir("/repo/tools/win7_pack") == "/repo"


def test_sysinfo_survives_everything(tmp_path):
    import sysinfo
    ctx = rc.Context(str(tmp_path), str(tmp_path))
    res = sysinfo.run(ctx)
    assert res["status"] == "ok"
    for key in ("windows", "codepages", "screen", "proxy", "disk", "dlls"):
        assert key in res
    assert res["disk"]["free_mb"] > 0


def test_selftest_check(tmp_path):
    import selftest
    app = tmp_path / "Папка с пробелом"
    app.mkdir()
    (app / "run.py").write_text("import sys\nprint('1 passed')\nsys.exit(0)\n", encoding="utf-8")
    ctx = rc.Context(str(app), str(tmp_path))
    res = selftest.run(ctx)
    assert res["status"] == "ok" and res["tail"] == ["1 passed"]
    (app / "run.py").write_text("import sys\nprint('1 failed')\nsys.exit(1)\n", encoding="utf-8")
    res = selftest.run(ctx)
    assert res["status"] == "fail" and "crashed_tests" not in res


CRASHING = """import os, sys
args = sys.argv[sys.argv.index('--selftest') + 1:]
tests = ['tests/test_a.py::test_one', 'tests/test_b.py::test_qt[x-1]', 'tests/test_c.py::test_two',
         'tests/test_d.py::test_native']
bad = ('tests/test_b.py::test_qt[x-1]', 'tests/test_d.py::test_native')
skip = [a.split('=', 1)[1] for a in args if a.startswith('--deselect=')]
for t in tests:
    if t in skip:
        continue
    if '-vv' in args:
        sys.stdout.write(t + ' ')
    if t in bad:
        sys.stdout.flush()
        os._exit(70)        # обрыв вместо кода pytest
    sys.stdout.write('PASSED [ 50%]\\n' if '-vv' in args else '.')
print('\\n2 passed, 2 deselected')
"""


def test_selftest_check_finds_crashing_tests_and_runs_the_rest(tmp_path):
    import selftest
    app = tmp_path / "Папка с пробелом"
    app.mkdir()
    (app / "run.py").write_text(CRASHING, encoding="utf-8")
    res = selftest.run(rc.Context(str(app), str(tmp_path)))
    assert res["status"] == "fail" and res["returncode"] == 70
    assert res["crashed_tests"] == ["tests/test_b.py::test_qt[x-1]", "tests/test_d.py::test_native"]
    assert res["rest_returncode"] == 0 and res["rest_tail"][-1] == "2 passed, 2 deselected"
    verbose = (tmp_path / "selftest_verbose.txt").read_text(encoding="utf-8")
    assert verbose.count("=== -vv") == 3 and "--deselect=tests/test_d.py::test_native" in verbose


def test_selftest_last_started():
    import selftest
    assert selftest.last_started("tests/a.py::t1 PASSED [ 1%]\ntests/a.py::t2 \nFatal Python error: Aborted\n"
                                 '  File "x.py", line 3 in t2\n') == "tests/a.py::t2"
    assert selftest.last_started("tests/a.py::t1 PASSED [ 1%]\n") == "" and selftest.last_started("....") == ""
    assert selftest.crashed(1073741845) and selftest.crashed(-6) and not selftest.crashed(1) and not selftest.crashed(None)


def test_window_check_snapshot(tmp_path):
    pytest.importorskip("PyQt5")
    root = os.path.dirname(PACK.rstrip(os.sep))
    root = os.path.dirname(root)
    png = tmp_path / "Снимок.png"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([sys.executable, os.path.join(PACK, "window.py"), "--snap", str(png), root], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    assert p.returncode == 0, p.stdout.decode("utf-8", "replace")
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_downloads_report_and_offline(tmp_path):
    import downloads
    rows = [{"part": "NE555", "status": "ok", "valid": 1, "leads": 3, "seconds": 4.2, "budget": False,
             "not_whitelisted": ["files.example"], "sources": [{"id": "ddg", "status": "ok", "leads": 3, "pdfs": 1}],
             "attempts": [
                 {"step": "fetch", "host": "a.com", "ok": False, "failure_class": "site_protected", "error": "HTTP 403"},
                 {"step": "crawl", "host": "b.com", "found": 0, "detail": "страница на b.com: ссылок на PDF нет"},
                 {"step": "fetch", "host": "ti.com", "ok": True, "valid": True, "pages": 12, "size": 204800,
                  "has_text": True, "reason": "", "active": []}]},
            {"part": "LM358", "status": "no_leads", "valid": 0, "leads": 0, "seconds": 150.0, "budget": True,
             "not_whitelisted": [], "sources": [], "attempts": []}]
    hosts = [{"host": "a.com", "tried": 1, "downloaded": 0, "valid": 0, "failures": {"site_protected": 1}}]
    text = downloads.md(rows, hosts, "Чипов 2")
    assert "| NE555 | ok | 1 | 3 | 4.2 | a.com: ✘ site_protected; обход b.com: страница на b.com: ссылок на PDF нет; " \
           "ti.com: ✔ 12 стр., 200 КБ |" in text
    assert "| LM358 | no_leads (время вышло) |" in text and "ddg: ok (3)" in text
    assert "| a.com | 1 | 0 | 0 | site_protected — 1 |" in text and "- files.example" in text

    assert downloads.run(rc.Context(str(tmp_path), str(tmp_path)))["status"] == "fail"      # нет data/sources.json
    assert "downloads" in rc.ORDER and rc.ORDER.index("downloads") > rc.ORDER.index("adapters")


def _img(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


def test_photos_found_next_to_program_in_tmp_and_in_subfolders(tmp_path):
    """Выезд 2: папку «фото» клали в tmp/ — проверка её не видела."""
    app = tmp_path / "Моя программа"
    app.mkdir()
    assert rc.find_photos(str(app))[0] == str(app / "фото")           # нигде нет — основная папка
    _img(app / "tmp" / "фото" / "Вырезанные" / "W25Q64.JPG")
    path, rows = rc.find_photos(str(app))
    assert path == str(app / "tmp" / "фото") and rc.Context(str(app), str(tmp_path)).photos_dir == path
    _img(app / "фото" / "NE555.png")
    assert rc.find_photos(str(app))[0] == str(app / "фото")           # рядом с программой — главнее


def test_photos_report_says_why_files_are_not_seen(tmp_path):
    app = tmp_path / "app"
    (app / "фото").mkdir(parents=True)
    (app / "фото" / "IMG_1.HEIC").write_bytes(b"x")
    (app / "фото" / "ПОЛОЖИТЕ ФОТО СЮДА.txt").write_text("x", encoding="utf-8")
    text = "\n".join(rc.photos_text(rc.find_photos(str(app))[1]))
    assert "картинок 0" in text and ".heic — 1" in text and ".txt" not in text
    assert "## Где искали фото" in rc.report_md([], [], rc.photos_text(rc.find_photos(str(app))[1]))


def test_ask_for_photos_waits_until_user_puts_them(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    said, opened, asked = [], [], []

    def ask(prompt):
        asked.append(prompt)
        if len(asked) == 2:
            _img(app / "фото" / "LM358.jpg")
        return ""

    assert rc.ask_for_photos(str(app), ask=ask, log=said.append, opener=opened.append) == 1
    assert opened == [str(app / "фото")] * 2 and any(str(app / "фото") in s for s in said)
    assert any("1 шт." in s for s in said)


def test_ask_for_photos_gives_up_and_survives_closed_input(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    asked = []
    assert rc.ask_for_photos(str(app), ask=lambda p: asked.append(p) or "", log=lambda s: None,
                             opener=lambda p: None) == 0
    assert len(asked) == rc.PHOTO_ASK

    def closed(prompt):
        raise EOFError

    assert rc.ask_for_photos(str(app), ask=closed, log=lambda s: None, opener=lambda p: None) == 0


def test_screens_check_in_order_and_build():
    import screens
    assert "screens" in rc.ORDER and rc.ORDER.index("screens") < rc.ORDER.index("live")
    assert screens._tool().endswith("screenshots.py")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import build_portable
    dst = [d for _s, d in build_portable.check_files()]
    assert "checks/screens.py" in dst and "checks/screenshots.py" in dst and "checks/search_cli.py" in dst


def test_screens_check_runs(tmp_path):
    pytest.importorskip("PyQt5")
    import screens
    root = os.path.dirname(os.path.dirname(PACK.rstrip(os.sep)))
    work = tmp_path / "w"
    work.mkdir()
    ctx = rc.Context(root, str(work), "screens")
    ctx.photos_dir = str(tmp_path / "нет")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    res = screens.run(ctx)
    assert res["status"] == "ok", res
    assert res["themes"]["light"] > 5 and res["themes"]["dark"] > 5


def test_notes_file_goes_into_report(tmp_path):
    import zipfile
    app = tmp_path / "прог"
    out = tmp_path / "out"
    app.mkdir()
    out.mkdir()
    (app / "впечатления.txt").write_text("окно ок", encoding="utf-8")
    z = rc.build_report(str(app), str(out), [], [], str(tmp_path))
    assert "впечатления.txt" in zipfile.ZipFile(z).namelist()


def test_screenshots_skip_dot_files_and_broken_photo(tmp_path, monkeypatch):
    """Выезд 2026-10-10: `._25Q64FV.JPG` с флешки из macOS сорвал все снимки."""
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    sys.path.insert(0, tools if os.path.isdir(tools) else os.path.dirname(PACK))
    shots = pytest.importorskip("screenshots")
    d = tmp_path / u"фото" / "ocr"
    d.mkdir(parents=True)
    (tmp_path / u"фото" / ".Spotlight-V100").mkdir()
    (tmp_path / u"фото" / ".Spotlight-V100" / "x.jpg").write_bytes(b"x")
    for name in ("._25Q64FV.JPG", "25Q64FV.JPG", "7972.jpg"):
        (d / name).write_bytes(b"x")
    monkeypatch.setattr(shots, "PHOTOS_DIR", str(tmp_path / u"фото"))
    photos = shots.sample_photos()
    assert [os.path.basename(p) for p in photos] == ["25Q64FV.JPG", "7972.jpg"]

    class Pipe:
        def analyze_image(self, path):
            if path.endswith("25Q64FV.JPG"):
                raise ValueError(u"Не удалось открыть изображение")
            return "report", ["variant"]

    class Window:
        pipe, got = Pipe(), []

        def _analyzed(self, res):
            self.got.append(res)

    w = Window()
    assert shots.analyze_first(w, photos) == photos[1] and w.got == [(photos[1], "report", ["variant"])]
    assert shots.analyze_first(w, photos[:1]) == ""
