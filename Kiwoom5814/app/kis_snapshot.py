"""Independent KIS read-only cache; never modifies Kiwoom trading state."""
import calendar
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3

from kis_portfolio import PortfolioClient, KISError
from refresh_schedule import KST, protected_reason


def number(value):
    n = Decimal(str(value).strip().replace(',', ''))
    if not n.is_finite():
        raise ValueError('Invalid number')
    return float(n)


def save(path, data):
    temp = Path(path).with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)


def guard(root):
    if protected_reason():
        raise KISError('KIS_DEFERRED_ATS_EVALUATION')
    for name in ('kiwoom5814_live.sqlite3', 'kiwoom5814_us_live.sqlite3'):
        p = root / 'data' / name
        if not p.exists():
            continue
        with closing(sqlite3.connect(p.as_uri()+'?mode=ro', uri=True, timeout=.1)) as db:
            for (raw,) in db.execute("SELECT value FROM system_state WHERE key LIKE 'live_intent:%'"):
                if json.loads(raw).get('status') in {'SENDING','UNCERTAIN','ACK','PARTIAL','CANCEL_SENT','PARTIAL_CANCELED'}:
                    raise KISError('KIS_DEFERRED_ATS_ORDER')


def one(value):
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError('Missing summary')
    return value


def balances(client):
    domestic = client.balance('domestic')
    overseas = client.balance('us')
    present, more = client.read('present', dict(WCRC_FRCR_DVSN_CD='02', NATN_CD='000',
                                                TR_MKET_CD='00', INQR_DVSN_CD='00'))
    # This endpoint has no cursor. Never accept an incomplete account silently.
    if more in ('M', 'F'):
        raise KISError('KIS_PRESENT_CONTINUATION_UNSUPPORTED')
    rows = []
    for market, response in (('domestic', domestic), ('us', overseas)):
        for raw in response['output1']:
            qty = number(raw['hldg_qty' if market == 'domestic' else 'ovrs_cblc_qty'])
            if qty <= 0:
                continue
            foreign = market == 'us'
            if foreign and raw.get('tr_crcy_cd') != 'USD':
                raise KISError('KIS_UNSUPPORTED_HOLDING_CURRENCY')
            symbol = str(raw['ovrs_pdno' if foreign else 'pdno']).strip()
            rows.append(dict(broker='kis', market=market, symbol=symbol,
                name=raw.get('ovrs_item_name') or raw.get('prdt_name') or symbol, quantity=qty,
                purchase=number(raw['frcr_pchs_amt1' if foreign else 'pchs_amt']),
                evaluation=number(raw['ovrs_stck_evlu_amt' if foreign else 'evlu_amt'])))
    # Present-balance KRW summary repeats domestic cash: do not add it again.
    krw = number(one(domestic['output2'])['prvs_rcdl_excc_amt'])
    currency_rows = present.get('output2')
    if not isinstance(currency_rows, list):
        raise ValueError('Missing currency rows')
    usd = 0.0
    for raw in currency_rows:
        currency = str(raw.get('crcy_cd', '')).strip()
        cash = number(raw['frcr_dncl_amt_2'])
        if currency == 'USD':
            usd += cash
        elif currency != 'KRW' and cash != 0:
            raise KISError('KIS_UNSUPPORTED_CASH_CURRENCY')
    # Detect assets outside the domestic/US scope rather than omit them from an alleged total.
    for raw in present.get('output1', []):
        if number(raw.get('cblc_qty13', 0)) > 0 and raw.get('buy_crcy_cd') != 'USD':
            raise KISError('KIS_UNSUPPORTED_HOLDING_CURRENCY')
    margin, more = client.read('cash', {})
    if more in ('M','F') or not isinstance(margin.get('output'),list):
        raise KISError('KIS_CASH_ROWS_INCOMPLETE')
    us_cash = [r for r in margin['output'] if str(r.get('natn_name','')).strip()=='미국']
    if len(us_cash)!=1:
        raise KISError('KIS_US_CASH_AMBIGUOUS')
    m=us_cash[0]
    deposit=number(m['frcr_dncl_amt1'])
    if abs(deposit-usd)>.02:
        raise KISError('KIS_CASH_RECONCILIATION_PENDING')
    if number(m['frcr_rcvb_amt'])!=0:
        raise KISError('KIS_CASH_RECEIVABLE_REVIEW_REQUIRED')
    # Match trade-date holdings with cash after outstanding settlements.
    # Never add integrated order buying power: it can already include KRW cash.
    usd=deposit+number(m['ustl_sll_amt'])-number(m['ustl_buy_amt'])
    return dict(at=datetime.now(KST).isoformat(), complete=True, holdings=rows,
                krw_cash=krw, usd_cash=usd, cash_basis='KRW_D2_USD_AFTER_SETTLEMENT')


