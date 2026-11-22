"""Hourly USD/KRW quote for display conversion."""
from datetime import datetime, timezone
from decimal import Decimal


def exchange_rate(client, previous, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(previous['fx_at'])
        value = Decimal(str(previous['fx']))
        elapsed = (now - stamp).total_seconds()
        if value.is_finite() and value > 0 and 0 <= elapsed < 3600:
            return value, previous['fx_at']
    except (KeyError, ValueError, TypeError, ArithmeticError):
        pass
    value = Decimal(str(client.exchange_rate()))
    if not value.is_finite() or value <= 0:
        raise ValueError('Invalid USD/KRW quote')
    return value, now.isoformat()
