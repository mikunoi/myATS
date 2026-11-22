"""Live service scheduling and fresh-data evaluation."""
import asyncio
import hashlib
import json
from datetime import datetime, time
from decimal import Decimal

from src.broker.kiwoom import KST
from src.runtime.service import PaperRuntimeService
from src.runtime.close_phases import CloseEvaluationPhases
from src.telegram.error_messages import order_blocked_message


class WatchlistReloadRequired(RuntimeError):
    """New symbols need authenticated subscriptions before evaluation."""


class LiveRuntimeService(PaperRuntimeService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.evaluating_close = False
        self.close_phases = CloseEvaluationPhases(self.store, "domestic", time(14, 0),
                                                  buy_time=time(14, 10), buy_retry_time=time(14, 30))

    def on_connection_state(self, state):
        super().on_connection_state(state)
        if state != "CONNECTED":
            self.coordinator.gate.disconnected()

    def evaluate_addons(self, now):
        if not self.evaluating_close and not self.close_phases.buys_paused(now):
            super().evaluate_addons(now)

    @staticmethod
    def api_window_open(now):
        # Kiwoom domestic account/open-order ledgers can be withdrawn during
        # post-close processing. Finish reconciliation after the KRX close,
        # then remain idle until the next pre-open window.
        return now.weekday() < 5 and time(8, 0) <= now.time() < time(16, 0)

    def has_active_orders(self):
        return any(entry["status"] in self.coordinator.ACTIVE
                   for entry in self.coordinator.journal.values())

    def _buy_attempted_today(self, symbol, day):
        journal = getattr(self.coordinator, "journal", {})
        return isinstance(journal, dict) and any(
            entry.get("symbol") == symbol and entry.get("side") == "BUY"
            and entry.get("date") == day for entry in journal.values())

    async def refresh_buy_priority(self, now, phase):
        raw = await asyncio.to_thread(self.client.synchronized_watchlist, None,
                                      self.settings.watchlist_group_name)
        entries = tuple(dict.fromkeys(s for s in raw if s not in self.settings.excluded_symbols))
        if any(symbol not in self.symbols for symbol in entries):
            raise WatchlistReloadRequired("New watchlist symbols require subscription reload")
        # Preserve management of existing holdings while applying the broker's
        # latest entry scope and ordering. Never use an old list after read failure.
        self.entry_symbols = frozenset(entries)
        self.candidate_symbols = entries + tuple(s for s in self.candidate_symbols if s not in entries)
        self.store.set_system_value(f"buy_priority:{now.date()}:{phase}", json.dumps({
            "checked_at": datetime.now(KST).isoformat(), "symbols": entries}))

    def _notify_cash_shortfalls(self, now, phase, shortfalls):
        if not shortfalls:
            return
        if phase == "BUY_RETRY":
            for symbol, _ in shortfalls:
                self.telegram.notify(f"cash-shortfall:{now.date()}:1430:{symbol}",
                                     f"[국내] {self.symbol_names.get(symbol, symbol)}({symbol}) 매수 실패: 예수금 부족")
            return
        available = int(shortfalls[0][1]["available"])
        required = sum(int(item["required"]) for _, item in shortfalls)
        lines = [f"[국내] 계좌 예수금 부족: 현재 {available:,}원, 필요 약 {required:,}원",
                 "", "<진입대기 리스트>", ""]
        for index, (symbol, item) in enumerate(shortfalls, 1):
            name = self.symbol_names.get(symbol, symbol)
            lines.append(f"{index}) {name}({symbol}): 약 {item['quantity']:,}주, {int(item['required']):,}원")
        self.telegram.notify(f"cash-shortfall:{now.date()}:1410", "\n".join(lines))

    async def evaluate_close(self, now, phase="SELL"):
        if self.store.control() == "STOP":
            return
        self.evaluating_close = True
        try:
            if phase in {"BUY", "BUY_RETRY"}:
                await self.refresh_buy_priority(now, phase)
            self.close_phases.begin(now, phase)
            ready, failed = [], []
            day = now.date().isoformat()
            buy_phase = phase in {"BUY", "BUY_RETRY"}
            shortfalls = []
            cash_shortfalls = getattr(self.coordinator, "cash_shortfalls", None)
            if buy_phase and isinstance(cash_shortfalls, dict):
                cash_shortfalls.clear()
            for symbol in self.candidate_symbols:
                if phase == "BUY_RETRY" and self._buy_attempted_today(symbol, day):
                    self.close_phases.complete_symbol(now, phase, symbol)
                    continue
                if self.store.system_value(f"live_close:{day}:{symbol}", "") == "DONE":
                    continue
                if self.close_phases.symbol_done(now, phase, symbol):
                    self.store.delete_system_value(f"evaluation_issue:{day}:{phase}:{symbol}")
                    continue
                try:
                    await self.refresh_indicators(symbol)
                    if len(self.daily.get(symbol, ())) < 2 or len(self.weekly.get(symbol, ())) < 2:
                        raise RuntimeError("Insufficient indicators")
                    ready.append(symbol)
                except Exception:
                    failed.append(symbol)
            for symbol in failed:
                self.store.set_system_value(f"evaluation_issue:{day}:{phase}:{symbol}", "DATA_UNAVAILABLE")
            if buy_phase:
                # Refresh filled sells, holdings and broker-orderable cash after
                # the wait, not merely the cached snapshot from the sell phase.
                for completion, action in self.coordinator.sync_orders(force_capital_refresh=True):
                    self.telegram.notify(f"live-fill:{completion.order_id}",
                        f"[국내] {self.label(completion.request.symbol)} {action} 체결 "
                        f"{completion.filled_quantity}주 #{completion.order_id}")
            for symbol in ready:
                if self.store.control() == "STOP":
                    return
                if (self.coordinator._has_open_order(symbol)
                        or self.store.system_value("manual_reference_pending:" + symbol, "")):
                    self.store.set_system_value(f"evaluation_issue:{day}:{phase}:{symbol}", "SYMBOL_PENDING")
                    continue
                if buy_phase and self.store.system_value(f"evaluation_issue:{day}:SELL:{symbol}", ""):
                    self.store.set_system_value(f"evaluation_issue:{day}:{phase}:{symbol}", "SELL_EVALUATION_PENDING")
                    continue
                if not self.clock.regular_market_open(datetime.now(KST)):
                    raise RuntimeError("Regular market session ended during evaluation")
                # Avoid acting on a quote that aged during another symbol's API calls.
                try:
                    quote = await asyncio.to_thread(self.client.quote, symbol)
                except Exception:
                    self.store.set_system_value(f"evaluation_issue:{day}:{phase}:{symbol}", "QUOTE_UNAVAILABLE")
                    continue
                self.quotes[symbol] = Decimal(quote.current_price)
                if self.store.control() == "STOP":
                    return
                if not self.coordinator.gate.orders_allowed(is_buy=buy_phase):
                    raise RuntimeError("Account gate not ready for evaluation")
                if not self.clock.regular_market_open(datetime.now(KST)):
                    raise RuntimeError("Regular market session ended during quote refresh")
                try:
                    self.coordinator.evaluate_close(symbol, self.daily[symbol], self.weekly[symbol],
                                                     self.quotes[symbol], day,
                                                     allow_buys=buy_phase,
                                                     allow_sells=phase == "SELL",
                                                     allow_new_entries=symbol in self.entry_symbols)
                except Exception as error:
                    self.store.set_system_value(f"evaluation_issue:{day}:{phase}:{symbol}", type(error).__name__)
                    # Revalidate the account after any possible submission before
                    # allowing another symbol to use its cash snapshot.
                    self.coordinator.sync_orders(force_capital_refresh=True)
                    continue
                if buy_phase and isinstance(cash_shortfalls, dict):
                    shortage = cash_shortfalls.pop(symbol, None)
                    if shortage is not None:
                        shortfalls.append((symbol, shortage))
                self.close_phases.complete_symbol(now, phase, symbol)
                self.store.delete_system_value(f"evaluation_issue:{day}:{phase}:{symbol}")
            self.close_phases.complete(now, phase)
            if buy_phase:
                self._notify_cash_shortfalls(now, phase, shortfalls)
        finally:
            self.evaluating_close = False

    def sync_reporting(self):
        """Reporting failures must not disconnect the trading reconciliation gate."""
        try:
            if self.notion_sync is not None:
                self.notion_sync.manual_holdings.sync(
                    self.coordinator.broker.snapshot().positions,
                    set(self.coordinator.broker.managed_symbols) | set(self.coordinator.states),
                    "KRX", self.client.symbol_name)
            self.store.set_system_value("reporting_sync_error:domestic", "")
        except Exception as error:
            self.store.set_system_value("reporting_sync_error:domestic", type(error).__name__)
            self.logger.error("Reporting sync pending: %s", type(error).__name__)

    async def supervise(self):
        failure_delay = 5
        consecutive_failures = 0
        while self.store.control() != "STOP":
            cycle_delay = 5
            now = datetime.now(KST)
            self.store.set_system_value("service_last_heartbeat:domestic", now.isoformat())
            if not self.api_window_open(now) and not self.has_active_orders():
                self.store.set_system_value("domestic_api_state", "IDLE_OFF_SESSION")
                await asyncio.sleep(60)
                continue
            try:
                if self.store.control() == "STOP":
                    break
                self.coordinator.reconcile_uncertain_submissions()
                completed = list(self.coordinator.sync_orders())
                self.store.set_system_value("domestic_api_state", "CONNECTED")
                self.store.set_system_value("account_last_success:domestic", datetime.now(KST).isoformat())
                self.store.set_system_value("account_sync_error:domestic", "")
                now = datetime.now(KST)
                if self.clock.regular_market_open(now):
                    self.coordinator.retry_confirmed_unsubmitted(now.date().isoformat())
                pending_symbols = [symbol for symbol in self.candidate_symbols if
                    self.store.system_value("manual_reference_pending:" + symbol, "")]
                if pending_symbols:
                    references = {}
                    for symbol in pending_symbols:
                        await self.refresh_indicators(symbol)
                        state = self.coordinator.states.get(symbol)
                        mode = state.mode if state else "DAILY"
                        points = self.weekly[symbol] if mode == "WEEKLY" else self.daily[symbol]
                        if points and points[-1].atr is not None:
                            references[symbol] = (mode, points[-1].atr)
                    completed.extend(self.coordinator.sync_orders(references))
                for completion, action in completed:
                    self.telegram.notify(f"live-fill:{completion.order_id}",
                        f"[국내] {self.label(completion.request.symbol)} {action} 체결 "
                        f"{completion.filled_quantity}주 #{completion.order_id}")
                now = datetime.now(KST)
                sync_time = time(self.settings.watchlist_sync_hour, self.settings.watchlist_sync_minute)
                if (now.weekday() < 5 and now.time() >= sync_time
                        and self.store.system_value("watchlist_sync_date", "") != now.date().isoformat()):
                    self.store.set_system_value("watchlist_sync_date", now.date().isoformat())
                    return  # main rebuilds using the newly retrieved ordered watchlist.
                phase = self.close_phases.due(now, self.clock.regular_market_open(now))
                if phase:
                    try:
                        await self.evaluate_close(now, phase)
                    except WatchlistReloadRequired:
                        return  # Rebuild subscriptions; the unfinished phase remains due.
                    except Exception:
                        self.close_phases.retry(datetime.now(KST))
                        raise
                if now.time() >= time(15, 30):
                    self.coordinator.cancel_remaining()
                self.sync_reporting()
                self.telegram.retry_pending()
                recovery = self.store.system_value("auto_recovery_pending:domestic", "")
                if recovery:
                    self.telegram.notify("auto-recovery:domestic:" + recovery,
                                         "[국내] 자동 복구 완료")
                    self.store.delete_system_value("auto_recovery_pending:domestic")
                consecutive_failures = 0
                failure_delay = 5
            except Exception as error:
                consecutive_failures += 1
                self.store.set_system_value("domestic_api_state", "ERROR")
                self.store.set_system_value("account_sync_error:domestic",
                                            type(error).__name__ + ": " + str(error)[:200])
                self.coordinator.gate.disconnected()
                self.logger.error("Live reconciliation/evaluation blocked: %s", str(error))
                fingerprint = hashlib.sha256((type(error).__name__ + ":" + str(error)).encode()).hexdigest()[:12]
                if consecutive_failures >= 2:
                    self.telegram.notify("live-error:" + datetime.now(KST).strftime("%Y%m%d") + ":" + fingerprint,
                        order_blocked_message("[국내]", error))
                    self.store.set_system_value("auto_recovery_pending:domestic", fingerprint)
                self.client.disconnect()
                if hasattr(self.client, "unified_peer"):
                    self.client.unified_peer.disconnect()
                cycle_delay = failure_delay
                failure_delay = min(failure_delay * 2, 60)
            await asyncio.sleep(cycle_delay)
        self.realtime.stop()
