# -*- coding: utf-8 -*-
"""Конвейер обработки: связывает модули в порядке
фото → улучшение → OCR → партномер → локальная база → интернет → сверка → память.
Каждый шаг можно вызвать отдельно (так делает окно программы)."""
from __future__ import annotations

import os
from typing import Any, List, Optional, Tuple

from .config import load_config, setup_logging
from .interfaces import CancelToken, Context, ProgressFn
from .models import ChipReport, DatasheetHit, ImageVariant
from .registry import load_modules
from .utils import imread

FIXED_NOTE = "Маркировка исправлена по запомненным заменам: %s"


def create_context(app_dir: str) -> Context:
    cfg = load_config(app_dir)
    logger = setup_logging(app_dir, cfg)
    ctx = Context(cfg, app_dir, logger)
    load_modules(ctx)
    return ctx


class ChipPipeline:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.m = ctx.modules
        self.opts = ctx.config.get("pipeline", {})
        # крючки расширений (API 1.1, `Services.add_marking_filter` / `on_marking_corrected`)
        self.marking_filters: List[Any] = []     # (распознанная маркировка) → исправленная
        self.marking_listeners: List[Any] = []   # (было, стало) после ручного исправления → строка в журнал или None

    def _say(self, report: ChipReport, progress: Optional[ProgressFn], msg: str) -> None:
        report.log.append(msg)
        self.ctx.log.info(msg)
        if progress:
            progress(msg)

    # 1. Фото → кандидаты → локальная база
    def analyze_image(self, path: str, progress: Optional[ProgressFn] = None,
                      marking_override: str = "", ocr_mode: str = "") -> Tuple[ChipReport, List[ImageVariant]]:
        """ocr_mode — способ распознавания (`auto`, id провайдера, `compare`); пусто — из настроек."""
        r = ChipReport(image_path=path)
        img = imread(path)
        self._say(r, progress, "Улучшаю изображение…")
        variants = self.m["enhancer"].enhance(img)
        r.chip = self.m["identifier"].estimate_chip(img)

        if marking_override:
            text, alts = marking_override, []
        else:
            ocr = self.m["ocr"]
            if not ocr.is_available():
                self._say(r, progress, "OCR недоступен: %s. Введите маркировку вручную." % getattr(ocr, "error", ""))
                return r, variants
            how = {"mode": ocr_mode} if ocr_mode and hasattr(ocr, "modes") else {}
            r.ocr = ocr.recognize(variants, progress=lambda s: progress and progress(s), original=img, **how)
            text = r.ocr.best_text
            alts = [l.text for l in r.ocr.lines if l.confidence >= 40]
            how = " (%s)" % r.ocr.provider_title if r.ocr.provider_title else ""
            self._say(r, progress, "Распознано%s: %s" % (how, text.replace("\n", " / ")))
            fixed = self.fix_marking(text)
            if fixed != text:
                text = r.ocr.best_text = fixed
                self._say(r, progress, FIXED_NOTE % text.replace("\n", " / "))
        self.identify(r, text, alts, progress)
        return r, variants

    def fix_marking(self, text: str) -> str:
        """Распознанная маркировка после фильтров расширений (словарь замен, шаг 8.3g)."""
        for fn in list(self.marking_filters):
            fixed = fn(text)
            if isinstance(fixed, str) and fixed.strip():
                text = fixed
        return text

    def refresh_marking(self, r: ChipReport, progress: Optional[ProgressFn] = None) -> bool:
        """Заново применяет фильтры к уже распознанному фото. True — маркировка изменилась, партномер определён заново."""
        if r.ocr is None or not r.ocr.best_text:
            return False
        fixed = self.fix_marking(r.ocr.best_text)
        if fixed == r.ocr.best_text:
            return False
        r.ocr.best_text = fixed
        self._say(r, progress, FIXED_NOTE % fixed.replace("\n", " / "))
        self.identify(r, fixed, [l.text for l in r.ocr.lines if l.confidence >= 40], progress)
        return True

    def identify(self, r: ChipReport, text: str, alts: Optional[List[str]] = None,
                 progress: Optional[ProgressFn] = None) -> None:
        r.candidates = self.m["identifier"].identify(text, alts or [])
        if r.ocr is None or (r.ocr and r.ocr.best_text != text):
            from .models import OcrResult
            if r.ocr is None:
                r.ocr = OcrResult(best_text=text, best_variant="введено вручную")
            else:
                before, r.ocr.best_text = r.ocr.best_text, text
                for fn in list(self.marking_listeners):      # ручное исправление: расширения могут его запомнить
                    note = fn(before, text)
                    if isinstance(note, str) and note:
                        self._say(r, progress, note)
        if not r.candidates:
            self._say(r, progress, "Партномер не определён — исправьте маркировку вручную")
            return
        self._say(r, progress, "Кандидаты: " + ", ".join("%s (%d%%)" % (c.part, c.score * 100) for c in r.candidates[:5]))
        self.set_part(r, r.candidates[0].part, progress)

    def set_part(self, r: ChipReport, part: str, progress: Optional[ProgressFn] = None) -> None:
        r.chosen_part = part
        r.hits = [h for h in r.hits if not h.is_local]
        local = self.m["local_db"].search(part)
        r.hits = local + r.hits
        if local:
            self._say(r, progress, "В локальной базе: %d совпадений, лучшее: %s (%s)"
                      % (len(local), local[0].title, local[0].note))
        else:
            self._say(r, progress, "В локальной базе не найдено")
        r.datasheet_path = ""
        best = local[0] if local and local[0].score >= float(self.opts.get("local_accept_score", 0.8)) else None
        if best:
            r.datasheet_path = best.location
        self.evaluate(r, progress)

    # 2. Интернет
    def search_web(self, r: ChipReport, progress: Optional[ProgressFn] = None,
                   cancel: Optional[CancelToken] = None, levels: Optional[List[str]] = None) -> None:
        cands = [c for c in r.candidates if c.part == r.chosen_part] + \
                [c for c in r.candidates if c.part != r.chosen_part]
        if not cands and r.chosen_part:
            from .models import Candidate
            cands = [Candidate(part=r.chosen_part, score=1.0, reason="введено вручную")]
        hits = self.m["web_search"].search(cands, progress=progress, cancel=cancel, levels=levels)
        r.web_searched = True
        r.records = list(getattr(self.m["web_search"], "last_records", []))
        r.conclusion = getattr(self.m["web_search"], "last_conclusion", None)
        known = set(h.location for h in r.hits)
        r.hits += [h for h in hits if h.location not in known]
        found = [h for h in hits if h.is_local]       # поиск уже скачал, проверил и положил в библиотеку
        if found and not r.datasheet_path:
            r.datasheet_path = max(found, key=lambda x: x.score).location
        self._say(r, progress, "Интернет: найдено ссылок %d, из них PDF %d" %
                  (len(hits), sum(1 for h in hits if h.is_pdf)))
        if self.opts.get("auto_download", True) and not r.datasheet_path:
            n = int(self.opts.get("auto_download_count", 3))
            tried = 0
            for h in sorted(hits, key=lambda x: -x.score):
                if cancel and cancel.cancelled or tried >= n:
                    break
                if not (h.is_pdf and h.allowed and h.score >= 0.5):
                    continue
                tried += 1
                if self.download_hit(r, h, progress):
                    break
        self.evaluate(r, progress)

    def download_hit(self, r: ChipReport, h: DatasheetHit, progress: Optional[ProgressFn] = None,
                     force_accept: bool = False) -> bool:
        res = self.m["web_search"].download(h, progress=progress)
        if not res.ok:
            self._say(r, progress, "Не скачано (%s): %s" % (h.source, res.message))
            h.note = res.message
            return False
        if res.suspicious and not force_accept:
            self._say(r, progress, res.message)
            h.note = "в карантине: " + ", ".join(res.suspicious)
            return False
        if not res.verified_part and not force_accept:
            self._say(r, progress, "Скачан PDF, но партномер %s в нём не найден — оставлен в карантине (%s)"
                      % (h.part, os.path.basename(res.path)))
            h.note = "не подтверждён: " + res.path
            return False
        saved = self.m["local_db"].add_to_library(res.path, h.part, {"url": h.location, "source": h.source,
                                                                      "level": h.level, "sha256": res.sha256})
        try:
            os.remove(res.path)
        except OSError:
            pass
        self._say(r, progress, "Сохранено в базу: %s" % saved)
        h.note = "сохранён: " + saved
        r.datasheet_path = saved
        r.hits.insert(0, DatasheetHit(part=h.part, title=os.path.basename(saved), location=saved,
                                      source="library", level="local", score=1.0, is_local=True, is_pdf=True,
                                      note="скачан из " + h.location))
        self.evaluate(r, progress)
        return True

    # 3. Сверка и память
    # 3. Решение пользователя по найденному документу и свой PDF (вкладка «Документы»; вызывать из фона)
    def decide_record(self, r: ChipReport, rec: Any, accept: bool, progress: Optional[ProgressFn] = None) -> Any:
        ws = self.m["web_search"]
        if not hasattr(ws, "accept"):
            self._say(r, progress, "Модуль поиска не поддерживает решения по документам")
            return rec
        old = rec.stored_path
        rec = ws.accept(rec, r.records) if accept else ws.reject(rec)
        if rec.verdict and rec.verdict.status == "confirmed" and rec.stored_path:
            r.datasheet_path = rec.stored_path
        elif old and r.datasheet_path == old and not rec.stored_path:
            r.datasheet_path = ""
        self._say(r, progress, "Документ %s вами" % ("подтверждён" if accept else "отклонён"))
        self.evaluate(r, progress)
        return rec

    def check_own_pdf(self, r: ChipReport, path: str, progress: Optional[ProgressFn] = None) -> Any:
        ws = self.m["web_search"]
        if not hasattr(ws, "check_pdf"):
            self._say(r, progress, "Модуль поиска не умеет проверять свой PDF")
            return None
        from ..acquire.models import PhotoContext
        cand = next((c for c in r.candidates if c.part == r.chosen_part), None)
        ctx = PhotoContext(part=r.chosen_part, manufacturer=cand.manufacturer if cand else "",
                           package=r.chip.package or "", marking=r.ocr.best_text if r.ocr else "")
        rec = ws.check_pdf(path, ctx, r.records)
        r.records.append(rec)
        if rec.verdict and rec.verdict.status == "confirmed" and rec.stored_path:
            r.datasheet_path = rec.stored_path
        self._say(r, progress, "Свой PDF проверен: %s" % (rec.verdict.status if rec.verdict else "не прочитан"))
        self.evaluate(r, progress)
        return rec

    def evaluate(self, r: ChipReport, progress: Optional[ProgressFn] = None) -> None:
        text = ""
        if r.datasheet_path and os.path.exists(r.datasheet_path):
            try:
                text = self.m["pdf_text"].extract(r.datasheet_path)
            except Exception as ex:  # noqa
                self._say(r, progress, "PDF не читается: %s" % ex)
            marking = r.ocr.best_text if r.ocr else ""
            r.comparison = self.m["comparator"].compare(r.chosen_part, marking, r.chip, text)
            self._say(r, progress, "Сверка: %s" % r.comparison.verdict)
        else:
            r.comparison = None
        desc = next((c.description for c in r.candidates if c.part == r.chosen_part), "")
        r.memory = self.m["memory"].analyze(r.chosen_part, text, desc)
        if r.memory:
            self._say(r, progress, "Память: %s" % r.memory.summary)

    def render(self, r: ChipReport, variants=None, colors=None) -> str:
        """`colors` — цвета темы окна; модуль отчёта, не знающий этого параметра, рисует по-старому."""
        if colors:
            try:
                return self.m["report"].render(r, variants, colors=colors)
            except TypeError:
                pass
        return self.m["report"].render(r, variants)