def profit_rows(client, market, start, end):
    foreign = market == 'us'
    params = dict(INQR_STRT_DT=start, INQR_END_DT=end, PDNO='')
    params.update(dict(OVRS_EXCG_CD='NASD', NATN_CD='', CRCY_CD='USD', WCRC_FRCR_DVSN_CD='01')
                  if foreign else dict(SORT_DVSN='02', CBLC_DVSN='00'))
    raw, summary = client.pages(market+'_profit', params, suffix='200' if foreign else '100')
    rows = []
    for r in raw:
        qty = number(r['slcl_qty' if foreign else 'sll_qty'])
        if qty <= 0:
            continue
        day = str(r['trad_day' if foreign else 'trad_dt']).strip()
        if not start <= day <= end:
            raise ValueError('Profit outside period')
        rows.append(dict(market=market, symbol=str(r['ovrs_pdno' if foreign else 'pdno']).strip(),
            trade_date=datetime.strptime(day, '%Y%m%d').date().isoformat(), side='SELL', quantity=qty,
            profit=number(r['ovrs_rlzt_pfls_amt' if foreign else 'rlzt_pfls']),
            price=number(r['avg_sll_unpr' if foreign else 'sll_pric']),
            cost=number(r['frcr_pchs_amt1']) if foreign else number(r['pchs_unpr'])*qty,
            rate=number(r['pftrt' if foreign else 'pfls_rt'])/100,
            profit_source='KIS:'+market))
    # The domestic summary includes fees on buy-only rows as well.
    total = sum(Decimal(str(number(r['ovrs_rlzt_pfls_amt' if foreign else 'rlzt_pfls']))) for r in raw)
    expected = number(one(summary)['ovrs_rlzt_pfls_tot_amt' if foreign else 'tot_rlzt_pfls'])
    if abs(float(total)-expected) > (0.02 if foreign else 1):
        raise KISError('KIS_PROFIT_SUMMARY_MISMATCH')
    return rows, str(total)


