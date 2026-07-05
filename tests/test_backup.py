"""Tests for SQLite backup/restore helpers (app/services/backup.py)."""
import sqlite3

import pytest

from app.services import backup

REQUIRED = ("students", "opportunities", "hour_submissions")


def _make_db(path, tables=REQUIRED, marker=None):
    conn = sqlite3.connect(path)
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
    if marker is not None:
        conn.execute("INSERT INTO students (id) VALUES (?)", (marker,))
    conn.commit()
    conn.close()


@pytest.fixture
def sqlite_settings(tmp_path, monkeypatch):
    """Point the backup service at an isolated temp SQLite database + backup dir."""
    db_file = tmp_path / "munus.db"
    _make_db(str(db_file), marker=42)
    monkeypatch.setattr(backup.settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(backup.settings, "backup_dir", str(tmp_path / "backups"))
    return tmp_path, db_file


def test_create_snapshot_is_valid_db_with_tables(sqlite_settings, tmp_path):
    dest = str(tmp_path / "snap.db")

    backup.create_snapshot(dest)

    assert backup.validate_sqlite_file(dest) is True
    conn = sqlite3.connect(dest)
    rows = conn.execute("SELECT id FROM students").fetchall()
    conn.close()
    assert rows == [(42,)]


def test_validate_rejects_non_sqlite(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database")
    assert backup.validate_sqlite_file(str(junk)) is False


def test_validate_rejects_db_missing_required_tables(tmp_path):
    partial = tmp_path / "partial.db"
    _make_db(str(partial), tables=("students",))
    assert backup.validate_sqlite_file(str(partial)) is False


def test_nightly_backup_creates_and_rotates(sqlite_settings, monkeypatch):
    monkeypatch.setattr(backup.settings, "backup_keep", 2)

    import time
    for _ in range(4):
        time.sleep(0.01)  # distinct timestamps to avoid name collisions
        backup.nightly_backup()

    kept = backup.list_backups()
    assert len(kept) == 2  # rotated down to backup_keep


def test_stage_and_apply_restore_roundtrip(sqlite_settings):
    tmp_path, db_file = sqlite_settings
    replacement = tmp_path / "replacement.db"
    _make_db(str(replacement), marker=999)
    upload_bytes = replacement.read_bytes()

    ok, _ = backup.stage_restore(upload_bytes)
    assert ok is True

    applied = backup.apply_pending_restore()
    assert applied is True

    conn = sqlite3.connect(str(db_file))
    rows = conn.execute("SELECT id FROM students").fetchall()
    conn.close()
    assert rows == [(999,)]


def test_stage_restore_rejects_invalid_file(sqlite_settings):
    ok, msg = backup.stage_restore(b"not a database")
    assert ok is False
    assert "valid" in msg.lower()
