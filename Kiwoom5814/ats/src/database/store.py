import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Store:
    """Single-owner connection; each future worker must open its own connection."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")

    def migrate(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise RuntimeError("Database schema is newer than this application")
        if version == 0:
            schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            try:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + schema + "\nPRAGMA user_version=2;\nCOMMIT;")
            except Exception:
                self.connection.rollback()
                raise
        elif version == 1:
            migration = Path(__file__).with_name("migration_2.sql").read_text(encoding="utf-8")
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + migration + "\nPRAGMA user_version=2;\nCOMMIT;")
            except Exception:
                self.connection.rollback()
                raise

    def record(self, event_key: str, category: str, payload: dict) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO events(event_key,category,occurred_at,payload_json) VALUES (?,?,?,?) "
                "ON CONFLICT(event_key) DO NOTHING",
                (event_key, category, datetime.now(timezone.utc).isoformat(),
                 json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        return cursor.rowcount == 1

    def control(self) -> str:
        return self.connection.execute("SELECT value FROM system_state WHERE key='control'").fetchone()[0]

    def set_control(self, value: str):
        if value not in {"STOP", "PAUSE", "RUNNING"}:
            raise ValueError("Unknown control state")
        with self.connection:
            self.connection.execute("UPDATE system_state SET value=? WHERE key='control'", (value,))

    def system_value(self, key, default=None):
        row = self.connection.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_system_value(self, key, value, *, only_if_missing=False):
        with self.connection:
            if only_if_missing:
                self.connection.execute("INSERT OR IGNORE INTO system_state(key,value) VALUES (?,?)",
                                        (key, str(value)))
            else:
                self.connection.execute(
                    "INSERT INTO system_state(key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def system_values(self, prefix):
        return tuple(self.connection.execute(
            "SELECT key,value FROM system_state WHERE key LIKE ? ORDER BY key",
            (prefix + "%",)).fetchall())

    def delete_system_value(self, key):
        with self.connection:
            self.connection.execute("DELETE FROM system_state WHERE key=?", (key,))

    def events_by_category(self, category):
        return tuple(self.connection.execute(
            "SELECT event_key,payload_json FROM events WHERE category=? ORDER BY occurred_at",
            (category,)).fetchall())

    @staticmethod
    def _order_payload(order):
        return json.dumps({"limit_price": str(order.limit_price),
                           "filled_quantity": order.filled_quantity,
                           "remaining_quantity": order.remaining_quantity,
                           "average_fill_price": (str(order.average_fill_price)
                                                  if order.average_fill_price is not None else None),
                           "market": order.request.market,
                           "trade_type": order.request.trade_type},
                          ensure_ascii=False, allow_nan=False)

    def create_paper_order(self, order):
        with self.connection:
            self.connection.execute(
                "INSERT INTO orders(order_id,idempotency_key,symbol,side,quantity,status,payload_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (order.order_id, order.request.idempotency_key, order.request.symbol,
                 order.request.side.value, order.request.quantity, order.status.value,
                 self._order_payload(order)))

    def persist_paper_result(self, order, fills, position):
        """Write fills, final order state and position in one SQLite transaction."""
        with self.connection:
            for fill in fills:
                self.connection.execute(
                    "INSERT INTO fills(fill_id,order_id,quantity,price,occurred_at) VALUES (?,?,?,?,?)",
                    (fill.fill_id, fill.order_id, fill.quantity, str(fill.price),
                     datetime.now(timezone.utc).isoformat()))
            self.connection.execute("UPDATE orders SET status=?,payload_json=? WHERE order_id=?",
                                    (order.status.value, self._order_payload(order), order.order_id))
            if position is not None:
                self.connection.execute(
                    "INSERT INTO positions(symbol,quantity,cost_basis,state_json) VALUES (?,?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,"
                    "cost_basis=excluded.cost_basis,state_json=excluded.state_json",
                    (position.symbol, position.quantity, str(position.average_price),
                     json.dumps({"source": "PAPER"})))

    def load_paper_rows(self):
        orders = self.connection.execute(
            "SELECT order_id,idempotency_key,symbol,side,quantity,status,payload_json FROM orders "
            "ORDER BY rowid").fetchall()
        fills = self.connection.execute(
            "SELECT fill_id,order_id,quantity,price FROM fills ORDER BY rowid").fetchall()
        positions = self.connection.execute(
            "SELECT symbol,quantity,cost_basis FROM positions ORDER BY symbol").fetchall()
        return orders, fills, positions

    def save_strategy_state(self, state):
        payload = {"initial_quantity": state.initial_quantity,
                   "latest_entry_price": (str(state.latest_entry_price)
                                          if state.latest_entry_price is not None else None),
                   "latest_entry_atr": (str(state.latest_entry_atr)
                                        if state.latest_entry_atr is not None else None),
                   "addon_count": state.addon_count, "sell_reason": state.sell_reason}
        with self.connection:
            self.connection.execute(
                "INSERT INTO strategy_state(symbol,mode,state_json,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET mode=excluded.mode,state_json=excluded.state_json,"
                "updated_at=excluded.updated_at",
                (state.symbol, state.mode, json.dumps(payload, allow_nan=False),
                 datetime.now(timezone.utc).isoformat()))

    def delete_strategy_state(self, symbol):
        with self.connection:
            self.connection.execute("DELETE FROM strategy_state WHERE symbol=?", (symbol,))

    def load_strategy_states(self):
        return self.connection.execute(
            "SELECT symbol,mode,state_json FROM strategy_state ORDER BY symbol").fetchall()

    def enqueue_notification(self, event_key, message):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT INTO notification_outbox(event_key,message,status,updated_at) VALUES (?,?,'PENDING',?) "
                "ON CONFLICT(event_key) DO NOTHING", (event_key, message, now))

    def pending_notifications(self, limit=20):
        return self.connection.execute(
            "SELECT event_key,message FROM notification_outbox WHERE status='PENDING' "
            "ORDER BY updated_at LIMIT ?", (limit,)).fetchall()

    def mark_notification_sent(self, event_key):
        with self.connection:
            self.connection.execute(
                "UPDATE notification_outbox SET status='SENT',attempts=attempts+1,last_error=NULL,updated_at=? "
                "WHERE event_key=?", (datetime.now(timezone.utc).isoformat(), event_key))

    def mark_notification_failed(self, event_key, error="delivery_failed"):
        with self.connection:
            self.connection.execute(
                "UPDATE notification_outbox SET attempts=attempts+1,last_error=?,updated_at=? WHERE event_key=?",
                (error, datetime.now(timezone.utc).isoformat(), event_key))

    def close(self):
        self.connection.close()
