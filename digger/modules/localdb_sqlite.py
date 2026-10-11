# -*- coding: utf-8 -*-
"""Локальная база datasheet (SQLite).

* Индексирует PDF-файлы в папках на сетевых дисках (только чтение).
* Хранит скачанные из интернета datasheet в папке-библиотеке.
* Кэширует извлечённый из PDF текст.
* Каталог описаний деталей (импорт из библиотек KiCad или CSV) — помогает
  опознать чип без интернета и узнать, есть ли в нём память.
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import zipfile
from typing import Any, Dict, Iterable, List, Optional

from ..core.config import resolve_path
from ..core.interfaces import CancelToken, LocalDB, ProgressFn
from ..core.models import DatasheetHit
from ..core.utils import norm_part, safe_filename

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, size INTEGER, mtime REAL,
  origin TEXT, sha256 TEXT, url TEXT, added TEXT);
CREATE TABLE IF NOT EXISTS parts(part TEXT, pattern TEXT, file_id INTEGER, kind TEXT);
CREATE INDEX IF NOT EXISTS parts_part ON parts(part);
CREATE INDEX IF NOT EXISTS parts_file ON parts(file_id);
CREATE TABLE IF NOT EXISTS textcache(path TEXT PRIMARY KEY, mtime REAL, text TEXT);
CREATE TABLE IF NOT EXISTS catalog(part TEXT PRIMARY KEY, name TEXT, description TEXT,
  manufacturer TEXT, footprint TEXT, datasheet_url TEXT, source TEXT);
"""

DOC_EXT = (".pdf",)


def tokens_from_name(name: str) -> List[Dict[str, str]]:
    """Из имени файла 'STM32F103x8_datasheet.pdf' → [{'part':'STM32F103X8','pattern':'STM32F103.8'}]."""
    stem = os.path.splitext(name)[0]
    out = []
    pieces = re.split(r"[\s_,;()\[\]{}]+|(?<=\D)-(?=\D)", stem)
    # "AT24C01A_02_04_08_16" → добавляем AT24C02, AT24C04, AT24C08, AT24C16
    expanded = []
    last = ""
    for piece in pieces:
        if re.fullmatch(r"\d{1,4}[A-Za-z]?", piece) and last:
            m = re.match(r"^(.*?)(\d+)([A-Za-z]*)$", last)
            if m and len(m.group(2)) >= len(re.match(r"\d+", piece).group(0)):
                digits = re.match(r"\d+", piece).group(0)
                base = m.group(1) + m.group(2)[:len(m.group(2)) - len(digits)]
                expanded.append(base + piece)
                continue
        if re.search(r"[A-Za-z]", piece) and re.search(r"\d", piece):
            last = piece
        expanded.append(piece)
    pieces = expanded
    seen = set()
    for piece in pieces + [stem]:
        raw = re.sub(r"[^A-Za-z0-9xX*?]", "", piece)
        norm = norm_part(raw)
        if len(norm) < 3 or not re.search(r"\d", norm) or not re.search(r"[A-Z]", norm) and len(norm) < 4:
            continue
        if norm in seen or len(norm) > 40:
            continue
        seen.add(norm)
        # строчная x или */? в имени datasheet обычно означает "любой символ"
        pat = re.sub(r"(?<=[0-9A-Z])x|[*?]", ".", raw)
        pat = re.sub(r"[^A-Za-z0-9.]", "", pat).upper()
        out.append({"part": norm, "pattern": pat if "." in pat else ""})
    return out


