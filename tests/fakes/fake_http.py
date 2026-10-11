# -*- coding: utf-8 -*-
"""Подмена сети для тестов: URL -> фикстура / код / редирект / исключение."""
from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional, Tuple

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status
        self.content = body
        self.headers = dict(headers or {})

    def iter_content(self, size: int = 65536):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def close(self) -> None:
        pass


class VirtualTime:
    """Часы для тестов ограничителя частоты: `sleep` не ждёт, а сдвигает время своего потока.

    Подключение: `http._clock, http._sleep = vt.clock, vt.sleep`."""

    def __init__(self, start: float = 1000.0) -> None:
        self.start = start
        self._local = threading.local()

    def clock(self) -> float:
        return self.start + getattr(self._local, "shift", 0.0)

    def sleep(self, seconds: float) -> None:
        self._local.shift = getattr(self._local, "shift", 0.0) + seconds


class FakeHttp:
    """Вызываемый транспорт для SafeHttp(transport=...). Запоминает все запросы в calls."""

    def __init__(self) -> None:
        self.routes: Dict[str, object] = {}
        self.calls: List[Tuple[str, str]] = []
        self.sent: List[Tuple[str, Dict[str, str], bytes]] = []      # запросы с телом: адрес, заголовки, тело

    def add(self, url: str, body=b"", status: int = 200, content_type: str = "text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[url] = FakeResponse(status, body, {"Content-Type": content_type})
        return self

    def add_fixture(self, url: str, rel_path: str, status: int = 200, content_type: str = "text/html; charset=utf-8"):
        with open(os.path.join(FIXTURES, rel_path), "rb") as f:
            return self.add(url, f.read(), status, content_type)

    def add_redirect(self, url: str, location: str, status: int = 302):
        self.routes[url] = FakeResponse(status, b"", {"Location": location})
        return self

    def add_error(self, url: str, exc: Exception):
        self.routes[url] = exc
        return self

    def __call__(self, method: str, url: str, headers: Dict[str, str], data: Optional[bytes] = None):
        self.calls.append((method, url))
        if data is not None:
            self.sent.append((url, dict(headers), data))
        r = self.routes.get(url)
        if r is None:
            return FakeResponse(404, b"not found", {"Content-Type": "text/plain"})
        if isinstance(r, Exception):
            raise r
        return r
