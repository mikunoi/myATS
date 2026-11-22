"""Long-running Paper service. No live broker or order endpoint is imported."""
import asyncio
import hashlib
import json
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path

from src.broker.kiwoom import KST
from src.market_data.websocket import KiwoomRealtimeClient, RealtimeCache, StaleMarketData
from src.runtime.paper import EvaluationClock
from src.reporting.notion_export import write_holdings_snapshot


class PaperRuntimeService:
    def __init__(self, settings, client, coordinator, store, telegram, symbols, logger,
                 service_name="Paper", user_symbols=(), symbol_names=None,
                 candidate_symbols=None, notion_sync=None, entry_symbols=None):
        self.settings, self.client, self.coordinator = settings, client, coordinator
        self.store, self.telegram, self.symbols, self.logger = store, telegram, symbols, logger
        self.service_name = service_name
        self.user_symbols = tuple(user_symbols)
        self.symbol_names = symbol_names or {}
        self.candidate_symbols = tuple(symbols if candidate_symbols is None else candidate_symbols)
        self.entry_symbols = frozenset(self.candidate_symbols if entry_symbols is None else entry_symbols)
        # The watchlist is the complete order scope, including sells.
        self.symbols = self.candidate_symbols
        symbols = self.symbols
        self.notion_sync = notion_sync
        self.clock = EvaluationClock()
        self.trade_observations = {}
        now = datetime.now(KST)
        if self.store.system_value("market_open_date", "") == now.date().isoformat():
            self.clock.market_status("3", now)
        self.daily, self.weekly, self.daily_candles = {}, {}, {}
        self.quotes, self.volumes, self.books = {}, {}, {}
        self.realtime = KiwoomRealtimeClient(
            client.realtime_token, symbols, environment=settings.kiwoom_environment,
            cache=RealtimeCache(settings.realtime_stale_after_seconds),
            login_timeout=settings.realtime_login_timeout_seconds,
            reconnect_base=settings.realtime_reconnect_base_seconds,
            reconnect_cap=settings.realtime_reconnect_cap_seconds,
            event_handler=self.on_event, state_handler=self.on_connection_state)

    def label(self, symbol):
        marker = "*" if symbol in self.user_symbols else ""
        return f"{self.symbol_names.get(symbol, symbol)}({symbol}){marker}"

    def notification_label(self):
        return "[국내]" if self.service_name == "Kiwoom 5814 Live" else self.service_name

    def on_connection_state(self, state):
        self.store.set_system_value("paper_ws_state", state)
        if state == "CONNECTED":
            self.logger.info("%s WebSocket connected and registered", self.service_name)
        elif state.startswith("ERROR:"):
            if hasattr(self.coordinator, "gate"):
                self.coordinator.gate.disconnected()
            self.logger.error("Paper WebSocket %s", state)

    async def refresh_indicators(self, symbol):
        base_date = datetime.now(KST).strftime("%Y%m%d")
        daily_candles = await asyncio.to_thread(
            self.client.candles, symbol, "daily", base_date, True,
            self.settings.signal_chart_market)
        weekly_candles = await asyncio.to_thread(
            self.client.candles, symbol, "weekly", base_date, True,
            self.settings.signal_chart_market)
        self.daily_candles[symbol] = daily_candles
        self.daily[symbol] = self.coordinator.strategy.indicators(daily_candles, "DAILY")
        self.weekly[symbol] = self.coordinator.strategy.indicators(weekly_candles, "WEEKLY")

    async def on_event(self, kind, symbol, value):
        now = datetime.now(KST)
        if kind == "market_status":
            self.clock.market_status(value.code, now)
            self.store.set_system_value("market_open_date",
                now.date().isoformat() if value.code == "3" else "")
            return
        if kind == "order_book":
            self.books[symbol] = value
            fills = (self.coordinator.on_order_book(value)
                     if hasattr(self.coordinator, "on_order_book") else ())
            for fill in fills:
                self.telegram.notify(f"paper-fill:{fill.fill_id}",
                    f"{self.notification_label()} {self.label(fill.symbol)} {fill.side.value} 부분체결 {fill.quantity}주 @{fill.price}")
            return
        if kind == "quote":
            previous = self.trade_observations.get(symbol)
            self.trade_observations[symbol] = (now, value.volume)
            if (previous and 0 < (now - previous[0]).total_seconds() <= 30
                    and value.volume > previous[1] and now.weekday() < 5
                    and time(9, 0) <= now.time() < time(15, 20)
                    and self.clock._open_date != now.date()):
                self.clock.market_status("3", now)
                self.store.set_system_value("market_open_date", now.date().isoformat())
                self.logger.info("Market open recovered from increasing live trade volume")
            self.quotes[symbol] = Decimal(value.current_price)
            self.volumes[symbol] = value.volume
            if not self.clock.regular_market_open(now):
                return
            self.evaluate_addons(now)

    def evaluate_addons(self, now):
        # All currently eligible quotes compete in watchlist order, not score order.
        if not self.clock.regular_market_open(now):
            return
        for symbol in self.candidate_symbols:
            state = self.coordinator.states.get(symbol)
            active = self.weekly.get(symbol) if state and state.mode == "WEEKLY" else self.daily.get(symbol)
            if not state or not active or active[-1].atr is None:
                continue
            try:
                quote = self.realtime.cache.get("quote", symbol).value
            except StaleMarketData:
                continue
            order_id = self.coordinator.evaluate_addon(
                symbol, Decimal(quote.current_price), active[-1].atr, now.date().isoformat())
            if order_id:
                self.telegram.notify(f"order:{order_id}",
                    f"{self.notification_label()} {self.label(symbol)} 추가매수 주문 #{order_id}")

    async def evaluate_close(self, now):
        for symbol in self.symbols:
            try:
                await self.refresh_indicators(symbol)
                # Some ETFs and thinly traded symbols may not emit a WebSocket
                # trade between a restart and the scheduled evaluation.  Use
                # the production REST quote so an otherwise valid signal is
                # not silently skipped.
                if symbol not in self.quotes:
                    quote = await asyncio.to_thread(self.client.quote, symbol)
                    self.quotes[symbol] = Decimal(quote.current_price)
                    self.volumes[symbol] = quote.volume
                    self.logger.info("REST quote fallback loaded for %s", symbol)
            except Exception as error:
                self.daily.pop(symbol, None)
                self.weekly.pop(symbol, None)
                self.logger.error("Indicator refresh failed for %s: %s", symbol, type(error).__name__)
        ready = tuple(symbol for symbol in self.candidate_symbols if symbol in self.quotes and self.daily.get(symbol) and self.weekly.get(symbol))
        for symbol in ready:
            order_id = self.coordinator.evaluate_close(
                symbol, self.daily[symbol], self.weekly[symbol], self.quotes[symbol],
                now.date().isoformat(), allow_new_entries=symbol in self.entry_symbols)
            if order_id:
                self.telegram.notify(f"order:{order_id}",
                                     f"{self.notification_label()} {self.label(symbol)} 전략 주문 #{order_id}")
        missing = [symbol for symbol in self.symbols
                   if symbol not in self.quotes or not self.daily.get(symbol) or not self.weekly.get(symbol)]
        if missing:
            raise RuntimeError("Scheduled evaluation data unavailable: " + ",".join(missing))

    async def supervise(self):
        while self.store.control() != "STOP":
            if hasattr(self.coordinator, "sync_orders"):
                try:
                    completed = self.coordinator.sync_orders()
                    for completion, action in completed:
                        self.telegram.notify(f"mock-fill:{completion.order_id}",
                            f"[모의] {self.label(completion.request.symbol)} {action} 체결 {completion.filled_quantity}주 #{completion.order_id}")
                    if completed:
                        write_holdings_snapshot(Path(__file__).resolve().parents[2],
                                                self.coordinator.broker,
                                                self.coordinator.states, self.store,
                                                self.symbol_names)
                except Exception as error:
                    self.coordinator.gate.disconnected()
                    self.logger.error("Mock account reconciliation failed: %s", type(error).__name__)
            now = datetime.now(KST)
            sync_date = self.store.system_value("watchlist_sync_date", "")
            sync_time = time(self.settings.watchlist_sync_hour, self.settings.watchlist_sync_minute)
            if (self.service_name == "Kiwoom Mock" and now.weekday() < 5
                    and now.time() >= sync_time and sync_date != now.date().isoformat()):
                self.store.set_system_value("watchlist_sync_requested", "1")
                self.store.set_system_value("watchlist_sync_date", now.date().isoformat())
                self.logger.info("14:00 Kiwoom watchlist synchronization requested")
                return
            if self.clock.due(now):
                self.logger.info("Scheduled strategy evaluation started")
                try:
                    await self.evaluate_close(now)
                    self.logger.info("Scheduled strategy evaluation completed")
                except Exception as error:
                    self.clock.retry_after_failure(now)
                    self.logger.error("Strategy evaluation failed: %s: %s", type(error).__name__, str(error))
            self.telegram.retry_pending()
            if self.notion_sync is not None:
                self.notion_sync.sync_if_pending()
            await asyncio.sleep(5)
        self.realtime.stop()

    async def run(self):
        previous_raw = self.store.system_value("active_watchlist", "")
        previous = json.loads(previous_raw) if previous_raw else []
        current = list(self.candidate_symbols)
        if current != previous:
            added = [symbol for symbol in current if symbol not in previous]
            removed = [symbol for symbol in previous if symbol not in current]
            self.store.set_system_value("active_watchlist", json.dumps(current))
            added_labels = [self.label(symbol) for symbol in added]
            removed_labels = [self.label(symbol) for symbol in removed]
            changes = []
            if added_labels:
                changes.append("+" + ", ".join(added_labels))
            if removed_labels:
                changes.append("-" + ", ".join(removed_labels))
            fingerprint = hashlib.sha256(json.dumps(current).encode()).hexdigest()[:12]
            self.telegram.notify(f"watchlist:{datetime.now(KST).date()}:{fingerprint}",
                                 f"{self.notification_label()} 관심종목 " + " / ".join(changes))
        lifecycle = self.store.system_value("lifecycle_start_pending", "")
        if lifecycle:
            message = "업데이트 완료" if lifecycle == "UPDATE_COMPLETE" else "시스템 재개"
            self.telegram.notify(
                f"lifecycle-start:{self.service_name}:{datetime.now(KST).isoformat(timespec='seconds')}",
                f"[국내] {message}")
            self.store.delete_system_value("lifecycle_start_pending")
            self.store.delete_system_value("lifecycle_stop_reason")
        elif "Live" not in self.service_name:
            self.telegram.notify(f"service-start:{self.service_name}:{datetime.now(KST).date()}",
                                 f"{self.service_name} 시작")
        realtime_task = asyncio.create_task(self.realtime.run())
        try:
            await self.supervise()
        finally:
            self.realtime.stop()
            realtime_task.cancel()
            await asyncio.gather(realtime_task, return_exceptions=True)
            if self.store.control() == "STOP":
                if self.store.system_value("lifecycle_stop_notified", "0") != "1":
                    reason = self.store.system_value("lifecycle_stop_reason", "SYSTEM")
                    message = "업데이트 중" if reason == "UPDATE" else "시스템 중지"
                    self.telegram.notify(
                        f"lifecycle-stop:{self.service_name}:{datetime.now(KST).isoformat(timespec='seconds')}",
                        f"[국내] {message}")
                    self.store.set_system_value("lifecycle_stop_notified", "1")
            else:
                self.logger.info("%s configuration reload requested", self.service_name)
