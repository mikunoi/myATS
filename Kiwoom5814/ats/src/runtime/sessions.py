"""Trading-session definitions. NXT remains dormant while broker mode is mock."""
from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class TradingSession:
    market: str
    close: time
    evaluate: time


KRX_REGULAR = TradingSession("KRX", time(15, 30), time(14, 30))
NXT_AFTER = TradingSession("NXT", time(20, 0), time(19, 0))


def enabled_sessions(broker_environment: str):
    if broker_environment == "mock":
        return (KRX_REGULAR,)
    if broker_environment == "production":
        return (KRX_REGULAR, NXT_AFTER)
    raise ValueError("Invalid broker environment")