class SQLiteLocalDB(LocalDB):
    name = "sqlite"

    def __init__(self, settings, ctx):
        super().__init__(settings, ctx)
        paths = ctx.config["paths"]
        self.db_path = resolve_path(ctx.app_dir, paths["db"])
        self.library_dir = resolve_path(ctx.app_dir, paths["library_dir"])
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -------------------- индексация --------------------
    def _add_file(self, path: str, origin: str, sha: str = "", url: str = "",
                  extra_parts: Iterable[str] = ()) -> int:
        st = os.stat(path)
        name = os.path.basename(path)
        cur = self.conn.execute("SELECT id, size, mtime FROM files WHERE path=?", (path,))
        row = cur.fetchone()
        if row and row[1] == st.st_size and abs((row[2] or 0) - st.st_mtime) < 1 and not extra_parts:
            return row[0]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        if row:
            fid = row[0]
            self.conn.execute("UPDATE files SET size=?, mtime=? WHERE id=?", (st.st_size, st.st_mtime, fid))
            self.conn.execute("DELETE FROM parts WHERE file_id=? AND kind='filename'", (fid,))
        else:
            cur = self.conn.execute(
                "INSERT INTO files(path,name,size,mtime,origin,sha256,url,added) VALUES(?,?,?,?,?,?,?,?)",
                (path, name, st.st_size, st.st_mtime, origin, sha, url, now))
            fid = cur.lastrowid
        for t in tokens_from_name(name):
            self.conn.execute("INSERT INTO parts(part,pattern,file_id,kind) VALUES(?,?,?,?)",
                              (t["part"], t["pattern"], fid, "filename"))
        for p in extra_parts:
            n = norm_part(p)
            if n:
                self.conn.execute("INSERT INTO parts(part,pattern,file_id,kind) VALUES(?,?,?,?)",
                                  (n, "", fid, "confirmed"))
        return fid

    def index(self, roots: List[str], progress: Optional[ProgressFn] = None,
              cancel: Optional[CancelToken] = None) -> int:
        say = progress or (lambda m: None)
        roots = [resolve_path(self.ctx.app_dir, r) for r in roots if r] + [self.library_dir]
        count = 0
        with self._lock:
            for root in roots:
                if not os.path.isdir(root):
                    say("Папка недоступна: %s" % root)
                    continue
                seen = set()
                for dirpath, _dirs, files in os.walk(root):
                    if cancel and cancel.cancelled:
                        self.conn.commit()
                        return count
                    for fn in files:
                        if not fn.lower().endswith(DOC_EXT):
                            continue
                        full = os.path.join(dirpath, fn)
                        try:
                            self._add_file(full, "library" if root == self.library_dir else "scan")
                            seen.add(full)
                            count += 1
                        except OSError as e:
                            self.ctx.log.warning("Пропущен %s: %s", full, e)
                        if count % 200 == 0:
                            say("Проиндексировано файлов: %d (%s)" % (count, dirpath))
                            self.conn.commit()
                # удаляем из индекса файлы, которых больше нет в этой папке
                like = root.rstrip("\\/") + "%"
                for fid, path in self.conn.execute("SELECT id, path FROM files WHERE path LIKE ?", (like,)).fetchall():
                    if path not in seen and not os.path.exists(path):
                        self.conn.execute("DELETE FROM parts WHERE file_id=?", (fid,))
                        self.conn.execute("DELETE FROM files WHERE id=?", (fid,))
            self.conn.commit()
        say("Индексация завершена: %d файлов" % count)
        return count

    # -------------------- поиск --------------------
    def search(self, part: str, limit: int = 20) -> List[DatasheetHit]:
        p = norm_part(part)
        if len(p) < 2:
            return []
        core = p[:5]
        # хвостовые буквы/цифры партномера часто — корпус и температура (N, T6, SIQ, -10SU)
        shorter = [p[:k] for k in range(len(p) - 1, max(4, int(len(p) * 0.6)) - 1, -1)]
        with self._lock:
            rows = self.conn.execute(
                "SELECT parts.part, parts.pattern, parts.kind, files.path, files.name, files.origin "
                "FROM parts JOIN files ON files.id=parts.file_id WHERE parts.part LIKE ? OR parts.part LIKE ?",
                (p[:3] + "%", "%" + core + "%")).fetchall()
        best: Dict[str, DatasheetHit] = {}
        for tpart, pattern, kind, path, name, origin in rows:
            score, why = 0.0, ""
            if tpart == p:
                score, why = 1.0, "точное совпадение"
            elif pattern and re.fullmatch(pattern, p):
                score, why = 0.95, "совпадение по шаблону %s" % pattern
            elif pattern and re.match(pattern, p) and len(pattern) >= 0.6 * len(p):
                score, why = 0.85, "семейство по шаблону %s" % pattern
            elif len(p) >= 5 and p in tpart:
                score, why = 0.85, "партномер входит в %s (другой префикс производителя)" % tpart
            elif p.startswith(tpart) and len(tpart) >= max(4, int(0.6 * len(p))):
                score, why = 0.6 + 0.3 * len(tpart) / len(p), "datasheet на семейство %s" % tpart
            elif tpart.startswith(p) and len(p) >= 4:
                score, why = 0.75, "расширенный партномер %s" % tpart
            else:
                sub = next((s for s in shorter if s in tpart), "")
                if sub:
                    score = 0.55 + 0.3 * len(sub) / len(p)
                    why = "совпадает основа %s с %s" % (sub, tpart)
                else:
                    r = difflib.SequenceMatcher(None, p, tpart).ratio()
                    if r >= 0.85:
                        score, why = r * 0.75, "похожее имя (%.0f%%)" % (r * 100)
            if kind == "confirmed" and score >= 0.6:
                score = min(1.0, score + 0.05)
            if score <= 0 or (path in best and best[path].score >= score):
                continue
            best[path] = DatasheetHit(part=part, title=name, location=path,
                                      source="library" if origin in ("library", "pending") else "network",
                                      level="local", score=round(score, 3), is_local=True,
                                      is_pdf=True, note=why)
        return sorted(best.values(), key=lambda h: -h.score)[:limit]

    # -------------------- библиотека --------------------
    def add_to_library(self, pdf_path: str, part: str, meta: Dict[str, Any]) -> str:
        with open(pdf_path, "rb") as f:
            data = f.read()
        sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            row = self.conn.execute("SELECT path FROM files WHERE sha256=?", (sha,)).fetchone()
            if row and os.path.exists(row[0]):
                self._add_file(row[0], "library", sha, meta.get("url", ""), [part])
                self.conn.commit()
                return row[0]
            sub = os.path.join(self.library_dir, safe_filename(norm_part(part)[:2] or "_"))
            os.makedirs(sub, exist_ok=True)
            fname = "%s__%s__%s.pdf" % (safe_filename(part), safe_filename(meta.get("source", "web"), 20), sha[:8])
            dst = os.path.join(sub, fname)
            with open(dst, "wb") as f:
                f.write(data)
            meta = dict(meta)
            meta.update({"part": part, "sha256": sha,
                         "saved": datetime.datetime.now().isoformat(timespec="seconds")})
            with io.open(dst + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            self._add_file(dst, "library", sha, meta.get("url", ""), [part])
            self.conn.commit()
            return dst

    def confirm_part(self, path: str, part: str) -> None:
        """Пользователь подтвердил, что файл — datasheet на этот партномер."""
        with self._lock:
            row = self.conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if row:
                self.conn.execute("INSERT INTO parts(part,pattern,file_id,kind) VALUES(?,?,?,?)",
                                  (norm_part(part), "", row[0], "confirmed"))
                self.conn.commit()

    # -------------------- кэш текста --------------------
    def get_cached_text(self, path: str) -> Optional[str]:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        with self._lock:
            row = self.conn.execute("SELECT mtime, text FROM textcache WHERE path=?", (path,)).fetchone()
        if row and abs(row[0] - mtime) < 1:
            return row[1]
        return None

    def put_cached_text(self, path: str, text: str) -> None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO textcache(path,mtime,text) VALUES(?,?,?)",
                              (path, mtime, text))
            self.conn.commit()

    # -------------------- каталог --------------------
    def catalog_lookup(self, part: str) -> Optional[Dict[str, str]]:
        p = norm_part(part)
        if len(p) < 3:
            return None
        row = None
        # точное совпадение, затем без хвостовых символов (корпус/температура): 24LC02B → 24LC02
        for k in range(len(p), max(4, int(len(p) * 0.7)) - 1, -1):
            with self._lock:
                row = self.conn.execute(
                    "SELECT name, description, manufacturer, footprint, datasheet_url, source FROM catalog WHERE part=?",
                    (p[:k],)).fetchone()
            if row:
                break
        if not row:
            return None
        keys = ("name", "description", "manufacturer", "footprint", "datasheet_url", "source")
        return dict(zip(keys, row))

    def _catalog_put(self, name, desc, maker, footprint, url, source) -> None:
        p = norm_part(name)
        if len(p) < 3:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO catalog(part,name,description,manufacturer,footprint,datasheet_url,source) "
            "VALUES(?,?,?,?,?,?,?)", (p, name, desc or "", maker or "", footprint or "", url or "", source))

    def import_catalog(self, path: str, progress: Optional[ProgressFn] = None) -> int:
        """Импорт: zip/папка библиотек KiCad (.kicad_sym, .dcm) или CSV
        (столбцы: part;description;manufacturer;package;datasheet_url)."""
        say = progress or (lambda m: None)
        n = 0
        with self._lock:
            if path.lower().endswith(".csv"):
                n += self._import_csv(path)
            elif path.lower().endswith(".zip"):
                with zipfile.ZipFile(path) as z:
                    names = [x for x in z.namelist() if x.endswith((".kicad_sym", ".dcm"))]
                    for i, x in enumerate(names):
                        text = z.read(x).decode("utf-8", errors="replace")
                        n += self._import_kicad_text(text, x)
                        if i % 20 == 0:
                            say("Каталог: %d/%d файлов, %d деталей" % (i, len(names), n))
            elif os.path.isdir(path):
                for dirpath, _d, files in os.walk(path):
                    for fn in files:
                        if fn.endswith((".kicad_sym", ".dcm")):
                            with io.open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace") as f:
                                n += self._import_kicad_text(f.read(), fn)
                            say("Каталог: %s, всего %d деталей" % (fn, n))
            self.conn.commit()
        say("Импортировано деталей в каталог: %d" % n)
        return n

    def _import_csv(self, path: str) -> int:
        import csv
        n = 0
        with io.open(path, encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            for row in csv.reader(f, dialect):
                if not row or row[0].lower() in ("part", "партномер"):
                    continue
                row += [""] * 5
                self._catalog_put(row[0], row[1], row[2], row[3], row[4], os.path.basename(path))
                n += 1
        return n

    def _import_kicad_text(self, text: str, src: str) -> int:
        n = 0
        if "$CMP" in text:  # старый формат .dcm
            for m in re.finditer(r"\$CMP\s+(\S+)(.*?)\$ENDCMP", text, re.S):
                name, body = m.group(1), m.group(2)
                d = re.search(r"^D\s+(.*)$", body, re.M)
                u = re.search(r"^F\s+(.*)$", body, re.M)
                self._catalog_put(name, d.group(1).strip() if d else "", "", "",
                                  u.group(1).strip() if u else "", "kicad:" + os.path.basename(src))
                n += 1
            return n
        # формат .kicad_sym (KiCad 6+): символы верхнего уровня
        starts = [m for m in re.finditer(r'^(?:\t|  )\(symbol "([^"]+)"', text, re.M)]
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
            block = text[m.start():end]
            name = m.group(1).split(":")[-1]

            def prop(key):
                pm = re.search(r'\(property "%s" "((?:[^"\\]|\\.)*)"' % key, block)
                return pm.group(1) if pm else ""
            desc = prop("Description") or prop("ki_description")
            kw = prop("ki_keywords")
            if kw and kw.lower() not in desc.lower():
                desc = (desc + " [" + kw + "]").strip()
            url = prop("Datasheet")
            if url in ("~", ""):
                url = ""
            self._catalog_put(name, desc, "", prop("Footprint"), url, "kicad:" + os.path.basename(src))
            n += 1
        return n

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            files = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            lib = self.conn.execute("SELECT COUNT(*) FROM files WHERE origin IN ('library','pending')").fetchone()[0]
            cat = self.conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
        return {"files": files, "library": lib, "catalog": cat, "db": self.db_path,
                "library_dir": self.library_dir}
