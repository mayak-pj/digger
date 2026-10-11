# -*- coding: utf-8 -*-
"""Данные вкладок «Документы» и «Почему» (ARCHITECTURE §4.4, §5): таблица найденных документов и текст улик.

Строка таблицы — `AcquisitionRecord` из результата поиска: статус цветом, баллы, источник, тип, страницы.
«Почему» — те же улики E1…E12, что в паспорте, с баллами и цитатами из документа (≤ 150 символов), причины
решения человеческим языком и сайты, подтвердившие документ. Вид и кнопки — `gui/main_window.py`.
"""
from __future__ import annotations

from html import escape
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtGui import QBrush

from ..acquire.models import AcquisitionRecord, Evidence

# статус: подпись, цвет (имя токена темы)
STATUS = {"confirmed": (u"Подтверждён", "success"), "probable": (u"Вероятно", "warning"),
          "needs_user": (u"Нужно ваше решение", "accent"), "rejected": (u"Отклонён", "danger")}
EVIDENCE = {"E1": u"Партномер есть в документе", "E2": u"Полный код заказа есть в документе",
            "E3": u"Партномер подходит под шаблон семейства", "E4": u"Партномер в заголовке",
            "E5": u"Тип документа", "E6": u"Производитель", "E7": u"Корпус", "E8": u"Код маркировки",
            "E9": u"Репутация сайта", "E10": u"Тот же документ на независимых сайтах",
            "E11": u"В заголовке другой партномер", "E12": u"Нет текстового слоя (скан)"}
DOC_TYPES = {"datasheet": u"datasheet", "family": u"datasheet на семейство", "app_note": u"руководство по применению",
             "errata": u"errata", "reference_manual": u"справочное руководство", "catalog": u"каталог",
             "distributor_page": u"страница магазина", "product_brief": u"краткое описание", "unrelated": u"не о микросхемах",
             "unknown": u"неизвестно"}
REASONS = {"low_score": u"баллов меньше порога «вероятно»", "no_part_match": u"партномера в документе нет",
           "other_part_in_heading": u"в заголовке другой партномер", "manufacturer_conflict": u"производитель не совпал",
           "package_conflict": u"корпус не совпал", "no_text_layer": u"это скан — текст не прочитать, нужен ваш взгляд",
           "ocr_text": u"это скан — текст распознан с картинок страниц, возможны ошибки чтения",
           "unconfirmed": u"нет подтверждения: ни сайта производителя, ни двух независимых сайтов с тем же файлом",
           "user_confirmed": u"подтверждён вами", "user_rejected": u"отклонён вами",
           "hard:not_pdf": u"файл не PDF", "hard:too_big": u"файл слишком большой", "hard:damaged": u"файл повреждён",
           "hard:encrypted": u"PDF зашифрован", "hard:active_content": u"в PDF есть активное содержимое (скрипты)",
           "hard:unreadable": u"файл не прочитался"}
HINT = {"needs_user": u"Откройте PDF и решите сами: «Подтвердить» или «Отклонить».",
        "probable": u"Документ в папке «вероятные». «Подтвердить» переведёт его в подтверждённые."}
COLUMNS = [u"Статус", u"Баллы", u"Источник", u"Тип", u"Стр.", u"Название / адрес"]

def status_text(rec: AcquisitionRecord) -> str:
    return STATUS.get(rec.verdict.status if rec.verdict else "rejected", STATUS["rejected"])[0]


def reason_text(code: str) -> str:
    return REASONS.get(code, code.split(":", 1)[-1])


def evidence_lines(rec: AcquisitionRecord) -> List[Tuple[Evidence, str]]:
    """Улики записи в порядке значимости (по модулю баллов): сама улика и её человеческое название."""
    ev = list(rec.verdict.evidence) if rec.verdict else []
    ev.sort(key=lambda e: (-abs(e.points), e.code))
    out = []
    for e in ev:
        title = EVIDENCE.get(e.code, e.code)
        if e.code == "E5" and e.detail:
            title = u"%s: %s" % (title, DOC_TYPES.get(e.detail, e.detail))
        out.append((e, title))
    return out


