"""Create a local, credential-free payload for the Codex Notion synchronizer."""
import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.broker.kiwoom import KST


def _number(value):
    return float(Decimal(value)) if value is not None else None


def write_holdings_snapshot(root: Path, broker, states, store, symbol_names):
    rows = []
    for position in broker.snapshot().positions:
        if position.quantity <= 0:
            continue
        state = states.get(position.symbol)
        if state is None:
            continue
        latest = state.latest_entry_price
        atr = state.latest_entry_atr
        rows.append({
            "symbol": position.symbol,
            "name": symbol_names.get(position.symbol, position.symbol),
            "average_price": _number(position.average_price),
            "quantity": position.quantity,
            "purchase_amount": _number(position.average_price * position.quantity),
            "units": state.addon_count + 1,
            "contracts": int(store.system_value(f"latest_contract:{position.symbol}",
                                                  str(state.initial_quantity))),
            "latest_entry_price": _number(latest),
            "atr": _number(atr),
            "addon_price": _number(latest + Decimal("1.0") * atr),
            "stop_price": _number(latest - Decimal("3") * atr),
            "market": "KRX",
            "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
        })
    target = root / "reports" / "input" / "notion_holdings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"generated_at": datetime.now(KST).isoformat(),
                                     "rows": rows}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    os.replace(temporary, target)
    store.set_system_value("notion_holdings_pending", "1")
    return target
