# -*- coding: utf-8 -*-
"""Безопасный сетевой клиент.

Правила безопасности (все настраиваются в config.json, раздел "network"):
  * Программа обращается ТОЛЬКО к доменам из белого списка. Ссылки на другие
    сайты показываются пользователю, но не открываются и не скачиваются.
  * Каждый редирект проверяется заново по белому списку.
  * Только HTTPS (кроме доменов, явно разрешённых в allow_http).
  * Сертификаты всегда проверяются.
  * Ограничение размера, таймауты, пауза между запросами к одному сайту.
  * Скачанный файл должен быть настоящим PDF (сигнатура %PDF-), попадает в
    карантин, проверяется на активное содержимое (JavaScript, Launch,
    вложенные файлы). Программа ничего не запускает и не открывает сама.
  * Cookies хранятся только в памяти, на диск не пишутся.
  * Каждое обращение записывается в logs/network_audit.log.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

try:
    import requests
except ImportError:  # программа должна запускаться и без сети
    requests = None

net_log = logging.getLogger("digger.net")


def _masked(url: str) -> str:
    """Адрес для журнала: значения key=/apikey=/token= скрыты (ключи API не попадают в логи)."""
    return re.sub(r"(?i)\b(key|apikey|api_key|token)=[^&#\s]+", r"\1=***", url)

USER_AGENT = ("Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/109.0 Safari/537.36")

# Признаки активного содержимого в PDF
PDF_DANGER = {
    b"/JavaScript": "JavaScript",
    b"/JS": "JavaScript (JS)",
    b"/Launch": "запуск программ (Launch)",
    b"/EmbeddedFile": "вложенные файлы",
    b"/RichMedia": "Flash/RichMedia",
    b"/XFA": "XFA-формы",
    b"/SubmitForm": "отправка форм",
    b"/ImportData": "импорт данных",
}


class NetBlocked(Exception):
    pass


class HttpStatus(NetBlocked):
    """Сервер ответил кодом ≥ 400 (для решения «повторять или нет»)."""

    def __init__(self, status: int, headers: Optional[Dict[str, str]] = None, body: bytes = b"") -> None:
        super().__init__("HTTP %d" % status)
        self.status = status
        self.headers = dict(headers or {})      # заголовки и начало тела — для классификатора неудач (§4.11)
        self.body = bytes(body[:65536])


class NotPdf(NetBlocked):
    """Вместо PDF пришло что-то другое (чаще всего HTML-страница)."""


class TooBig(NetBlocked):
    """Файл больше лимита."""


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""


def domain_match(host: str, patterns: List[str]) -> bool:
    host = host.lower()
    for p in patterns:
        p = p.lower().strip().lstrip("*").lstrip(".")
        if p and (host == p or host.endswith("." + p)):
            return True
    return False


class SafeHttp:
    def __init__(self, net_cfg: Dict, quarantine_dir: str, logger: logging.Logger,
                 transport=None) -> None:
        # transport(method, url, headers) -> объект, похожий на requests.Response;
        # в тестах подменяется на tests/fakes/fake_http.py
        self.transport = transport
        self.cfg = net_cfg
        self.quarantine_dir = quarantine_dir
        self.log = logger
        self.allowed: List[str] = list(net_cfg.get("allowed_domains", []))
        self.allow_http: List[str] = list(net_cfg.get("allow_http", []))
        self.blocked: List[str] = list(net_cfg.get("blocked_domains", []))
        self.timeout = float(net_cfg.get("timeout_sec", 15))
        self.max_html = int(net_cfg.get("max_html_mb", 3)) * 1024 * 1024
        self.max_pdf = int(net_cfg.get("max_pdf_mb", 40)) * 1024 * 1024
        self.min_interval = float(net_cfg.get("min_interval_sec", 1.5))
        self._last: Dict[str, float] = {}       # сайт → время его последнего (или уже назначенного) запроса
        self._lock = threading.Lock()
        self._clock, self._sleep = time.monotonic, time.sleep       # в тестах подменяются
        self._session = None
        os.makedirs(quarantine_dir, exist_ok=True)

    # ---------- настройки ----------
    @property
    def offline(self) -> bool:
        return bool(self.cfg.get("offline", False)) or (requests is None and self.transport is None)

    def add_allowed(self, domains: List[str]) -> None:
        for d in domains:
            if d and d not in self.allowed:
                self.allowed.append(d)

    def is_allowed(self, url: str) -> bool:
        try:
            self._check_url(url)
            return True
        except NetBlocked:
            return False

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https"):
            raise NetBlocked("Запрещённая схема: %s" % parts.scheme)
        if not host:
            raise NetBlocked("Нет адреса сайта")
        if re.match(r"^[\d.]+$", host) or host in ("localhost",) or host.endswith(".local"):
            raise NetBlocked("Обращения к IP-адресам и локальной сети запрещены: %s" % host)
        if parts.port not in (None, 80, 443):
            raise NetBlocked("Нестандартный порт: %s" % parts.port)
        if domain_match(host, self.blocked):
            raise NetBlocked("Домен в чёрном списке: %s" % host)
        if not domain_match(host, self.allowed):
            raise NetBlocked("Домен не в белом списке: %s" % host)
        if parts.scheme == "http" and not domain_match(host, self.allow_http):
            raise NetBlocked("Только HTTPS (домен %s не разрешён для http)" % host)

    def _get_session(self):
        if self._session is None:
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT,
                              "Accept-Language": "en-US,en;q=0.8,ru;q=0.6,zh-CN;q=0.5"})
            proxy = self.cfg.get("proxy") or ""
            if proxy:
                s.proxies = {"http": proxy, "https": proxy}
            s.trust_env = bool(self.cfg.get("use_system_proxy", True))
            self._session = s
        return self._session

    def _send(self, method: str, url: str, headers: Dict[str, str], timeout: Optional[float] = None,
              data: Optional[bytes] = None):
        """Транспорт: единственное место, где выполняется настоящий HTTP-запрос."""
        if self.transport is not None:
            return self.transport(method, url, headers) if data is None else self.transport(method, url, headers, data)
        return self._get_session().request(method, url, headers=headers, timeout=timeout or self.timeout, data=data,
                                           allow_redirects=False, stream=True, verify=True)

    def _throttle(self, host: str) -> None:
        """К одному сайту — не чаще раза в `min_interval` секунд, из скольких бы потоков ни шли запросы:
        поток под замком занимает ближайшее свободное время сайта и ждёт его уже без замка."""
        with self._lock:
            now = self._clock()
            at = max(now, self._last.get(host, now - self.min_interval) + self.min_interval)
            self._last[host] = at
        if at > now:
            self._sleep(at - now)

    # ---------- запросы ----------
    def _request(self, url: str, max_bytes: int, method: str = "GET",
                 accept: str = "*/*", referer: str = "",
                 truncate: bool = False, timeout: Optional[float] = None, data: Optional[bytes] = None,
                 extra: Optional[Dict[str, str]] = None) -> Tuple[str, Dict[str, str], bytes, int]:
        """Возвращает (финальный_url, заголовки, тело, статус). Редиректы проверяются вручную.

        truncate=True — тело больше max_bytes обрезается (для проб), иначе NetBlocked.
        data, extra — тело и заголовки запроса к API (`post_json`): такой запрос по перенаправлению не идёт."""
        if self.offline:
            raise NetBlocked("Автономный режим: интернет выключен в настройках")
        cur = url
        for _hop in range(6):
            self._check_url(cur)
            host = host_of(cur)
            self._throttle(host)
            headers = {"Accept": accept}
            if referer:
                headers["Referer"] = referer
            headers.update(extra or {})
            t0 = time.time()
            try:
                r = self._send(method, cur, headers, timeout, data)
            except Exception as e:
                net_log.info("ERR\t%s\t%s", _masked(cur), type(e).__name__)
                raise
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                if data is not None or extra:     # ключ API в теле или заголовках на другой адрес не уходит
                    net_log.info("%s\t%s\tперенаправление запроса к API запрещено", r.status_code, _masked(cur))
                    r.close()
                    raise NetBlocked("Перенаправление запроса к API запрещено")
                nxt = urljoin(cur, r.headers["Location"])
                nparts = urlsplit(nxt)
                if nparts.scheme == "http" and not domain_match(host_of(nxt), self.allow_http):
                    # сайт сам увёл на http (Sogou → /antispider): остаёмся на https, правила не ослабляем
                    nxt = nparts._replace(scheme="https").geturl()
                net_log.info("%s\t%s\t-> %s", r.status_code, _masked(cur), _masked(nxt))
                r.close()
                cur = nxt
                continue
            body = b""
            if method != "HEAD":
                clen = r.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) > max_bytes and not truncate:
                    r.close()
                    raise TooBig("Файл слишком большой (%s)" % _size_text(int(clen)))
                chunks = []
                total = 0
                for chunk in r.iter_content(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        if not truncate:
                            r.close()
                            raise TooBig("Превышен лимит размера (%s)" % _size_text(max_bytes))
                        chunks.append(chunk[:max(0, len(chunk) - (total - max_bytes))])
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
            net_log.info("%s\t%s\t%d байт\t%.1fs", r.status_code, _masked(cur), len(body), time.time() - t0)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            r.close()
            return cur, hdrs, body, r.status_code
        raise NetBlocked("Слишком много перенаправлений")

    def get_html(self, url: str, referer: str = "") -> Tuple[str, str]:
        final, hdrs, body, status = self._request(url, self.max_html,
                                                  accept="text/html,application/xhtml+xml,*/*;q=0.8",
                                                  referer=referer)
        if status >= 400:
            raise HttpStatus(status, hdrs, body)
        ctype = hdrs.get("content-type", "")
        enc = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            enc = m.group(1)
        else:
            m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4000], re.I)
            if m:
                enc = m.group(1).decode("ascii", "ignore")
        if enc.lower() in ("gb2312", "gbk"):
            enc = "gb18030"
        try:
            text = body.decode(enc, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return final, text

    def get_json(self, url: str):
        import json
        final, hdrs, body, status = self._request(url, self.max_html, accept="application/json")
        if status >= 400:
            raise HttpStatus(status, hdrs, body)
        return json.loads(body.decode("utf-8", errors="replace"))

    def post_json(self, url: str, payload: Any = None, form: Optional[Dict[str, str]] = None,
                  headers: Optional[Dict[str, str]] = None):
        """Запрос к API с телом: `payload` — JSON, `form` — поля формы (выдача пропуска OAuth2). Те же правила,
        что у `get_json` (белый список, HTTPS, сертификаты, частота); тело и заголовки в журнал не пишутся."""
        import json
        from urllib.parse import urlencode
        extra = dict(headers or {})
        if form is not None:
            data = urlencode(form).encode("utf-8")
            extra["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload if payload is not None else {}).encode("utf-8")
            extra["Content-Type"] = "application/json"
        final, hdrs, body, status = self._request(url, self.max_html, method="POST", accept="application/json",
                                                  data=data, extra=extra)
        if status >= 400:
            raise HttpStatus(status, hdrs, body)
        return json.loads(body.decode("utf-8", errors="replace"))

    def fetch(self, url: str, max_bytes: int = 512 * 1024, timeout: Optional[float] = None) -> Dict:
        """Для диагностики: не бросает на коде ≥ 400, большое тело обрезает; `timeout` — свой срок ответа
        вместо общего (тяжёлые страницы каталогов). Возвращает {url, status, headers, body, truncated};
        сетевые ошибки — исключением."""
        final, hdrs, body, status = self._request(url, max_bytes + 1, accept="text/html,*/*", truncate=True,
                                                  timeout=timeout)
        return {"url": final, "status": status, "headers": hdrs, "body": body[:max_bytes],
                "truncated": len(body) > max_bytes}

    def probe(self, url: str) -> Tuple[bool, str]:
        """Проверка доступности сайта (для диагностики)."""
        try:
            final, hdrs, body, status = self._request(url, 512 * 1024, accept="text/html,*/*", truncate=True)
            if status >= 400:
                return False, "HTTP %d" % status
            return True, "HTTP %d, %d КБ" % (status, len(body) // 1024)
        except NetBlocked as e:
            return False, str(e)
        except Exception as e:  # noqa
            return False, "%s: %s" % (type(e).__name__, str(e)[:120])

    def download_pdf(self, url: str, referer: str = "") -> Tuple[str, str, int, List[str]]:
        """Скачивает PDF в карантин. Возвращает (путь, sha256, размер, подозрительные_признаки)."""
        return self.download_pdf_ex(url, referer)[:4]

    def download_pdf_ex(self, url: str, referer: str = "") -> Tuple[str, str, int, List[str], str]:
        """То же, плюс пятый элемент — адрес после редиректов."""
        final, hdrs, body, status = self._request(url, self.max_pdf,
                                                  accept="application/pdf,*/*;q=0.5", referer=referer)
        if status >= 400:
            raise HttpStatus(status, hdrs, body)
        head = body[:1024]
        if b"%PDF-" not in head:
            raise NotPdf("Это не PDF (сервер вернул %s)" % hdrs.get("content-type", "?"))
        sha = hashlib.sha256(body).hexdigest()
        path = os.path.join(self.quarantine_dir, sha[:16] + ".pdf.quarantine")
        with open(path, "wb") as f:
            f.write(body)
        return path, sha, len(body), pdf_danger_scan(body), final


def _size_text(n: int) -> str:
    return "%d МБ" % (n // 1048576) if n >= 1048576 else "%d КБ" % (n // 1024)


def pdf_danger_scan(data: bytes) -> List[str]:
    found = []
    for marker, name in PDF_DANGER.items():
        # /JS должен быть отдельным именем, а не началом /JSomething
        for m in re.finditer(re.escape(marker) + rb"(?![A-Za-z])", data):
            found.append(name)
            break
    return sorted(set(found))
