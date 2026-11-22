from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Capital:
    acquisition_cost: Decimal
    cash: Decimal

    @property
    def investment_capital(self) -> Decimal:
        return self.acquisition_cost + self.cash
