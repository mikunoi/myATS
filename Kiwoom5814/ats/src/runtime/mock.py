"""Strategy coordinator for Kiwoom mock-account orders."""
import json
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime

from src.orders.models import OrderRequest, Side
from src.broker.kiwoom import KST
from src.strategy.engine import StrategyState
from src.strategy.signals import reentry_mode


@dataclass(frozen=True)
class PendingAction:
    kind: str
    atr: Decimal
    mode: str


class MockTradingCoordinator:
    def __init__(self, settings, strategy, broker, gate, store, investment_capital):
        self.settings, self.strategy, self.broker = settings, strategy, broker
        self.gate, self.store = gate, store
        self.investment_capital = Decimal(investment_capital)
        self.maximum_commitment = self.investment_capital * Decimal(str(settings.capital_usage_limit))
        self.states, self.pending, self.addons_today = {}, {}, {}

    def restore(self):
        for symbol, mode, raw in self.store.load_strategy_states():
            payload = json.loads(raw)
            self.states[symbol] = StrategyState(
                symbol, mode, int(payload["initial_quantity"]),
                Decimal(payload["latest_entry_price"]) if payload["latest_entry_price"] else None,
                Decimal(payload["latest_entry_atr"]) if payload["latest_entry_atr"] else None,
                int(payload["addon_count"]), payload.get("sell_reason"))
        for key, raw in self.store.system_values("mock_pending:"):
            payload = json.loads(raw)
            self.broker.restore_pending(payload["broker"])
            action = payload["action"]
            self.pending[payload["broker"]["order_id"]] = PendingAction(
                action["kind"], Decimal(action["atr"]), action["mode"])
        self.broker.startup_reconcile(self.states)
        self._refresh_capital()
        self.gate.reconciled = True

    def _refresh_capital(self):
        self.investment_capital = self.broker.investment_capital()
        self.maximum_commitment = self.investment_capital * Decimal(str(self.settings.capital_usage_limit))

    def _position_quantity(self, symbol):
        return next((p.quantity for p in self.broker.snapshot().positions
                     if p.symbol == symbol), 0)

    def _has_open_order(self, symbol):
        return self.broker.has_pending_symbol(symbol) or any(
            item.symbol.removeprefix("A") == symbol for item in self.broker.snapshot().open_orders)

    def sync_orders(self):
        processed = []
        completions = self.broker.refresh_completed()
        self._refresh_capital()
        for completion in completions:
            action = self.pending.pop(completion.order_id, None)
            if action is None or completion.filled_quantity == 0:
                self.store.delete_system_value(f"mock_pending:{completion.order_id}")
                continue
            processed.append((completion, action.kind))
            symbol = completion.request.symbol
            if action.kind == "INITIAL":
                if completion.average_fill_price is None:
                    raise RuntimeError("Missing mock buy fill price")
                state = StrategyState(symbol, action.mode, completion.filled_quantity,
                                      completion.average_fill_price, action.atr, 0, None)
                self.states[symbol] = state; self.store.save_strategy_state(state)
                self.store.set_system_value(f"rule3_reentry:{symbol}", "0")
                self.store.set_system_value(f"latest_contract:{symbol}",
                                            str(completion.filled_quantity))
                self.store.set_system_value(f"first_entry_date:{symbol}",
                                            datetime.now(KST).date().isoformat())
            elif action.kind == "RULE3_REENTRY":
                if completion.average_fill_price is None:
                    raise RuntimeError("Missing mock re-entry fill price")
                state = StrategyState(symbol, "DAILY", completion.filled_quantity,
                                      completion.average_fill_price, action.atr, 0, None)
                self.states[symbol] = state; self.store.save_strategy_state(state)
                self.store.set_system_value(f"rule3_reentry:{symbol}", "0")
                self.store.set_system_value(f"latest_contract:{symbol}",
                                            str(completion.filled_quantity))
                self.store.set_system_value(f"first_entry_date:{symbol}",
                                            datetime.now(KST).date().isoformat())
            elif action.kind == "ADDON":
                old = self.states[symbol]
                state = StrategyState(symbol, old.mode, old.initial_quantity,
                                      completion.average_fill_price, action.atr,
                                      old.addon_count + 1, None)
                self.states[symbol] = state; self.store.save_strategy_state(state)
                self.store.set_system_value(f"latest_contract:{symbol}",
                                            str(completion.filled_quantity))
            elif action.kind.startswith("SELL:") and self._position_quantity(symbol) == 0:
                old = self.states.pop(symbol, None)
                realized = completion.realized_profit
                if realized is None:
                    raise RuntimeError("Missing Kiwoom realized profit for completed sell")
                reason_map = {
                    "SELL_RULE_1_BEARISH_CROSS": "일목역전 청산",
                    "SELL_RULE_2_LATEST_ENTRY_3ATR": "손절가 청산",
                    "SELL_WEEKLY_RULE_2_LATEST_ENTRY_2ATR": "주봉 손절가 청산",
                    "SELL_WEEKLY_KIJUN": "주봉 기준선 하회 청산",
                    "SELL_RULE_3_KIJUN_CURRENT_ATR": "기준선 하회청산",
                }
                exit_reason = next((label for code, label in reason_map.items()
                                    if code in action.kind), "전략 청산")
                rate_text = realized["profit_rate"].replace(",", "").replace("%", "")
                record = {
                    "trade_id": completion.order_id, "symbol": symbol,
                    "name": realized["name"] or symbol,
                    "first_buy_date": self.store.system_value(
                        f"first_entry_date:{symbol}", datetime.now(KST).date().isoformat()),
                    "sell_date": datetime.now(KST).date().isoformat(),
                    "buy_price": realized["buy_price"],
                    "sell_price": realized["sell_price"],
                    "profit_rate": float(rate_text) / 100,
                    "profit_amount": realized["profit_amount"],
                    "exit_reason": exit_reason,
                }
                self.store.record(f"closed-trade:{completion.order_id}", "closed_trade", record)
                self.store.set_system_value("notion_trades_pending", "1")
                self.store.delete_strategy_state(symbol)
                self.store.delete_system_value(f"latest_contract:{symbol}")
                self.store.delete_system_value(f"first_entry_date:{symbol}")
                if "SELL_RULE_3_KIJUN_CURRENT_ATR" in action.kind:
                    self.store.set_system_value(f"rule3_reentry:{symbol}", "1")
                    self.store.set_system_value(f"rule3_prev_price:{symbol}", "")
                    self.store.set_system_value(f"rule3_reentry_from:{symbol}", old.mode)
                elif old is not None:
                    next_mode = "WEEKLY" if old.mode == "DAILY" else "DAILY"
                    self.store.set_system_value(f"next_cycle:{symbol}", next_mode)
            self.store.record(f"mock-completion:{completion.order_id}", "mock_completion", {
                "order_id": completion.order_id, "filled_quantity": completion.filled_quantity,
                "action": action.kind})
            self.store.delete_system_value(f"mock_pending:{completion.order_id}")
        self.gate.reconciled = True
        return tuple(processed)

    def on_order_book(self, book):
        return self.broker.update_order_book(book)

    def new_entry_allowed(self, daily):
        """Live coordinators may impose an additional daily entry gate."""
        return True

    def post_weekly_entry_allowed(self, symbol, daily, current_price):
        return True

    def _submit(self, symbol, side, quantity, key, action):
        if quantity <= 0 or not self.gate.orders_allowed(is_buy=side == Side.BUY):
            return None
        if self._has_open_order(symbol):
            return None
        if side == Side.BUY:
            positions = tuple(item for item in self.broker.snapshot().positions if item.quantity)
            if not any(item.symbol == symbol for item in positions) and len(positions) >= self.settings.max_positions:
                return None
            price = self.broker.best_price(symbol, side)
            if self.broker.committed_cash + price * quantity > self.maximum_commitment:
                return None
        request = OrderRequest(key, symbol, side, quantity, "KRX", self.settings.order_trade_type)
        order_id = self.broker.submit(request)
        self.pending[order_id] = action
        self.store.set_system_value(f"mock_pending:{order_id}", json.dumps({
            "broker": self.broker.pending_record(order_id),
            "action": {"kind": action.kind, "atr": str(action.atr), "mode": action.mode}
        }))
        self.store.record(f"mock-order:{order_id}", "mock_order", {
            "order_id": order_id, "symbol": symbol, "side": side.value,
            "quantity": quantity, "action": action.kind})
        return order_id

    def evaluate_close(self, symbol, daily, weekly, current_price, evaluation_date, *,
                       allow_buys=True, allow_new_entries=True, allow_sells=True):
        if len(daily) < 2 or len(weekly) < 2:
            return None
        state, quantity = self.states.get(symbol), self._position_quantity(symbol)
        if state is not None and quantity:
            active = weekly if state.mode == "WEEKLY" else daily
            reasons = self.strategy.sell_signals(active[-2], active[-1], current_price, state)
            if reasons:
                if not allow_sells:
                    return None
                return self._submit(symbol, Side.SELL, quantity,
                    f"{evaluation_date}:{symbol}:SELL",
                    PendingAction("SELL:" + ",".join(reasons), active[-1].atr, state.mode))
            if not allow_buys:
                return None
            return self.evaluate_addon(symbol, current_price, active[-1].atr, evaluation_date, "CLOSE")
        if not allow_buys:
            return None
        if state is None and quantity:
            raise RuntimeError("Mock position has no strategy state")
        if not allow_new_entries:
            return None
        if not self.post_weekly_entry_allowed(symbol, daily, current_price):
            return None
        if self.store.system_value(f"rule3_reentry:{symbol}", "0") == "1":
            point = daily[-1]
            previous_price_text = self.store.system_value(f"rule3_prev_price:{symbol}", "")
            self.store.set_system_value(f"rule3_prev_price:{symbol}", str(current_price))
            exited_mode = self.store.system_value(f"rule3_reentry_from:{symbol}", "DAILY")
            weekly_exit_ready = (exited_mode == "WEEKLY" and previous_price_text
                and self.strategy.weekly_rule3_reentry(
                    daily[-2], point, Decimal(previous_price_text), current_price))
            daily_exit_ready = (exited_mode != "WEEKLY"
                and self.strategy.rule3_reentry(
                    daily[-2], point, Decimal(previous_price_text or current_price), current_price))
            if (point.atr is not None and (weekly_exit_ready or daily_exit_ready)
                    and self.new_entry_allowed(daily)):
                return self._submit(symbol, Side.BUY,
                    self.strategy.initial_quantity(self.investment_capital, point.atr),
                    f"{evaluation_date}:{symbol}:RULE3_REENTRY",
                    PendingAction("RULE3_REENTRY", point.atr, "DAILY"))
            return None
        desired = self.store.system_value(f"next_cycle:{symbol}", "")
        entry_signal = (self.strategy.cycle_reentry(daily[-2], daily[-1])
                        if desired in {"DAILY", "WEEKLY"}
                        else self.strategy.initial_entry(daily[-2], daily[-1]))
        if entry_signal and self.new_entry_allowed(daily):
            mode = (reentry_mode(desired,
                    self.strategy.entry_common(weekly[-1]))
                    if desired in {"DAILY", "WEEKLY"} else "DAILY")
            point = weekly[-1] if mode == "WEEKLY" else daily[-1]
            if point.atr is None:
                return None
            return self._submit(symbol, Side.BUY,
                self.strategy.initial_quantity_for_mode(self.investment_capital, point.atr, mode),
                f"{evaluation_date}:{symbol}:INITIAL", PendingAction("INITIAL", point.atr, mode))
        return None

    def evaluate_addon(self, symbol, current_price, current_atr, trading_date, window="INTRADAY"):
        state = self.states.get(symbol)
        count = self.addons_today.get((trading_date, symbol), 0)
        if window == "INTRADAY" and count >= 1:
            return None
        if state is None or not self.strategy.addon_entry(current_price, state, count):
            return None
        order_id = self._submit(symbol, Side.BUY, state.initial_quantity,
            f"{trading_date}:{symbol}:ADDON:{state.addon_count + 1}:{window}",
            PendingAction("ADDON", current_atr, state.mode))
        if order_id:
            self.addons_today[(trading_date, symbol)] = count + 1
        return order_id
