"""KIS domestic trading adapter for an explicitly started account worker."""
from datetime import datetime
from decimal import Decimal
import re
from types import SimpleNamespace

from kis_managed import ManagedKIS
from kis_portfolio import KISError
from kis_snapshot import one
from refresh_schedule import KST


def unsigned(value):
    text = str(value).strip()
    if not re.fullmatch(r'[0-9]+', text):
        raise KISError('KIS_INTEGER_INVALID')
    return int(text)


def amount(value):
    result = Decimal(str(value).strip())
    if not result.is_finite():
        raise KISError('KIS_AMOUNT_INVALID')
    return result


def order_id(branch, number):
    if not 1 <= len(str(branch)) <= 5 or not 1 <= len(str(number)) <= 10:
        raise KISError('KIS_ORDER_ID_INVALID')
    return f'{unsigned(branch):05d}{unsigned(number):010d}'


class ExecutionClient(ManagedKIS):
    def __init__(self, root, profile, authorize):
        super().__init__(root, profile)
        self.authorize = authorize
        real = profile.mode == 'live'
        self.READS['buy_power'] = ('/uapi/domestic-stock/v1/trading/inquire-psbl-order',
                                   'TTTC8908R' if real else 'VTTC8908R')
        if real:
            self.READS['cancelable'] = ('/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl', 'TTTC0084R')
        self._positions = ()

    def account_overview(self):
        response = self.balance('domestic')
        summary = one(response['output2'])
        positions = []
        seen = set()
        for row in response['output1']:
            quantity = unsigned(row['hldg_qty'])
            if not quantity:
                continue
            symbol = str(row['pdno']).strip()
            available = unsigned(row['ord_psbl_qty'])
            purchase, average = amount(row['pchs_amt']), amount(row['pchs_avg_pric'])
            if not symbol or symbol in seen or available > quantity or min(purchase, average) <= 0:
                raise KISError('KIS_HOLDINGS_INVALID')
            seen.add(symbol)
            positions.append(SimpleNamespace(symbol=symbol, quantity=quantity,
                available_quantity=available, average_price=average, purchase_amount=purchase))
        self._positions = tuple(positions)
        settled = amount(summary['dnca_tot_amt'])
        estimated = amount(summary['prvs_rcdl_excc_amt'])
        return SimpleNamespace(orderable_cash=max(Decimal(0), min(settled, estimated)),
                               d2_estimated_cash=estimated)

    def holdings(self):
        return self._positions

    def history(self, day):
        rows, _ = self.pages('domestic_orders', dict(
            INQR_STRT_DT=day, INQR_END_DT=day, SLL_BUY_DVSN_CD='00', PDNO='',
            CCLD_DVSN='00', INQR_DVSN='00', INQR_DVSN_3='00', ORD_GNO_BRNO='',
            ODNO='', INQR_DVSN_1='', EXCG_ID_DVSN_CD='KRX'))
        return rows

    def open_orders(self):
        rows = self.history(datetime.now(KST).strftime('%Y%m%d'))
        return tuple(SimpleNamespace(symbol=str(r['pdno']).strip(),
                     order_id=order_id(r['ord_gno_brno'],r['odno']))
                     for r in rows if r['cncl_yn'] == 'N' and unsigned(r['rmn_qty']))

    def order_details(self, day):
        rows = []
        for raw in self.history(day):
            filled = unsigned(raw['tot_ccld_qty'])
            remaining = unsigned(raw['rmn_qty'])
            rows.append(dict(ord_no=order_id(raw['ord_gno_brno'],raw['odno']),
                stk_cd=str(raw['pdno']).strip(), ord_qty=str(unsigned(raw['ord_qty'])),
                cntr_qty=str(filled), ord_remnq='0' if raw['cncl_yn']=='Y' else str(remaining),
                cntr_uv=str(amount(raw['tot_ccld_amt'])/filled) if filled else None,
                mdfy_cncl='취소확인' if raw.get('cnc_cfrm_qty') and unsigned(raw['cnc_cfrm_qty']) else ''))
        return rows

    def quote(self, symbol):
        body = self.market_read('quote', FID_COND_MRKT_DIV_CODE='J', FID_INPUT_ISCD=symbol)['output']
        price = unsigned(body['stck_prpr'])
        volume = unsigned(body['acml_vol'])
        if price <= 0 or body.get('temp_stop_yn') != 'N':
            raise KISError('KIS_QUOTE_UNAVAILABLE')
        return SimpleNamespace(current_price=price, volume=volume)

    def order_book(self, symbol):
        from src.market_data.models import OrderBook, OrderBookLevel
        body = self.market_read('book', FID_COND_MRKT_DIV_CODE='J', FID_INPUT_ISCD=symbol)['output1']
        now = datetime.now(KST)
        stamp = datetime.strptime(body['aspr_acpt_hour'], '%H%M%S').time()
        if not 0 <= (now - datetime.combine(now.date(), stamp, KST)).total_seconds() <= 10:
            raise KISError('KIS_QUOTE_STALE')
        def levels(side):
            price, size = unsigned(body[side+'p1']), unsigned(body[side+'p_rsqn1'])
            return (OrderBookLevel(1, price, size),) if price and size else ()
        return OrderBook(symbol, now.isoformat(), levels('ask'), levels('bid'))

    def _post(self, path, code, params, action):
        if not self.authorize(action):
            raise KISError('KIS_ORDER_DISABLED')
        headers = self.headers(('TTTC' if self.profile.mode == 'live' else 'VTTC') + code)
        if not self.authorize(action):
            raise KISError('KIS_ORDER_DISABLED')
        previous_guard = self.guard
        def permit():
            if previous_guard:
                previous_guard()
            if not self.authorize(action):
                raise KISError('KIS_ORDER_DISABLED')

        self.guard = permit
        try:
            body, _ = self._exchange('/uapi/domestic-stock/v1/trading/' + path, payload={
                'CANO': self.account, 'ACNT_PRDT_CD': self.product, **params}, headers=headers)
        finally:
            self.guard = previous_guard
        if str(body.get('rt_cd')) != '0':
            raise KISError('KIS_ORDER_' + self._safe_code(body.get('msg_cd')))
        result = body['output']
        return order_id(result.get('KRX_FWDG_ORD_ORGNO') or result['krx_fwdg_ord_orgno'],
                        result.get('ODNO') or result['odno'])

    def submit(self, order):
        from src.orders.models import Side
        if not self.authorize('submit') or order.market != 'KRX' or order.trade_type != '6':
            raise KISError('KIS_ORDER_DISABLED')
        book = self.order_book(order.symbol)
        levels = book.asks if order.side == Side.BUY else book.bids
        if not levels:
            raise KISError('KIS_QUOTE_UNAVAILABLE')
        if order.side == Side.BUY:
            raw, more = self.read('buy_power', dict(PDNO=order.symbol, ORD_UNPR='0', ORD_DVSN='03',
                                                  CMA_EVLU_AMT_ICLD_YN='N', OVRS_ICLD_YN='N'))
            if more in ('M','F') or order.quantity > unsigned(raw['output']['nrcvb_buy_qty']):
                raise KISError('KIS_BUY_POWER_INSUFFICIENT')
        else:
            self.account_overview()
            if not any(p.symbol == order.symbol and p.available_quantity >= order.quantity for p in self.holdings()):
                raise KISError('KIS_SELL_QUANTITY_UNAVAILABLE')
        # Account checks may take multiple API calls. Refresh the executable side.
        latest_book = self.order_book(order.symbol)
        if not (latest_book.asks if order.side == Side.BUY else latest_book.bids):
            raise KISError('KIS_QUOTE_UNAVAILABLE')
        code = '0012U' if order.side == Side.BUY else '0011U'
        return self._post('order-cash', code, dict(PDNO=order.symbol, ORD_DVSN='03',
                           ORD_QTY=str(order.quantity), ORD_UNPR='0', EXCG_ID_DVSN_CD='KRX',
                           SLL_TYPE='01' if order.side == Side.SELL else ''), 'submit')

    def cancel(self, original, symbol, _quantity):
        if not self.authorize('cancel'):
            raise KISError('KIS_ORDER_DISABLED')
        if self.profile.mode == 'live':
            rows, _ = self.pages('cancelable', dict(INQR_DVSN_1='0', INQR_DVSN_2='0'))
            field = 'psbl_qty'
        else:
            rows = self.history(datetime.now(KST).strftime('%Y%m%d'))
            field = 'rmn_qty'
        found = [row for row in rows if str(row['pdno']).strip() == symbol and
                 order_id(row['ord_gno_brno'],row['odno']) == original and unsigned(row[field]) > 0]
        if len(found) != 1:
            raise KISError('KIS_CANCEL_ORDER_UNAVAILABLE')
        row = found[0]
        return self._post('order-rvsecncl', '0013U', dict(
            KRX_FWDG_ORD_ORGNO=row['ord_gno_brno'], ORGN_ODNO=row['odno'], ORD_DVSN='03',
            RVSE_CNCL_DVSN_CD='02', ORD_QTY='0', ORD_UNPR='0', QTY_ALL_ORD_YN='Y',
            EXCG_ID_DVSN_CD='KRX'), 'cancel')
