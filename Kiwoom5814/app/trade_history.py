"""Year-to-date broker trade ledger plus unsettled executed orders (read only)."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path

KST = timezone(timedelta(hours=9))


def number(value):
    result = Decimal(str(value).replace(',', '').strip())
    if not result.is_finite():
        raise ValueError('Invalid trade number')
    return float(result)


def side_of(value):
    value = str(value)
    if '취소' in value or '정정' in value:
        return None
    if '매수' in value:
        return 'BUY'
    if '매도' in value:
        return 'SELL'
    return None


def pages(transport, client, api, body, field):
    client.connect()
    rows, seen, key = [], set(), None
    for _ in range(1000):
        try:
            result = transport.read('/api/dostk/acnt' if api.startswith(('kt', 'ka')) else '/api/us/acnt',
                                    api, client._token, body,
                                    cont_yn='Y' if key else None, next_key=key)
        except Exception as error:
            message = str(error)
            if api in ('ust21150', 'ust21530') and not key and '[2000]' in message and '571758' in message:
                return []
            raise
        page = result.get(field)
        if not isinstance(page, list):
            raise ValueError('Missing trade history rows')
        rows.extend(page)
        key = result.get('_next_key')
        if not key:
            return rows
        if key in seen:
            raise ValueError('Repeated history continuation')
        seen.add(key)
    raise ValueError('Incomplete trade history')


def ledger_row(raw, year):
    side = side_of(raw.get('io_tp_nm'))
    if not side:
        raise ValueError('Unrecognized ledger trade side')
    currency = str(raw.get('crnc_cd', '')).strip()
    if currency not in ('KRW', 'USD'):
        raise ValueError('Unsupported trade currency')
    market = 'domestic' if currency == 'KRW' else 'us'
    day = str(raw.get('cntr_dt', '')).strip()
    stamp = datetime.strptime(day, '%Y%m%d').replace(tzinfo=KST)
    if stamp.year != year:
        return None  # Settlement in January can belong to a prior-year fill.
    symbol = str(raw['stk_cd']).strip()
    if market == 'domestic' and len(symbol) == 7:
        symbol = symbol[1:]
    qty, price = number(raw['trde_qty_jwa_cnt']), number(raw['trde_unit'])
    if qty <= 0 or price <= 0:
        raise ValueError('Invalid executed trade')
    return dict(id=f"ledger:{raw['trde_dt']}:{raw['trde_no']}:{market}",
                market=market, symbol=symbol, name=raw.get('stk_nm') or symbol,
                side=side, quantity=qty, price=price, at=stamp.isoformat(),
                trade_date=stamp.date().isoformat(), date_only=True, reason='',
                profit=None, rate=None, source='kt00015', settlement_date=raw['trde_dt'])


def order_row(raw, market, day):
    qty = number(raw.get('cntr_qty', '0'))
    if qty <= 0:
        return None
    side = side_of(raw.get('io_tp_nm') if market == 'domestic' else raw.get('slby_tp_nm'))
    if side is None:
        return None
    symbol = str(raw['stk_cd']).strip()
    if market == 'domestic' and len(symbol) == 7:
        symbol = symbol[1:]
    stamp = datetime.strptime(day, '%Y%m%d').replace(tzinfo=KST)
    # Domestic response gives order/confirmation time, not execution time.
    clock = raw.get('cntr_time') if market == 'us' else None
    date_only = not clock
    if clock:
        clock = str(clock).replace(':', '')
        stamp = datetime.strptime(day + clock, '%Y%m%d%H%M%S').replace(tzinfo=KST)
        # The broker trading date rolls over after the US after-market session.
        if stamp.hour < 12:
            stamp += timedelta(days=1)
    order_id = str(raw['ord_no']).lstrip('0') or '0'
    return dict(id=f'order:{market}:{day}:{order_id}', market=market, symbol=symbol,
                name=raw.get('stk_nm') or raw.get('frgn_stk_nm') or symbol,
                side=side, quantity=qty, price=number(raw['cntr_uv']), at=stamp.isoformat(),
                trade_date=datetime.strptime(day, '%Y%m%d').date().isoformat(),
                date_only=date_only, order_id=order_id, reason='', profit=None, rate=None,
                source='kt00007' if market == 'domestic' else 'ust21150')


def group_key(row):
    return row['market'], row['trade_date'], row['symbol'], row['side']


def combine(ledger, orders):
    """Never count a settled ledger trade again as a recent executed order."""
    groups, recent = defaultdict(list), defaultdict(list)
    for row in ledger:
        groups[group_key(row)].append(row)
    for row in orders:
        recent[group_key(row)].append(row)
    for key, rows in recent.items():
        old = groups.get(key, [])
        old_qty = sum(Decimal(str(r['quantity'])) for r in old)
        new_qty = sum(Decimal(str(r['quantity'])) for r in rows)
        if not old or new_qty >= old_qty:
            groups[key] = rows
    return sorted([r for rows in groups.values() for r in rows], key=lambda r: (r['at'], r['id']), reverse=True)


def realized_row(raw, market):
    domestic = market == 'domestic'
    day = str(raw['dt' if domestic else 'sell_dt']).strip()
    symbol = str(raw['stk_cd']).strip()
    if domestic and len(symbol) == 7:
        symbol = symbol[1:]
    quantity = number(raw['cntr_qty' if domestic else 'sell_qty'])
    if quantity <= 0:
        raise ValueError('Invalid realized quantity')
    return dict(market=market, trade_date=datetime.strptime(day, '%Y%m%d').date().isoformat(),
                symbol=symbol, side='SELL', quantity=quantity,
                price=number(raw['cntr_pric' if domestic else 'avg_sell_uv']),
                profit=number(raw['tdy_sel_pl' if domestic else 'pl_amt']),
                rate=number(raw['pl_rt']) / 100,
                cost=number(raw['buy_uv']) * quantity if domestic else number(raw['buy_amt']),
                profit_source='ka10073' if domestic else 'ust21530:USD')


def collect_profits(transport, domestic, overseas, now, previous):
    import calendar
    today = now.strftime('%Y%m%d')
    periods = {}
    for market, client, step in (('domestic', domestic, 1), ('us', overseas, 3)):
        for month in range(1, now.month + 1, step):
            start = f'{now.year}{month:02d}01'
            last = month + step - 1
            end = min(today, f'{now.year}{last:02d}{calendar.monthrange(now.year, last)[1]:02d}')
            key = market + ':' + start
            cached = previous.get(key, {})
            if end < today and cached.get('through') == end and cached.get('checked') == today:
                periods[key] = cached
                continue
            api = 'ka10073' if market == 'domestic' else 'ust21530'
            field = 'dt_stk_rlzt_pl' if market == 'domestic' else 'result_list'
            body = dict(strt_dt=start, end_dt=end, **({'stk_cd': ''} if market == 'domestic' else {'fc_krw_tp': '0'}))
            raw = pages(transport, client, api, body, field)
            rows = [realized_row(r, market) for r in raw]
            if any(not start <= r['trade_date'].replace('-', '') <= end for r in rows):
                raise ValueError('Realized profit outside period')
            periods[key] = dict(through=end, checked=today, rows=rows)
    return periods


def attach_profits(fills, realized):
    """Match broker day/symbol quantities; never repeat a daily profit per fill."""
    groups, profits = defaultdict(list), defaultdict(list)
    for row in fills:
        groups[group_key(row)].append(row)
    for row in realized:
        profits[group_key(row)].append(row)
    output = []
    unmatched = sum(len(rows) for key, rows in profits.items() if key not in groups)
    for key, rows in groups.items():
        evidence = profits.get(key, [])
        quantity = sum(Decimal(str(r['quantity'])) for r in rows)
        expected = sum(Decimal(str(r['quantity'])) for r in evidence)
        if key[-1] != 'SELL' or not evidence or quantity != expected:
            output.extend(rows)
            if key[-1] == 'SELL':
                unmatched += len(rows)
            continue
        # The broker supplies per-day realized records, sometimes split by cost lot.
        # Retain the original execution when the day has just one display row.
        if len(rows) == 1:
            row = dict(rows[0])
        else:
            row = dict(rows[0], id='realized:' + ':'.join(key), quantity=float(quantity),
                       price=float(sum(Decimal(str(r['price'])) * Decimal(str(r['quantity'])) for r in rows) / quantity),
                       date_only=True, reason='', aggregated=True, execution_count=len(rows))
            row.pop('order_id', None)
        profit = sum(Decimal(str(r['profit'])) for r in evidence)
        cost = sum(Decimal(str(r['cost'])) for r in evidence)
        row.update(profit=float(profit),
                   rate=evidence[0]['rate'] if len(evidence) == 1 else (float(profit / cost) if cost > 0 else None),
                   profit_source=evidence[0]['profit_source'],
                   rate_source='broker' if len(evidence) == 1 else 'net_profit_over_broker_cost',
                   profit_records=len(evidence))
        output.append(row)
    return sorted(output, key=lambda r: (r['at'], r['id']), reverse=True), unmatched


def collect(root, path, transport, domestic, overseas, *, now=None, force=False):
    now = now or datetime.now(KST)
    today, year = now.strftime('%Y%m%d'), now.year
    path = Path(path)
    try:
        old = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        old = {}
    if old.get('root') != str(root.resolve()) or old.get('year') != year:
        old = {}
    if not force and old.get('complete') and old.get('profit_checked') and old.get('through') == today:
        elapsed = (now - datetime.fromisoformat(old['at'])).total_seconds()
        if 0 <= elapsed < 300:
            return old
    try:
        start = f'{year}0101'
        # Re-read recent settlement dates; preserve the earlier complete history.
        if old.get('complete'):
            start = max(start, (datetime.strptime(old['through'], '%Y%m%d') - timedelta(days=14)).strftime('%Y%m%d'))
        raw = pages(transport, domestic, 'kt00015',
                    dict(strt_dt=start, end_dt=today, tp='3', stk_cd='', crnc_cd='',
                         gds_tp='0', frgn_stex_code='', dmst_stex_tp='%', qry_sort_tp='2'),
                    'trst_ovrl_trde_prps_array')
        ledger = {r['id']: r for r in old.get('ledger', []) if r['settlement_date'] < start}
        for item in raw:
            if not start <= item['trde_dt'] <= today:
                raise ValueError('Ledger outside requested period')
            row = ledger_row(item, year)
            if row:
                if row['id'] in ledger and ledger[row['id']] != row:
                    raise ValueError('Conflicting ledger identity')
                ledger[row['id']] = row
        daily = dict(old.get('daily', {}))
        recent_orders = {}
        for offset in range(10):
            day = (now - timedelta(days=offset)).strftime('%Y%m%d')
            if day[:4] != str(year):
                continue
            for market, client in (('domestic', domestic), ('us', overseas)):
                cache_key = market + ':' + day
                if offset > 2 and cache_key in daily:
                    rows = daily[cache_key]
                else:
                    if market == 'domestic':
                        api, field = 'kt00007', 'acnt_ord_cntr_prps_dtl'
                        body = dict(ord_dt=day, qry_tp='1', stk_bond_tp='1', sell_tp='0', stk_cd='', fr_ord_no='', dmst_stex_tp='%')
                    else:
                        api, field = 'ust21150', 'result_list'
                        body = dict(ord_dt=day, query_tp='1', slby_tp='0', stex_tp='', stk_cd='', oppo_trde_tp='%', fr_ord_no='')
                    rows = [r for item in pages(transport, client, api, body, field)
                            if (r := order_row(item, market, day))]
                    daily[cache_key] = rows
                for row in rows:
                    if row['id'] in recent_orders and recent_orders[row['id']] != row:
                        raise ValueError('Conflicting order identity')
                    recent_orders[row['id']] = row
        # Keep past daily executions too, covering settlement publication delays.
        orders = {r['id']: r for rows in daily.values() for r in rows}
        combined = combine(list(ledger.values()), list(orders.values()))
        profit_error = ''
        try:
            profit_periods = collect_profits(transport, domestic, overseas, now, {} if force else old.get('profit_periods', {}))
        except Exception as error:
            profit_periods = old.get('profit_periods', {})
            profit_error = type(error).__name__
        enriched, unmatched = attach_profits(combined, [r for p in profit_periods.values() for r in p['rows']])
        result = dict(schema=1, root=str(root.resolve()), year=year, start=f'{year}0101',
                      through=today, at=now.isoformat(), complete=True, error='',
                      ledger=list(ledger.values()), daily=daily,
                      fills=enriched, profit_periods=profit_periods, profit_complete=unmatched == 0 and not profit_error, profit_unmatched=unmatched,
                      profit_checked=True, profit_error=profit_error,
                      profit_totals={market: str(sum((Decimal(str(r['profit'])) for p in profit_periods.values() for r in p['rows'] if r['market'] == market), Decimal(0))) for market in ('domestic', 'us')})
    except Exception as error:
        result = dict(old, schema=1, root=str(root.resolve()), year=year,
                      error=type(error).__name__, attempt_at=now.isoformat())
        if not old:
            result.update(complete=False, fills=[])
        # The exception type is safe for UI; callers can inspect only a redacted message.
        result['error_detail'] = str(error)[:200] if isinstance(error, ValueError) else type(error).__name__
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)
    return result


def attach_evidence(rows, local):
    """Attach stored grounds only for one unambiguous matching execution."""
    def signature(r):
        return (*group_key(r), Decimal(str(r['quantity'])), Decimal(str(r['price'])))
    evidence, counts = defaultdict(list), defaultdict(int)
    for row in local:
        if row.get('price') is not None:
            evidence[signature(row)].append(row)
    for row in rows:
        counts[signature(row)] += 1
    output = []
    for source in rows:
        row = dict(source)
        candidates = evidence.get(signature(row), [])
        if len(candidates) == 1 and counts[signature(row)] == 1:
            known = candidates[0]
            if not row.get('order_id') or row['order_id'] == known.get('order_id'):
                row['reason'] = known.get('reason', '')
                if not row.get('profit_source'):
                    row.update(profit=known.get('profit'), rate=known.get('rate'))
        output.append(row)
    return output
