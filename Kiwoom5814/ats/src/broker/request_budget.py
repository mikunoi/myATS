"""Cross-process REST pacing and 429 cooldown for this checkout; stores no secrets."""
import sqlite3
import time
from pathlib import Path


class RequestCooldown(RuntimeError):
    def __init__(self, seconds):
        self.retry_after = max(1.0, min(300.0, float(seconds)))
        super().__init__(f"API shared cooldown; retry after {self.retry_after:.0f}s")


class RequestBudget:
    def __init__(self, environment, path=None, clock=time.time, sleep=time.sleep,
                 max_queue_wait=30.0):
        self.environment = environment
        self.path = (Path(path) if path else
                     Path(__file__).resolve().parents[2] / "data/kiwoom_request_budget.sqlite3")
        self.clock, self.sleep = clock, sleep
        self.max_queue_wait = float(max_queue_wait)

    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS budget (environment TEXT PRIMARY KEY, "
            "next_at REAL NOT NULL, cooldown_until REAL NOT NULL)")
        return connection

    def _cooldown_wait(self):
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT cooldown_until FROM budget WHERE environment=?",
                (self.environment,)).fetchone()
        finally:
            connection.close()
        return max(0.0, (row[0] if row else 0.0) - self.clock())

    def acquire(self, interval):
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_at,cooldown_until FROM budget WHERE environment=?",
                (self.environment,)).fetchone() or (0.0, 0.0)
            now = self.clock()
            cooldown_wait = row[1] - now
            if cooldown_wait > 0:
                raise RequestCooldown(cooldown_wait)
            target = max(now, row[0])
            queue_wait = target - now
            if queue_wait > self.max_queue_wait:
                raise RequestCooldown(queue_wait)
            connection.execute(
                "INSERT OR REPLACE INTO budget(environment,next_at,cooldown_until) VALUES (?,?,?)",
                (self.environment, target + interval, 0.0))
            connection.commit()
        finally:
            connection.close()
        if queue_wait > 0:
            self.sleep(queue_wait)
        # A concurrent request may have received 429 while this reservation waited.
        cooldown_wait = self._cooldown_wait()
        if cooldown_wait > 0:
            raise RequestCooldown(cooldown_wait)

    def defer(self, seconds):
        delay = max(5.0, min(300.0, float(seconds)))
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_at,cooldown_until FROM budget WHERE environment=?",
                (self.environment,)).fetchone() or (0.0, 0.0)
            until = max(row[1], self.clock() + delay)
            connection.execute(
                "INSERT OR REPLACE INTO budget(environment,next_at,cooldown_until) VALUES (?,?,?)",
                (self.environment, max(row[0], until), until))
            connection.commit()
        finally:
            connection.close()

