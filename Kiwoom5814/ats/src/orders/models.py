from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"


@dataclass(frozen=True)
class OrderRequest:
    idempotency_key: str
    symbol: str
    side: Side
    quantity: int
    market: str
    trade_type: str = "6"

    def __post_init__(self):
        if not self.idempotency_key or not self.symbol:
            raise ValueError("Order identity required")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("Quantity must be a positive integer")
        if not isinstance(self.side, Side):
            raise ValueError("Invalid side")
        domestic = self.market in {"KRX", "NXT", "SOR"} and self.trade_type == "6"
        overseas = self.market in {"NA", "ND", "NY"} and self.trade_type == "00"
        if not (domestic or overseas):
            raise ValueError("Unsupported market/order type")


@dataclass(frozen=True)
class BrokerSnapshot:
    cash: Decimal
    positions: tuple
    open_orders: tuple


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: Decimal


@dataclass
class PaperOrder:
    order_id: str
    request: OrderRequest
    limit_price: Decimal
    status: OrderStatus = OrderStatus.OPEN
    filled_quantity: int = 0
    filled_notional: Decimal = Decimal(0)

    @property
    def remaining_quantity(self):
        return self.request.quantity - self.filled_quantity

    @property
    def average_fill_price(self):
        return self.filled_notional / self.filled_quantity if self.filled_quantity else None


@dataclass(frozen=True)
class PaperPosition:
    symbol: str
    quantity: int
    average_price: Decimal
