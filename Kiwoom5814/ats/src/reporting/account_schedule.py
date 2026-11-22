"""Twice daily account-to-Notion refresh using fresh broker holdings."""
import json
from datetime import datetime
from decimal import Decimal
from src.broker.kiwoom import KST


def sync_due(store, sync, client, expected_account, market, now=None):
    now = now or datetime.now(KST)
    hours = [hour for hour in (8, 20) if now.hour >= hour]
    if not hours:
        return False
    key = f"notion_account_schedule:{market}:{now.date()}:{max(hours):02d}"
    if store.system_value(key, "") == "DONE":
        return False
    if client.account_number() != expected_account:
        raise RuntimeError("Account mismatch")
    holdings = client.holdings()
    active = {json.loads(raw)["symbol"] for _, raw in store.system_values("live_intent:")
              if json.loads(raw).get("status") in {"SENDING", "UNCERTAIN", "ACK", "PARTIAL", "CANCEL_SENT", "PARTIAL_CANCELED"}}
    states = {symbol for symbol, _, _ in store.load_strategy_states()}
    errors = []
    for holding in holdings:
        if holding.symbol in active or holding.quantity <= 0:
            continue
        page = sync._page(holding.symbol) if market == "domestic" else sync.page(holding.symbol)
        if not page or holding.symbol not in states:
            continue
        cost = holding.purchase_amount if market == "domestic" else holding.purchase_amount_krw
        if cost is None:
            errors.append(holding.symbol)
            continue
        props = {"보유수량": {"number": holding.quantity},
                 "평단": {"number": float(Decimal(cost) / holding.quantity)},
                 "상태": {"status": {"name": "매수"}}}
        sync.transport.request("PATCH", "/v1/pages/" + page, {"properties": props})
    results = sync.manual_holdings.sync(holdings, states | active,
        "KRX" if market == "domestic" else "US",
        client.symbol_name if market == "domestic" else lambda symbol: symbol, force=True)
    errors.extend(r["symbol"] for r in results if r.get("error"))
    if errors:
        raise RuntimeError("Account reporting needs retry")
    store.set_system_value(key, "DONE")
    store.set_system_value("notion_account_schedule:last_success:" + market, now.isoformat())
    return True
