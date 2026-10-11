# -*- coding: utf-8 -*-
"""Извлечение текста из PDF (pypdf — чистый Python, ничего не исполняет из PDF).
Страницы скана (без текстового слоя) читает `acquire/scanocr.py` — заключение о памяти делается и по скану."""
from __future__ import annotations

import logging

from ..acquire import scanocr
from ..core.interfaces import PdfText

logging.getLogger("pypdf").setLevel(logging.ERROR)


class PyPdfText(PdfText):
    name = "pypdf"

    def __init__(self, settings, ctx):
        super().__init__(settings, ctx)
        scanocr.configure(ctx.config, ctx.app_dir)

    def extract(self, path: str, max_pages: int = 40) -> str:
        db = self.ctx.module("local_db")
        full = max_pages >= int(self.settings.get("max_pages", 40))
        if full and db is not None:
            cached = db.get_cached_text(path)
            if cached is not None:
                return cached
        from pypdf import PdfReader
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa
                pass
        parts = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            try:
                parts.append(page.extract_text() or "")
            except Exception as e:  # noqa
                self.ctx.log.debug("Страница %d не читается: %s", i, e)
                parts.append("")
        blank = scanocr.scan_pages({i + 1: part for i, part in enumerate(parts)})
        for n, found in scanocr.ocr_pages(path, blank).items():
            parts[n - 1] = (parts[n - 1] + "\n" + found).strip()
        text = "\n".join(parts)
        if full and db is not None and text.strip():
            db.put_cached_text(path, text)
        return text
