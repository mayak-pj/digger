# -*- coding: utf-8 -*-
"""Менеджер провайдеров распознавания — реализация роли `ocr`.

Цепочка «Авто» из `config → recognition.chain`: следующий провайдер пробуется, если текущий недоступен,
упал, ничего не прочитал, прочитал с уверенностью ниже порога или (если включено `require_confirmed`)
ни один кандидат партномера из прочитанного не подтвердился справочником, каталогом или локальной
базой (`recognition.auto`).
Справившийся не вытесняет способ, стоящий в цепочке раньше, если сам не подтверждён справочником, а тот
подтверждён или прочитал больше (уверенность × число букв и цифр — та же мера, что при выборе поворота).
Способ (`mode`): «Авто» — цепочка; id провайдера — только он (и не из цепочки тоже); «Сравнить все» —
все доступные провайдеры по очереди, в `OcrResult.attempts` — что прочитал каждый.
Облачный провайдер вызывается только если облако включено в настройках и пользователь согласился
отправить фото (`consent.py`). Ход цепочки — события `ocr.*`. О конкретных провайдерах менеджер не знает — встроенные находит в
`recognition/providers/`, сторонние — в `plugins/ocr_<имя>/` (provider.json + provider.py).
Результат несёт список попыток (`OcrResult.attempts`) и строки всех отработавших провайдеров;
время и исходы копятся в `RecognitionManager.stats`.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..acquire.events import EventBus, render
from ..core.config import read_json
from ..core.interfaces import OCR, ProgressFn
from ..core.models import ImageVariant, OcrAttempt, OcrResult
from . import providers as builtin_package
from .api import OcrProvider
from .consent import CloudConsent

DEFAULT_CHAIN = ["ppocr", "tesseract"]
PLUGIN_PREFIX = "ocr_"
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
KINDS = ("local", "cloud")
AUTO, COMPARE = "auto", "compare"
MODE_TITLES = {AUTO: "Авто", COMPARE: "Сравнить все"}


def _alnum(s: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]", s))


def _chosen_lines(res: OcrResult) -> list:
    chosen = {t.strip() for t in res.best_text.splitlines() if t.strip()}
    return [l for l in res.lines if l.text.strip() in chosen] or res.lines


def result_evidence(res: OcrResult) -> float:
    """Сколько прочитано: сумма «уверенность × число букв и цифр» по строкам итоговой маркировки."""
    return sum(l.confidence * _alnum(l.text) for l in _chosen_lines(res))


def result_confidence(res: OcrResult) -> float:
    """Средняя уверенность строк итоговой маркировки (вес — число букв и цифр), 0..100."""
    lines = _chosen_lines(res)
    weight = sum(max(1, _alnum(l.text)) for l in lines)
    return round(sum(l.confidence * max(1, _alnum(l.text)) for l in lines) / weight, 1) if weight else 0.0


@dataclass
class ProviderInfo:
    id: str
    title: str = ""
    kind: str = "local"       # local | cloud
    builtin: bool = False
    path: str = ""            # папка стороннего провайдера
    error: str = ""           # почему не загрузился


def builtin_ids() -> List[str]:
    return sorted(m.name for m in pkgutil.iter_modules(builtin_package.__path__) if m.ispkg)


def discover_plugins(plugins_dir: str) -> List[ProviderInfo]:
    """Папки `ocr_*` с provider.json. Код провайдера здесь не выполняется — только описание."""
    found: List[ProviderInfo] = []
    if not os.path.isdir(plugins_dir):
        return found
    for name in sorted(os.listdir(plugins_dir)):
        path = os.path.join(plugins_dir, name)
        manifest = os.path.join(path, "provider.json")
        if not name.startswith(PLUGIN_PREFIX) or not os.path.isfile(manifest):
            continue
        info = ProviderInfo(id=name[len(PLUGIN_PREFIX):], path=path)
        try:
            data = read_json(manifest)
            info.id = str(data.get("id") or info.id)
            info.title = str(data.get("title") or info.id)
            info.kind = str(data.get("kind") or "local")
            if not ID_RE.match(info.id):
                raise ValueError("id «%s»: нужны латинские строчные буквы, цифры и _" % info.id)
            if info.kind not in KINDS:
                raise ValueError("kind «%s»: нужно local или cloud" % info.kind)
        except Exception as e:  # noqa
            info.error = "provider.json: %s" % e
        found.append(info)
    return found


def load_provider_class(info: ProviderInfo):
    """Класс `Provider(OcrProvider)` встроенного провайдера или `provider.py` из папки стороннего."""
    if info.builtin:
        where = "digger.recognition.providers.%s" % info.id
        mod = importlib.import_module(where)
    else:
        where = os.path.join(info.path, "provider.py")
        name = "digger_ocr_" + info.id
        spec = importlib.util.spec_from_file_location(name, where)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    cls = getattr(mod, "Provider", None)
    if not (isinstance(cls, type) and issubclass(cls, OcrProvider)):
        raise ImportError("в %s нет класса Provider(OcrProvider)" % where)
    return cls


class RecognitionManager(OCR):
    name = "recognition"

    def __init__(self, settings, ctx):
        super().__init__(settings, ctx)
        cfg = ctx.config.get("recognition", {})
        self.chain: List[str] = list(cfg.get("chain") or DEFAULT_CHAIN)
        self.known: Dict[str, ProviderInfo] = {}
        self.providers: Dict[str, OcrProvider] = {}
        self.problems: Dict[str, str] = {}      # id → почему не работает
        self.stats: Dict[str, Dict[str, float]] = {}   # id → calls / seconds / число каждого исхода
        auto = cfg.get("auto", {})
        self.min_confidence = float(auto.get("min_confidence", 60))
        self.require_confirmed = bool(auto.get("require_confirmed", False))
        self.cloud_enabled = bool(cfg.get("cloud_enabled", False))
        self.consent = CloudConsent()   # спрашивает окно; пока спрашивать некому — облако не вызывается
        self._lock = threading.Lock()
        self._discover(os.path.join(ctx.app_dir, "plugins"))
        for pid in self.chain:
            self._ensure(pid)
        self.mode = self._valid_mode(str(cfg.get("mode") or AUTO))     # способ по умолчанию (настройки)

    # -------------------- поиск и загрузка --------------------
    def _discover(self, plugins_dir: str) -> None:
        for pid in builtin_ids():
            self.known[pid] = ProviderInfo(id=pid, title=pid, builtin=True)
        for info in discover_plugins(plugins_dir):
            if info.id in self.known:
                self.ctx.log.warning("Провайдер распознавания из %s пропущен: id «%s» уже занят", info.path, info.id)
                continue
            self.known[info.id] = info
            if info.error:
                self.ctx.log.warning("Провайдер распознавания в %s: %s", info.path, info.error)

    def _ensure(self, pid: str) -> None:
        """Создаёт провайдера при первом обращении (из цепочки — при запуске, остальные — когда выбраны)."""
        if pid in self.providers or pid in self.problems:
            return
        info = self.known.get(pid)
        if info is None:
            self._broken(pid, "нет такого провайдера (ни встроенного, ни plugins/%s%s)" % (PLUGIN_PREFIX, pid))
        elif info.error:
            self._broken(pid, info.error)
        else:
            self._create(info, self.ctx.config.get("recognition", {}).get("providers", {}).get(pid, {}))

    def _create(self, info: ProviderInfo, settings: Dict[str, Any]) -> None:
        try:
            cls = load_provider_class(info)
            self.providers[info.id] = cls(settings, self.ctx)
        except Exception as e:  # noqa — сломанный провайдер не должен ронять программу
            info.error = "не загрузился: %s: %s" % (type(e).__name__, e)
            self._broken(info.id, info.error)
            return
        if info.builtin or not info.title:
            info.title = cls.title
        if cls.kind == "cloud":         # облаком считается, если так сказано хоть где-то
            info.kind = "cloud"

    def _broken(self, pid: str, why: str) -> None:
        self.problems[pid] = why
        self.ctx.log.warning("Провайдер распознавания %s: %s", pid, why)

    def catalog(self) -> List[ProviderInfo]:
        """Все известные провайдеры: сначала цепочка по порядку, потом остальные."""
        rest = [i for pid, i in sorted(self.known.items()) if pid not in self.chain]
        return [self.known[pid] for pid in self.chain if pid in self.known] + rest

    def modes(self) -> List[Tuple[str, str]]:
        """Способы для списка в окне: (id, название) — «Авто», провайдеры, «Сравнить все»."""
        def title(info: ProviderInfo) -> str:
            # сторонний провайдер вне цепочки не загружается ради подписи — его доступность выяснится при выборе
            if info.builtin or info.id in self.providers:
                ok, why = self._available(info.id)
                if not ok:
                    return "%s — недоступно: %s" % (info.title or info.id, why)
            return info.title or info.id
        return ([(AUTO, MODE_TITLES[AUTO])] + [(i.id, title(i)) for i in self.catalog() if not i.error]
                + [(COMPARE, MODE_TITLES[COMPARE])])

    def _valid_mode(self, mode: str) -> str:
        if mode in (AUTO, COMPARE) or (mode in self.known and not self.known[mode].error):
            return mode
        self.ctx.log.warning("Способ распознавания «%s» неизвестен — использую «Авто»", mode)
        return AUTO

    # -------------------- доступность --------------------
    def _title(self, pid: str) -> str:
        info = self.known.get(pid)
        return (info.title if info else "") or pid

    def _is_cloud(self, pid: str) -> bool:
        return self.known[pid].kind == "cloud"

    def _available(self, pid: str) -> Tuple[bool, str]:
        """Можно ли пробовать провайдера (согласие на отправку здесь не спрашивается)."""
        self._ensure(pid)
        p = self.providers.get(pid)
        if p is None:
            return False, self.problems.get(pid, "нет такого провайдера")
        if self._is_cloud(pid) and not self.cloud_enabled:
            ok, why = False, "облачные способы выключены в настройках"
        else:
            try:
                ok, why = p.is_available()
            except Exception as e:  # noqa
                ok, why = False, str(e)
        if not ok:
            self.problems[pid] = why
        return ok, why

    def is_available(self) -> bool:
        return any(self._available(pid)[0] for pid in self.chain)

    @property
    def error(self) -> str:
        return "; ".join("%s — %s" % (pid, self.problems[pid]) for pid in self.chain if pid in self.problems)

    # -------------------- распознавание --------------------
    def _count(self, attempt: OcrAttempt) -> None:
        with self._lock:
            st = self.stats.setdefault(attempt.provider, {"calls": 0, "seconds": 0.0})
            st["calls"] += 1
            st[attempt.status] = st.get(attempt.status, 0) + 1
            st["seconds"] = round(st["seconds"] + attempt.seconds, 3)

    def _confirmed(self, res: OcrResult) -> bool:
        """Есть ли среди кандидатов партномера подтверждённый справочником, каталогом или локальной базой."""
        ident = self.ctx.module("identifier")
        if ident is None:
            return False
        try:
            cands = ident.identify(res.best_text, [l.text for l in res.lines if l.confidence >= 40])
        except Exception:  # noqa
            self.ctx.log.exception("Проверка прочитанного по справочнику не удалась")
            return False
        return any(getattr(c, "confirmed_by", "") for c in cands)

    def _judge(self, res: OcrResult) -> str:
        if not res.best_text.strip():
            return "empty"
        res.confidence = result_confidence(res)
        res.confirmed = self._confirmed(res)
        if res.confidence < self.min_confidence:
            return "weak"
        return "ok" if res.confirmed or not self.require_confirmed else "unconfirmed"

    def recognize(self, variants: List[ImageVariant], progress: Optional[ProgressFn] = None,
                  original=None, mode: str = "") -> OcrResult:
        """mode: пусто — способ из настроек; `auto`; id провайдера; `compare`."""
        mode = self._valid_mode(mode or self.mode)
        if mode == COMPARE:
            chain = [i.id for i in self.catalog() if not i.error]
        else:
            chain = self.chain if mode == AUTO else [mode]
        if self.ctx.bus is None:
            self.ctx.bus = EventBus()

        def emit(key: str, pid: str, **params: Any) -> None:
            event = self.ctx.bus.emit(key, lang="ru", source=pid, provider=self._title(pid), **params)
            if progress:
                progress(render(event))

        attempts: List[OcrAttempt] = []
        done: List[OcrResult] = []      # результаты отработавших провайдеров, по порядку цепочки
        good: List[OcrResult] = []      # из них справившиеся
        for pid in chain:
            attempt = OcrAttempt(pid, self._title(pid))
            attempts.append(attempt)
            ok, why = self._available(pid)
            if not ok:
                attempt.status, attempt.detail = "unavailable", why
                emit("ocr.unavailable", pid, reason=why)
                continue
            if self._is_cloud(pid) and not self.consent.allowed(pid, attempt.title):
                attempt.status = "no_consent"
                emit("ocr.no_consent", pid)
                continue
            p = self.providers[pid]
            imgs = variants
            if p.wants_original and original is not None:
                imgs = [ImageVariant(name="original", image=original)]
            emit("ocr.next" if done else "ocr.start", pid)
            t0 = time.perf_counter()
            try:
                res = p.recognize(imgs, hints={}, progress=progress)
            except Exception as e:  # noqa — упал → следующий в цепочке
                attempt.status, attempt.detail = "failed", "%s: %s" % (type(e).__name__, e)
                attempt.seconds = round(time.perf_counter() - t0, 3)
                self.problems[pid] = "ошибка: %s" % e
                self.ctx.log.exception("Провайдер распознавания %s упал", pid)
                self._count(attempt)
                emit("ocr.failed", pid, reason=attempt.detail)
                continue
            attempt.seconds = round(time.perf_counter() - t0, 3)
            attempt.lines = len(res.lines)
            attempt.text = res.best_text
            res.provider, res.provider_title, res.seconds = pid, attempt.title, attempt.seconds
            for line in res.lines:
                line.provider = pid
            attempt.status = self._judge(res)
            attempt.confidence = res.confidence
            self._count(attempt)
            self.ctx.log.info("Распознавание: %s, %.2f с, строк %d, уверенность %.0f, исход %s",
                              pid, res.seconds, len(res.lines), res.confidence, attempt.status)
            emit("ocr." + attempt.status, pid, conf=int(round(res.confidence)), min=int(self.min_confidence))
            done.append(res)
            if attempt.status == "ok":
                good.append(res)
                if mode != COMPARE:
                    break
        # справившийся (при сравнении), иначе подтверждённый справочником, иначе первый, кто что-то прочитал
        read = [r for r in done if r.best_text.strip()]
        best = (good or [r for r in read if r.confirmed] or read or done[-1:] or [OcrResult()])[0]
        kept = self._earlier_better(best, read) if good else None
        if kept is not None:
            emit("ocr.kept", best.provider, kept=kept.provider_title)
            best = kept
        elif read and not good and len(chain) > 1:
            emit("ocr.fallback", best.provider)
        return self._merge(best, done, attempts, mode)

    @staticmethod
    def _earlier_better(best: OcrResult, read: List[OcrResult]) -> Optional[OcrResult]:
        """Способ, стоящий в цепочке раньше справившегося, чьё прочитанное весомее: подтверждено справочником
        или (когда не подтверждён никто) прочитано больше. Короткий мусор с уверенностью чуть выше порога
        не должен вытеснять длинную маркировку с уверенностью чуть ниже."""
        if best.confirmed:
            return None
        earlier = read[:read.index(best)]
        confirmed = [r for r in earlier if r.confirmed]
        if confirmed:
            return confirmed[0]
        heavier = [r for r in earlier if result_evidence(r) > result_evidence(best)]
        return max(heavier, key=result_evidence) if heavier else None

    @staticmethod
    def _merge(result: OcrResult, done: List[OcrResult], attempts: List[OcrAttempt], mode: str = "") -> OcrResult:
        """К строкам итога добавляются строки остальных отработавших провайдеров (без повторов) —
        как запасные варианты для выбора партномера."""
        seen = {line.text.strip().upper() for line in result.lines}
        for other in done:
            if other is result:
                continue
            for line in other.lines:
                key = line.text.strip().upper()
                if key and key not in seen:
                    seen.add(key)
                    result.lines.append(line)
        result.attempts = attempts
        result.mode = mode
        result.total_seconds = round(sum(a.seconds for a in attempts), 3)
        return result

    def close(self) -> None:
        for p in self.providers.values():
            try:
                p.close()
            except Exception:  # noqa
                pass
