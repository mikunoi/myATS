"""Kiwoom REST adapter for United States equities."""
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from src.broker.http import AuthTransport, KiwoomError
from src.broker.kiwoom import KiwoomClient
from src.market_data.models import Candle, OrderBook, OrderBookLevel, Quote
from src.market_data.watchlist import WatchlistGroup
from src.orders.models import OrderRequest, Side
from src.portfolio.account import Holding, OpenOrder


def us_order_price(price):
    """Kiwoom: prices below USD 1 allow four decimals, others two."""
    price = Decimal(price)
    if not price.is_finite() or price <= 0:
        raise ValueError("Positive finite US order price required")
    quantum = Decimal("0.0001") if price < 1 else Decimal("0.01")
    rounded = price.quantize(quantum)
    if rounded != price:
        raise ValueError("US quote exceeds permitted order price precision")
    result = format(rounded, "f")
    if len(result) > 12:
        raise ValueError("US order price exceeds field length")
    return result


def decimal_value(value, field):
    try:
        return abs(Decimal(str(value).replace(",", "").strip()))
    except (InvalidOperation, ValueError):
        raise KiwoomError(f"Invalid US numeric field {field}") from None


def signed_decimal_value(value, field):
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise KiwoomError(f"Invalid US numeric field {field}") from None
    if not result.is_finite():
        raise KiwoomError(f"Invalid US numeric field {field}")
    return result


class USProductionOrderTransport(AuthTransport):
    def __init__(self, authorize, cancel_authorize=None, **kwargs):
        super().__init__("production", **kwargs)
        self.authorize = authorize
        self.cancel_authorize = cancel_authorize or authorize

    def order(self, api_id, token, body):
        permitted = self.cancel_authorize if api_id == "ust20003" else self.authorize
        if permitted() is not True:
            raise KiwoomError("US production order execution is not armed")
        if api_id not in {"ust20000", "ust20001", "ust20003"}:
            raise KiwoomError("US order endpoint is not allowlisted")
        if not isinstance(body, dict) or body.get("stex_tp") not in {"NA", "ND", "NY"}:
            raise KiwoomError("Invalid US exchange")
        KiwoomClient._symbol(body.get("stk_cd"))
        if api_id != "ust20003":
            trade_type = body.get("trde_tp")
            if trade_type == "00":
                if decimal_value(body.get("ord_uv"), "ord_uv") <= 0:
                    raise KiwoomError("US limit orders require a positive price")
            elif trade_type == "03":
                if body.get("ord_uv") not in {"", None}:
                    raise KiwoomError("US market orders require an empty price")
            else:
                raise KiwoomError("Unsupported US automatic order type")
            if not str(body.get("ord_qty", "")).isdigit() or int(body["ord_qty"]) <= 0:
                raise KiwoomError("Invalid US order quantity")
        return self._send_order_path("/api/us/ordr", api_id, token, body)