def why_html(rec: Optional[AcquisitionRecord], colors: Optional[Dict[str, str]] = None) -> str:
    """Текст вкладки «Почему»: итог и баллы, причины, улики с цитатами, подтвердившие сайты."""
    c = {"success": "#1a7f37", "danger": "#c62828", "muted": "#777"}
    c.update(colors or {})
    if rec is None or rec.verdict is None:
        return u"<p style='color:%s'>Выберите документ во вкладке «Документы» — здесь будет, почему он принят или нет.</p>" % c["muted"]
    v = rec.verdict
    label, token = STATUS.get(v.status, STATUS["rejected"])
    head = rec.lead.title if rec.lead and rec.lead.title else (rec.lead.url if rec.lead else rec.part)
    out = [u"<h3>%s</h3>" % escape(head or rec.part),
           u"<p><b style='color:%s'>%s</b> · баллов: <b>%d</b> из 100</p>" % (c.get(token, c["muted"]), label, v.score)]
    reasons = [reason_text(r) for r in v.reasons]
    if reasons:
        out.append(u"<p>Причины: %s.</p>" % escape("; ".join(reasons)))
    if v.status in HINT:
        out.append(u"<p style='color:%s'>%s</p>" % (c["muted"], HINT[v.status]))
    lines = evidence_lines(rec)
    if lines:
        out.append(u"<table cellspacing='4'>")
        for e, title in lines:
            color = c["success"] if e.points > 0 else c["danger"] if e.points < 0 else c["muted"]
            quote = u"«%s»" % escape(e.detail) if e.detail and e.code != "E5" else ""
            page = u" (стр. %d)" % e.page if e.page else ""
            out.append(u"<tr><td style='color:%s'><b>%+d</b></td><td>%s%s %s</td></tr>"
                       % (color, e.points, escape(title), page, quote) if e.points else
                       u"<tr><td style='color:%s'>—</td><td>%s%s</td></tr>" % (c["muted"], escape(title), page))
        out.append(u"</table>")
    if rec.sources_agreeing:
        out.append(u"<p>Тот же документ нашёлся на: %s.</p>" % escape(", ".join(rec.sources_agreeing)))
    if rec.stored_path:
        out.append(u"<p style='color:%s'>В библиотеке: %s</p>" % (c["muted"], escape(rec.stored_path)))
    return u"".join(out)


class DocsModel(QAbstractTableModel):
    """Найденные документы поиска (записи `AcquisitionRecord`), лучшие сверху. `colors`: токены темы → QColor."""

    def __init__(self, colors: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._colors = colors
        self._recs: List[AcquisitionRecord] = []

    def set_records(self, records: Iterable[AcquisitionRecord]) -> None:
        order = {"confirmed": 0, "probable": 1, "needs_user": 2, "rejected": 3}
        recs = [r for r in records if r.verdict is not None]
        recs.sort(key=lambda r: (order.get(r.verdict.status, 9), -r.verdict.score))
        self.beginResetModel()
        self._recs = recs
        self.endResetModel()

    def record(self, row: int) -> Optional[AcquisitionRecord]:
        return self._recs[row] if 0 <= row < len(self._recs) else None

    def row_of(self, rec: AcquisitionRecord) -> int:
        return self._recs.index(rec) if rec in self._recs else -1

    def refresh(self, rec: AcquisitionRecord) -> None:
        """Запись изменилась (решение пользователя): строка перерисовывается на месте."""
        if rec in self._recs:
            row = self._recs.index(rec)
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._recs)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        return COLUMNS[section] if orientation == Qt.Horizontal else section + 1

    def data(self, index, role=Qt.DisplayRole):
        rec = self.record(index.row()) if index.isValid() else None
        if rec is None:
            return None
        v, lead, col = rec.verdict, rec.lead, index.column()
        if role == Qt.DisplayRole:
            return (status_text(rec), "%d" % v.score, lead.source_id if lead else "",
                    DOC_TYPES.get(rec.facts.doc_type, rec.facts.doc_type) if rec.facts else "",
                    "%d" % rec.facts.pages if rec.facts and rec.facts.pages else "",
                    (lead.title or lead.url) if lead else "")[col]
        if role == Qt.ForegroundRole and col == 0:
            return QBrush(self._colors[STATUS.get(v.status, STATUS["rejected"])[1]])
        if role == Qt.ToolTipRole:
            return u"; ".join(reason_text(r) for r in v.reasons) or (lead.url if lead else "")
        if role == Qt.TextAlignmentRole and col in (1, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None
