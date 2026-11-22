"""Shared 5814 capital based on Kiwoom's consolidated estimated cash."""
import time
from decimal import Decimal


class UnifiedCapitalProvider:
    def __init__(self, domestic_client, us_client, ttl_seconds=30):
        self.domestic, self.us, self.ttl = domestic_client, us_client, ttl_seconds
        self.cached, self.cached_at = None, 0.0

    def invalidate(self):
        self.cached, self.cached_at = None, 0.0

    def refresh(self, domestic=None, overseas=None):
        if self.cached and time.monotonic() - self.cached_at < self.ttl:
            return self.cached
        overview, domestic_holdings = domestic or (self.domestic.account_overview(), self.domestic.holdings())
        overseas_holdings = overseas or self.us.holdings()
        rate = self.us.exchange_rate()
        domestic_purchase = sum((Decimal(h.purchase_amount) for h in domestic_holdings), Decimal(0))
        overseas_purchase_usd = sum((Decimal(h.purchase_amount) for h in overseas_holdings), Decimal(0))
        d2 = Decimal(overview.d2_estimated_cash)
        overseas_d2_usd = Decimal(self.us.d2_estimated_cash_usd())
        integrated_estimated_cash = d2 + overseas_d2_usd * rate
        purchase_krw = domestic_purchase + overseas_purchase_usd * rate
        self.cached = {"d2_krw": d2,
                       "overseas_d2_usd": overseas_d2_usd,
                       "integrated_estimated_cash_krw": integrated_estimated_cash,
                       "domestic_purchase_krw": domestic_purchase,
                       "overseas_purchase_usd": overseas_purchase_usd, "usd_krw": rate,
                       "purchase_krw": purchase_krw,
                       "capital_krw": integrated_estimated_cash + purchase_krw}
        self.cached_at = time.monotonic()
        return self.cached

    def values(self, currency, **known):
        data = self.refresh(**known)
        if currency not in {"KRW", "USD"}:
            raise ValueError("Unsupported capital currency")
        divisor = Decimal(1) if currency == "KRW" else data["usd_krw"]
        return (data["capital_krw"] / divisor, data["purchase_krw"] / divisor,
                data["integrated_estimated_cash_krw"] / divisor)
