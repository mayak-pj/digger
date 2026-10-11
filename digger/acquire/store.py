# -*- coding: utf-8 -*-
"""Хранение документов (ARCHITECTURE §4.9; шаг 5.2).

Файл ложится в `<library_dir>/confirmed|probable/<2 символа>/<PART>__<source>__<sha8>.pdf` рядом с паспортом
`.json`. Индекс — те же таблицы `files`/`parts` локальной базы (`SQLiteLocalDB`), поэтому найденный документ
сразу виден локальному поиску. К ним добавлены `acquisitions` (каждое решение по документу, в том числе
отказы) и `attempts` (каждая попытка скачать ссылку). Таблицы создаёт `IF NOT EXISTS`, версия схемы — в
`PRAGMA user_version`: старый индекс не теряется.

Один и тот же sha256 не дублируется: повторное скачивание возвращает прежний файл; «вероятный» файл,
который потом подтвердили, переезжает в `confirmed/` (обратно не понижается).

Несколько рабочих мест (шаг 9.5a): файл пишется во временное имя `.part` и переименовывается, поэтому другая
копия программы, сканирующая общую библиотеку, не увидит недокачанный PDF. Если сетевая библиотека
недоступна, документ ложится в локальную папку `library_pending/` рядом с индексом (та же раскладка) и
переносится в библиотеку при следующем сохранении или вызове `flush_pending()`; поиск его видит сразу.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import logging
import os
import shutil
from typing import Any, Dict, List

from .. import __version__
from ..core.utils import norm_part, safe_filename
from .models import AcquisitionRecord

log = logging.getLogger("digger.acquire.store")
PENDING_DIR = "library_pending"
SCHEMA_VERSION = 1
STORED_STATUSES = ("confirmed", "probable")
SCHEMA = """
CREATE TABLE IF NOT EXISTS acquisitions(
  id INTEGER PRIMARY KEY, part TEXT, sha256 TEXT, status TEXT, score INTEGER, url TEXT, final_url TEXT,
  source TEXT, path TEXT, started_at TEXT, finished_at TEXT, passport TEXT);
