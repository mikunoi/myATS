"""Connect strategy decisions to PaperBroker without any live order path."""
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from src.orders.models import OrderRequest, OrderStatus, Side
from src.strategy.engine import StrategyState


@dataclass(frozen=True)
class PendingAction:
    kind: str
    atr: Decimal
    mode: str


class EvaluationClock:
    def __init__(self, evaluation_time=time(14, 30)):
        self.evaluation_time = evaluation_time
        self._last_date = None
        self._open_date = None
        self._retry_after = None

    def market_status(self, code, now: datetime):
        if code == "3":
            self._open_date = now.date()
        elif code in {"4", "8", "9"}:
            self._open_date = None

    def due(self, now: datetime):
        if (now.weekday() >= 5 or self._open_date != now.date()
                or not time(9, 0) <= now.time() < time(15, 30)
                or now.time() < self.evaluation_time or self._last_date == now.date()):
            return False
        if self._retry_after is not None and now < self._retry_after:
            return False
        self._last_date = now.date()
        self._retry_after = None
        return True

    def retry_after_failure(self, now: datetime, seconds=60):
        """Allow today's scheduled evaluation to retry after connectivity recovers."""
        if self._last_date == now.date():
            self._last_date = None
        self._retry_after = now + timedelta(seconds=seconds)

    def regular_market_open(self, now: datetime):
        """True only after today's KRX open event and before its close event."""
        return (now.weekday() < 5 and self._open_date == now.date()
                and time(9, 0) <= now.time() < time(15, 30))


class PaperTradingCoordinator:
    def __init__(self, settings, strategy, broker, gate, investment_capital: Decimal):
        if investment_capital < 0:
            raise ValueError("Invalid investment capital")
        self.settings, self.strategy, self.broker, self.gate = settings, strategy, broker, gate
        self.investment_capital = investment_capital
        self.maximum_commitment = investment_capital * Decimal(str(settings.capital_usage_limit))
        self.states, self.pending, self.addons_today = {}, {}, {}

    def restore_strategy_states(self):
        if self.broker._store is None:
            raise RuntimeError("Paper store required for strategy recovery")
        restored = {}
        for symbol, mode, raw in self.broker._store.load_strategy_states():
            payload = json.loads(raw)
            restored[symbol] = StrategyState(
                symbol, mode, int(payload["initial_quantity"]),
                Decimal(payload["latest_entry_price"]) if payload["latest_entry_price"] else None,
                Decimal(payload["latest_entry_atr"]) if payload["latest_entry_atr"] else None,
                int(payload["addon_count"]), payload.get("sell_reason"))
        positioned = {p.symbol for p in self.broker.snapshot().positions if p.quantity}
        if set(restored) != positioned:
            raise RuntimeError("Strategy state does not match paper positions")
        self.states = restored
        return tuple(restored.values())

    def _has_open_order(self, symbol):
        return any(o.request.symbol == symbol for o in self.broker.snapshot().open_orders)

    def _position_quantity(self, symbol):
        return next((p.quantity for p in self.broker.snapshot().positions if p.symbol == symbol), 0)

    def _commitment(self):
        return self.broker.committed_cash

    def on_order_book(self, book):
        fills = self.broker.update_order_book(book)
        self._finalize_completed()
        return fills

    def _submit(self, symbol, side, quantity, key, action):
        if quantity <= 0 or not self.gate.orders_allowed(is_buy=side == Side.BUY):
            return None
        if self._has_open_order(symbol):
            return None
        if side == Side.BUY:
            best = self.broker.best_price(symbol, Side.BUY)
            if self._commitment() + best * quantity > self.maximum_commitment:
                return None
        order_id = self.broker.submit(OrderRequest(key, symbol, side, quantity, "KRX",
                                                   self.settings.order_trade_type))
        self.pending[order_id] = action
        self._finalize_completed()
        return order_id

    def evaluate_close(self, symbol, daily, weekly, current_price: Decimal, evaluation_date, *,
                       allow_buys=True, allow_new_entries=True):
        """Run once at 14:30 KST. Exits are evaluated before entries."""
        if len(daily) < 2 or len(weekly) < 2:
            return None
        state = self.states.get(symbol)
        quantity = self._position_quantity(symbol)
        if state is not None and quantity:
            active = weekly if state.mode == "WEEKLY" else daily
            reasons = self.strategy.sell_signals(active[-2], active[-1], current_price, state)
            if reasons:
                return self._submit(symbol, Side.SELL, quantity,
                                    f"{evaluation_date}:{symbol}:SELL", PendingAction(
                                        "SELL:" + ",".join(reasons), active[-1].atr, state.mode))
            if not allow_buys:
                return None
            return self.evaluate_addon(symbol, current_price, active[-1].atr, evaluation_date, "CLOSE")
        if not allow_buys:
            return None
        if not allow_new_entries:
            return None
        if self.strategy.initial_entry(daily[-2], daily[-1]):
            mode = "DAILY"
            point = daily[-1]
            if point.atr is None:
                return None
            quantity = self.strategy.initial_quantity(self.investment_capital, point.atr)
            return self._submit(symbol, Side.BUY, quantity,
                                f"{evaluation_date}:{symbol}:INITIAL", PendingAction(
                                    "INITIAL", point.atr, mode))
        return None

    def evaluate_addon(self, symbol, current_price: Decimal, current_atr: Decimal,
                       trading_date, window="INTRADAY"):
        state = self.states.get(symbol)
        count = self.addons_today.get((trading_date, symbol), 0)
        if window == "INTRADAY" and count >= 1:
            return None
        if state is None or not self.strategy.addon_entry(current_price, state, count):
            return None
        order_id = self._submit(symbol, Side.BUY, state.initial_quantity,
                                f"{trading_date}:{symbol}:ADDON:{state.addon_count + 1}:{window}",
                                PendingAction("ADDON", current_atr, state.mode))
        if order_id is not None:
            self.addons_today[(trading_date, symbol)] = count + 1
        return order_id

    def _finalize_completed(self):
        for order_id, action in list(self.pending.items()):
            order = self.broker.order(order_id)
            if order.status != OrderStatus.FILLED:
                continue
            symbol = order.request.symbol
            if action.kind == "INITIAL":
                self.states[symbol] = StrategyState(
                    symbol, action.mode, order.request.quantity, order.average_fill_price,
                    action.atr, 0, None)
                if self.broker._store is not None:
                    self.broker._store.save_strategy_state(self.states[symbol])
            elif action.kind == "ADDON":
                old = self.states[symbol]
                self.states[symbol] = StrategyState(
                    symbol, old.mode, old.initial_quantity, order.average_fill_price,
                    action.atr, old.addon_count + 1, None)
                if self.broker._store is not None:
                    self.broker._store.save_strategy_state(self.states[symbol])
            elif action.kind.startswith("SELL:"):
                del self.states[symbol]
                if self.broker._store is not None:
                    self.broker._store.delete_strategy_state(symbol)
            del self.pending[order_id]