def order_rows(client, market, start, end, now):
    foreign = market == 'us'
    if foreign:
        raw, _ = client.pages('us_orders', dict(PDNO='%', ORD_STRT_DT=start, ORD_END_DT=end,
            SLL_BUY_DVSN='00', CCLD_NCCS_DVSN='01', OVRS_EXCG_CD='NASD', SORT_SQN='DS',
            ORD_DT='', ORD_GNO_BRNO='', ODNO=''), field='output', suffix='200')
    else:
        # Split at the calendar three-month boundary; each range has the required TR.
        month_index = now.year*12 + now.month-1-3
        y, m = divmod(month_index, 12)
        boundary = now.replace(year=y, month=m+1, day=min(now.day, calendar.monthrange(y,m+1)[1]))
        split = boundary.strftime('%Y%m%d')
        raw = []
        for a,b,name in ((start,min(end,(boundary-timedelta(days=1)).strftime('%Y%m%d')),'domestic_orders_old'),
                         (max(start,split),end,'domestic_orders')):
            if a > b:
                continue
            page, _ = client.pages(name, dict(INQR_STRT_DT=a, INQR_END_DT=b, SLL_BUY_DVSN_CD='00',
                PDNO='', CCLD_DVSN='01', INQR_DVSN='01', INQR_DVSN_3='00', ORD_GNO_BRNO='',
                ODNO='', INQR_DVSN_1='', EXCG_ID_DVSN_CD='ALL'))
            raw.extend(page)
    result = {}
    for r in raw:
        qty = number(r['ft_ccld_qty' if foreign else 'tot_ccld_qty'])
        if qty <= 0:
            continue
        day = str(r['ord_dt']).strip()
        if not start <= day <= end:
            raise ValueError('Order outside period')
        if foreign and r.get('tr_crcy_cd') != 'USD':
            raise ValueError('Unsupported trade currency')
        side = {'01':'SELL','02':'BUY'}.get(r['sll_buy_dvsn_cd'])
        if not side:
            raise ValueError('Unrecognized side')
        stamp = datetime.strptime(day,'%Y%m%d').replace(tzinfo=KST)
        key = ':'.join(('kis',market,day,str(r['ord_gno_brno']),str(r['odno'])))
        price = number(r['ft_ccld_unpr3']) if foreign else number(r['tot_ccld_amt'])/qty
        row = dict(id=key,broker='kis',market=market,symbol=str(r['pdno']).strip(),
            name=r.get('prdt_name') or str(r['pdno']),side=side,quantity=qty,price=price,
            at=stamp.isoformat(),trade_date=stamp.date().isoformat(),date_only=True,
            reason='',profit=None,rate=None,source='KIS:orders')
        if key in result and result[key] != row:
            raise ValueError('Conflicting order identity')
        result[key] = row
    return list(result.values())


def collect(root, path, *, force=False):
    """Publish balances first, then progressively persist independent history months."""
    root, path = Path(root).resolve(), Path(path)
    client = PortfolioClient(root)
    client.guard = lambda: guard(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        else:
            import fcntl
        try:
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        try:
            try:
                old = json.loads(path.read_text(encoding='utf-8'))
                if old.get('identity') != client.identity or old.get('root') != str(root):
                    old = {}
            except (OSError, ValueError):
                old = {}
            now = datetime.now(KST); today = now.strftime('%Y%m%d')
            data = dict(schema=1, root=str(root), identity=client.identity, enabled=True,
                        balance=old.get('balance'), periods=old.get('periods',{}),
                        orders=old.get('orders',{}) if old.get('year')==now.year else {},
                        year=now.year, through=today, attempt_at=now.isoformat(), error='', history_error='')
            try:
                data['balance'] = balances(client)
            except Exception as exc:
                data['error'] = str(exc) if isinstance(exc,KISError) else type(exc).__name__
                save(path,data)
                return False
            save(path,data)
            # Current-year orders and P/L before historical popup backfill.
            for year in range(now.year,2023,-1):
                for month in range(now.month if year==now.year else 12,0,-1):
                    start=f'{year}{month:02d}01'
                    end=min(today,f'{year}{month:02d}{calendar.monthrange(year,month)[1]:02d}')
                    for market in ('domestic','us'):
                        key=market+':'+start[:6]
                        try:
                            if year==now.year:
                                cached=data['orders'].get(key,{})
                                if force or end==today or cached.get('checked')!=today or cached.get('through')!=end:
                                    rows=order_rows(client,market,start,end,now)
                                    data['orders'][key]=dict(rows=rows,through=end,checked=today,complete=True)
                                    save(path,data)
                            cached=data['periods'].get(key,{})
                            if force or end==today or cached.get('checked')!=today or cached.get('through')!=end:
                                rows,total=profit_rows(client,market,start,end)
                                data['periods'][key]=dict(rows=rows,profit=total,through=end,checked=today,
                                    complete=True,at=datetime.now(KST).isoformat())
                                save(path,data)
                        except Exception as exc:
                            data['history_error']=str(exc) if isinstance(exc,KISError) else type(exc).__name__
                            data['failed_period']=key
                            save(path,data)
                            return False
            data['history_at']=datetime.now(KST).isoformat()
            save(path,data)
            return True
        finally:
            if os.name == "nt":
                lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