class KiwoomUSLiveClient(KiwoomClient):
    def __init__(self, secrets, *, expected_account, authorize, cancel_authorize=None,
                 managed_symbols=(), transport=None, clock=None):
        self.expected_account = expected_account
        self.authorize = authorize
        self.cancel_authorize = cancel_authorize or authorize
        self.managed_symbols = frozenset(managed_symbols)
        self.exchanges = {}
        super().__init__(secrets, environment="production", clock=clock,
            transport=transport or USProductionOrderTransport(authorize, self.cancel_authorize))

    def market_for(self, symbol):
        market = self.exchanges.get(symbol)
        if market not in {"NA", "ND", "NY"}:
            raise KiwoomError("US symbol exchange is unavailable")
        return market

    def _authenticated_read(self, path, api, body):
        """Retry one transient read and refresh an explicitly rejected token."""
        self.connect()
        token = self._token
        try:
            return self._transport.read(path, api, token, body)
        except KiwoomError as error:
            message = str(error)
            if "8005" not in message:
                if "connection failed" not in message and "HTTP status 5" not in message:
                    raise
                time.sleep(1)
                return self._transport.read(path, api, token, body)
            self._rejected_token = token
            self.disconnect()
            self.connect()
            try:
                return self._transport.read(path, api, self._token, body)
            except KiwoomError as retry_error:
                if "8005" in str(retry_error):
                    self._rejected_token = self._token
                    self.disconnect()
                raise

    def watchlist_groups(self):
        self.connect()
        result = self._authenticated_read("/api/us/watchlist", "usa20200", {})
        rows = result.get("nofi")
        if result.get("rtcd") != "S" or not isinstance(rows, list):
            raise KiwoomError("Invalid US watchlist group response")
        return tuple(WatchlistGroup(str(r.get("gcod", "")), str(r.get("name", ""))) for r in rows)

    def synchronized_watchlist(self, maximum=10, group_name="(5814)감시종목", groups=None,
                               *, allow_empty=False):
        groups = self.watchlist_groups() if groups is None else groups
        matching = [g for g in groups if g.name == group_name]
        if len(matching) != 1:
            raise KiwoomError(f"Expected exactly one US watchlist group named {group_name!r}; found {len(matching)}")
        result = self._authenticated_read("/api/us/watchlist", "usa20201",
                                      {"arn_grp_id": matching[0].code})
        rows = result.get("nofj")
        if result.get("rtcd") != "S" or not isinstance(rows, list):
            raise KiwoomError("Invalid US watchlist detail response")
        symbols=[]
        for row in rows:
            symbol, exchange = str(row.get("cod2", "")).strip().upper(), str(row.get("stex_tp", ""))
            if not symbol or exchange not in {"NA", "ND", "NY"}:
                raise KiwoomError("Invalid US watchlist item")
            if symbol not in symbols:
                symbols.append(symbol)
            self.exchanges[symbol] = exchange
        if (not symbols and not allow_empty) or len(symbols) > maximum:
            raise KiwoomError(f"US watchlist size must be 1..{maximum}")
        return tuple(symbols)

    def holdings(self):
        self.connect()
        result = self._authenticated_read("/api/us/acnt", "ust21070", {"stex_tp":"", "stk_cd":""})
        if result.get("_next_key"):
            raise KiwoomError("US holdings need pagination; incomplete snapshot refused")
        rows = result.get("result_list")
        if not isinstance(rows, list): raise KiwoomError("Invalid US holdings response")
        holdings=[]
        for row in rows:
            symbol=str(row.get("stk_cd", "")).strip().upper()
            exchange={"NASDAQ":"ND","NYSE":"NY","AMEX":"NA"}.get(str(row.get("stex_nm", "")).upper(), self.exchanges.get(symbol))
            if exchange: self.exchanges[symbol]=exchange
            qty=int(decimal_value(row.get("poss_qty", row.get("qty")), "poss_qty"))
            purchase_usd = decimal_value(row.get("frgn_stk_book_amt"), "frgn_stk_book_amt")
            purchase_krw = decimal_value(row.get("frgn_stk_book_amt_krw"), "frgn_stk_book_amt_krw")
            acquisition_rate = purchase_krw / purchase_usd if purchase_usd else self.exchange_rate()
            holdings.append(Holding(symbol, qty, int(decimal_value(row.get("sell_alowq"), "sell_alowq")),
                                    purchase_usd, decimal_value(row.get("evlt_amt"), "evlt_amt"),
                                    purchase_krw, acquisition_rate))
        return tuple(holdings)

    def account_overview(self):
        self.connect()
        result=self._authenticated_read("/api/us/acnt", "ust21110", {})
        rows=result.get("result_list")
        if not isinstance(rows,list): raise KiwoomError("Invalid US cash response")
        usd=next((r for r in rows if r.get("crnc_code")=="USD"), None)
        if usd is None: raise KiwoomError("USD cash row is missing")
        cash=decimal_value(usd.get("fc_pymn_alowa"), "fc_pymn_alowa")
        return SimpleNamespace(cash=cash, orderable_cash=cash, d2_estimated_cash=None)

    def d2_estimated_cash_usd(self):
        """D+2 USD deposit after pending overseas settlements."""
        self.connect()
        result = self._authenticated_read("/api/us/acnt", "ust21160", {})
        return signed_decimal_value(result.get("d2_usd_fx_entr"), "d2_usd_fx_entr")

    def exchange_rate(self):
        self.connect()
        result = self._authenticated_read("/api/us/exchange", "ust31301", {"exch_tp": "1"})
        rate = decimal_value(result.get("aplc_exrt"), "aplc_exrt")
        if rate <= 0:
            raise KiwoomError("USD/KRW exchange rate is unavailable")
        return rate

    def open_orders(self):
        self.connect()
        body={"ord_dt":datetime.now().strftime("%Y%m%d"),"slby_tp":"0","stex_tp":"","stk_cd":""}
        try:
            result=self._authenticated_read("/api/us/acnt", "ust21050", body)
        except KiwoomError as error:
            # Kiwoom uses business code 2000 when the open-order ledger has no rows.
            if "[2000]" in str(error): return ()
            raise
        if result.get("_next_key"): raise KiwoomError("US open orders need pagination")
        rows=result.get("result_list")
        if not isinstance(rows,list): raise KiwoomError("Invalid US open-orders response")
        return tuple(OpenOrder(str(r.get("ord_no","")),str(r.get("stk_cd","")).upper(),
                    int(decimal_value(r.get("ord_remnq"),"ord_remnq")),str(r.get("ord_stat","")))
                    for r in rows if decimal_value(r.get("ord_remnq"),"ord_remnq")>0)

    def order_details(self, order_date):
        self.connect()
        try:
            result=self._authenticated_read("/api/us/acnt", "ust21150",
                {"ord_dt":order_date,"query_tp":"1","slby_tp":"0","stex_tp":"","stk_cd":"","oppo_trde_tp":"%","fr_ord_no":""})
        except KiwoomError as error:
            # Kiwoom reports business code 2000 when the date has no order rows.
            if "[2000]" in str(error): return ()
            raise
        if result.get("_next_key"): raise KiwoomError("US order details need pagination")
        rows=result.get("result_list")
        if not isinstance(rows,list): raise KiwoomError("Invalid US order-detail response")
        return tuple(rows)

    def today_realized_profit(self, symbol):
        self.connect()
        result=self._authenticated_read("/api/us/acnt","ust21630",
            {"stex_tp":self.market_for(symbol),"stk_cd":symbol,"fc_krw_tp":"0"})
        rows=result.get("result_list")
        if not isinstance(rows,list): raise KiwoomError("Invalid US realized-profit response")
        row=next((r for r in rows if str(r.get("stk_cd","")).upper()==symbol),None)
        if row is None: raise KiwoomError("US completed sell is missing from realized-profit response")
        return {"symbol":symbol,"name":str(row.get("stk_nm","")),
                "quantity":int(decimal_value(row.get("cntr_sellq"),"cntr_sellq")),
                "buy_price":decimal_value(row.get("avg_buy_uv"),"avg_buy_uv"),
                "sell_price":decimal_value(row.get("cntr_sella"),"cntr_sella")/decimal_value(row.get("cntr_sellq"),"cntr_sellq"),
                "profit_amount":str(row.get("tdy_pl_amt","0")).replace(",",""),
                "profit_rate":str(row.get("pl_rt","0"))}

    def realized_profit_for_date(self, symbol, trading_date):
        day = datetime.strptime(trading_date, "%Y-%m-%d").strftime("%Y%m%d")
        self.connect()
        result = self._authenticated_read("/api/us/acnt", "ust21640",
            {"stex_tp": self.exchanges.get(symbol, ""), "stk_cd": symbol,
             "cntr_dt": day, "fc_krw_tp": "0"})
        if result.get("_next_key"):
            raise KiwoomError("US dated realized-profit response needs pagination")
        rows = result.get("result_list")
        if not isinstance(rows, list):
            raise KiwoomError("Invalid US dated realized-profit response")
        matches = [r for r in rows if isinstance(r, dict)
                   and str(r.get("stk_cd", "")).upper() == symbol.upper()]
        if len(matches) != 1:
            raise KiwoomError("US realized-profit rows are missing or ambiguous")
        row = matches[0]
        quantity = self._nonnegative(row.get("cntr_sellq"), "cntr_sellq")
        if not quantity:
            raise KiwoomError("US realized-profit sell quantity is zero")
        sell_amount = self._realized_decimal(row.get("cntr_sella"), "cntr_sella", positive=True)
        return {"symbol": symbol, "name": str(row.get("stk_nm", "")),
                "quantity": quantity,
                "buy_price": self._realized_decimal(row.get("avg_buy_uv"), "avg_buy_uv", positive=True),
                "sell_price": str(Decimal(sell_amount) / quantity),
                "profit_amount": self._realized_decimal(row.get("pl_amt"), "pl_amt"),
                "profit_rate": self._realized_decimal(row.get("pl_rt"), "pl_rt")}

    def quote(self, symbol):
        self.connect(); market=self.market_for(symbol)
        r=self._authenticated_read("/api/us/mrkcond","usa20100",{"stex_tp":market,"stk_cd":symbol})
        return Quote(symbol,str(r.get("stk_nm") or r.get("stk_enm") or symbol),decimal_value(r.get("cur_prc"),"cur_prc"),int(decimal_value(r.get("acc_trde_qty","0"),"acc_trde_qty")))

    def order_book(self, symbol):
        self.connect(); market=self.market_for(symbol)
        r=self._authenticated_read("/api/us/mrkcond","usa20101",{"stex_tp":market,"stk_cd":symbol})
        asks=tuple(OrderBookLevel(i,decimal_value(r.get(f"sel_{i}bid"),f"sel_{i}bid"),int(decimal_value(r.get(f"sel_{i}bid_req","0"),f"sel_{i}bid_req"))) for i in range(1,11))
        bids=tuple(OrderBookLevel(i,decimal_value(r.get(f"buy_{i}bid"),f"buy_{i}bid"),int(decimal_value(r.get(f"buy_{i}bid_req","0"),f"buy_{i}bid_req"))) for i in range(1,11))
        return OrderBook(symbol,str(r.get("bid_tm","")),asks,bids)

    def candles(self, symbol, timeframe, base_date, adjusted=True, market=None):
        if timeframe not in {"daily","weekly"}: raise ValueError("Timeframe must be daily or weekly")
        api="usa06012" if timeframe=="daily" else "usa06013"
        r=self._authenticated_read("/api/us/chart",api,{"stex_tp":self.market_for(symbol),"stk_cd":symbol,"strt_dt":base_date,"upd_stkpc_tp":"1" if adjusted else "0","exrt_appl_tp":"0"})
        rows=r.get("result_list")
        if not isinstance(rows,list): raise KiwoomError("Invalid US chart response")
        candles=tuple(Candle(str(x.get("dt")),decimal_value(x.get("open_pric"),"open_pric"),decimal_value(x.get("high_pric"),"high_pric"),decimal_value(x.get("low_pric"),"low_pric"),decimal_value(x.get("cur_prc"),"cur_prc"),int(decimal_value(x.get("acc_trde_qty","0"),"acc_trde_qty"))) for x in rows)
        valid=[]
        for candle in candles:
            inconsistent=(candle.high < max(candle.open,candle.close,candle.low)
                          or candle.low > min(candle.open,candle.close,candle.high))
            current_period = candle.date == base_date
            if timeframe == "weekly":
                try:
                    current_period = (datetime.strptime(candle.date, "%Y%m%d").isocalendar()[:2]
                                      == datetime.strptime(base_date, "%Y%m%d").isocalendar()[:2])
                except ValueError:
                    current_period = False
            if inconsistent and current_period:
                continue  # Kiwoom can expose a partially initialized current-session row.
            if inconsistent:
                raise KiwoomError(f"Invalid completed US OHLC candle {timeframe} {candle.date} open={candle.open} high={candle.high} low={candle.low} close={candle.close}")
            valid.append(candle)
        return tuple(valid)

    def submit(self, order=None):
        if not isinstance(order,OrderRequest) or order.symbol not in self.managed_symbols or order.trade_type!="00": raise ValueError("Managed US limit order required")
        if self.account_number()!=self.expected_account: raise RuntimeError("Production account mismatch")
        price=self.order_book(order.symbol).asks[0].price if order.side==Side.BUY else self.order_book(order.symbol).bids[0].price
        api="ust20000" if order.side==Side.BUY else "ust20001"
        if type(order.quantity) is not int or order.quantity <= 0:
            raise ValueError("Positive US limit quantity required")
        r=self._transport.order(api,self._token,{"stex_tp":self.market_for(order.symbol),"stk_cd":order.symbol,"ord_qty":str(order.quantity),"ord_uv":us_order_price(price),"trde_tp":"00",**({"stop_pric":""} if order.side==Side.SELL else {})})
        oid=str(r.get("ord_no",""));
        if not oid.isdigit(): raise KiwoomError("US order outcome uncertain")
        return oid

    def submit_market(self, symbol, side, quantity):
        if symbol not in self.managed_symbols or side not in {Side.BUY, Side.SELL}:
            raise ValueError("Managed US market order required")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("Positive US market quantity required")
        if self.account_number()!=self.expected_account: raise RuntimeError("Production account mismatch")
        api="ust20000" if side==Side.BUY else "ust20001"
        r=self._transport.order(api,self._token,{"stex_tp":self.market_for(symbol),"stk_cd":symbol,
            "ord_qty":str(quantity),"ord_uv":"","trde_tp":"03",
            **({"stop_pric":""} if side==Side.SELL else {})})
        oid=str(r.get("ord_no",""))
        if not oid.isdigit(): raise KiwoomError("US market order outcome uncertain")
        return oid

    def cancel(self, order_id="", symbol="", quantity=0):
        if self.account_number()!=self.expected_account: raise RuntimeError("Production account mismatch")
        r=self._transport.order("ust20003",self._token,{"ord_no":str(order_id),
            "stex_tp":self.market_for(symbol),"stk_cd":symbol})
        oid=str(r.get("ord_no",""));
        if not oid.isdigit(): raise KiwoomError("US cancellation outcome uncertain")
        return oid
