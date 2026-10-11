# -*- coding: utf-8 -*-
"""Хранение документов (шаг 5.2, ARCHITECTURE §4.9)."""
import json
import logging
import os
import sqlite3

import pytest

from digger import __version__
from digger.acquire.models import AcquisitionRecord, DocFacts, Evidence, FetchResult, Lead, Verdict
from digger.acquire.store import AcquireStore
from digger.core.interfaces import Context
from digger.modules.localdb_sqlite import SQLiteLocalDB

PDF = b"%PDF-1.4\n" + b"NE555 datasheet body\n" * 20 + b"%%EOF\n"
PDF2 = PDF + b"% other\n"


def make_db(tmp_path):
    cfg = {"paths": {"db": "data/digger.sqlite", "library_dir": "lib"}}
    return SQLiteLocalDB({}, Context(cfg, str(tmp_path), logging.getLogger("t")))


def make_rec(tmp_path, data=PDF, status="confirmed", part="NE555P", name="a.pdf",
             url="https://www.ti.com/lit/ds/ne555.pdf"):
    q = tmp_path / "quarantine"
    q.mkdir(exist_ok=True)
    path = q / name
    path.write_bytes(data)
    import hashlib
    return AcquisitionRecord(
        part=part, lead=Lead(url=url, kind="pdf", source_id="ti"),
        fetch=FetchResult(ok=True, path_in_quarantine=str(path), sha256=hashlib.sha256(data).hexdigest(),
                          size=len(data), final_url=url),
        facts=DocFacts(pages=3, has_text=True, doc_type="datasheet"),
        verdict=Verdict(status=status, score=85, evidence=[Evidence(code="E1", points=30, detail="NE555P")]),
        sources_agreeing=["ti.com"], started_at="2026-10-06T10:00:00Z", finished_at="2026-10-06T10:00:09Z")


@pytest.fixture
def store(tmp_path):
    db = make_db(tmp_path)
    yield AcquireStore(db)
    db.close()


def test_store_confirmed_writes_file_and_passport(tmp_path, store):
    rec = make_rec(tmp_path)
    dst = store.save(rec)
    assert os.path.isfile(dst)
    rel = os.path.relpath(dst, store.db.library_dir).replace("\\", "/")
    assert rel.startswith("confirmed/NE/NE555P__ti__")
    passport = json.load(open(dst + ".json", encoding="utf-8"))
    assert passport["part"] == "NE555P" and passport["level"] == ""
    assert passport["source"] == "ti" and passport["verdict"] == "confirmed" and passport["score"] == 85
    assert passport["sha256"] == rec.fetch.sha256 and passport["pages"] == 3
    assert passport["evidence"][0]["code"] == "E1" and passport["confirmed_by"] == ["ti.com"]
    assert passport["app_version"] == __version__ and passport["doc_type"] == "datasheet"
    assert rec.stored_path == dst


def test_probable_goes_to_own_folder(tmp_path, store):
    dst = store.save(make_rec(tmp_path, status="probable"))
    assert os.path.relpath(dst, store.db.library_dir).replace("\\", "/").startswith("probable/NE/")


@pytest.mark.parametrize("status", ["rejected", "needs_user"])
def test_not_stored(tmp_path, store, status):
    rec = make_rec(tmp_path, status=status)
    assert store.save(rec) == ""
    assert not os.path.exists(os.path.join(store.db.library_dir, "confirmed"))
    assert store.acquisitions("NE555P")[0]["status"] == status
    assert store.acquisitions("NE555P")[0]["path"] == ""


def test_found_by_local_search(tmp_path, store):
    dst = store.save(make_rec(tmp_path, name="download (1).pdf"))
    hits = store.db.search("NE555P")
    assert hits and hits[0].location == dst and hits[0].score >= 1.0


def test_same_sha_does_not_duplicate(tmp_path, store):
    first = store.save(make_rec(tmp_path, url="https://www.ti.com/a.pdf"))
    second = store.save(make_rec(tmp_path, url="https://www.st.com/b.pdf"))
    assert second == first
    pdfs = [f for _d, _s, fs in os.walk(store.db.library_dir) for f in fs if f.endswith(".pdf")]
    assert len(pdfs) == 1
    assert store.db.conn.execute("SELECT COUNT(*) FROM files WHERE sha256=?",
                                 (make_rec(tmp_path).fetch.sha256,)).fetchone()[0] == 1
    assert len(store.acquisitions("NE555P")) == 2


def test_probable_promoted_when_confirmed(tmp_path, store):
    first = store.save(make_rec(tmp_path, status="probable"))
    second = store.save(make_rec(tmp_path, status="confirmed"))
    assert "confirmed" in second.replace("\\", "/") and not os.path.exists(first)
    assert os.path.isfile(second) and os.path.isfile(second + ".json")
    assert not os.path.exists(first + ".json")
    assert json.load(open(second + ".json", encoding="utf-8"))["verdict"] == "confirmed"
    assert store.db.search("NE555P")[0].location == second


