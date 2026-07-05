"""
SQLite database backup / restore.

Snapshots use `VACUUM INTO`, which produces a consistent copy of the live database
without stopping the app. Restores are applied at the next startup: the uploaded file
is staged next to the database and swapped in by `apply_pending_restore()` before the
engine opens any connection (see app/database.py).
"""
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

# Tables we expect a valid backup to contain (sanity check before restoring).
REQUIRED_TABLES = {"students", "opportunities", "hour_submissions"}


def sqlite_path() -> Optional[str]:
    """Return the on-disk path of the SQLite database, or None if not SQLite."""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    # e.g. "sqlite+aiosqlite:///./munus.db" -> "./munus.db"
    _, _, path = url.partition(":///")
    return path or None


def is_sqlite() -> bool:
    return sqlite_path() is not None


def pending_restore_path() -> Optional[str]:
    path = sqlite_path()
    return f"{path}.pending-restore" if path else None


def create_snapshot(dest_path: str) -> None:
    """Write a consistent snapshot of the live database to dest_path via VACUUM INTO."""
    src = sqlite_path()
    if not src:
        raise RuntimeError("Backups are only supported for SQLite databases.")
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    if os.path.exists(dest_path):
        os.remove(dest_path)  # VACUUM INTO requires the target not to exist
    conn = sqlite3.connect(src)
    try:
        conn.execute("VACUUM INTO ?", (dest_path,))
    finally:
        conn.close()


def validate_sqlite_file(path: str) -> bool:
    """Return True if path is a SQLite DB containing the expected tables."""
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    tables = {r[0] for r in rows}
    return REQUIRED_TABLES.issubset(tables)


def stage_restore(upload_bytes: bytes) -> tuple[bool, str]:
    """Validate uploaded bytes and stage them for restore on next startup.

    Returns (ok, message).
    """
    pending = pending_restore_path()
    if not pending:
        return False, "Restore is only supported for SQLite databases."

    tmp = f"{pending}.tmp"
    with open(tmp, "wb") as f:
        f.write(upload_bytes)

    if not validate_sqlite_file(tmp):
        os.remove(tmp)
        return False, "That file is not a valid Munus database backup."

    os.replace(tmp, pending)
    return True, "Backup staged — it will be applied when the app restarts."


def apply_pending_restore() -> bool:
    """If a staged restore exists, swap it into place (keeping a safety copy).

    Called at startup before the engine connects. Returns True if a restore was applied.
    """
    pending = pending_restore_path()
    db_path = sqlite_path()
    if not pending or not db_path or not os.path.exists(pending):
        return False

    if os.path.exists(db_path):
        safety = f"{db_path}.pre-restore-{datetime.now():%Y%m%d-%H%M%S}"
        os.replace(db_path, safety)
        log.info("Restore: existing database preserved at %s", safety)
    os.replace(pending, db_path)
    log.info("Restore: applied staged backup to %s", db_path)
    return True


def list_backups() -> list[dict]:
    """Return existing snapshots, newest first."""
    d = settings.backup_dir
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if name.endswith(".db"):
            full = os.path.join(d, name)
            st = os.stat(full)
            out.append({
                "name": name,
                "size_kb": round(st.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(st.st_mtime),
            })
    return sorted(out, key=lambda b: b["modified"], reverse=True)


def nightly_backup() -> Optional[str]:
    """Create a timestamped snapshot in backup_dir and rotate old ones.

    Returns the snapshot path, or None if backups aren't applicable.
    """
    if not is_sqlite():
        return None
    name = f"munus-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    dest = os.path.join(settings.backup_dir, name)
    create_snapshot(dest)
    _rotate()
    log.info("Backup written to %s", dest)
    return dest


def _rotate() -> None:
    """Keep only the newest settings.backup_keep snapshots."""
    backups = list_backups()
    for old in backups[settings.backup_keep:]:
        try:
            os.remove(os.path.join(settings.backup_dir, old["name"]))
        except OSError:
            pass
