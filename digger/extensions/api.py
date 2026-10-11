# -*- coding: utf-8 -*-
"""API расширений (ARCHITECTURE §6). Изменения — только с записью в «Решения» и новой версией.

Расширение — папка с `extension.json` и `extension.py`, в котором есть класс `Extension`:

    from digger.extensions.api import Extension

    class Extension(Extension):
        def setup(self, services): ...       # сервисы программы, подписка на события, свои таблицы
        def contribute(self, ui): ...        # вкладка, панель, пункт меню, кнопка, страница настроек…

Здесь нет Qt: вклады в интерфейс — описания, окно применяет их само. Вкладка и панель задаются
функцией, которая возвращает виджет; она вызывается в потоке интерфейса.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

API_VERSION = (1, 1)     # 1.1: крючки маркировки — `add_marking_filter`, `on_marking_corrected`
API_VERSION_STR = "%d.%d" % API_VERSION
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
KINDS = ("tab", "side_panel", "menu_item", "toolbar_button", "settings_page", "csv_column", "report_section")


def api_compatible(required: Any) -> bool:
    """Расширению нужна версия «старшая.младшая»: старшая совпадает, младшая не новее нашей."""
    try:
        major, minor = (int(x) for x in (str(required).split(".") + ["0"])[:2])
    except ValueError:
        return False
    return major == API_VERSION[0] and minor <= API_VERSION[1]


class Extension:
    """Базовый класс расширения. `info` — сведения из extension.json."""

    def __init__(self, info: Any) -> None:
        self.info = info

    def setup(self, services: "Services") -> None:
        pass

    def contribute(self, ui: "ExtensionUi") -> None:
        pass

    def shutdown(self) -> None:
        pass


class ExtensionDb:
    """Свои таблицы расширения в базе программы: только с префиксом `ext_<id>_`, версии — в `ext_migrations`.

    В SQL префикс пишется как `{prefix}`: `CREATE TABLE {prefix}seen (part TEXT PRIMARY KEY)`.
    """

    def __init__(self, path: str, ext_id: str) -> None:
        self.ext_id = ext_id
        self.prefix = "ext_%s_" % ext_id
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        with self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS ext_migrations "
                              "(ext_id TEXT PRIMARY KEY, version INTEGER NOT NULL)")

    def table(self, name: str) -> str:
        return self.prefix + name

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[tuple]:
        with self._lock, self.conn:
            return self.conn.execute(sql.replace("{prefix}", self.prefix), params).fetchall()

    def version(self) -> int:
        row = self.execute("SELECT version FROM ext_migrations WHERE ext_id = ?", (self.ext_id,))
        return int(row[0][0]) if row else 0

    def migrate(self, migrations: Sequence[str]) -> int:
        """Применяет ещё не применённые миграции по порядку (номер — место в списке). Возвращает версию."""
        for sql in migrations:
            if "{prefix}" not in sql:
                raise ValueError("миграция без {prefix}: расширение работает только со своими таблицами")
        with self._lock:
            done = self.version()
            for n, sql in enumerate(migrations[done:], done + 1):
                with self.conn:
                    self.conn.executescript(sql.replace("{prefix}", self.prefix))
                    self.conn.execute("INSERT OR REPLACE INTO ext_migrations (ext_id, version) VALUES (?, ?)",
                                      (self.ext_id, n))
            return self.version()

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class Services:
    """Сервисы программы для одного расширения (создаёт загрузчик)."""
    api_version = API_VERSION

    def __init__(self, manager: Any, info: Any) -> None:
        self._manager = manager
        self._info = info
        self.ext_id = info.id
        self.ctx = manager.ctx
        self.config = manager.ctx.config
        self.settings = dict(self.config.get("extensions", {}).get("settings", {}).get(info.id, {}))
        self.app_dir = manager.ctx.app_dir
        self.bus = manager.bus
        self.pipeline = manager.pipeline
        self.log = manager.ctx.log.getChild("ext.%s" % info.id)

    @property
    def orchestrator(self) -> Any:
        """Оркестратор поиска (создаётся при первом обращении); None, если модуль поиска другой."""
        return getattr(self.ctx.module("web_search"), "orchestrator", None)

    def subscribe(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        """События поиска и программы (`acquire/events.py`). Вызов — в потоке издателя: быстро и без окна."""
        return self._manager.subscribe(self._info, callback)

    def run_in_background(self, fn: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None) -> None:
        """Долгая работа — в фоне; `on_done(результат)` в окне вызывается в потоке интерфейса."""
        self._manager.run_in_background(self._info, fn, on_done)

    def db(self) -> ExtensionDb:
        return self._manager.db(self._info)

    def add_marking_filter(self, fn: Callable[[str], Any]) -> Callable[[], None]:
        """`fn(распознанная маркировка) → исправленная`: конвейер применяет после распознавания, до определения
        партномера; вручную введённый текст не трогает. Вызов — в фоновом потоке. (API 1.1)"""
        return self._manager.hook(self._info, "marking_filters", fn)

    def on_marking_corrected(self, fn: Callable[[str, str], Any]) -> Callable[[], None]:
        """`fn(было, стало)` — пользователь исправил распознанную маркировку. Возвращённая строка попадёт в журнал
        фото. Вызов — в фоновом потоке. (API 1.1)"""
        return self._manager.hook(self._info, "marking_listeners", fn)


@dataclass
class Contribution:
    kind: str
    ext_id: str
    title: str
    target: Callable[..., Any]      # tab/side_panel/settings_page: () → виджет; меню/кнопка: (); колонка/раздел: (отчёт) → str
    menu: str = ""
    tip: str = ""


class UiContributions:
    """Все вклады расширений в интерфейс. Окно читает `of(kind)`; вызовы уже защищены от ошибок."""

    def __init__(self, guard: Callable[[str, Callable[..., Any], Any], Callable[..., Any]]) -> None:
        self._guard = guard
        self.items: List[Contribution] = []

    def add(self, kind: str, ext_id: str, title: str, target: Callable[..., Any], default: Any = None,
            menu: str = "", tip: str = "") -> None:
        if kind not in KINDS or not callable(target):
            raise ValueError("вклад «%s»: неизвестный вид или нет функции" % kind)
        self.items.append(Contribution(kind, ext_id, str(title), self._guard(ext_id, target, default), menu, tip))

    def of(self, kind: str) -> List[Contribution]:
        return [c for c in self.items if c.kind == kind]

    def remove(self, ext_id: str) -> None:
        self.items = [c for c in self.items if c.ext_id != ext_id]


class ExtensionUi:
    """То, что получает `Extension.contribute(ui)`."""

    def __init__(self, contributions: UiContributions, ext_id: str) -> None:
        self._c = contributions
        self._id = ext_id

    def add_tab(self, title: str, factory: Callable[[], Any]) -> None:
        self._c.add("tab", self._id, title, factory)

    def add_side_panel(self, title: str, factory: Callable[[], Any]) -> None:
        self._c.add("side_panel", self._id, title, factory)

    def add_menu_item(self, menu: str, title: str, callback: Callable[[], Any]) -> None:
        self._c.add("menu_item", self._id, title, callback, menu=menu)

    def add_toolbar_button(self, title: str, callback: Callable[[], Any], tip: str = "") -> None:
        self._c.add("toolbar_button", self._id, title, callback, tip=tip)

    def add_settings_page(self, title: str, factory: Callable[[], Any]) -> None:
        self._c.add("settings_page", self._id, title, factory)

    def add_csv_column(self, title: str, value: Callable[[Any], Any]) -> None:
        self._c.add("csv_column", self._id, title, value, default="")

    def add_report_section(self, title: str, html: Callable[[Any], Any]) -> None:
        self._c.add("report_section", self._id, title, html, default="")
