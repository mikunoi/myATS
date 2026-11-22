from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AccountOverview:
    cash: int
    orderable_cash: int
    d2_estimated_cash: int | None = None


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: int
    available_quantity: int
    purchase_amount: int
    evaluation_amount: int
    purchase_amount_krw: Decimal | None = None
    acquisition_exchange_rate: Decimal | None = None

    @property
    def average_price(self):
        return Decimal(self.purchase_amount) / self.quantity if self.quantity else Decimal(0)


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    symbol: str
    remaining_quantity: int
    status: str
