"""Persistencia de pedidos y eventos.

SQLite (stdlib) por defecto; Postgres (Supabase / Render) si DATABASE_URL
empieza por postgres y psycopg está instalado. Misma interfaz en ambos.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        app TEXT NOT NULL,
        order_ref TEXT NOT NULL,
        customer_ref TEXT NOT NULL,
        gelato_order_id TEXT,
        order_type TEXT NOT NULL,
        status TEXT NOT NULL,
        currency TEXT NOT NULL,
        items_json TEXT NOT NULL,
        shipping_json TEXT NOT NULL,
        shipment_json TEXT,
        tracking_json TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (app, order_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT,
        gelato_order_id TEXT,
        source TEXT NOT NULL,
        event TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_gelato ON orders (gelato_order_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_order ON events (order_id)",
]

ORDER_COLS = ["id", "app", "order_ref", "customer_ref", "gelato_order_id", "order_type", "status",
              "currency", "items_json", "shipping_json", "shipment_json", "tracking_json",
              "metadata_json", "created_at", "updated_at"]
JSON_COLS = {"items_json", "shipping_json", "shipment_json", "tracking_json", "metadata_json"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, database_url: str = "sqlite:///./print_service.db"):
        self.lock = threading.Lock()
        if database_url.startswith(("postgres://", "postgresql://")):
            import psycopg  # type: ignore  # opcional, solo en producción
            self.conn = psycopg.connect(database_url, autocommit=True)
            self.ph = "%s"
            self.pg = True
        else:
            path = database_url.replace("sqlite:///", "", 1) if database_url.startswith("sqlite") else database_url
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.ph = "?"
            self.pg = False
        self._init()

    def _init(self):
        for stmt in SCHEMA:
            if self.pg:
                stmt = stmt.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            self._exec(stmt)

    def _exec(self, sql: str, params: tuple = ()):
        sql = sql.replace("?", self.ph) if self.pg else sql
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            if not self.pg:
                self.conn.commit()
            return cur

    def _rows(self, cur) -> list[dict]:
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            for c in JSON_COLS:
                if c in d and d[c] is not None:
                    d[c[:-5]] = json.loads(d.pop(c))
                elif c in d:
                    d[c[:-5]] = None
                    d.pop(c)
            out.append(d)
        return out

    # --- pedidos -----------------------------------------------------------
    def insert_order(self, o: dict) -> dict:
        ts = now_iso()
        row = {
            "id": o["id"], "app": o["app"], "order_ref": o["order_ref"], "customer_ref": o["customer_ref"],
            "gelato_order_id": o.get("gelato_order_id"), "order_type": o["order_type"], "status": o["status"],
            "currency": o["currency"], "items_json": json.dumps(o["items"]),
            "shipping_json": json.dumps(o["shipping"]), "shipment_json": json.dumps(o.get("shipment")),
            "tracking_json": json.dumps(o.get("tracking") or []), "metadata_json": json.dumps(o.get("metadata") or {}),
            "created_at": ts, "updated_at": ts,
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._exec(f"INSERT INTO orders ({cols}) VALUES ({marks})", tuple(row.values()))
        return self.get_order(o["id"])

    def update_order(self, order_id: str, **fields: Any) -> dict:
        sets, vals = [], []
        for k, v in fields.items():
            col = f"{k}_json" if f"{k}_json" in JSON_COLS else k
            sets.append(f"{col} = ?")
            vals.append(json.dumps(v) if col in JSON_COLS else v)
        sets.append("updated_at = ?")
        vals.append(now_iso())
        vals.append(order_id)
        self._exec(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", tuple(vals))
        return self.get_order(order_id)

    def get_order(self, order_id: str) -> dict | None:
        rows = self._rows(self._exec("SELECT * FROM orders WHERE id = ?", (order_id,)))
        return rows[0] if rows else None

    def get_order_by_ref(self, app: str, order_ref: str) -> dict | None:
        rows = self._rows(self._exec("SELECT * FROM orders WHERE app = ? AND order_ref = ?", (app, order_ref)))
        return rows[0] if rows else None

    def get_orders_by_gelato_id(self, gelato_order_id: str) -> list[dict]:
        return self._rows(self._exec("SELECT * FROM orders WHERE gelato_order_id = ?", (gelato_order_id,)))

    def get_orders_by_order_ref(self, order_ref: str) -> list[dict]:
        return self._rows(self._exec("SELECT * FROM orders WHERE order_ref = ?", (order_ref,)))

    def list_orders(self, app: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM orders", []
        conds = []
        if app:
            conds.append("app = ?"); params.append(app)
        if status:
            conds.append("status = ?"); params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += f" ORDER BY created_at DESC LIMIT {int(limit)}"
        return self._rows(self._exec(sql, tuple(params)))

    # --- eventos -----------------------------------------------------------
    def add_event(self, source: str, event: str, payload: dict, order_id: str | None = None,
                  gelato_order_id: str | None = None) -> None:
        self._exec("INSERT INTO events (order_id, gelato_order_id, source, event, payload_json, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (order_id, gelato_order_id, source, event, json.dumps(payload), now_iso()))

    def list_events(self, order_id: str) -> list[dict]:
        return self._rows(self._exec("SELECT * FROM events WHERE order_id = ? ORDER BY id", (order_id,)))
