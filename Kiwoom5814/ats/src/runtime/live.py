"""Durable live-account execution; strategy signals are inherited unchanged."""
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from src.broker.kiwoom import KST
from src.broker.mock import account_capital_from_snapshot
from src.orders.models import BrokerSnapshot, OrderRequest, Side
from src.runtime.mock import MockTradingCoordinator, PendingAction
from src.strategy.engine import StrategyState
from src.strategy.signals import daily_chikou_tenkan_entry, missed_daily_cross_exit, SELL_MISSED_DAILY_CROSS


def number(value):
    if not isinstance(value, str):
        raise RuntimeError("Missing numeric order detail")
    result = int(value.strip())
    if result < 0:
        raise RuntimeError("Negative order detail quantity")
    return result


def order_number(value):
    if not isinstance(value, str) or not value.isdigit():
        raise RuntimeError("Invalid order identifier")
    return value.lstrip("0") or "0"


def fill_price(value):
    if value in (None, ""):
        return None
    try:
        return abs(Decimal(str(value).replace(",", "").strip()))
    except Exception:
        raise RuntimeError("Invalid order fill price") from None


class LiveAccountBroker:
    def __init__(self, client, symbols, unified_capital=None, currency="KRW"):
        self.client, self.managed_symbols = client, frozenset(symbols)
        self.unified_capital, self.currency = unified_capital, currency
        self._snapshot = None
        self._capital = None
        self._integrated_estimated_cash = None
        self.domestic_orderable_cash = None

    def sync_account(self):
        overview, holdings = self.client.account_overview(), self.client.holdings()
        self.domestic_orderable_cash = Decimal(overview.orderable_cash)
        orders = self.client.open_orders()
        if self.unified_capital:
            known = {"domestic": (overview, holdings)} if self.currency == "KRW" else {"overseas": holdings}
            self._capital, self._committed, available = self.unified_capital.values(self.currency, **known)
            self._integrated_estimated_cash = self.unified_capital.cached["integrated_estimated_cash_krw"]
            self._snapshot = BrokerSnapshot(available, holdings, orders)
        else:
            self._snapshot = BrokerSnapshot(Decimal(overview.orderable_cash), holdings, orders)
            self._capital = account_capital_from_snapshot(overview, holdings)
            self._committed = sum((Decimal(h.purchase_amount) for h in holdings), Decimal(0))
        return self._snapshot

    def snapshot(self):
        return self._snapshot if self._snapshot is not None else self.sync_account()

    def investment_capital(self):
        if self._capital is None:
            self.sync_account()
        return self._capital

    def committed_capital(self):
        if self._capital is None:
            self.sync_account()
        return self._committed

    @property
    def integrated_estimated_cash(self):
        if self._integrated_estimated_cash is None:
            self.sync_account()
        if self._integrated_estimated_cash is None:
            raise RuntimeError("Kiwoom consolidated estimated cash is unavailable")
        return self._integrated_estimated_cash

    def best_price(self, symbol, side):
        book = self.client.order_book(symbol)
        levels = book.asks if side == Side.BUY else book.bids
        if not levels or levels[0].price <= 0:
            raise RuntimeError("No valid KRX executable quote")
        return Decimal(levels[0].price)

    def update_order_book(self, book):
        return ()  # Submission uses a newly fetched REST order book.


