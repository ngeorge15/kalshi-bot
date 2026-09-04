"""SQLite data access layer for the Kalshi trading bot.

Initializes the schema on first connect and provides simple query helpers
that return plain ``dict`` objects (row factory applied).

Usage:
    from src.db.database import Database

    db = Database()                   # uses default path data/kalshi_bot.db
    db.execute(
        "INSERT INTO trades (order_id, ...) VALUES (?, ...)",
        (order_id, ...),
    )
    rows = db.fetchall("SELECT * FROM trades WHERE status = ?", ("resting",))
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Database:
    """SQLite data access layer with schema versioning.

    On instantiation the database file is created (if absent) and the full
    schema is applied via ``executescript``.  The schema is idempotent
    (``CREATE TABLE IF NOT EXISTS``) so re-running it on an existing database
    is safe.

    Args:
        db_path: Path to the SQLite database file.  Defaults to
            ``data/kalshi_bot.db`` relative to the project root.
    """

    def __init__(self, db_path: str = "data/kalshi_bot.db") -> None:
        self.db_path = db_path
        # Ensure the parent directory exists
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._schema_path = Path(__file__).parent / "schema.sql"
        self._init_schema()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        """Open a new SQLite connection with foreign keys enabled.

        Returns:
            A configured ``sqlite3.Connection`` with ``row_factory`` set to
            ``sqlite3.Row`` so rows can be accessed by column name.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        """Apply the schema SQL to the database (idempotent).

        Reads ``schema.sql`` from the same directory as this module and
        executes it via ``executescript``.  Safe to run multiple times.
        """
        schema_sql = self._schema_path.read_text()
        conn = self._get_connection()
        try:
            conn.executescript(schema_sql)
            conn.commit()
        finally:
            conn.close()
        logger.debug("Database schema initialized at %s", self.db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_schema_version(self) -> int:
        """Return the current schema version recorded in the database.

        Returns:
            The maximum ``version`` value from ``schema_version``, or 0 if
            the table exists but is empty.
        """
        row = self.fetchone("SELECT MAX(version) AS version FROM schema_version")
        if row is None:
            return 0
        return row["version"] or 0

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a single SQL statement (INSERT, UPDATE, DELETE).

        Opens a connection, executes the statement, commits, and closes.

        Args:
            sql: The SQL statement to execute.
            params: Positional parameters bound to ``?`` placeholders.

        Returns:
            The ``sqlite3.Cursor`` after execution (``lastrowid``,
            ``rowcount`` etc. are accessible on the returned cursor).
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """Yield a single connection for a batch of statements, committed once.

        ``execute`` opens and closes a connection per statement, which is fine
        for the handful of writes a trading loop makes but pathological for bulk
        work (seeding, backfills, analytics writes).  This keeps one connection
        open so ``cursor.lastrowid`` stays available for linking rows, and rolls
        back the whole batch if any statement raises.

        Usage:
            with db.transaction() as conn:
                pred_id = conn.execute(sql, params).lastrowid
                conn.execute(other_sql, (pred_id, ...))

        Yields:
            An open ``sqlite3.Connection`` with foreign keys on and a
            ``sqlite3.Row`` row factory.

        Raises:
            Exception: Re-raises anything the caller raises, after rolling back.
        """
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute a SELECT and return all matching rows as dicts.

        Args:
            sql: The SQL query.
            params: Positional parameters bound to ``?`` placeholders.

        Returns:
            A list of row dicts.  Empty list if no rows match.
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        """Execute a SELECT and return the first matching row as a dict.

        Args:
            sql: The SQL query.
            params: Positional parameters bound to ``?`` placeholders.

        Returns:
            The first row as a dict, or ``None`` if no rows match.
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()
