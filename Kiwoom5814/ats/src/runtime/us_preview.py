"""Reuse live signals with an orderless broker and isolated state overlay."""
from decimal import Decimal
from types import SimpleNamespace
from src.orders.models import Side
from src.runtime.live import LiveTradingCoordinator


def integrated_cash_available(coordinator, snapshot=None):
    """Kiwoom consolidated estimated cash in USD, for notices only."""
    snapshot = coordinator.broker.snapshot() if snapshot is None else snapshot
    cash = Decimal(snapshot.cash)
    pending = Decimal(0)
    for entry in coordinator.journal.values():
        if entry["status"] in coordinator.ACTIVE and entry["side"] == "BUY":
            reserve = Decimal(entry["reserve"])
            if not reserve.is_finite() or reserve < 0:
                raise RuntimeError("Invalid pending cash reservation")
            pending += reserve
    if not cash.is_finite():
        raise RuntimeError("Integrated estimated cash unavailable")
    return max(Decimal(0), cash - pending)


class ReadOverlay:
    def __init__(self, store):
        self.source, self.values = store, {}

    def system_value(self, key, default=""):
        return self.values.get(key, self.source.system_value(key, default))

    def set_system_value(self, key, value):
        self.values[key] = str(value)

    def record(self, *args, **kwargs):
        pass


class PreviewBroker:
    """Deliberately has no client, submit, cancel or reconciliation methods."""
    def __init__(self, snapshot, capital, committed, symbols):
        self._snapshot, self._capital, self._committed = snapshot, capital, committed
        self.managed_symbols = frozenset(symbols)

    def snapshot(self):
        return self._snapshot

    def investment_capital(self):
        return self._capital

    def committed_capital(self):
        return self._committed


class USPreview(LiveTradingCoordinator):
    def __init__(self, live, snapshot, symbols):
        broker = PreviewBroker(snapshot, live.broker.investment_capital(),
                               live.broker.committed_capital(), symbols)
        super().__init__(live.settings, live.strategy, broker,
                         SimpleNamespace(orders_allowed=lambda **kw: True),
                         ReadOverlay(live.store), market="US", trade_type=live.trade_type)
        self.states = dict(live.states)
        self.journal = {k: dict(v) for k, v in live.journal.items()}
        self.addons_today = dict(live.addons_today)
        pending = sum((Decimal(e["reserve"]) for e in self.journal.values()
                       if e["status"] in self.ACTIVE and e["side"] == "BUY"), Decimal(0))
        self.available = integrated_cash_available(live, snapshot)
        self.remaining = self.available
        self.capacity = max(Decimal(0), self.maximum_commitment - broker.committed_capital() - pending)
        self.rows = []
        self.price = Decimal(0)

    def _submit(self, symbol, side, quantity, key, action, **kwargs):
        if side != Side.BUY or quantity <= 0 or key in self.journal or self._has_open_order(symbol):
            return None
        held = next((h for h in self.broker.snapshot().positions if h.symbol == symbol), None)
        quantity = self._symbol_capital_quantity(symbol, held, self.price, quantity, key)
        if quantity <= 0:
            return None
        required = self.price * quantity
        status = ("자금사용 상한" if required > self.capacity else
                  "예수금 부족" if required > self.remaining else "자금 충족")
        shortage = max(Decimal(0), required - self.remaining)
        self.rows.append(dict(symbol=symbol, quantity=quantity, required=required,
                              shortage=shortage, status=status, kind=action.kind))
        if status == "자금 충족":
            self.remaining -= required
            self.capacity -= required
        return None

    def evaluate(self, symbol, daily, weekly, price, day):
        self.price = Decimal(price)
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("Invalid preview quote")
        self.evaluate_close(symbol, daily, weekly, self.price, day,
                            allow_buys=True, allow_sells=False, allow_new_entries=True)
