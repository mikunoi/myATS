"""Kiwoom mock-account broker with production market-data separation."""
from dataclasses import dataclass
from decimal import Decimal

from src.orders.models import BrokerSnapshot, OrderRequest, Side


def account_capital_from_snapshot(overview, holdings):
    """Use spendable cash: unsettled deposit cash can include invested funds."""
    return Decimal(overview.orderable_cash + sum(item.purchase_amount for item in holdings))


@dataclass(frozen=True)
class MockCompletion:
    order_id: str
    request: OrderRequest
    filled_quantity: int
    average_fill_price: Decimal | None
    realized_profit: dict | None = None


class KiwoomMockBroker:
    def __init__(self, mock_client, production_data_client, *, capital_limit_krw=None, managed_symbols=None):
        if capital_limit_krw is not None and (
                type(capital_limit_krw) is not int or capital_limit_krw <= 0):
            raise ValueError("Invalid mock capital limit")
        self.mock_client = mock_client
        self.production_data_client = production_data_client
        self.capital_limit_krw = capital_limit_krw
        self.managed_symbols = None if managed_symbols is None else frozenset(managed_symbols)
        self._submitted = {}
        self._pending = {}
        self._snapshot = None
        self._account_capital = None
        self._books = {}

    def account_capital(self):
        overview = self.mock_client.account_overview()
        holdings = self.mock_client.holdings()
        self._account_capital = account_capital_from_snapshot(overview, holdings)
        return self._account_capital

    def investment_capital(self):
        account_capital = self._account_capital
        if account_capital is None:
            account_capital = self.account_capital()
        if self.capital_limit_krw is None:
            return account_capital
        return min(account_capital, Decimal(self.capital_limit_krw))

    @property
    def committed_cash(self):
        snapshot = self.snapshot()
        acquisition = sum(Decimal(item.purchase_amount) for item in snapshot.positions)
        remote_commitment = max(acquisition, self.investment_capital() - snapshot.cash)
        pending_buys = sum(self.best_price(request.symbol, Side.BUY) * request.quantity
                           for request, _baseline in self._pending.values()
                           if request.side == Side.BUY)
        return remote_commitment + pending_buys

    def best_price(self, symbol, side):
        book = self._books.get(symbol)
        if book is None:
            book = self.production_data_client.order_book(symbol)
        levels = book.asks if side == Side.BUY else book.bids
        if not levels:
            raise RuntimeError("No executable production quote")
        return Decimal(levels[0].price)

    def snapshot(self):
        if self._snapshot is not None:
            return self._snapshot
        return self.sync_account()

    def sync_account(self):
        overview = self.mock_client.account_overview()
        holdings = self.mock_client.holdings()
        open_orders = self.mock_client.open_orders()
        self._account_capital = account_capital_from_snapshot(overview, holdings)
        self._snapshot = BrokerSnapshot(Decimal(overview.orderable_cash), holdings, open_orders)
        return self._snapshot

    def update_order_book(self, book):
        self._books[book.symbol] = book
        return ()

    def submit(self, order: OrderRequest):
        if self.managed_symbols is not None and order.symbol not in self.managed_symbols:
            raise RuntimeError("Order symbol is outside the current watchlist")
        previous = self._submitted.get(order.idempotency_key)
        if previous is not None:
            old_order, order_id = previous
            if old_order != order:
                raise RuntimeError("Idempotency key reused for a different mock order")
            return order_id
        holdings = {item.symbol: item for item in self.snapshot().positions}
        held = holdings.get(order.symbol)
        baseline = (held.quantity, held.purchase_amount) if held else (0, 0)
        order_id = self.mock_client.submit(order)
        self._submitted[order.idempotency_key] = (order, order_id)
        self._pending[order_id] = (order, baseline)
        return order_id

    def cancel(self, order_id: str):
        match = next((order for order, saved_id in self._submitted.values()
                      if saved_id == order_id), None)
        if match is None:
            raise KeyError("Unknown submitted mock order")
        return self.mock_client.cancel(order_id, match.symbol, match.quantity)

    def refresh_completed(self):
        snapshot = self.sync_account()
        open_ids = {item.order_id for item in snapshot.open_orders}
        holdings = {item.symbol: item for item in snapshot.positions}
        completed = []
        for order_id, (request, baseline) in list(self._pending.items()):
            if order_id in open_ids:
                continue
            held = holdings.get(request.symbol)
            quantity = held.quantity if held else 0
            purchase = held.purchase_amount if held else 0
            if request.side == Side.BUY:
                filled = max(0, quantity - baseline[0])
                cost = max(0, purchase - baseline[1])
                average = Decimal(cost) / filled if filled else None
            else:
                filled = max(0, baseline[0] - quantity)
                average = None
            realized = (self.mock_client.today_realized_profit(request.symbol)
                        if request.side == Side.SELL and filled else None)
            if realized is not None:
                average = Decimal(realized["sell_price"])
            completed.append(MockCompletion(order_id, request, filled, average, realized))
            del self._pending[order_id]
        return tuple(completed)

    def startup_reconcile(self, strategy_symbols):
        snapshot = self.sync_account()
        open_ids = {item.order_id for item in snapshot.open_orders}
        if not open_ids.issubset(set(self._pending)):
            raise RuntimeError("Mock account has an unknown open order")
        held_symbols = {item.symbol for item in snapshot.positions if item.quantity}
        if self.managed_symbols is not None:
            held_symbols &= self.managed_symbols
            strategy_symbols = set(strategy_symbols) & self.managed_symbols
        pending_buys = {request.symbol for request, _ in self._pending.values()
                        if request.side == Side.BUY}
        pending_sells = {request.symbol for request, _ in self._pending.values()
                         if request.side == Side.SELL}
        if (held_symbols - (set(strategy_symbols) | pending_buys)
                or set(strategy_symbols) - (held_symbols | pending_sells)):
            raise RuntimeError("Mock holdings do not match saved strategy state")
        return self.snapshot()

    def has_pending_symbol(self, symbol):
        return any(request.symbol == symbol for request, _baseline in self._pending.values())

    def pending_record(self, order_id):
        request, baseline = self._pending[order_id]
        return {"order_id": order_id, "idempotency_key": request.idempotency_key,
                "symbol": request.symbol, "side": request.side.value,
                "quantity": request.quantity, "market": request.market,
                "trade_type": request.trade_type, "baseline_quantity": baseline[0],
                "baseline_purchase_amount": baseline[1]}

    def restore_pending(self, record):
        request = OrderRequest(record["idempotency_key"], record["symbol"],
                               Side(record["side"]), int(record["quantity"]),
                               record["market"], record["trade_type"])
        order_id = str(record["order_id"])
        baseline = (int(record["baseline_quantity"]),
                    int(record["baseline_purchase_amount"]))
        self._pending[order_id] = (request, baseline)
        self._submitted[request.idempotency_key] = (request, order_id)
