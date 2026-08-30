"""SQLite connection handling and schema management (plan section 39).

Design notes:

* WAL journalling so the writer thread never blocks UI reads.
* One connection per thread, created lazily - sqlite3 connections are not
  shareable across threads.
* Schema version is tracked in `user_version` so future migrations are additive
  and a newer database is never silently downgraded.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from app.config.defaults import DATABASE_FILENAME
from app.utils.logger import get_logger
from app.utils.platform import user_data_dir

log = get_logger("storage.database")

SCHEMA_VERSION = 1

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time  REAL NOT NULL,
        end_time    REAL,
        name        TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS targets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        host        TEXT NOT NULL,
        port        INTEGER,
        protocol    TEXT NOT NULL DEFAULT 'icmp',
        interval_ms INTEGER NOT NULL DEFAULT 500,
        enabled     INTEGER NOT NULL DEFAULT 1,
        category    TEXT NOT NULL DEFAULT 'custom'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS samples (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER,
        target_id   INTEGER,
        timestamp   REAL NOT NULL,
        latency_ms  REAL,
        success     INTEGER NOT NULL,
        error_type  TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        FOREIGN KEY (target_id)  REFERENCES targets(id)  ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id    INTEGER,
        timestamp     REAL NOT NULL,
        type          TEXT NOT NULL,
        severity      TEXT,
        target_id     INTEGER,
        message       TEXT NOT NULL DEFAULT '',
        metadata_json TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS system_samples (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id     INTEGER,
        timestamp      REAL NOT NULL,
        download_bps   REAL NOT NULL DEFAULT 0,
        upload_bps     REAL NOT NULL DEFAULT 0,
        cpu_percent    REAL NOT NULL DEFAULT 0,
        memory_percent REAL NOT NULL DEFAULT 0,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS traceroutes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        timestamp  REAL NOT NULL,
        target     TEXT NOT NULL,
        hops_json  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_samples_time    ON samples(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id, target_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_events_time     ON events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_events_session  ON events(session_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_system_time     ON system_samples(session_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_traceroute_time ON traceroutes(timestamp)",
]


class Database:
    """Thread-aware SQLite wrapper."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else user_data_dir() / DATABASE_FILENAME
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._initialised = False
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._shared_memory_conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------- connection ---
    def connect(self) -> sqlite3.Connection:
        """Connection for the calling thread, creating it on first use."""
        if str(self.path) == ":memory:":
            # An in-memory database must be one shared connection or each
            # thread would get its own empty database.
            with self._lock:
                if self._shared_memory_conn is None:
                    self._shared_memory_conn = self._new_connection()
                return self._shared_memory_conn

        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=15.0,
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit transactions where needed
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    # -------------------------------------------------------------- schema ---
    def initialise(self) -> None:
        """Create the schema if needed. Safe to call more than once."""
        if self._initialised:
            return
        conn = self.connect()
        with self._lock:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version > SCHEMA_VERSION:
                log.warning(
                    "Database schema version %s is newer than this build (%s)",
                    version, SCHEMA_VERSION,
                )
            else:
                self._migrate(conn, version)
        self._initialised = True
        log.info("Database ready at %s", self.path)

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply forward migrations. Only additive changes are supported."""
        if from_version >= SCHEMA_VERSION:
            return
        log.info("Migrating database schema %s -> %s", from_version, SCHEMA_VERSION)
        # Future migrations are appended here as `if from_version < N: ...`.
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # --------------------------------------------------------------- query ---
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        return self.connect().executemany(sql, [tuple(row) for row in seq])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, tuple(params)).fetchone()

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        cursor = self.connect().execute(sql, tuple(params))
        return int(cursor.lastrowid or 0)

    def transaction(self) -> "_Transaction":
        return _Transaction(self.connect())

    # ------------------------------------------------------------ lifecycle ---
    def vacuum(self) -> None:
        try:
            self.connect().execute("VACUUM")
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            log.warning("VACUUM failed: %s", exc)

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            return 0

    def close(self) -> None:
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - defensive
                    pass
            self._connections.clear()
            if self._shared_memory_conn is not None:
                try:
                    self._shared_memory_conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
                self._shared_memory_conn = None
        self._local = threading.local()
        self._initialised = False


class _Transaction:
    """Context manager wrapping BEGIN / COMMIT / ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
            log.error("Transaction rolled back: %s", exc)
        return False


_DATABASE: Database | None = None


def get_database(path: Path | str | None = None) -> Database:
    """Process-wide database singleton."""
    global _DATABASE
    if _DATABASE is None or (path is not None and Path(path) != _DATABASE.path):
        if _DATABASE is not None:
            _DATABASE.close()
        _DATABASE = Database(path)
        _DATABASE.initialise()
    return _DATABASE


def set_database(database: Database | None) -> None:
    global _DATABASE
    if _DATABASE is not None and _DATABASE is not database:
        _DATABASE.close()
    _DATABASE = database