CREATE INDEX IF NOT EXISTS acquisitions_part ON acquisitions(part);
CREATE INDEX IF NOT EXISTS acquisitions_sha ON acquisitions(sha256);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY, part TEXT, url TEXT, source TEXT, status TEXT, error TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS attempts_part ON attempts(part);
"""


def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class AcquireStore:
    """Надстройка над `SQLiteLocalDB`: общее соединение, короткие транзакции, одна блокировка."""

    def __init__(self, db) -> None:
        self.db = db
        with db._lock:
            db.conn.executescript(SCHEMA)
            if db.conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                db.conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            db.conn.commit()

    # -------------------- паспорт --------------------
    @staticmethod
    def passport(rec: AcquisitionRecord, sha: str, size: int) -> Dict[str, Any]:
        lead, fetch, facts, verdict = rec.lead, rec.fetch, rec.facts, rec.verdict
        return {
            "part": rec.part,
            "url": lead.url if lead else "",
            "final_url": fetch.final_url if fetch else "",
            "source": (lead.source_id if lead else "") or "web",
            "level": lead.level if lead else "",
            "manual": bool(lead and lead.source_id == "manual"),
            "sha256": sha, "size": size,
            "pages": facts.pages if facts else 0,
            "doc_type": facts.doc_type if facts else "",
            "verdict": verdict.status if verdict else "",
            "score": verdict.score if verdict else 0,
            "evidence": [e.to_dict() for e in verdict.evidence] if verdict else [],
            "confirmed_by": list(rec.sources_agreeing),
            "downloaded_at": rec.finished_at or _now(),
            "app_version": __version__,
        }

    # -------------------- сохранение --------------------
    def save(self, rec: AcquisitionRecord) -> str:
        """Кладёт файл из карантина в библиотеку; возвращает путь или "" (отказ, нужен пользователь)."""
        self.flush_pending()
        status = rec.verdict.status if rec.verdict else "rejected"
        path = ""
        passport: Dict[str, Any] = {}
        src = rec.fetch.path_in_quarantine if rec.fetch else ""
        if status in STORED_STATUSES and src and os.path.isfile(src):
            sha = (rec.fetch.sha256 if rec.fetch else "") or _sha256(src)
            passport = self.passport(rec, sha, os.path.getsize(src))
            path = self._place(rec, src, status, sha, passport)
            rec.stored_path = path
        self._log_acquisition(rec, status, path, passport)
        return path

    def _dest(self, part: str, source: str, status: str, sha: str) -> str:
        sub = os.path.join(self.db.library_dir, status, safe_filename(norm_part(part)[:2] or "_"))
        return os.path.join(sub, "%s__%s__%s.pdf" % (safe_filename(part), safe_filename(source, 20), sha[:8]))

    @property
    def pending_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self.db.db_path)), PENDING_DIR)

    @staticmethod
    def _copy_atomic(src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part"
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise

    def _place(self, rec: AcquisitionRecord, src: str, status: str, sha: str, passport: Dict[str, Any]) -> str:
        db = self.db
        with db._lock:
            row = db.conn.execute("SELECT path FROM files WHERE sha256=?", (sha,)).fetchone()
            if row and os.path.exists(row[0]):
                return self._reuse(row[0], rec, status, passport)
            dst = self._dest(rec.part, passport["source"], status, sha)
            origin = "library"
            try:
                self._copy_atomic(src, dst)
                self._write_passport(dst, passport)
            except OSError as e:        # сетевая библиотека недоступна или переполнена — не теряем документ
                log.warning("библиотека недоступна (%s), документ сохранён локально", e)
                dst = os.path.join(self.pending_dir, os.path.relpath(dst, db.library_dir))
                self._copy_atomic(src, dst)
                self._write_passport(dst, passport)
                origin = "pending"
            db._add_file(dst, origin, sha, passport["url"], [rec.part])
            db.conn.commit()
            return dst

    def _reuse(self, path: str, rec: AcquisitionRecord, status: str, passport: Dict[str, Any]) -> str:
        """Тот же sha уже есть: файл не дублируется; probable → confirmed переносится."""
        db = self.db
        rel = os.path.relpath(path, db.library_dir).replace("\\", "/")
        if status == "confirmed" and rel.startswith("probable/"):
            new = os.path.join(db.library_dir, "confirmed", *rel.split("/")[1:])
            os.makedirs(os.path.dirname(new), exist_ok=True)
            os.replace(path, new)
            if os.path.exists(path + ".json"):
                os.remove(path + ".json")
            db.conn.execute("UPDATE files SET path=? WHERE path=?", (new, path))
            path = new
            self._write_passport(path, passport)
        elif not os.path.exists(path + ".json"):
            self._write_passport(path, passport)
        db._add_file(path, "library", passport["sha256"], passport["url"], [rec.part])
        db.conn.commit()
        return path

    @staticmethod
    def _write_passport(path: str, passport: Dict[str, Any]) -> None:
        tmp = path + ".json.part"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(passport, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path + ".json")

    def pending_count(self) -> int:
        """Сколько документов ждут переноса в библиотеку (локально, быстро)."""
        root = self.pending_dir
        return sum(1 for _, _, names in os.walk(root) for n in names if n.endswith(".pdf")) if os.path.isdir(root) else 0

    def library_status(self) -> Dict[str, Any]:
        """Состояние общей библиотеки: доступна ли для записи и сколько документов ждут. Может быть медленным
        (сетевой диск) — вызывать в фоне. Пробный файл создаётся и сразу удаляется."""
        lib, error = self.db.library_dir, ""
        try:
            os.makedirs(lib, exist_ok=True)
            probe = os.path.join(lib, ".digger_probe_%d.part" % os.getpid())
            with open(probe, "wb") as f:
                f.write(b"ok")
            os.remove(probe)
        except OSError as e:
            error = str(e)
        return {"library_dir": lib, "available": not error, "error": error,
                "pending": self.pending_count(), "pending_dir": self.pending_dir}

    def flush_pending(self) -> int:
        """Переносит документы из `library_pending/` в библиотеку; возвращает, сколько перенесено.
        Недоступная библиотека — не ошибка: файлы остаются на месте до следующего раза."""
        db, root, moved = self.db, self.pending_dir, 0
        if not os.path.isdir(root):
            return 0
        for folder, _, names in os.walk(root):
            for name in sorted(names):
                if not name.endswith(".pdf"):
                    continue
                path = os.path.join(folder, name)
                dst = os.path.join(db.library_dir, os.path.relpath(path, root))
                try:
                    if os.path.exists(dst):                  # другая копия уже положила тот же документ
                        os.remove(path)
                    else:
                        self._copy_atomic(path, dst)
                        if os.path.exists(path + ".json"):
                            self._copy_atomic(path + ".json", dst + ".json")
                        os.remove(path)
                    if os.path.exists(path + ".json"):
                        os.remove(path + ".json")
                except OSError as e:
                    log.warning("перенос в библиотеку не удался (%s)", e)
                    return moved
                with db._lock:
                    row = db.conn.execute("SELECT id FROM files WHERE path=?", (dst,)).fetchone()
                    if row:                                   # путь в библиотеке уже в индексе — лишняя строка
                        db.conn.execute("DELETE FROM parts WHERE file_id IN (SELECT id FROM files WHERE path=?)", (path,))
                        db.conn.execute("DELETE FROM files WHERE path=?", (path,))
                    else:
                        db.conn.execute("UPDATE files SET path=?, origin='library' WHERE path=?", (dst, path))
                    db.conn.commit()
                moved += 1
        return moved

    # -------------------- журналы --------------------
    def _log_acquisition(self, rec: AcquisitionRecord, status: str, path: str, passport: Dict[str, Any]) -> None:
        lead, fetch = rec.lead, rec.fetch
        with self.db._lock:
            self.db.conn.execute(
                "INSERT INTO acquisitions(part,sha256,status,score,url,final_url,source,path,started_at,"
                "finished_at,passport) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rec.part, fetch.sha256 if fetch else "", status, rec.verdict.score if rec.verdict else 0,
                 lead.url if lead else "", fetch.final_url if fetch else "", lead.source_id if lead else "",
                 path, rec.started_at, rec.finished_at or _now(),
                 json.dumps(passport, ensure_ascii=False) if passport else ""))
            self.db.conn.commit()

    def remove(self, path: str) -> bool:
        """Убирает файл из библиотеки вместе с паспортом и строками индекса (отклонённый пользователем документ)."""
        db = self.db
        root = os.path.abspath(db.library_dir)
        if not os.path.abspath(path).startswith(root + os.sep):      # только своя библиотека
            return False
        with db._lock:
            row = db.conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if row:
                db.conn.execute("DELETE FROM parts WHERE file_id=?", (row[0],))
                db.conn.execute("DELETE FROM files WHERE id=?", (row[0],))
                db.conn.commit()
        for p in (path, path + ".json"):
            try:
                os.remove(p)
            except OSError:
                pass
        return True

    def record_attempt(self, part: str, url: str, status: str, error: str = "", source: str = "") -> None:
        with self.db._lock:
            self.db.conn.execute("INSERT INTO attempts(part,url,source,status,error,at) VALUES(?,?,?,?,?,?)",
                                 (part, url, source, status, error, _now()))
            self.db.conn.commit()

    def _rows(self, table: str, part: str) -> List[Dict[str, Any]]:
        with self.db._lock:
            cur = self.db.conn.execute("SELECT * FROM %s WHERE part=? ORDER BY id" % table, (part,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def acquisitions(self, part: str) -> List[Dict[str, Any]]:
        return self._rows("acquisitions", part)

    def attempts(self, part: str) -> List[Dict[str, Any]]:
        return self._rows("attempts", part)
