# -*- coding: utf-8 -*-
"""Загрузчик расширений: поиск папок с extension.json, проверка версии API, изоляция ошибок.

Любая ошибка расширения (при загрузке, в `setup`/`contribute`, в обработчике события, в пункте меню)
отключает только его: подписки и вклады в интерфейс снимаются, причина — в `ExtensionInfo.error`.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..acquire.events import EventBus
from ..core.config import read_json, resolve_path, save_user_config
from .api import API_VERSION_STR, ID_RE, Extension, ExtensionDb, ExtensionUi, Services, UiContributions, api_compatible

BUILTIN_DIR = os.path.dirname(os.path.abspath(__file__))
STATES = {"active": "работает", "disabled": "выключено", "failed": "отключено из-за ошибки",
          "incompatible": "не подходит версия API"}
_FAILED = object()


@dataclass
class ExtensionInfo:
    id: str
    name: str = ""
    version: str = ""
    api: str = ""
    description: str = ""
    path: str = ""
    builtin: bool = False
    enabled_by_default: bool = True
    state: str = "disabled"
    error: str = ""
    instance: Any = None


def _thread_runner(work: Callable[[], Any], finish: Callable[[Any], None]) -> None:
    threading.Thread(target=lambda: finish(work()), daemon=True).start()


class ExtensionManager:
    def __init__(self, ctx: Any, pipeline: Any = None, dirs: Optional[List[str]] = None,
                 runner: Optional[Callable[..., None]] = None,
                 on_failure: Optional[Callable[[ExtensionInfo], None]] = None) -> None:
        self.ctx = ctx
        self.pipeline = pipeline
        if getattr(ctx, "bus", None) is None:
            ctx.bus = EventBus()
        self.bus = ctx.bus
        self.dirs = dirs if dirs is not None else [BUILTIN_DIR, os.path.join(ctx.app_dir, "plugins")]
        self.runner = runner or _thread_runner
        self.on_failure = on_failure
        self.ui = UiContributions(self.guarded)
        self.extensions: List[ExtensionInfo] = []
        self._unsubscribe: Dict[str, List[Callable[[], None]]] = {}
        self._dbs: Dict[str, ExtensionDb] = {}

    # -------------------- поиск и загрузка --------------------
    def discover(self) -> List[ExtensionInfo]:
        found: List[ExtensionInfo] = []
        for folder in self.dirs:
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                manifest = os.path.join(path, "extension.json")
                if not os.path.isfile(manifest):
                    continue
                info = ExtensionInfo(id=name, name=name, path=path, builtin=(folder == BUILTIN_DIR))
                try:
                    data = read_json(manifest)
                    info.id = str(data.get("id", ""))
                    info.name = str(data.get("name") or info.id)
                    info.version = str(data.get("version", ""))
                    info.api = str(data.get("api", ""))
                    info.description = str(data.get("description", ""))
                    info.enabled_by_default = bool(data.get("enabled_by_default", True))
                    if not ID_RE.match(info.id):
                        raise ValueError("id «%s»: нужны латинские строчные буквы, цифры и _" % info.id)
                    if any(x.id == info.id for x in found):
                        raise ValueError("id «%s» уже занят другим расширением" % info.id)
                except Exception as e:  # noqa
                    info.state, info.error = "failed", "extension.json: %s" % e
                found.append(info)
        return found

    def is_enabled(self, info: ExtensionInfo) -> bool:
        cfg = self.ctx.config.get("extensions", {})
        if info.id in cfg.get("disabled", []):
            return False
        return info.enabled_by_default or info.id in cfg.get("enabled", [])

    def load(self) -> List[ExtensionInfo]:
        self.extensions = self.discover()
        for info in self.extensions:
            if info.state == "failed":
                self._report(info)
            elif not self.is_enabled(info):
                info.state = "disabled"
            elif not api_compatible(info.api):
                info.state = "incompatible"
                info.error = "нужна версия API %s, в программе %s" % (info.api or "?", API_VERSION_STR)
                self._report(info)
            else:
                self._start(info)
        return self.extensions

    def _start(self, info: ExtensionInfo) -> None:
        info.state = "active"
        try:
            name = "digger_ext_" + info.id
            spec = importlib.util.spec_from_file_location(name, os.path.join(info.path, "extension.py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            cls = getattr(module, "Extension", None)
            if not (isinstance(cls, type) and issubclass(cls, Extension)):
                raise TypeError("в extension.py нет класса Extension — наследника digger.extensions.api.Extension")
            info.instance = cls(info)
        except Exception as e:  # noqa
            self.fail(info, e)
            return
        self.call(info, info.instance.setup, Services(self, info))
        if info.state == "active":
            self.call(info, info.instance.contribute, ExtensionUi(self.ui, info.id))

    # -------------------- изоляция ошибок --------------------
    def get(self, ext_id: str) -> Optional[ExtensionInfo]:
        return next((x for x in self.extensions if x.id == ext_id), None)

    def call(self, info: ExtensionInfo, fn: Callable[..., Any], *args: Any, default: Any = None) -> Any:
        """Вызов кода расширения: при ошибке оно отключается, возвращается `default`."""
        if info.state != "active":
            return default
        try:
            return fn(*args)
        except Exception as e:  # noqa
            self.fail(info, e)
            return default

    def guarded(self, ext_id: str, fn: Callable[..., Any], default: Any = None) -> Callable[..., Any]:
        def wrapper(*args: Any) -> Any:
            info = self.get(ext_id)
            return self.call(info, fn, *args, default=default) if info else default
        return wrapper

    def fail(self, info: ExtensionInfo, exc: BaseException) -> None:
        if info.state == "failed":
            return
        info.state = "failed"
        info.error = "%s: %s" % (type(exc).__name__, exc)
        self.ctx.log.error("Расширение «%s» отключено", info.id, exc_info=exc)
        self._release(info)
        self._report(info)

    def _report(self, info: ExtensionInfo) -> None:
        if self.on_failure:
            self.on_failure(info)

    def _release(self, info: ExtensionInfo) -> None:
        for unsubscribe in self._unsubscribe.pop(info.id, []):
            unsubscribe()
        self.ui.remove(info.id)
        db = self._dbs.pop(info.id, None)
        if db:
            db.close()

    # -------------------- сервисы --------------------
    def subscribe(self, info: ExtensionInfo, callback: Callable[[Any], None]) -> Callable[[], None]:
        unsubscribe = self.bus.subscribe(self.guarded(info.id, callback))
        self._unsubscribe.setdefault(info.id, []).append(unsubscribe)
        return unsubscribe

    def hook(self, info: ExtensionInfo, name: str, callback: Callable[..., Any]) -> Callable[[], None]:
        """Крючок конвейера (`marking_filters`, `marking_listeners`); снимается вместе с расширением."""
        hooks = getattr(self.pipeline, name, None)
        if hooks is None:
            return lambda: None
        fn = self.guarded(info.id, callback)
        hooks.append(fn)

        def remove() -> None:
            if fn in hooks:
                hooks.remove(fn)
        self._unsubscribe.setdefault(info.id, []).append(remove)
        return remove

    def run_in_background(self, info: ExtensionInfo, fn: Callable[[], Any],
                          on_done: Optional[Callable[[Any], None]] = None) -> None:
        def work() -> Any:
            return self.call(info, fn, default=_FAILED)

        def finish(result: Any) -> None:
            if result is not _FAILED and on_done:
                self.call(info, on_done, result)
        self.runner(work, finish)

    def db(self, info: ExtensionInfo) -> ExtensionDb:
        if info.id not in self._dbs:
            path = resolve_path(self.ctx.app_dir, self.ctx.config["paths"]["db"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._dbs[info.id] = ExtensionDb(path, info.id)
        return self._dbs[info.id]

    # -------------------- включение и завершение --------------------
    def set_enabled(self, ext_id: str, enabled: bool) -> None:
        """Запоминает выбор в config.json; действует после новой загрузки расширений."""
        cfg = self.ctx.config.setdefault("extensions", {})
        on = [x for x in cfg.get("enabled", []) if x != ext_id]
        off = [x for x in cfg.get("disabled", []) if x != ext_id]
        (on if enabled else off).append(ext_id)
        cfg["enabled"], cfg["disabled"] = on, off
        save_user_config(self.ctx.app_dir, {"extensions": {"enabled": on, "disabled": off}})

    def close(self) -> None:
        for info in self.extensions:
            if info.state == "active":
                self.call(info, info.instance.shutdown)
            self._release(info)
