"""Durable, non-blocking sell/buy evaluation windows for live services."""
from datetime import datetime, timedelta


class CloseEvaluationPhases:
    def __init__(self, store, market, sell_time, buy_time=None, buy_retry_time=None):
        self.store, self.market, self.sell_time = store, market, sell_time
        self.buy_time = buy_time
        self.buy_retry_time = buy_retry_time

    def key(self, now, suffix):
        return f"close_phases:{self.market}:{now.date().isoformat()}:{suffix}"

    def due(self, now, market_open):
        if self.buy_time is not None:
            if not market_open:
                return None
            retry = self.store.system_value(self.key(now, "retry_after"), "")
            if retry and now < datetime.fromisoformat(retry):
                return None
            if self.buy_time < self.sell_time and now.time() >= self.sell_time and not self.store.system_value(self.key(now, "BUY:done"), ""):
                self.store.set_system_value(self.key(now, "BUY:done"), "MISSED_WINDOW")
                self.store.delete_system_value(self.key(now, "retry_after"))
            for phase, scheduled in sorted((("SELL", self.sell_time), ("BUY", self.buy_time)), key=lambda item: item[1]):
                if now.time() >= scheduled and not self.store.system_value(self.key(now, phase + ":done"), ""):
                    retry = self.store.system_value(self.key(now, "retry_after"), "")
                    if not retry or now >= datetime.fromisoformat(retry):
                        return phase
            if (self.buy_retry_time is not None and now.time() >= self.buy_retry_time
                    and not self.store.system_value(self.key(now, "BUY_RETRY:done"), "")):
                return "BUY_RETRY"
            # A completed pass may still have isolated failed symbols. Initial
            # passes take precedence so one failed sell cannot starve all buys.
            for phase, scheduled in (("SELL", self.sell_time), ("BUY", self.buy_time)):
                pending = self.store.system_values(f"evaluation_issue:{now.date()}:{phase}:")
                retry = self.store.system_value(self.key(now, phase + ":retry_after"), "")
                if now.time() >= scheduled and pending and (not retry or now >= datetime.fromisoformat(retry)):
                    return phase
            return None
        if not market_open or now.time() < self.sell_time:
            return None
        retry = self.store.system_value(self.key(now, "retry_after"), "")
        if retry and now < datetime.fromisoformat(retry):
            return None
        if not self.store.system_value(self.key(now, "SELL:done"), ""):
            return "SELL"
        if self.store.system_value(self.key(now, "BUY:done"), ""):
            return None
        started = self.store.system_value(self.key(now, "SELL:started"), "")
        if started and now >= datetime.fromisoformat(started) + timedelta(minutes=3):
            return "BUY"
        return None

    def begin(self, now, phase):
        key = self.key(now, phase + ":started")
        if not self.store.system_value(key, ""):
            self.store.set_system_value(key, now.isoformat())

    def complete(self, now, phase):
        self.store.set_system_value(self.key(now, phase + ":done"), now.isoformat())
        self.store.delete_system_value(self.key(now, "retry_after"))
        if self.buy_time is not None:
            self.store.set_system_value(self.key(now, phase + ":retry_after"),
                                        (now + timedelta(seconds=60)).isoformat())

    def retry(self, now):
        self.store.set_system_value(self.key(now, "retry_after"),
                                    (now + timedelta(seconds=60)).isoformat())

    def buys_paused(self, now):
        if self.buy_time is not None:
            return now.time() >= min(self.sell_time, self.buy_time) and not self.store.system_value(self.key(now, "BUY:done"), "")
        return (now.time() >= self.sell_time and
                not self.store.system_value(self.key(now, "BUY:done"), ""))

    def symbol_done(self, now, phase, symbol):
        return self.store.system_value(self.key(now, f"{phase}:{symbol}"), "") == "DONE"

    def complete_symbol(self, now, phase, symbol):
        self.store.set_system_value(self.key(now, f"{phase}:{symbol}"), "DONE")