class LiveTradingCoordinator(MockTradingCoordinator):
    ACTIVE = {"SENDING", "UNCERTAIN", "ACK", "PARTIAL", "CANCEL_SENT", "PARTIAL_CANCELED"}

    def new_entry_allowed(self, daily):
        return daily_chikou_tenkan_entry(daily)

    def post_weekly_entry_allowed(self, symbol, daily, current_price):
        # Legacy flat DAILY-next states represent a completed weekly cycle.
        # An explicit flag overrides that fallback after subsequent fills.
        fallback = "1" if (self.store.system_value("next_cycle:" + symbol, "") == "DAILY"
                          and self.store.system_value("rule3_reentry:" + symbol, "0") != "1") else "0"
        pending = self.store.system_value("post_weekly_entry:" + symbol, fallback) == "1"
        return not pending or (daily[-1].tenkan is not None and current_price >= daily[-1].tenkan)

    def __init__(self, settings, strategy, broker, gate, store, market="KRX", trade_type="6"):
        super().__init__(settings, strategy, broker, gate, store, broker.investment_capital())
        self.journal = {}
        self.cash_shortfalls = {}
        self.market, self.trade_type = market, trade_type

    def _put(self, key, value):
        self.store.connection.execute(
            "INSERT INTO system_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def _save(self, entry):
        self._put("live_intent:" + entry["key"], json.dumps(entry, ensure_ascii=False))

    def _save_state(self, state):
        payload = asdict(state)
        for field in ("latest_entry_price", "latest_entry_atr"):
            payload[field] = str(payload[field]) if payload[field] is not None else None
        self.store.connection.execute(
            "INSERT INTO strategy_state(symbol,mode,state_json,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET mode=excluded.mode,state_json=excluded.state_json,updated_at=excluded.updated_at",
            (state.symbol, state.mode, json.dumps(payload), datetime.now(KST).isoformat()))

    def _record_event(self, event_key, category, payload):
        self.store.connection.execute(
            "INSERT INTO events(event_key,category,occurred_at,payload_json) VALUES (?,?,?,?) "
            "ON CONFLICT(event_key) DO NOTHING",
            (event_key, category, datetime.now(KST).isoformat(),
             json.dumps(payload, ensure_ascii=False, allow_nan=False)))

    def _manual_event_key(self, symbol):
        sequence = int(self.store.system_value("manual_reconcile_sequence", "0")) + 1
        self._put("manual_reconcile_sequence", sequence)
        return f"manual-live:{datetime.now(KST).date().isoformat()}:{sequence}:{symbol}"

    def _manual_notion_buy(self, event_key, symbol, holding, state):
        payload = {
            "order_id": event_key, "symbol": symbol, "side": "BUY",
            "kind": "MANUAL_BUY", "date": datetime.now(KST).date().isoformat(),
            "new_cycle": False, "average_price": str(holding.average_price),
            "quantity": holding.quantity, "latest_entry_price": str(state.latest_entry_price),
            "atr": str(state.latest_entry_atr), "units": state.addon_count + 1,
            "contracts": state.initial_quantity, "mode": state.mode}
        if getattr(holding, "purchase_amount_krw", None) is not None:
            payload.update(average_price_krw=str(holding.purchase_amount_krw / holding.quantity),
                           purchase_amount_krw=str(holding.purchase_amount_krw),
                           exchange_rate=str(holding.acquisition_exchange_rate))
        self._record_event(event_key, "notion_holding_update", payload)

    def _adopt_manual_change(self, symbol, old_quantity, old_purchase, holding, reference):
        actual_quantity = holding.quantity if holding else 0
        actual_purchase = holding.purchase_amount if holding else 0
        state = self.states.get(symbol)
        event_key = self._manual_event_key(symbol)
        if actual_quantity == 0 and state is None:
            # A completed exit may already have removed its strategy while an
            # earlier holdings snapshot left a residual quantity. Reconcile the
            # empty snapshot without inventing another sale or changing cycles.
            self._record_event(event_key + ":audit", "holding_reconciliation", {
                "symbol": symbol, "old_quantity": old_quantity,
                "quantity": 0, "reason": "EMPTY_HOLDING_WITHOUT_STRATEGY"})
            return None
        if actual_quantity == 0 and state is not None:
            self.store.connection.execute("DELETE FROM strategy_state WHERE symbol=?", (symbol,))
            self._put("next_cycle:" + symbol, "WEEKLY" if state.mode == "DAILY" else "DAILY")
            self._put("post_weekly_entry:" + symbol, "1" if state.mode == "WEEKLY" else "0")
            self._put("manual_exit_date:" + symbol, datetime.now(KST).date().isoformat())
            self._record_event(event_key, "notion_holding_update", {
                "order_id": event_key, "symbol": symbol, "side": "SELL",
                "kind": "MANUAL_SELL", "date": datetime.now(KST).date().isoformat(),
                "profit_pending": True, "quantity": old_quantity, "mode": state.mode})
            self._record_event(event_key + ":audit", "manual_trade", {
                "symbol": symbol, "side": "SELL", "quantity": old_quantity})
            return None
        if actual_quantity > old_quantity:
            delta_quantity = actual_quantity - old_quantity
            delta_purchase = actual_purchase - old_purchase
            if delta_purchase <= 0 or reference is None:
                self._put("manual_reference_pending:" + symbol, json.dumps({
                    "old_quantity": old_quantity, "old_purchase": str(old_purchase)}, ensure_ascii=False))
                return state
            mode, atr = reference
            price = Decimal(delta_purchase) / delta_quantity
            if state is None:
                state = StrategyState(symbol, mode, delta_quantity, price, atr, 0)
                self._put("first_entry_date:" + symbol, datetime.now(KST).date().isoformat())
                self._put("rule3_reentry:" + symbol, "0")
                self._put("post_weekly_entry:" + symbol, "0")
            else:
                state = StrategyState(symbol, state.mode, state.initial_quantity,
                                      price, atr, state.addon_count + 1)
            self._save_state(state)
            day = datetime.now(KST).date().isoformat()
            daily_count = self.addons_today.get((day, symbol), 0) + (1 if old_quantity else 0)
            self._put("live_addons:" + day + ":" + symbol, daily_count)
            self.addons_today[(day, symbol)] = daily_count
            self.store.connection.execute("DELETE FROM system_state WHERE key=?",
                                          ("manual_reference_pending:" + symbol,))
            self._manual_notion_buy(event_key, symbol, holding, state)
            self._record_event(event_key + ":audit", "manual_trade", {
                "symbol": symbol, "side": "BUY", "quantity": delta_quantity,
                "price": str(price), "atr": str(atr), "mode": state.mode})
            return state
        sync_payload = {
            "order_id": event_key, "symbol": symbol, "side": "SYNC",
            "kind": "MANUAL_SELL_PARTIAL" if actual_quantity < old_quantity else "MANUAL_SYNC",
            "date": datetime.now(KST).date().isoformat(),
            "average_price": str(holding.average_price), "quantity": actual_quantity}
        if getattr(holding, "purchase_amount_krw", None) is not None:
            sync_payload.update(average_price_krw=str(holding.purchase_amount_krw / holding.quantity),
                                purchase_amount_krw=str(holding.purchase_amount_krw),
                                exchange_rate=str(holding.acquisition_exchange_rate))
        self._record_event(event_key, "notion_holding_update", sync_payload)
        self._record_event(event_key + ":audit", "manual_trade", {
            "symbol": symbol, "side": "SELL" if actual_quantity < old_quantity else "SYNC",
            "quantity_change": actual_quantity - old_quantity})
        return state

    @staticmethod
    def _notion_payload(entry, holding, state, prior_state):
        base = {"order_id": entry["order_id"], "symbol": entry["symbol"],
                "side": entry["side"], "kind": entry["kind"], "date": entry["date"]}
        if entry["side"] == "BUY":
            if holding is None or state is None:
                raise RuntimeError("Completed buy is missing its confirmed holding")
            base.update({"new_cycle": entry["kind"] != "ADDON",
                         "average_price": str(holding.average_price),
                         "quantity": holding.quantity,
                         "latest_entry_price": str(state.latest_entry_price),
                         "atr": str(state.latest_entry_atr),
                         "units": state.addon_count + 1,
                         "contracts": state.initial_quantity,
                         "mode": state.mode})
            if getattr(holding, "purchase_amount_krw", None) is not None:
                base.update(average_price_krw=str(holding.purchase_amount_krw / holding.quantity),
                            purchase_amount_krw=str(holding.purchase_amount_krw),
                            exchange_rate=str(holding.acquisition_exchange_rate))
        else:
            base.update({"profit_pending": True,
                         "quantity": entry["quantity"],
                         "mode": prior_state.mode if prior_state else entry["mode"]})
        return base

    def enrich_notion_sells(self, *, force=False):
        """Attach Kiwoom-reported realized results without blocking trade recovery."""
        changed = 0
        events = [(key, json.loads(raw)) for key, raw in
                  self.store.events_by_category("notion_holding_update")]
        for event_key, payload in events:
            if payload.get("side") != "SELL" or not payload.get("profit_pending"):
                continue
            retry_key = "notion_profit_retry:" + event_key
            error_key = "notion_profit_error:" + event_key
            now = datetime.now(timezone.utc)
            retry = self.store.system_value(retry_key, "")
            if not force and retry and now < datetime.fromisoformat(retry):
                continue
            try:
                same_day = [p for _, p in events if p.get("side") == "SELL"
                            and p.get("symbol") == payload["symbol"] and p.get("date") == payload["date"]]
                if len(same_day) != 1:
                    raise RuntimeError("Multiple exits need order-level realized-profit attribution")
                dated = getattr(self.broker.client, "realized_profit_for_date", None)
                realized = (dated(payload["symbol"], payload["date"]) if dated else
                            self.broker.client.today_realized_profit(payload["symbol"]))
                if realized["quantity"] != payload["quantity"]:
                    raise RuntimeError("Kiwoom realized-profit quantity differs from the completed sell")
                rate = Decimal(str(realized["profit_rate"]).replace(",", "").replace("%", "").strip())
                if not rate.is_finite() or not Decimal(str(realized["profit_amount"])).is_finite():
                    raise ValueError("Nonfinite realized profit")
                payload.update(profit_pending=False, profit_amount=realized["profit_amount"],
                               profit_rate=str(rate / Decimal("100")),
                               buy_price=realized["buy_price"], sell_price=realized["sell_price"])
                with self.store.connection:
                    self.store.connection.execute("UPDATE events SET payload_json=? WHERE event_key=?",
                        (json.dumps(payload, ensure_ascii=False, allow_nan=False), event_key))
                self.store.delete_system_value(retry_key)
                self.store.delete_system_value(error_key)
                changed += 1
            except (RuntimeError, ValueError, TypeError, KeyError, ArithmeticError, OSError) as error:
                self.store.set_system_value(error_key, type(error).__name__ + ": " + str(error)[:180])
                self.store.set_system_value(retry_key, (now + timedelta(seconds=60)).isoformat())
        return changed

    def import_initial_holdings(self, seed):
        if self.store.system_value("live_seed_imported", ""):
            return False
        if seed.get("status") != "APPROVED_INITIAL_BASELINE":
            raise RuntimeError("Initial holding baseline is not approved")
        if self.store.load_strategy_states() or self.store.system_values("live_intent:"):
            raise RuntimeError("Refusing to overwrite an existing strategy or journal")
        actual = {h.symbol: h.quantity for h in self.broker.snapshot().positions if h.quantity}
        prepared = []
        for row in seed["holdings"]:
            symbol = row["symbol"]
            if symbol not in self.broker.managed_symbols:
                continue
            expected = row["initial_quantity"] * (1 + row["addon_count"])
            if actual.get(symbol, 0) != expected or expected != row["observed_quantity"]:
                raise RuntimeError("Approved holding quantity differs from account")
            state = StrategyState(symbol, row["mode"], row["initial_quantity"],
                Decimal(row["latest_entry_price"]), Decimal(row["latest_entry_atr"]), row["addon_count"])
            if state.latest_entry_atr <= 0 or state.latest_entry_price <= 0:
                raise RuntimeError("Invalid initial holding reference")
            prepared.append((state, row))
        if (set(actual) & self.broker.managed_symbols) != {s.symbol for s, _ in prepared}:
            raise RuntimeError("Managed holding is missing an approved baseline")
        with self.store.connection:
            for state, row in prepared:
                self._save_state(state)
                self._put("live_quantity:" + state.symbol, actual[state.symbol])
                position = next(h for h in self.broker.snapshot().positions if h.symbol == state.symbol)
                self._put("live_purchase:" + state.symbol, position.purchase_amount)
                self._put("live_addons:" + row["addons_today_date"] + ":" + state.symbol, row["addons_today"])
                self._put("live_latest_buy_date:" + state.symbol, "UNKNOWN")
                self._put("first_entry_date:" + state.symbol, "UNKNOWN")
            self._put("live_seed_imported", datetime.now(KST).isoformat())
        return True

    def restore(self):
        self.states = {}
        for symbol, mode, raw in self.store.load_strategy_states():
            p = json.loads(raw)
            self.states[symbol] = StrategyState(symbol, mode, int(p["initial_quantity"]),
                Decimal(p["latest_entry_price"]), Decimal(p["latest_entry_atr"]), int(p["addon_count"]))
        self.journal = {json.loads(raw)["key"]: json.loads(raw)
                        for _, raw in self.store.system_values("live_intent:")}
        self.addons_today = {}
        for key, count in self.store.system_values("live_addons:"):
            day, symbol = key.removeprefix("live_addons:").split(":", 1)
            self.addons_today[(day, symbol)] = int(count)
        self._resolve_unsubmitted_uncertain_orders()
        self.sync_orders()

    def reconcile_uncertain_submissions(self):
        self._resolve_unsubmitted_uncertain_orders()

    def _resolve_unsubmitted_uncertain_orders(self):
        """Resolve only submissions for which Kiwoom has no account-side evidence."""
        uncertain = [e for e in self.journal.values()
                     if e["status"] == "UNCERTAIN" and not e.get("order_id")]
        if not uncertain:
            return 0
        snapshot = self.broker.sync_account()
        held = {h.symbol: h for h in snapshot.positions}
        resolved = 0
        for entry in uncertain:
            symbol = entry["symbol"]
            position = held.get(symbol)
            quantity = position.quantity if position else 0
            purchase = position.purchase_amount if position else 0
            open_for_symbol = [o for o in snapshot.open_orders
                               if o.symbol.removeprefix("A") == symbol]
            rows = self.broker.client.order_details(entry["date"].replace("-", ""))
            matching_rows = [row for row in rows
                if str(row.get("stk_cd", "")).removeprefix("A").upper() == symbol
                and str(row.get("ord_qty", "")).replace(",", "").strip() == str(entry["quantity"])]
            if (quantity != entry["baseline_quantity"]
                    or Decimal(purchase) != Decimal(entry["baseline_purchase"])
                    or open_for_symbol or matching_rows):
                continue
            entry.update(status="NOT_SUBMITTED",
                         submission_resolution="NO_HOLDING_OPEN_ORDER_OR_ORDER_HISTORY",
                         resolved_at=datetime.now(timezone.utc).isoformat())
            with self.store.connection:
                self._save(entry)
            resolved += 1
        return resolved

    def halt(self, reason):
        self.gate.disconnected()
        with self.store.connection:
            self._put("control", "STOP")
            self._put("us_execution_armed" if self.market == "US" else "live_execution_armed", "0")
            self._put("live_block_reason", reason)
        raise RuntimeError(reason)

    def _has_open_order(self, symbol):
        return bool(self.store.system_value("symbol_quarantine:" + symbol, "")) or any(e["symbol"] == symbol and e["status"] in self.ACTIVE | {"REPLACE_READY", "REPLACEMENT_UNCERTAIN"} for e in self.journal.values()) or any(
            o.symbol.removeprefix("A") == symbol for o in self.broker.snapshot().open_orders)

    def evaluate_close(self, symbol, daily, weekly, current_price, evaluation_date, *,
                       allow_buys=True, allow_new_entries=True, allow_sells=True):
        # A fill can appear in holdings before its strategy state is committed.
        # Defer this symbol before the inherited evaluator inspects that state.
        if (self._has_open_order(symbol)
                or self.store.system_value("manual_reference_pending:" + symbol, "")):
            return None
        state = self.states.get(symbol)
        if (self.market != "US" and state is not None and state.mode == "DAILY"
                and len(daily) >= 2 and daily[-1].atr is not None
                and self._position_quantity(symbol) > 0
                and missed_daily_cross_exit(daily[-2], daily[-1], current_price)
                and not self.strategy.sell_signals(daily[-2], daily[-1], current_price, state)):
            if not allow_sells:
                return None
            return self._submit(symbol, Side.SELL, self._position_quantity(symbol),
                                f"{evaluation_date}:{symbol}:SELL",
                                PendingAction("SELL:" + SELL_MISSED_DAILY_CROSS, daily[-1].atr, "DAILY"))
        return super().evaluate_close(symbol, daily, weekly, current_price, evaluation_date,
                                      allow_buys=allow_buys, allow_new_entries=allow_new_entries,
                                      allow_sells=allow_sells)

    def _symbol_capital_quantity(self, symbol, held, price, quantity, key):
        if self.settings.max_symbol_capital_fraction is None:
            return quantity
        existing = Decimal(held.purchase_amount) if held else Decimal(0)
        pending = sum((Decimal(e["reserve"]) for e in self.journal.values()
                       if e["status"] in self.ACTIVE and e["side"] == "BUY"
                       and e["symbol"] == symbol), Decimal(0))
        limit = self.investment_capital * Decimal(str(self.settings.max_symbol_capital_fraction))
        available = max(Decimal(0), limit - existing - pending)
        allowed = min(quantity, int(available // price))
        if allowed < quantity:
            self.store.record("live-symbol-limit:" + key, "live_symbol_limit", {
                "symbol": symbol, "requested_quantity": quantity, "allowed_quantity": allowed,
                "current_purchase": str(existing), "pending": str(pending),
                "price": str(price), "limit": str(limit)})
        return allowed

    def retry_confirmed_unsubmitted(self, trade_date):
        """Retry after each confirmed non-submission while the signal session remains open."""
        submitted = 0
        for original in list(self.journal.values()):
            if original["status"] != "NOT_SUBMITTED" or original["date"] != trade_date:
                continue
            retry_after = original.get("retry_after_at")
            if retry_after and datetime.now(timezone.utc) < datetime.fromisoformat(retry_after):
                continue
            attempt = int(original.get("retry_attempt", 0)) + 1
            retry_key = original["key"] + f":RETRY{attempt}"
            if retry_key in self.journal:
                continue
            original.update(status="RETRYING", retry_attempt=attempt, retry_key=retry_key)
            with self.store.connection:
                self._save(original)
            try:
                order_id = self._submit(
                    original["symbol"], Side(original["side"]), int(original["quantity"]),
                    retry_key, PendingAction(original["kind"], Decimal(original["atr"]), original["mode"]),
                    count_addon=False)
            except Exception:
                original["status"] = "RETRY_DISPATCHED" if retry_key in self.journal else "NOT_SUBMITTED"
                if retry_key not in self.journal:
                    original["retry_after_at"] = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
                with self.store.connection:
                    self._save(original)
                raise
            if order_id is None:
                original.update(status="NOT_SUBMITTED",
                                retry_after_at=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat())
            else:
                original.update(status="RETRY_SUBMITTED", retry_order_id=order_id)
                submitted += 1
            with self.store.connection:
                self._save(original)
        return submitted

    def _submit(self, symbol, side, quantity, key, action, *, count_addon=True):
        if symbol not in self.broker.managed_symbols or quantity <= 0:
            return None
        if self.store.system_value("manual_reference_pending:" + symbol, ""):
            return None
        if key in self.journal or not self.gate.orders_allowed(is_buy=side == Side.BUY) or self._has_open_order(symbol):
            return None
        snapshot = self.broker.snapshot()
        held = next((h for h in snapshot.positions if h.symbol == symbol), None)
        if side == Side.SELL and (held is None or quantity > held.available_quantity):
            return None
        reserve = Decimal(0)
        if side == Side.BUY:
            price = self.broker.best_price(symbol, side)
            quantity = self._symbol_capital_quantity(symbol, held, price, quantity, key)
            if quantity <= 0:
                return None
            reserve = price * quantity
            pending = sum(Decimal(e["reserve"]) for e in self.journal.values()
                          if e["status"] in self.ACTIVE and e["side"] == "BUY")
            acquisition = self.broker.committed_capital()
            # Populated from account_overview in the broker's trading currency,
            # including actual USD cash for US (not just unified KRW converted).
            orderable = getattr(self.broker, "domestic_orderable_cash", None)
            if self.market == "US" and orderable is None:
                raise RuntimeError("USD orderable cash unavailable")
            cash = min(snapshot.cash, orderable) if orderable is not None else snapshot.cash
            available = max(Decimal(0), cash - pending)
            if reserve > available:
                self.cash_shortfalls[symbol] = {"quantity": quantity, "required": reserve,
                                                "available": available}
                self.store.record("live-skip:" + key, "live_skip", {
                    "symbol": symbol, "reason": "INSUFFICIENT_CASH", "quantity": quantity,
                    "required": str(reserve), "available": str(available)})
                return None
            if acquisition + reserve + pending > self.maximum_commitment:
                self.store.record("live-skip:" + key, "live_skip", {"symbol": symbol, "reason": "CAPITAL_USAGE_LIMIT"})
                return None
        trade_date = key.split(":", 1)[0] if self.market == "US" else datetime.now(KST).date().isoformat()
        entry = {"key": key, "date": trade_date, "symbol": symbol,
            "side": side.value, "quantity": quantity, "kind": action.kind, "atr": str(action.atr),
            "mode": action.mode, "status": "SENDING", "order_id": None, "filled": 0,
            "baseline_quantity": held.quantity if held else 0,
            "baseline_purchase": str(held.purchase_amount) if held else "0", "reserve": str(reserve),
            "submitted_at": datetime.now(timezone.utc).isoformat(), "order_type": "LIMIT"}
        with self.store.connection:
            self._save(entry)  # Commit BEFORE crossing the broker boundary.
            if action.kind == "ADDON" and count_addon:
                day = entry["date"]
                count = self.addons_today.get((day, symbol), 0) + 1
                self._put("live_addons:" + day + ":" + symbol, count)
        self.journal[key] = entry
        try:
            market = (self.broker.client.market_for(symbol)
                      if hasattr(self.broker.client, "market_for") else self.market)
            order_id = self.broker.client.submit(OrderRequest(
                key, symbol, side, quantity, market, self.trade_type))
        except Exception as error:
            entry["status"] = "UNCERTAIN"
            entry["submission_error"] = (type(error).__name__ + ": " + str(error))[:400]
            with self.store.connection:
                self._save(entry)
            self.gate.disconnected()
            raise RuntimeError("Order submission outcome uncertain; API reconciliation pending") from error
        entry.update(status="ACK", order_id=order_id)
        with self.store.connection:
            self._save(entry)
        return order_id

    def _apply_completed(self, e, held, confirmed_fill_price=None):
        symbol = e["symbol"]
        if e["side"] == "BUY":
            cost = Decimal(held.purchase_amount) - Decimal(str(e["baseline_purchase"]))
            entry_price = confirmed_fill_price or (Decimal(cost) / e["quantity"] if cost > 0 else None)
            if entry_price is None or entry_price <= 0:
                raise RuntimeError("Completed buy has no confirmed acquisition price")
            old = self.states.get(symbol)
            initial = (old.initial_quantity if e["kind"] == "ADDON"
                       else int(e.get("target_initial_quantity", e["quantity"])))
            count = old.addon_count + 1 if e["kind"] == "ADDON" else 0
            state = StrategyState(symbol, e["mode"], initial, entry_price, Decimal(e["atr"]), count)
            self._save_state(state)
            self._put("live_latest_buy_date:" + symbol, e["date"])
            if e["kind"] != "ADDON":
                self._put("first_entry_date:" + symbol, e["date"])
                self._put("rule3_reentry:" + symbol, "0")
                self._put("post_weekly_entry:" + symbol, "0")
            return state
        self.store.connection.execute("DELETE FROM strategy_state WHERE symbol=?", (symbol,))
        old = self.states.get(symbol)
        exited_mode = old.mode if old is not None else e["mode"]
        self._put("post_weekly_entry:" + symbol, "1" if exited_mode == "WEEKLY" else "0")
        if exited_mode == "WEEKLY":
            self._put("rule3_reentry:" + symbol, "0")
            self._put("next_cycle:" + symbol, "DAILY")
        elif "SELL_RULE_3_KIJUN_CURRENT_ATR" in e["kind"]:
            self._put("rule3_reentry:" + symbol, "1")
            self._put("rule3_prev_price:" + symbol, "")
        elif old is not None:
            self._put("next_cycle:" + symbol, "WEEKLY" if old.mode == "DAILY" else "DAILY")
        return None

    def cancel_remaining(self):
        for e in self.journal.values():
            if e["status"] not in {"ACK", "PARTIAL"}:
                continue
            if e["filled"] == e["quantity"]:
                continue  # Fully executed; only the holdings snapshot is pending.
            if e["symbol"] not in self.broker.managed_symbols:
                self.store.set_system_value("symbol_quarantine:" + e["symbol"], "UNMANAGED_ORDER")
                continue
            e["status"] = "CANCEL_SENT"
            with self.store.connection:
                self._save(e)
            try:
                cancel_id = self.broker.client.cancel(e["order_id"], e["symbol"], 0)
            except Exception:
                self.store.set_system_value("symbol_quarantine:" + e["symbol"], "CANCEL_UNCERTAIN")
                continue
            e["cancel_order_id"] = cancel_id
            with self.store.connection:
                self._save(e)

    def replace_stale_us_limits(self, now=None, timeout_seconds=180):
        if self.market != "US":
            return 0
        now = now or datetime.now(timezone.utc)
        changed = 0
        for e in self.journal.values():
            if e["status"] not in {"ACK", "PARTIAL"} or e.get("order_type", "LIMIT") != "LIMIT":
                continue
            if e["filled"] == e["quantity"]:
                continue
            try:
                submitted = datetime.fromisoformat(e["submitted_at"])
            except (KeyError, TypeError, ValueError):
                self.store.set_system_value("symbol_quarantine:" + e["symbol"], "INVALID_ORDER_TIME")
                continue
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            if (now - submitted).total_seconds() < timeout_seconds:
                continue
            e["replace_with_market"] = True
            e["status"] = "CANCEL_SENT"
            with self.store.connection:
                self._save(e)
            try:
                e["cancel_order_id"] = self.broker.client.cancel(e["order_id"], e["symbol"], 0)
            except Exception:
                self.store.set_system_value("symbol_quarantine:" + e["symbol"], "CANCEL_UNCERTAIN")
                continue
            with self.store.connection:
                self._save(e)
            changed += 1
        return changed

    def submit_ready_us_market_replacements(self, trade_date=None):
        if self.market != "US":
            return 0
        submitted = 0
        for original in list(self.journal.values()):
            if original.get("status") != "REPLACE_READY" or original.get("replacement_order_id"):
                continue
            if trade_date is not None and original["date"] != trade_date:
                self.store.set_system_value("symbol_quarantine:" + original["symbol"], "EXPIRED_REPLACEMENT")
                continue
            remaining = original["quantity"] - original["filled"]
            if remaining <= 0:
                original["status"] = "FILLED"
                continue
            snapshot = self.broker.snapshot()
            held = next((h for h in snapshot.positions if h.symbol == original["symbol"]), None)
            key = original["key"] + ":MARKET"
            if original["side"] == "BUY":
                price = self.broker.best_price(original["symbol"], Side.BUY)
                if self._symbol_capital_quantity(original["symbol"], held, price, remaining, key) < remaining:
                    continue
            child = dict(original, key=key, quantity=remaining, status="SENDING", order_id=None,
                         filled=0, baseline_quantity=held.quantity if held else 0,
                         baseline_purchase=held.purchase_amount if held else 0,
                         reserve="0", submitted_at=datetime.now(timezone.utc).isoformat(),
                         order_type="MARKET", replace_with_market=False,
                         cancel_order_id=None, target_initial_quantity=original["quantity"])
            with self.store.connection:
                self._save(child)
            self.journal[key] = child
            try:
                order_id = self.broker.client.submit_market(
                    child["symbol"], Side(child["side"]), remaining)
            except Exception:
                child["status"] = "UNCERTAIN"
                with self.store.connection:
                    self._save(child)
                original["status"] = "REPLACEMENT_UNCERTAIN"
                with self.store.connection:
                    self._save(original)
                self.store.set_system_value("symbol_quarantine:" + original["symbol"], "ORDER_UNCERTAIN")
                continue
            child.update(status="ACK", order_id=order_id)
            original["replacement_order_id"] = order_id
            original["status"] = "REPLACED"
            with self.store.connection:
                self._save(child)
                self._save(original)
            submitted += 1
        return submitted

    def sync_orders(self, manual_references=None, *, force_capital_refresh=False):
        manual_references = manual_references or {}
        self.gate.disconnected()
        if force_capital_refresh and self.broker.unified_capital is not None:
            self.broker.unified_capital.invalidate()
        snapshot = self.broker.sync_account()
        self.investment_capital = self.broker.investment_capital()
        self.maximum_commitment = self.investment_capital * Decimal(str(self.settings.capital_usage_limit))
        held = {h.symbol: h for h in snapshot.positions}
        active = [e for e in self.journal.values() if e["status"] in self.ACTIVE]
        if any(not e.get("symbol") for e in active):
            self.halt("Unresolved order identity; account review required")
        for symbol in self.broker.managed_symbols:
            self.store.delete_system_value("symbol_quarantine:" + symbol)
        owned = {order_number(e["order_id"]) for e in active if e["order_id"]}
        manual_open = {}
        for remote in snapshot.open_orders:
            symbol = remote.symbol.removeprefix("A")
            if symbol in self.broker.managed_symbols and order_number(remote.order_id) not in owned:
                manual_open.setdefault(symbol, []).append(remote.order_id)
        with self.store.connection:
            prior_open = {key.removeprefix("manual_open_orders:")
                          for key, _ in self.store.system_values("manual_open_orders:")}
            for symbol, order_ids in manual_open.items():
                self._put("manual_open_orders:" + symbol, json.dumps(order_ids))
            for symbol in prior_open - set(manual_open):
                self.store.connection.execute("DELETE FROM system_state WHERE key=?",
                                              ("manual_open_orders:" + symbol,))
        histories = {day: self.broker.client.order_details(day.replace("-", "")) for day in {e["date"] for e in active}}
        expected = {key.removeprefix("live_quantity:"): int(value)
                    for key, value in self.store.system_values("live_quantity:")}
        updates = []
        for e in active:
            if e["status"] in {"UNCERTAIN", "SENDING", "PARTIAL_CANCELED"}:
                self.store.set_system_value("symbol_quarantine:" + e["symbol"], "ORDER_UNCERTAIN")
                continue
            try:
                rows = [r for r in histories[e["date"]] if order_number(r.get("ord_no")) == order_number(e["order_id"])]
                if len(rows) != 1:
                    raise RuntimeError("Order detail is missing or ambiguous; waiting for reconciliation")
                row = rows[0]
                if row.get("stk_cd", "").removeprefix("A") != e["symbol"] or number(row.get("ord_qty")) != e["quantity"]:
                    raise ValueError("Order identity or quantity mismatch")
                filled, remaining = number(row.get("cntr_qty")), number(row.get("ord_remnq"))
                if not e["filled"] <= filled <= e["quantity"] or filled + remaining > e["quantity"]:
                    raise ValueError("Invalid cumulative fill quantities")
                expected[e["symbol"]] = e["baseline_quantity"] + (filled if e["side"] == "BUY" else -filled)
                terminal = remaining == 0 and filled == e["quantity"]
                if terminal:
                    actual_quantity = held[e["symbol"]].quantity if e["symbol"] in held else 0
                    # Holdings and order history are separate broker requests. Keep
                    # the order active until the holdings snapshot catches up.
                    terminal = actual_quantity == expected[e["symbol"]]
                if remaining == 0 and filled < e["quantity"]:
                    if e["status"] != "CANCEL_SENT" or not e.get("cancel_order_id"):
                        raise ValueError("Partial cancellation or rejection requires reconciliation review")
                    if filled and not e.get("replace_with_market"):
                        raise ValueError("Partial cancellation requires reconciliation review")
                    cancellations = [r for r in histories[e["date"]]
                        if order_number(r.get("ord_no")) == order_number(e["cancel_order_id"])
                        and r.get("mdfy_cncl", "").strip() == "취소확인"]
                    if not cancellations:
                        raise RuntimeError("Waiting for cancellation confirmation")
                    terminal = "REPLACE_READY" if e.get("replace_with_market") else "CANCELED"
                updates.append((e, filled, terminal, fill_price(row.get("cntr_uv"))))
            except (RuntimeError, ValueError, TypeError, KeyError, ArithmeticError) as error:
                self.store.set_system_value("symbol_quarantine:" + e["symbol"], type(error).__name__)
                continue
        purchases = {key.removeprefix("live_purchase:"): Decimal(value)
                     for key, value in self.store.system_values("live_purchase:")}
        active_symbols = {e["symbol"] for e in active}
        manual_changes = []
        for symbol in self.broker.managed_symbols:
            quantity = held[symbol].quantity if symbol in held else 0
            purchase = held[symbol].purchase_amount if symbol in held else 0
            if symbol not in purchases:
                purchases[symbol] = purchase
                continue
            if symbol in manual_open:
                continue
            if symbol in active_symbols:
                # The broker order history remains authoritative while an automatic
                # order is active. Any concurrent manual change is absorbed after
                # the automatic order reaches a terminal state.
                expected[symbol] = quantity
                purchases[symbol] = purchase
                continue
            if quantity != expected.get(symbol, 0) or purchase != purchases[symbol]:
                manual_changes.append((symbol, expected.get(symbol, 0), purchases[symbol],
                                       held.get(symbol), manual_references.get(symbol)))
                if not (quantity > expected.get(symbol, 0) and manual_references.get(symbol) is None):
                    expected[symbol] = quantity
                    purchases[symbol] = purchase
        completed, states_after = [], {}
        successful_updates = []
        with self.store.connection:
            for symbol, old_quantity, old_purchase, position, reference in manual_changes:
                if (self.store.system_value("manual_reference_pending:" + symbol, "")
                        and reference is None):
                    continue
                self.store.connection.execute("SAVEPOINT manual_symbol")
                old_addons = dict(self.addons_today)
                try:
                    states_after[symbol] = self._adopt_manual_change(
                        symbol, old_quantity, old_purchase, position, reference)
                    self.store.connection.execute("RELEASE manual_symbol")
                except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as error:
                    self.store.connection.execute("ROLLBACK TO manual_symbol")
                    self.store.connection.execute("RELEASE manual_symbol")
                    self.addons_today = old_addons
                    expected[symbol], purchases[symbol] = old_quantity, old_purchase
                    self._put("symbol_quarantine:" + symbol, type(error).__name__)
            for symbol, purchase in purchases.items():
                self._put("live_purchase:" + symbol, purchase)
            for symbol, quantity in expected.items():
                self._put("live_quantity:" + symbol, quantity)
            for original, filled, terminal, confirmed_price in updates:
                self.store.connection.execute("SAVEPOINT completed_symbol")
                try:
                    e = dict(original, filled=filled, status=terminal if isinstance(terminal, str) else "FILLED" if terminal else "CANCEL_SENT" if original["status"] == "CANCEL_SENT" else "PARTIAL" if filled else "ACK")
                    self._put("live_quantity:" + e["symbol"], expected[e["symbol"]])
                    if terminal is True:
                        prior_state = self.states.get(e["symbol"])
                        states_after[e["symbol"]] = self._apply_completed(
                            e, held.get(e["symbol"]), confirmed_price)
                        self._record_event("notion-live:" + e["date"] + ":" + order_number(e["order_id"]),
                            "notion_holding_update", self._notion_payload(
                                e, held.get(e["symbol"]), states_after[e["symbol"]], prior_state))
                        completed.append((SimpleNamespace(order_id=e["order_id"],
                            request=OrderRequest(e["key"], e["symbol"], Side(e["side"]), e["quantity"], "KRX"),
                            filled_quantity=filled), e["kind"]))
                    self._save(e)
                    self.store.connection.execute("RELEASE completed_symbol")
                    successful_updates.append((original, filled, terminal, confirmed_price))
                except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as error:
                    self.store.connection.execute("ROLLBACK TO completed_symbol")
                    self.store.connection.execute("RELEASE completed_symbol")
                    states_after.pop(original["symbol"], None)
                    completed = [item for item in completed if item[0].order_id != original["order_id"]]
                    self._put("symbol_quarantine:" + original["symbol"], type(error).__name__)
        for e, filled, terminal, _confirmed_price in successful_updates:
            e.update(filled=filled, status=terminal if isinstance(terminal, str) else "FILLED" if terminal else "CANCEL_SENT" if e["status"] == "CANCEL_SENT" else "PARTIAL" if filled else "ACK")
        for symbol, state in states_after.items():
            if state is None:
                self.states.pop(symbol, None)
            else:
                self.states[symbol] = state
        if not any(e["status"] in self.ACTIVE for e in self.journal.values()):
            self._refresh_capital()
        self.gate.reconciled = True
        return tuple(completed)
