# -*- coding: utf-8 -*-
"""Шаг 7.7 (+ выбор темы в настройках): заключение «Не найдено» — панель с кнопками, раздел в HTML-отчёте и ячейка CSV."""
import io
import json
import os
import sys

import pytest

pytest.importorskip("PyQt5")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_docs_tabs import _open  # noqa: E402
from test_photo_cards import _report  # noqa: E402

from digger.acquire.conclusion import Conclusion, SiteNote  # noqa: E402
from digger.acquire.site_actions import add_to_access_request, allow_domain  # noqa: E402
from digger.core.netsafe import SafeHttp  # noqa: E402
from digger.modules.report_html import HtmlReport, colors_from_tokens  # noqa: E402


def _conclusion():
    c = Conclusion(part="NE555P", sources=12, languages=3, seconds=40)
    c.protected = [SiteNote("alldatasheet.com", "captcha", "https://www.alldatasheet.com/x.html", "page")]
    c.blocked = [SiteNote("ti.com", "dns", "https://www.ti.com/search?q=NE555P", "search", level="maker")]
    c.not_whitelisted = [SiteNote("files.example.org", "", "https://files.example.org/ne555.pdf", "page")]
    return c


def test_report_html_and_csv_cell():
    r = _report("a.png", "NE555P", "no")
    r.conclusion = _conclusion()
    html = HtmlReport({}, None).render(r)
    assert u"Можно скачать вручную" in html and u"Нет доступа из сети производства" in html
    assert u"вне белого списка" in html and u"href='https://www.alldatasheet.com/x.html'" in html
    assert "href='https://www.ti.com" not in html          # закрытый сетью сайт не открыть
    assert u"Скачали вручную?" in html
    assert u"Заключение поиска" not in HtmlReport({}, None).render(_report("a.png", "NE555P", "no"))
    assert r.conclusion.csv_cell() == u"вручную: alldatasheet.com; нет доступа: ti.com; вне списка: files.example.org"
    assert json.dumps(r.to_dict(), default=str)             # в JSON-отчёт заключение попадает словарём


def test_panel_buttons_and_actions(window, tmp_path):
    w, app, (p,) = _open(window, tmp_path)
    r = _report(p, "NE555P", "no")
    r.conclusion = _conclusion()
    w._analyzed((p, r, []))
    w.list.setCurrentRow(0)
    panel = w.conclusion_panel
    assert not panel.isHidden() and panel.model.rowCount() == 3
    assert u"Проверено 12 источников" in panel.title.text()
    assert (panel.b_open.isEnabled(), panel.b_allow.isEnabled()) == (True, False)       # сайт под защитой
    panel.table.selectRow(1)                                                              # закрыт сетью
    assert (panel.b_open.isEnabled(), panel.b_add.isEnabled(), panel.b_allow.isEnabled()) == (False, True, False)
    panel.table.selectRow(2)                                                              # вне белого списка
    assert panel.b_allow.isEnabled() and panel.b_open.isEnabled()
    got = []
    panel.allow.connect(got.append)
    panel.b_allow.click()
    assert got[0].site == "files.example.org"
    w._analyzed((p, _report(p, "NE555P", "no"), []))                                      # без заключения — панели нет
    w.show_current()
    assert panel.isHidden()


def test_allow_domain_saves_config(tmp_path):
    http = SafeHttp({"allowed_domains": ["ti.com"]}, str(tmp_path / "q"), __import__("logging").getLogger("t"))
    assert not allow_domain(http, str(tmp_path), "www.ti.com")                            # уже разрешён поддоменом
    assert allow_domain(http, str(tmp_path), "Files.Example.org.")
    assert http.is_allowed("https://files.example.org/a.pdf")
    saved = json.load(io.open(str(tmp_path / "config.json"), encoding="utf-8"))
    assert saved["network"]["allowed_domains"] == ["files.example.org"]
    assert not allow_domain(http, str(tmp_path), "files.example.org")


def test_add_to_access_request():
    class Log:
        calls = []

        def record_failure(self, *a):
            self.calls.append(a)
            return True
    n = SiteNote("alldatasheet.com", "captcha", "https://www.alldatasheet.com/x.html", "page", level="catalog")
    assert add_to_access_request(Log(), n, "NE555P")
    assert Log.calls[0] == ("alldatasheet.com", "network_blocked", "NE555P", n.url, "catalog")
    assert not add_to_access_request(None, n, "NE555P")


def test_settings_theme_choice(window):
    from digger.gui.dialogs import SettingsDialog
    from digger.ui import theme as ui_theme
    w, app = window
    d = SettingsDialog(w.ctx, w)
    assert d.theme_box.currentData() == "light"
    d.theme_box.setCurrentIndex(d.theme_box.findData("dark"))
    d._save()
    assert ui_theme.configured_theme(w.app_dir) == "dark"


def test_report_follows_dark_theme():
    from digger.ui.theme import tokens
    r = _report("a.png", "NE555P", "yes")
    r.conclusion = _conclusion()
    light = HtmlReport({}, None).render(r)
    dark = HtmlReport({}, None).render(r, None, colors_from_tokens(tokens.tokens("dark")))
    assert "#f3f3f3" in light and "#ddd" in light
    for white in ("#f3f3f3", "#ddd", "#fde8e8", "#e8f5e9", "#eee", "color:#222"):
        assert white not in dark
    assert tokens.DARK["surface_alt"] in dark and tokens.DARK["text"] in dark


def test_sites_table_does_not_cover_memory_conclusion(window, tmp_path):
    """Шаг 8.3d: в тесном окне таблица сайтов сжимается, заключение о памяти остаётся видно; в большом — видна вся."""
    from digger.gui.conclusion_panel import MAX_ROWS
    from digger.gui.main_window import REPORT_MIN_HEIGHT
    w, app, (p,) = _open(window, tmp_path)
    c = _conclusion()
    c.protected += [SiteNote("s%d.example" % i, "captcha", "https://s%d.example/q" % i, "search") for i in range(8)]
    w.items[p] = {"report": _report(p, "NE555P", "?"), "variants": []}
    w.items[p]["report"].conclusion = c
    w.show()
    w.resize(w.minimumSizeHint())
    w.show_current()
    for _ in range(5):
        app.processEvents()
    panel, table = w.conclusion_panel, w.conclusion_panel.table
    line = table.verticalHeader().defaultSectionSize()
    assert panel.isVisible() and w.report_view.height() >= REPORT_MIN_HEIGHT
    assert table.height() < table.maximumHeight()              # сжалась: остальные строки — прокруткой
    w.resize(1400, 1500)
    for _ in range(5):
        app.processEvents()
    assert table.height() == table.maximumHeight() and table.height() >= MAX_ROWS * line
    assert w.report_view.height() >= 2 * REPORT_MIN_HEIGHT     # прибавка высоты — заключению