def test_confirmed_not_demoted_by_probable(tmp_path, store):
    first = store.save(make_rec(tmp_path, status="confirmed"))
    assert store.save(make_rec(tmp_path, status="probable")) == first


def test_different_sha_makes_second_file(tmp_path, store):
    a = store.save(make_rec(tmp_path, data=PDF))
    b = store.save(make_rec(tmp_path, data=PDF2, name="b.pdf"))
    assert a != b and os.path.isfile(a) and os.path.isfile(b)


def test_attempts(tmp_path, store):
    store.record_attempt("NE555P", "https://x.example/a.pdf", "failed", error="http_404", source="x")
    store.record_attempt("NE555P", "https://x.example/b.pdf", "ok", source="x")
    rows = store.attempts("NE555P")
    assert [r["status"] for r in rows] == ["failed", "ok"]
    assert rows[0]["error"] == "http_404" and rows[0]["at"]
    assert store.attempts("OTHER") == []


def test_migration_keeps_old_index(tmp_path):
    db = make_db(tmp_path)
    pdf = tmp_path / "scan" / "LM358_datasheet.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(PDF)
    db.index([str(pdf.parent)])
    db.conn.commit()
    db.close()
    # старая база без таблиц шага 5.2 (user_version 0) открывается без потерь
    con = sqlite3.connect(str(tmp_path / "data" / "digger.sqlite"))
    con.executescript("DROP TABLE IF EXISTS acquisitions; DROP TABLE IF EXISTS attempts; PRAGMA user_version=0;")
    con.commit()
    con.close()
    db = make_db(tmp_path)
    AcquireStore(db)
    AcquireStore(db)   # повторное открытие безопасно
    assert db.search("LM358")[0].location == str(pdf)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] >= 1
    db.close()


def test_russian_path_and_unknown_source(tmp_path):
    cfg = {"paths": {"db": "данные/база.sqlite", "library_dir": "библиотека папка"}}
    db = SQLiteLocalDB({}, Context(cfg, str(tmp_path), logging.getLogger("t")))
    rec = make_rec(tmp_path)
    rec.lead.source_id = ""
    dst = AcquireStore(db).save(rec)
    assert os.path.isfile(dst) and "__web__" in dst
    db.close()


def break_library(tmp_path, monkeypatch):
    """Библиотека «недоступна»: любая запись под ней — OSError."""
    import shutil
    lib = str(tmp_path / "lib")
    real_copy = shutil.copyfile

    def copy(src, dst, *a, **k):
        if os.path.abspath(dst).startswith(lib + os.sep):
            raise OSError(53, "сетевой путь не найден")
        return real_copy(src, dst, *a, **k)
    monkeypatch.setattr(shutil, "copyfile", copy)
    return lambda: monkeypatch.setattr(shutil, "copyfile", real_copy)


def test_copy_is_atomic_no_part_files_left(tmp_path, store):
    dst = store.save(make_rec(tmp_path))
    names = os.listdir(os.path.dirname(dst))
    assert sorted(n for n in names if n.endswith(".part")) == [] and os.path.basename(dst) + ".json" in names


def test_unreachable_library_keeps_document_locally_then_flushes(tmp_path, store, monkeypatch):
    repair = break_library(tmp_path, monkeypatch)
    dst = store.save(make_rec(tmp_path))
    assert dst.startswith(store.pending_dir) and os.path.isfile(dst) and os.path.isfile(dst + ".json")
    assert not any(n.endswith(".part") for _, _, fs in os.walk(str(tmp_path)) for n in fs)
    assert store.db.conn.execute("SELECT origin FROM files WHERE path=?", (dst,)).fetchone()[0] == "pending"
    assert store.flush_pending() == 0 and os.path.isfile(dst)               # библиотека всё ещё недоступна
    repair()
    assert store.flush_pending() == 1
    new = os.path.join(store.db.library_dir, os.path.relpath(dst, store.pending_dir))
    assert os.path.isfile(new) and os.path.isfile(new + ".json") and not os.path.exists(dst)
    assert store.db.conn.execute("SELECT path, origin FROM files").fetchall() == [(new, "library")]


def test_pending_is_flushed_on_next_save(tmp_path, store, monkeypatch):
    repair = break_library(tmp_path, monkeypatch)
    pending = store.save(make_rec(tmp_path))
    repair()
    other = store.save(make_rec(tmp_path, data=PDF2, part="LM358", name="b.pdf"))
    assert other.startswith(store.db.library_dir) and not os.path.exists(pending)
    assert store.db.conn.execute("SELECT COUNT(*) FROM files WHERE origin='library'").fetchone()[0] == 2


def test_flush_drops_duplicate_when_another_workstation_already_stored_it(tmp_path, store, monkeypatch):
    repair = break_library(tmp_path, monkeypatch)
    pending = store.save(make_rec(tmp_path))
    repair()
    theirs = os.path.join(store.db.library_dir, os.path.relpath(pending, store.pending_dir))
    os.makedirs(os.path.dirname(theirs), exist_ok=True)
    open(theirs, "wb").write(PDF)
    assert store.flush_pending() == 1 and not os.path.exists(pending) and os.path.isfile(theirs)
