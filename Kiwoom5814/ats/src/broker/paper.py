"""Conservative visible-depth paper broker; never sends network orders."""
import json
import time
from decimal import Decimal
from uuid import uuid4

from src.market_data.models import OrderBook
from src.orders.models import (BrokerSnapshot, Fill, OrderRequest, OrderStatus,
                               PaperOrder, PaperPosition, Side)


class PaperBroker:
    def __init__(self, initial_cash: Decimal = Decimal("0"), *, stale_after=5.0,
                 clock=None, id_factory=None, store=None):
        if not initial_cash.is_finite() or initial_cash < 0:
            raise ValueError("Invalid initial cash")
        if stale_after <= 0:
            raise ValueError("Invalid stale interval")
        self.initial_cash = initial_cash
        self._cash, self._reserved_cash = initial_cash, Decimal(0)
        self._clock, self._stale_after = clock or time.monotonic, stale_after
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._books, self._depth, self._depth_signatures = {}, {}, {}
        self._orders, self._idempotency, self._fills = {}, {}, []
        self._positions, self._reserved_sells = {}, {}
        self._store = store

    @property
    def available_cash(self):
        return self._cash - self._reserved_cash

    @property
    def committed_cash(self):
        return self._reserved_cash + sum(Decimal(quantity) * average
                                         for quantity, average in self._positions.values())

    def best_price(self, symbol, side):
        book = self._fresh_book(symbol)
        levels = book.asks if side == Side.BUY else book.bids
        if not levels:
            raise RuntimeError("No executable best quote")
        return Decimal(levels[0].price)

    @property
    def fills(self):
        return tuple(self._fills)

    def order(self, order_id):
        return self._orders[order_id]

    def snapshot(self) -> BrokerSnapshot:
        positions = tuple(PaperPosition(symbol, quantity, average)
                          for symbol, (quantity, average) in sorted(self._positions.items()) if quantity)
        open_orders = tuple(order for order in self._orders.values()
                            if order.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED})
        return BrokerSnapshot(self.available_cash, positions, open_orders)

    @staticmethod
    def _signature(book):
        return (tuple((x.price, x.quantity) for x in book.asks),
                tuple((x.price, x.quantity) for x in book.bids))

    def update_order_book(self, book: OrderBook):
        if not isinstance(book, OrderBook):
            raise ValueError("OrderBook required")
        if any(level.price <= 0 or level.quantity < 0 for level in book.asks + book.bids):
            raise ValueError("Invalid visible depth")
        signature = self._signature(book)
        self._books[book.symbol] = (book, self._clock())
        if self._depth_signatures.get(book.symbol) == signature:
            return ()
        self._depth_signatures[book.symbol] = signature
        self._depth[book.symbol] = {
            Side.BUY: {Decimal(level.price): level.quantity for level in book.asks},
            Side.SELL: {Decimal(level.price): level.quantity for level in book.bids},
        }
        created = []
        for order in self._orders.values():
            if order.request.symbol == book.symbol and order.status in {
                    OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
                created.extend(self._match(order))
        return tuple(created)

    def _fresh_book(self, symbol):
        record = self._books.get(symbol)
        if record is None or self._clock() - record[1] > self._stale_after:
            raise RuntimeError("Fresh order book required")
        return record[0]

    def submit(self, order: OrderRequest) -> str:
        previous = self._idempotency.get(order.idempotency_key)
        if previous is not None:
            if self._orders[previous].request != order:
                raise RuntimeError("Idempotency key reused for a different paper order")
            return previous
        limit = self.best_price(order.symbol, order.side)
        if limit <= 0:
            raise RuntimeError("No executable best quote")
        if order.side == Side.BUY:
            reservation = limit * order.quantity
            if reservation > self.available_cash:
                raise RuntimeError("Insufficient paper cash for full order quantity")
            self._reserved_cash += reservation
        else:
            held = self._positions.get(order.symbol, (0, Decimal(0)))[0]
            reserved = self._reserved_sells.get(order.symbol, 0)
            if order.quantity > held - reserved:
                raise RuntimeError("Insufficient available paper position")
            self._reserved_sells[order.symbol] = reserved + order.quantity
        order_id = self._id_factory()
        paper_order = PaperOrder(order_id, order, limit)
        self._orders[order_id] = paper_order
        self._idempotency[order.idempotency_key] = order_id
        if self._store is not None:
            self._store.create_paper_order(paper_order)
        self._match(paper_order)
        return order_id

    def _match(self, order):
        available = self._depth.get(order.request.symbol, {}).get(order.request.side, {})
        prices = sorted(available, reverse=order.request.side == Side.SELL)
        fills = []
        for price in prices:
            executable = price <= order.limit_price if order.request.side == Side.BUY else price >= order.limit_price
            if not executable or not order.remaining_quantity:
                continue
            quantity = min(order.remaining_quantity, available[price])
            if quantity <= 0:
                continue
            available[price] -= quantity
            fill = Fill(self._id_factory(), order.order_id, order.request.symbol,
                        order.request.side, quantity, price)
            self._fills.append(fill)
            fills.append(fill)
            order.filled_quantity += quantity
            order.filled_notional += price * quantity
            self._apply_fill(order, quantity, price)
        order.status = (OrderStatus.FILLED if not order.remaining_quantity else
                        OrderStatus.PARTIALLY_FILLED if order.filled_quantity else OrderStatus.OPEN)
        if self._store is not None and fills:
            held, average = self._positions.get(order.request.symbol, (0, Decimal(0)))
            self._store.persist_paper_result(
                order, fills, PaperPosition(order.request.symbol, held, average))
        return fills

    def _apply_fill(self, order, quantity, price):
        symbol = order.request.symbol
        if order.request.side == Side.BUY:
            self._reserved_cash -= order.limit_price * quantity
            self._cash -= price * quantity
            held, average = self._positions.get(symbol, (0, Decimal(0)))
            total = held + quantity
            self._positions[symbol] = (total, (average * held + price * quantity) / total)
        else:
            self._reserved_sells[symbol] -= quantity
            held, average = self._positions[symbol]
            remaining = held - quantity
            self._positions[symbol] = (remaining, average if remaining else Decimal(0))
            self._cash += price * quantity

    def cancel(self, order_id: str) -> None:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError("Unknown paper order")
        if order.status in {OrderStatus.FILLED, OrderStatus.CANCELED}:
            return
        if order.request.side == Side.BUY:
            self._reserved_cash -= order.limit_price * order.remaining_quantity
        else:
            self._reserved_sells[order.request.symbol] -= order.remaining_quantity
        order.status = OrderStatus.CANCELED
        if self._store is not None:
            self._store.persist_paper_result(order, (), None)

    def restore_completed_state(self):
        """Restore settled paper state; unresolved orders fail closed."""
        if self._store is None:
            raise RuntimeError("Paper store required for recovery")
        orders, fills, positions = self._store.load_paper_rows()
        if any(row[5] in {OrderStatus.OPEN.value, OrderStatus.PARTIALLY_FILLED.value}
               for row in orders):
            raise RuntimeError("Unresolved paper orders require reconciliation")
        restored_orders = {}
        idempotency = {}
        for order_id, key, symbol, side, quantity, status, raw in orders:
            payload = json.loads(raw)
            request = OrderRequest(key, symbol, Side(side), quantity,
                                   payload["market"], payload["trade_type"])
            paper_order = PaperOrder(order_id, request, Decimal(payload["limit_price"]),
                                     OrderStatus(status), payload["filled_quantity"],
                                     Decimal(payload["average_fill_price"] or 0) * payload["filled_quantity"])
            restored_orders[order_id] = paper_order
            if key in idempotency:
                raise RuntimeError("Duplicate persisted idempotency key")
            idempotency[key] = order_id
        restored_fills = []
        cash = self.initial_cash
        fill_totals = {}
        calculated_positions = {}
        for fill_id, order_id, quantity, price_text in fills:
            order = restored_orders.get(order_id)
            if order is None:
                raise RuntimeError("Orphaned paper fill")
            price = Decimal(price_text)
            fill = Fill(fill_id, order_id, order.request.symbol, order.request.side, quantity, price)
            restored_fills.append(fill)
            fill_totals[order_id] = fill_totals.get(order_id, 0) + quantity
            cash += price * quantity * (-1 if order.request.side == Side.BUY else 1)
            held, average = calculated_positions.get(order.request.symbol, (0, Decimal(0)))
            if order.request.side == Side.BUY:
                total = held + quantity
                calculated_positions[order.request.symbol] = (
                    total, (held * average + quantity * price) / total)
            else:
                if quantity > held:
                    raise RuntimeError("Persisted sell exceeds paper position")
                remaining = held - quantity
                calculated_positions[order.request.symbol] = (
                    remaining, average if remaining else Decimal(0))
        for order in restored_orders.values():
            if fill_totals.get(order.order_id, 0) != order.filled_quantity:
                raise RuntimeError("Persisted fill quantity mismatch")
        restored_positions = {symbol: (quantity, Decimal(cost))
                              for symbol, quantity, cost in positions}
        if restored_positions != calculated_positions:
            raise RuntimeError("Persisted position does not match fills")
        if cash < 0 or any(quantity < 0 for quantity, _ in restored_positions.values()):
            raise RuntimeError("Invalid persisted paper balance")
        self._orders, self._idempotency = restored_orders, idempotency
        self._fills, self._positions, self._cash = restored_fills, restored_positions, cash
        self._reserved_cash, self._reserved_sells = Decimal(0), {}
        return self.snapshot()
