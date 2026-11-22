from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """Ranking data only; never an order or trading signal."""
    symbol: str
    priority: int
    user_added: bool = False
