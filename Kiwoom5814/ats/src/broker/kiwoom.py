from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from pathlib import Path
from src.config import Secrets
from src.broker.http import AuthTransport, KiwoomError
from src.market_data.watchlist import WatchlistGroup, WatchlistItem
from src.portfolio.account import AccountOverview, Holding, OpenOrder
from src.market_data.models import Candle, OrderBook, OrderBookLevel, Quote
from src.orders.models import OrderRequest, Side
from src.broker.token_cache import shared_token

KST = timezone(timedelta(hours=9))


class KiwoomClient:
    """Auth implemented; quotes/account adapters remain pending. Orders disabled."""
    def __init__(self, secrets: Secrets, *, transport=None, environment="production", clock=None):
        if environment not in {"mock", "production"}:
            raise ValueError("Invalid Kiwoom environment")
        self._secrets = secrets
        self._environment = environment
        self._transport = transport if transport is not None else AuthTransport(environment)
        self._clock = clock or (lambda: datetime.now(KST))
        self._token = None
        self._expires = None
        self._lock = Lock()
        self._symbol_names = {}

    @property
    def credentials_present(self) -> bool:
        return bool(self._secrets.kiwoom_app_key and self._secrets.kiwoom_secret_key)

    def connect(self):
        """Cache only in memory; refresh 60 seconds before expiry, no blind retry."""
        with self._lock:
            now = self._clock()
            if self._token and self._expires and now + timedelta(seconds=60) < self._expires:
                return
            self._token, self._expires = None, None
            if not self.credentials_present:
                raise KiwoomError("Enter Kiwoom credentials in the local .env file")
            issuer = lambda: self._transport.issue_token(self._secrets.kiwoom_app_key,
                                                          self._secrets.kiwoom_secret_key)
            if (self._environment == "production"
                    and getattr(self._transport, "host", "") == "https://api.kiwoom.com"):
                token, expires = shared_token(
                    Path(__file__).resolve().parents[2] / "data" / "kiwoom_production_token.dpapi",
                    self._secrets.kiwoom_app_key, now, issuer,
                    rejected_token=getattr(self, "_rejected_token", None))
                result = {"return_code": 0, "token": token, "token_type": "bearer",
                          "expires_dt": expires.strftime("%Y%m%d%H%M%S")}
            else:
                result = issuer()
            if str(result.get("return_code", "0")) != "0":
                raise KiwoomError("Kiwoom rejected authentication; check local credentials and API access")
            token = result.get("token")
            if not isinstance(token, str) or not token or result.get("token_type", "").lower() != "bearer":
                raise KiwoomError("Invalid authentication token response")
            try:
                expires = datetime.strptime(result["expires_dt"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
            except (KeyError, TypeError, ValueError):
                raise KiwoomError("Invalid authentication expiry") from None
            if expires <= now + timedelta(seconds=60):
                raise KiwoomError("Authentication token already expired or too close to expiry")
            self._token, self._expires = token, expires
            self._rejected_token = None

    @property
    def authenticated(self):
        return bool(self._token and self._expires and self._clock() + timedelta(seconds=60) < self._expires)

    def realtime_token(self):
        """Return an in-memory token for the WebSocket login without logging it."""
        self.connect()
        return self._token

    def disconnect(self):
        with self._lock:
            self._token, self._expires = None, None

    def _authenticated_read(self, path, api_id, body, **extra):
        """Refresh a token rejected by Kiwoom once, then retry the read only."""
        self.connect()
        token = self._token
        try:
            return self._transport.read(path, api_id, token, body, **extra)
        except KiwoomError as error:
            if "8005" not in str(error):
                raise
            self._rejected_token = token
            self.disconnect()
            self.connect()
            try:
                return self._transport.read(path, api_id, self._token, body, **extra)
            except KiwoomError as retry_error:
                if "8005" in str(retry_error):
                    self._rejected_token = self._token
                    self.disconnect()
                raise

    def watchlist_groups(self):
        self.connect()
        result = self._authenticated_read("/api/dostk/watchlist", "ka01300", {})
        if result.get("rtcd") != "S" or not isinstance(result.get("nofi"), list):
            raise KiwoomError("Invalid watchlist group response")
        groups = []
        for row in result["nofi"]:
            if not isinstance(row, dict):
                raise KiwoomError("Invalid watchlist group row")
            code, name = row.get("gcod"), row.get("name")
            if not isinstance(code, str) or not code or len(code) > 3 or not isinstance(name, str):
                raise KiwoomError("Invalid watchlist group row")
            groups.append(WatchlistGroup(code, name))
        return tuple(groups)

    def watchlist_group(self, group_code):
        if not isinstance(group_code, str) or not group_code or len(group_code) > 3:
            raise ValueError("Invalid watchlist group code")
        self.connect()
        result = self._authenticated_read("/api/dostk/watchlist", "ka01301",
                                      {"arn_grp_id": group_code})
        if result.get("rtcd") != "S" or not isinstance(result.get("nofj"), list):
            raise KiwoomError("Invalid watchlist detail response")
        items = []
        for row in result["nofj"]:
            if not isinstance(row, dict):
                raise KiwoomError("Invalid watchlist detail row")
            symbol = row.get("cod2")
            if not isinstance(symbol, str) or not symbol or len(symbol) > 18:
                raise KiwoomError("Invalid watchlist detail row")
            items.append(WatchlistItem(symbol, str(row.get("bgb", "")), str(row.get("bgb_clr", ""))))
        return tuple(items)

    def synchronized_watchlist(self, maximum=10, group_name="모니터링종목", groups=None):
        if maximum is not None and (type(maximum) is not int or maximum <= 0):
            raise ValueError("Invalid watchlist maximum")
        groups = self.watchlist_groups() if groups is None else groups
        matching = [group for group in groups if group.name == group_name]
        if len(matching) != 1:
            raise KiwoomError(f"Expected exactly one watchlist group named {group_name!r}; found {len(matching)}")
        symbols = []
        seen = set()
        for item in self.watchlist_group(matching[0].code):
            if item.symbol not in seen:
                symbols.append(item.symbol)
                seen.add(item.symbol)
        if maximum is not None and len(symbols) > maximum:
            raise KiwoomError(f"Kiwoom watchlist has {len(symbols)} unique symbols; maximum is {maximum}")
        return tuple(symbols)

    @staticmethod
    def _integer(value, field):
        if not isinstance(value, str):
            raise KiwoomError(f"Invalid numeric account field {field}")
        try:
            return int(value.strip())
        except ValueError:
            raise KiwoomError(f"Invalid numeric account field {field}") from None

    def account_number(self):
        self.connect()
        result = self._authenticated_read("/api/dostk/acnt", "ka00001", {})
        account = result.get("acctNo")
        if not isinstance(account, str) or not account.isdigit() or not 8 <= len(account) <= 20:
            raise KiwoomError("Invalid account number response")
        return account

    def account_overview(self):
        self.connect()
        result = self._authenticated_read("/api/dostk/acnt", "kt00001", {"qry_tp": "3"})
        return AccountOverview(cash=self._integer(result.get("entr"), "entr"),
                               orderable_cash=self._integer(result.get("ord_alow_amt"), "ord_alow_amt"),
                               d2_estimated_cash=self._integer(result.get("d2_entra"), "d2_entra"))

    def holdings(self):
        self.connect()
        result = self._authenticated_read("/api/dostk/acnt", "kt00018",
                                      {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        if result.get("_next_key"):
            raise KiwoomError("Account response needs pagination; refusing an incomplete snapshot")
        rows = result.get("acnt_evlt_remn_indv_tot")
        if not isinstance(rows, list):
            raise KiwoomError("Invalid holdings response")
        holdings = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("stk_cd"), str):
                raise KiwoomError("Invalid holding row")
            symbol = row["stk_cd"].removeprefix("A")
            holdings.append(Holding(symbol=symbol,
                                    quantity=self._integer(row.get("rmnd_qty"), "rmnd_qty"),
                                    available_quantity=self._integer(row.get("trde_able_qty"), "trde_able_qty"),
                                    purchase_amount=self._integer(row.get("pur_amt"), "pur_amt"),
                                    evaluation_amount=self._integer(row.get("evlt_amt"), "evlt_amt")))
        return tuple(holdings)

    def open_orders(self):
        self.connect()
        result = self._authenticated_read("/api/dostk/acnt", "ka10075",
                                      {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"})
        if result.get("_next_key"):
            raise KiwoomError("Account response needs pagination; refusing an incomplete snapshot")
        rows = result.get("oso")
        if not isinstance(rows, list):
            raise KiwoomError("Invalid open-orders response")
        orders = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("ord_no"), str) or not isinstance(row.get("stk_cd"), str):
                raise KiwoomError("Invalid open-order row")
            orders.append(OpenOrder(order_id=row["ord_no"], symbol=row["stk_cd"],
                                    remaining_quantity=self._integer(row.get("oso_qty"), "oso_qty"),
                                    status=str(row.get("ord_stt", ""))))
        return tuple(orders)

    def order_details(self, order_date):
        datetime.strptime(order_date, "%Y%m%d")
        self.connect()
        body = {"ord_dt": order_date, "qry_tp": "1", "stk_bond_tp": "1",
                "sell_tp": "0", "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "KRX"}
        rows, seen, continuation = [], set(), None
        for _ in range(100):
            extra = {"cont_yn": "Y", "next_key": continuation} if continuation else {}
            result = self._authenticated_read("/api/dostk/acnt", "kt00007", body, **extra)
            page = result.get("acnt_ord_cntr_prps_dtl")
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise KiwoomError("Invalid order detail response")
            rows.extend(page)
            continuation = result.get("_next_key")
            if not continuation:
                return tuple(rows)
            if continuation in seen:
                raise KiwoomError("Repeated order detail continuation")
            seen.add(continuation)
        raise KiwoomError("Order detail pagination limit exceeded")

    def today_realized_profit(self, symbol):
        """Return Kiwoom's fee/tax-adjusted realized result for one sold symbol."""
        symbol = self._symbol(symbol)
        self.connect()
        result = self._authenticated_read("/api/dostk/acnt", "ka10077",
                                      {"stk_cd": symbol})
        if result.get("_next_key"):
            raise KiwoomError("Realized-profit response needs pagination")
        return self._realized_profit_row(symbol, result.get("tdy_rlzt_pl_dtl"))

    def realized_profit_for_date(self, symbol, trading_date):
        symbol = self._symbol(symbol)
        day = datetime.strptime(trading_date, "%Y-%m-%d")
        if day.date() == self._clock().date():
            return self.today_realized_profit(symbol)
        self.connect()
        result = self._authenticated_read("/api/dostk/acnt", "ka10072",
            {"stk_cd": symbol, "strt_dt": day.strftime("%Y%m%d")})
        if result.get("_next_key"):
            raise KiwoomError("Historical realized-profit response needs pagination")
        return self._realized_profit_row(symbol, result.get("dt_stk_div_rlzt_pl"))

    @staticmethod
    def _realized_decimal(value, field, *, positive=False):
        try:
            number = Decimal(str(value).replace(",", "").replace("%", "").strip())
        except (InvalidOperation, ValueError):
            raise KiwoomError(f"Invalid realized-profit field {field}") from None
        if not number.is_finite() or (positive and number <= 0):
            raise KiwoomError(f"Invalid realized-profit field {field}")
        return str(number)

    def _realized_profit_row(self, symbol, rows):
        if not isinstance(rows, list):
            raise KiwoomError("Invalid realized-profit response")
        matches = [row for row in rows if isinstance(row, dict)
                   and str(row.get("stk_cd", "")).removeprefix("A") == symbol]
        if not matches:
            raise KiwoomError("Completed sell has no realized-profit rows")
        total_quantity = 0
        total_buy_value = Decimal("0")
        total_sell_value = Decimal("0")
        total_profit = Decimal("0")
        weighted_rate = Decimal("0")
        name = ""
        for row in matches:
            quantity = self._nonnegative(row.get("cntr_qty"), "cntr_qty")
            if quantity <= 0:
                raise KiwoomError("Invalid realized-profit quantity")
            buy_price = Decimal(self._realized_decimal(row.get("buy_uv"), "buy_uv", positive=True))
            sell_price = Decimal(self._realized_decimal(row.get("cntr_pric"), "cntr_pric", positive=True))
            profit = Decimal(self._realized_decimal(row.get("tdy_sel_pl"), "tdy_sel_pl"))
            rate = Decimal(self._realized_decimal(row.get("pl_rt"), "pl_rt"))
            total_quantity += quantity
            total_buy_value += buy_price * quantity
            total_sell_value += sell_price * quantity
            total_profit += profit
            weighted_rate += rate * buy_price * quantity
            row_name = str(row.get("stk_nm", "")).strip()
            if row_name:
                if name and row_name != name:
                    raise KiwoomError("Inconsistent realized-profit symbol names")
                name = row_name
        if total_buy_value <= 0:
            raise KiwoomError("Invalid realized-profit purchase value")
        return {
            "symbol": symbol,
            "name": name,
            "quantity": total_quantity,
            "buy_price": str(total_buy_value / total_quantity),
            "sell_price": str(total_sell_value / total_quantity),
            "profit_amount": str(total_profit),
            "profit_rate": str(weighted_rate / total_buy_value),
        }

    @classmethod
    def _nonnegative(cls, value, field):
        parsed = cls._integer(value, field)
        if parsed < 0:
            raise KiwoomError(f"Negative quantity in field {field}")
        return parsed

    @classmethod
    def _price(cls, value, field):
        return abs(cls._integer(value, field))

    @staticmethod
    def _symbol(symbol):
        if not isinstance(symbol, str) or not symbol or len(symbol) > 20:
            raise ValueError("Invalid stock symbol")
        return symbol

    def quote(self, symbol):
        symbol = self._symbol(symbol)
        self.connect()
        result = self._authenticated_read("/api/dostk/stkinfo", "ka10001", {"stk_cd": symbol})
        if result.get("stk_cd") != symbol or not isinstance(result.get("stk_nm"), str):
            raise KiwoomError("Invalid quote response")
        return Quote(symbol=symbol, name=result["stk_nm"],
                     current_price=self._price(result.get("cur_prc"), "cur_prc"),
                     volume=self._nonnegative(result.get("trde_qty"), "trde_qty"))

    def order_book(self, symbol):
        symbol = self._symbol(symbol)
        self.connect()
        result = self._authenticated_read("/api/dostk/mrkcond", "ka10004", {"stk_cd": symbol})
        asks, bids = [], []
        for level in range(1, 11):
            if level == 1:
                ask_price, ask_qty = "sel_fpr_bid", "sel_fpr_req"
                bid_price, bid_qty = "buy_fpr_bid", "buy_fpr_req"
            else:
                ask_price, ask_qty = f"sel_{level}th_pre_bid", f"sel_{level}th_pre_req"
                bid_price, bid_qty = f"buy_{level}th_pre_bid", f"buy_{level}th_pre_req"
            asks.append(OrderBookLevel(level, self._price(result.get(ask_price), ask_price),
                                       self._nonnegative(result.get(ask_qty), ask_qty)))
            bids.append(OrderBookLevel(level, self._price(result.get(bid_price), bid_price),
                                       self._nonnegative(result.get(bid_qty), bid_qty)))
        return OrderBook(symbol=symbol, timestamp=str(result.get("bid_req_base_tm", "")),
                         asks=tuple(asks), bids=tuple(bids))

    def candles(self, symbol, timeframe, base_date, adjusted=True, market="KRX"):
        symbol = self._symbol(symbol)
        if market != "KRX":
            raise ValueError("Trading-signal charts are fixed to KRX")
        if timeframe not in {"daily", "weekly"}:
            raise ValueError("Timeframe must be daily or weekly")
        if not isinstance(base_date, str) or len(base_date) != 8 or not base_date.isdigit():
            raise ValueError("base_date must be YYYYMMDD")
        api_id = "ka10081" if timeframe == "daily" else "ka10082"
        response_key = "stk_dt_pole_chart_qry" if timeframe == "daily" else "stk_stk_pole_chart_qry"
        self.connect()
        result = self._authenticated_read("/api/dostk/chart", api_id,
                                      {"stk_cd": symbol, "base_dt": base_date,
                                       "upd_stkpc_tp": "1" if adjusted else "0"})
        if result.get("stk_cd") != symbol or not isinstance(result.get(response_key), list):
            raise KiwoomError("Invalid chart response")
        candles = []
        for row in result[response_key]:
            if not isinstance(row, dict) or not isinstance(row.get("dt"), str):
                raise KiwoomError("Invalid candle row")
            candle = Candle(date=row["dt"], open=self._price(row.get("open_pric"), "open_pric"),
                            high=self._price(row.get("high_pric"), "high_pric"),
                            low=self._price(row.get("low_pric"), "low_pric"),
                            close=self._price(row.get("cur_prc"), "cur_prc"),
                            volume=self._nonnegative(row.get("trde_qty"), "trde_qty"))
            if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
                raise KiwoomError("Inconsistent OHLC response")
            candles.append(candle)
        return tuple(candles)

    def trading_value_top(self, maximum=100):
        if type(maximum) is not int or maximum <= 0 or maximum > 100:
            raise ValueError("Invalid ranking maximum")
        self.connect()
        result = self._authenticated_read("/api/dostk/rkinfo", "ka10032",
                                      {"mrkt_tp": "000", "mang_stk_incls": "0",
                                       "stex_tp": "1"})
        rows = result.get("trde_prica_upper")
        if not isinstance(rows, list):
            raise KiwoomError("Invalid trading-value ranking response")
        candidates = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise KiwoomError("Invalid trading-value ranking row")
            symbol = str(row.get("stk_cd", "")).removeprefix("A")
            if not symbol.isdigit() or len(symbol) != 6:
                continue
            if symbol not in seen:
                candidates.append(symbol); seen.add(symbol)
                name = row.get("stk_nm")
                if isinstance(name, str) and name.strip():
                    self._symbol_names[symbol] = name.strip()
            if len(candidates) >= maximum:
                break
        return tuple(candidates)

    def symbol_name(self, symbol):
        symbol = self._symbol(symbol)
        if symbol not in self._symbol_names:
            self._symbol_names[symbol] = self.quote(symbol).name
        return self._symbol_names[symbol]

    def submit(self, order: OrderRequest = None):
        if self._environment != "mock":
            raise RuntimeError("Production orders are disabled")
        if not isinstance(order, OrderRequest) or order.market != "KRX":
            raise ValueError("A KRX OrderRequest is required")
        self.connect()
        api_id = "kt10000" if order.side == Side.BUY else "kt10001"
        result = self._transport.order(api_id, self._token, {
            "dmst_stex_tp": "KRX", "stk_cd": order.symbol,
            "ord_qty": str(order.quantity), "ord_uv": "",
            "trde_tp": "3", "cond_uv": "",
        })
        order_id = result.get("ord_no")
        if not isinstance(order_id, str) or not order_id.isdigit() or len(order_id) > 12:
            raise KiwoomError("Invalid mock order number response")
        return order_id

    def cancel(self, order_id: str = "", symbol: str = "", quantity: int = 0):
        if self._environment != "mock":
            raise RuntimeError("Production order mutations are disabled")
        if (not isinstance(order_id, str) or not order_id.isdigit()
                or not isinstance(symbol, str) or not symbol
                or type(quantity) is not int or quantity <= 0):
            raise ValueError("Valid original order, symbol and quantity are required")
        self.connect()
        result = self._transport.order("kt10003", self._token, {
            "dmst_stex_tp": "KRX", "orig_ord_no": order_id,
            "stk_cd": symbol, "cncl_qty": str(quantity),
        })
        cancel_id = result.get("ord_no")
        if not isinstance(cancel_id, str) or not cancel_id.isdigit() or len(cancel_id) > 12:
            raise KiwoomError("Invalid mock cancellation response")
        return cancel_id
