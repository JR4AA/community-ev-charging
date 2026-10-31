"""SQLite store for charging sessions. One process, so one connection behind a lock."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                      TEXT PRIMARY KEY,   -- random, also the driver's status-page token
    connector               TEXT NOT NULL,
    status                  TEXT NOT NULL,
    hold_amount             TEXT NOT NULL,      -- prices fixed when the driver saw them
    price_per_kwh           TEXT NOT NULL,      -- rate when the session started, for display
    tariff                  TEXT,               -- Tariff.to_json(), what the session is billed on
    breakdown               TEXT,               -- per-rate kWh and cost at settlement
    id_tag                  TEXT,               -- what the charger is started with (OCPP idTag)
    payment_method          TEXT,               -- 'paypal' (NULL in old rows) or 'manual' (admin, no payment)
    limit_wh                INTEGER,            -- manual sessions limited by energy rather than money
    note                    TEXT,               -- who/why, for manual sessions
    paypal_fee              TEXT,               -- what PayPal kept, from the capture
    paypal_order_id         TEXT UNIQUE,
    paypal_authorization_id TEXT,
    authorization_expires   TEXT,
    paypal_capture_id       TEXT,
    energy_wh               INTEGER,
    amount_charged          TEXT,
    error                   TEXT,
    created_at              TEXT NOT NULL,
    authorized_at           TEXT,
    started_at              TEXT,
    finished_at             TEXT,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_connector_status ON sessions (connector, status);
CREATE INDEX IF NOT EXISTS sessions_auth ON sessions (paypal_authorization_id);
CREATE TABLE IF NOT EXISTS session_events (
    id         INTEGER PRIMARY KEY,
    session_id TEXT,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT
);
-- OCPP transactions. AUTOINCREMENT so an id is never reused, even for
-- transactions the charger started that we refused.
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,                          -- NULL when refused
    charge_point TEXT NOT NULL,
    connector    INTEGER NOT NULL,
    id_tag       TEXT,
    meter_start  INTEGER NOT NULL,              -- Wh
    started_at   TEXT NOT NULL,
    meter_stop   INTEGER,
    stopped_at   TEXT,
    stop_reason  TEXT,
    ocpp_tx_id   TEXT                           -- OCPP 2.0.1: the charger's own transaction id
);
CREATE INDEX IF NOT EXISTS transactions_session ON transactions (session_id);
CREATE TABLE IF NOT EXISTS meter_readings (
    transaction_id INTEGER NOT NULL,
    at             TEXT NOT NULL,               -- charger's timestamp, UTC
    wh             INTEGER NOT NULL             -- energy register
);
CREATE INDEX IF NOT EXISTS meter_readings_tx ON meter_readings (transaction_id, at);
CREATE TABLE IF NOT EXISTS solar_periods (
    started_at TEXT NOT NULL,                   -- surplus (curtailed solar) on
    ended_at   TEXT                             -- off; NULL while still on
);
CREATE TABLE IF NOT EXISTS webhook_events (
    id          TEXT PRIMARY KEY,               -- PayPal event id; redeliveries are ignored
    event_type  TEXT NOT NULL,
    resource_id TEXT,
    received_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        added = {"tariff": "TEXT", "breakdown": "TEXT", "id_tag": "TEXT",   # added after the first deploy
                 "payment_method": "TEXT", "limit_wh": "INTEGER", "note": "TEXT", "paypal_fee": "TEXT"}
        for col, kind in added.items():
            if col not in cols:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {kind}")
        if "ocpp_tx_id" not in {r["name"] for r in self._conn.execute("PRAGMA table_info(transactions)")}:
            self._conn.execute("ALTER TABLE transactions ADD COLUMN ocpp_tx_id TEXT")
        # Indexes on added columns go after the migration, or an old database fails here.
        self._conn.execute("CREATE INDEX IF NOT EXISTS sessions_id_tag ON sessions (id_tag)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS transactions_ocpp ON transactions (charge_point, ocpp_tx_id)")
        self.lock = threading.RLock()

    def _one(self, sql: str, *args: Any) -> dict | None:
        with self.lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args: Any) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def insert_session(self, **fields: Any) -> None:
        fields.setdefault("created_at", now())
        fields.setdefault("updated_at", fields["created_at"])
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        with self.lock:
            self._conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({marks})", tuple(fields.values()))

    def update_session(self, sid: str, **fields: Any) -> None:
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self._conn.execute(f"UPDATE sessions SET {sets} WHERE id = ?", (*fields.values(), sid))

    def session(self, sid: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE id = ?", sid)

    def session_by_order(self, order_id: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE paypal_order_id = ?", order_id)

    def session_by_id_tag(self, id_tag: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE id_tag = ? ORDER BY created_at DESC LIMIT 1", id_tag)

    def session_by_authorization(self, auth_id: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE paypal_authorization_id = ?", auth_id)

    def sessions_with_status(self, *statuses: str, connector: str | None = None) -> list[dict]:
        sql = f"SELECT * FROM sessions WHERE status IN ({', '.join('?' * len(statuses))})"
        args: list[Any] = list(statuses)
        if connector:
            sql += " AND connector = ?"
            args.append(connector)
        return self._all(sql + " ORDER BY created_at", *args)

    def recent_sessions(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", limit)

    # Charges: sessions that got as far as a hold or a free authorisation, i.e. not
    # abandoned payment attempts. Month is by when the charge was authorised.
    _CHARGES = ("SELECT * FROM sessions WHERE status NOT IN ('CREATED', 'ABANDONED')"
                " AND authorized_at IS NOT NULL")

    def charges(self, month: str | None = None, limit: int | None = None) -> list[dict]:
        sql, args = self._CHARGES, []
        if month:
            sql += " AND substr(authorized_at, 1, 7) = ?"
            args.append(month)
        sql += " ORDER BY authorized_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._all(sql, *args)

    def charge_months(self) -> list[str]:
        return [r["m"] for r in self._all(
            f"SELECT DISTINCT substr(authorized_at, 1, 7) AS m FROM ({self._CHARGES}) ORDER BY m DESC")]

    def log(self, sid: str | None, kind: str, detail: str = "") -> None:
        with self.lock:
            self._conn.execute(
                "INSERT INTO session_events (session_id, at, kind, detail) VALUES (?, ?, ?, ?)",
                (sid, now(), kind, detail[:2000]),
            )

    def events(self, sid: str) -> list[dict]:
        return self._all("SELECT at, kind, detail FROM session_events WHERE session_id = ? ORDER BY id", sid)

    # -- OCPP transactions --------------------------------------------------

    def start_transaction(self, session_id: str | None, charge_point: str, connector: int,
                          id_tag: str, meter_start: int, at: str, ocpp_tx_id: str | None = None) -> int:
        with self.lock:
            cur = self._conn.execute(
                "INSERT INTO transactions (session_id, charge_point, connector, id_tag, meter_start, started_at,"
                " ocpp_tx_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, charge_point, connector, id_tag, meter_start, at, ocpp_tx_id),
            )
            tx = cur.lastrowid
            if session_id:
                self._conn.execute("INSERT INTO meter_readings VALUES (?, ?, ?)", (tx, at, meter_start))
        return tx

    def transaction(self, tx: int) -> dict | None:
        return self._one("SELECT * FROM transactions WHERE id = ?", tx)

    def transaction_by_ocpp(self, charge_point: str, ocpp_tx_id: str) -> dict | None:
        return self._one("SELECT * FROM transactions WHERE charge_point = ? AND ocpp_tx_id = ?", charge_point, ocpp_tx_id)

    def transaction_for_session(self, sid: str) -> dict | None:
        return self._one("SELECT * FROM transactions WHERE session_id = ? ORDER BY id DESC LIMIT 1", sid)

    def add_reading(self, tx: int, at: str, wh: int) -> None:
        with self.lock:
            self._conn.execute("INSERT INTO meter_readings VALUES (?, ?, ?)", (tx, at, wh))

    def readings(self, tx: int) -> list[tuple[datetime, int]]:
        rows = self._all("SELECT at, wh FROM meter_readings WHERE transaction_id = ? ORDER BY at, wh", tx)
        return [(datetime.fromisoformat(r["at"]), r["wh"]) for r in rows]

    def stop_transaction(self, tx: int, meter_stop: int, at: str, reason: str | None) -> None:
        with self.lock:
            self._conn.execute(
                "UPDATE transactions SET meter_stop = ?, stopped_at = ?, stop_reason = ? WHERE id = ? AND stopped_at IS NULL",
                (meter_stop, at, reason, tx),
            )
            self._conn.execute("INSERT INTO meter_readings VALUES (?, ?, ?)", (tx, at, meter_stop))

    def stopped_unsettled(self) -> list[dict]:
        """Transactions the charger has stopped whose payment hasn't gone through yet."""
        return self._all(
            "SELECT t.* FROM transactions t JOIN sessions s ON s.id = t.session_id"
            " WHERE t.stopped_at IS NOT NULL AND s.status IN ('AUTHORISED', 'CHARGING')"
        )

    def set_solar(self, on: bool, at: str | None = None) -> bool:
        """Turn the solar-surplus rate on or off. False if it was already so."""
        at = at or now()
        with self.lock:
            open_ = self._conn.execute("SELECT rowid FROM solar_periods WHERE ended_at IS NULL").fetchone()
            if on and not open_:
                self._conn.execute("INSERT INTO solar_periods (started_at) VALUES (?)", (at,))
                return True
            if not on and open_:
                self._conn.execute("UPDATE solar_periods SET ended_at = ? WHERE rowid = ?", (at, open_[0]))
                return True
        return False

    def solar_active(self) -> bool:
        return self._one("SELECT 1 AS x FROM solar_periods WHERE ended_at IS NULL") is not None

    def solar_periods(self, start: str, end: str) -> list[tuple[datetime, datetime | None]]:
        rows = self._all(
            "SELECT started_at, ended_at FROM solar_periods WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)",
            end, start,
        )
        return [(datetime.fromisoformat(r["started_at"]), r["ended_at"] and datetime.fromisoformat(r["ended_at"]))
                for r in rows]

    def record_webhook(self, event_id: str, event_type: str, resource_id: str | None) -> bool:
        """False if this event was seen before."""
        with self.lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO webhook_events (id, event_type, resource_id, received_at) VALUES (?, ?, ?, ?)",
                (event_id, event_type, resource_id, now()),
            )
        return cur.rowcount == 1
