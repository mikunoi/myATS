"""ATS telemetry reader. No broker imports, commands, or database writes."""
import json
import math
import sqlite3
import hashlib
from functools import lru_cache
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from refresh_schedule import KST

VERSION = '0.1.31'


@lru_cache(maxsize=8)
def source_version(root):
    files = sorted((Path(root) / 'src').rglob('*.py'))
    if not files:
        return '미확인'
    digest = hashlib.sha256()
    try:
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode('utf-8'))
            digest.update(path.read_bytes())
        return digest.hexdigest()[:8]
    except OSError:
        return '미확인'


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def age(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    except (ValueError, TypeError):
        return None


def json_value(value, fallback):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return fallback


def trade_reason(kind, mode):
    period={'DAILY':'일봉','WEEKLY':'주봉'}.get(mode)
    rules={
        'SELL_RULE_1_BEARISH_CROSS':'일봉 전환선<기준선 역전',
        'SELL_RULE_2_LATEST_ENTRY_3ATR':'일봉 주가<최근 진입가−3ATR',
        'SELL_RULE_3_KIJUN_CURRENT_ATR':'일봉 주가<기준선−ATR',
        'SELL_MISSED_DAILY_CROSS_BELOW_KIJUN':'일봉 역전 지속 · 주가<기준선',
        'SELL_WEEKLY_KIJUN':'주봉 주가<기준선',
        'SELL_WEEKLY_RULE_2_LATEST_ENTRY_2ATR':'주봉 주가<최근 진입가−2ATR',
    }
    if kind.startswith('SELL:'):
        return ' / '.join(rules[k] for k in kind[5:].split(',')) if all(k in rules for k in kind[5:].split(',')) else ''
    if kind=='INITIAL' and period:return period+' 전환선≥기준선 · 진입조건 충족'
    if kind=='RULE3_REENTRY':return '일봉 전환선≥기준선 · 주가≥기준선 재진입'
    if kind=='ADDON' and period:return period+' 추가매수 상승 기준 충족'
    if kind.startswith('MANUAL'):return '수동 거래'
    return ''


def entry_summary(state, quantity):
    initial, addons = numeric(state.get('initial_quantity')), numeric(state.get('addon_count'))
    # Do not classify manual holdings, partial exits, or unreconciled quantities as additions.
    if initial is None or initial <= 0 or addons is None or addons < 0 or not addons.is_integer() or state.get('sell_reason'):
        return None
    if quantity != initial * (1 + addons):
        return None
    return dict(initial=initial, additional=quantity-initial, count=int(addons)+1,
                label=f'{initial:g}+{quantity-initial:g}')


def atr_levels(state, mode, quantity, settings, symbol):
    """Display-only thresholds from reconciled strategy state, in trading currency."""
    if (not settings or mode not in ('DAILY', 'WEEKLY') or not entry_summary(state, quantity)
            or symbol in settings.get('excluded_symbols', ())):
        return None
    price, atr = numeric(state.get('latest_entry_price')), numeric(state.get('latest_entry_atr'))
    multiple = numeric(settings.get('addon_atr_multiple'))
    limit = numeric(settings.get('max_addons_cycle'))
    if price is None or atr is None or price <= 0 or atr <= 0:
        return None
    next_buy = price + atr * multiple if multiple is not None and multiple > 0 else None
    exhausted = limit is not None and numeric(state.get('addon_count')) >= limit
    # Daily sell rule 2 is fixed at 3ATR; weekly exits use Kijun, not 2ATR.
    stop = price - 3 * atr if mode == 'DAILY' else None
    return dict(latest_entry_atr=atr, next_buy=None if exhausted else next_buy, stop=stop,
                addon_status='상한 도달' if exhausted else '',
                stop_status='주봉 기준선 적용' if mode == 'WEEKLY' else '')


def read_market(root, market):
    path = Path(root) / 'data' / ('kiwoom5814_live.sqlite3' if market == 'domestic' else 'kiwoom5814_us_live.sqlite3')
    result = dict(market=market, available=False, status='기록 없음', holdings=[], fills=[], profit=None,
                  profit_pending=0, heartbeat='', account_at='', quarantined=0, states={})
    if not path.is_file():
        return result
    try:
        # mode=ro preserves WAL visibility. Never use immutable=1 on a live WAL DB.
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            # Deliberately avoid credentials and unrestricted state dumps.
            states = dict(db.execute("SELECT key,value FROM system_state WHERE key IN "
                "('control','paper_ws_state','us_connection_state','notion_cash_snapshot','live_block_reason','live_execution_armed','us_execution_armed') "
                "OR key LIKE 'service_last_heartbeat:%' OR key LIKE 'account_last_success:%' "
                "OR key LIKE 'account_sync_error:%' OR key LIKE 'live_quantity:%' "
                "OR key LIKE 'live_purchase:%' OR key LIKE 'symbol_quarantine:%'"))
            strategy = {symbol: (mode, json_value(raw, {})) for symbol, mode, raw in db.execute('SELECT symbol,mode,state_json FROM strategy_state')}
            modes = {symbol: item[0] for symbol, item in strategy.items()}
            intents = [json_value(r[0], {}) for r in db.execute(
                "SELECT value FROM system_state WHERE key LIKE 'live_intent:%'")]
            events = list(db.execute("SELECT event_key,occurred_at,payload_json FROM events "
                "WHERE category='notion_holding_update' AND event_key LIKE 'notion-live:%' ORDER BY occurred_at DESC"))
    except (sqlite3.Error, OSError):
        result['status'] = '기록 조회 실패 · 재시도 중'
        return result
    heartbeat = states.get('service_last_heartbeat:' + market, '')
    account_at = states.get('account_last_success:' + market, '')
    connected = states.get('paper_ws_state' if market == 'domestic' else 'us_connection_state', '')
    fresh = age(heartbeat)
    account_age = age(account_at)
    status = '응답 지연' if fresh is None or not -5 <= fresh <= 180 else '실행 기록 확인'
    if states.get('control') != 'RUNNING':
        status = '중지 상태'
    elif fresh is not None and -5 <= fresh <= 180:
        if states.get('account_sync_error:' + market) or states.get('live_block_reason'):
            status = '점검 필요'
        elif connected == 'IDLE_OFF_SESSION':
            status = '장외 대기'
        elif connected == 'CONNECTED' and account_age is not None and -5 <= account_age <= 180:
            status = '연결 기록 정상'
        else:
            status = '계좌 확인 대기'
    try:
        settings = json.loads((Path(root) / 'config/default.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        settings = {}
    holdings = []
    for key, raw in states.items():
        if not key.startswith('live_quantity:'):
            continue
        symbol, quantity = key.split(':', 1)[1], numeric(raw)
        if quantity is None or quantity <= 0:
            continue
        holdings.append(dict(ats_managed=symbol in strategy, symbol=symbol, name=symbol, quantity=quantity,
                             purchase=numeric(states.get('live_purchase:' + symbol)),
                             mode={'DAILY': '일봉', 'WEEKLY': '주봉'}.get(modes.get(symbol), '미분류'),
                             market=market, at=account_at, entry=entry_summary(strategy.get(symbol, ('', {}))[1], quantity),
                             atr_levels=atr_levels(strategy.get(symbol, ('', {}))[1], modes.get(symbol), quantity, settings, symbol)))
    by_order = {(str(i.get('date')), str(i.get('order_id')), i.get('symbol')): i for i in intents}
    fills, profits, pending = [], [], 0
    for key, stamp, raw in events:
        event = json_value(raw, {})
        intent = by_order.get((str(event.get('date')), str(event.get('order_id')), event.get('symbol')), {})
        # A Notion BUY payload quantity is total holding, not this order's filled quantity.
        filled = numeric(intent.get('filled'))
        if not intent or filled is None or filled <= 0 or intent.get('status') != 'FILLED':
            continue
        profit = numeric(event.get('profit_amount')) if event.get('profit_pending') is False else None
        if event.get('side') == 'SELL':
            if profit is None:
                pending += 1
            else:
                profits.append(profit)
        fills.append(dict(symbol=event.get('symbol', ''), side=event.get('side'), quantity=filled,
                          order_id=str(event.get('order_id','')).lstrip('0'), trade_date=event.get('date',stamp[:10]),
                          at=stamp, profit=profit, rate=numeric(event.get('profit_rate')), market=market,
                          kind=intent.get('kind', event.get('kind','')), mode=intent.get('mode',event.get('mode','')),
                          reason=trade_reason(intent.get('kind',event.get('kind','')),intent.get('mode',event.get('mode',''))),
                          price=numeric(event.get('sell_price') if event.get('side')=='SELL' else event.get('latest_entry_price'))))
    ready = (status in ('연결 기록 정상', '장외 대기') and
             states.get('live_execution_armed' if market == 'domestic' else 'us_execution_armed') == '1')
    result.update(available=True, ready=ready, managed_symbols=list(strategy),
                  operating=(states.get('control') == 'RUNNING' and fresh is not None and -5 <= fresh <= 180
                             and states.get('live_execution_armed' if market == 'domestic' else 'us_execution_armed') == '1'),
                  status=status, control=states.get('control'), holdings=holdings, fills=fills,
                  profit=sum(profits) if profits else None, profit_pending=pending,
                  heartbeat=heartbeat, account_at=account_at,
                  quarantined=sum(bool(v) for k, v in states.items() if k.startswith('symbol_quarantine:')),
                  states={'cash': json_value(states.get('notion_cash_snapshot'), {})})
    return result


def read_dashboard(root, snapshot_path=None):
    data = read_kiwoom_dashboard(root, snapshot_path)
    if snapshot_path:
        from portfolio_merge import merge, profit_views
        data = merge(data, root, snapshot_path)
        data['profit_views'] = profit_views(root, snapshot_path, data.get('fx'))
        from dividend_history import apply as apply_dividends
        data = apply_dividends(data, root, snapshot_path)
        from managed_snapshots import apply as apply_accounts
        data = apply_accounts(data, root)
        from portfolio_merge import broker_evaluation_rates
        data['broker_evaluation_rates'] = broker_evaluation_rates(data)
        return data
    return data


def read_cache(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def cached_symbol_names(cache, root, row_key):
    """Read only display labels, including those from a Windows 5814 backup."""
    source = cache.get('root')
    rows = cache.get(row_key)
    if (cache.get('schema') != 1 or cache.get('complete') is not True
            or not isinstance(source, str) or not source or not isinstance(rows, list)):
        return {}
    windows_source = PureWindowsPath(source)
    migrated = (root.anchor == '/' and windows_source.is_absolute()
                and tuple(part.casefold() for part in windows_source.parts[-2:]) == ('kiwoom5814', 'ats'))
    if not migrated:
        try:
            if Path(source).resolve() != root:
                return {}
        except (OSError, ValueError):
            return {}
    names = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        market, symbol, name = row.get('market'), row.get('symbol'), row.get('name')
        if (market in ('domestic', 'us') and isinstance(symbol, str) and symbol
                and isinstance(name, str) and name.strip() and name.strip() != symbol):
            names.setdefault((market, symbol), name.strip())
    return names


def read_kiwoom_dashboard(root, snapshot_path=None):
    resolved_root = Path(root).resolve()
    markets = [read_market(root, name) for name in ('domestic', 'us')]
    names = {}
    try:
        snapshot = json.loads((Path(root) / 'reports/input/notion_holdings.json').read_text(encoding='utf-8-sig'))
        names = {r['symbol']: r.get('name', r['symbol']) for r in snapshot.get('rows', [])}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    holdings = [h for m in markets for h in m['holdings']]
    cash = markets[0]['states'].get('cash', {})
    fx = numeric(cash.get('usd_krw'))
    for row in holdings:
        row['name'] = names.get(row['symbol'], row['symbol'])
        purchase = row['purchase']
        row['krw'] = (purchase * fx if fx and fx > 0 else None) if row['market'] == 'us' and purchase is not None else purchase
    complete = all(m['available'] for m in markets) and all(h['krw'] is not None for h in holdings)
    total_purchase = sum(h['krw'] for h in holdings) if complete else None
    cash_total = numeric(cash.get('total_krw'))
    # live_quantity covers strategy-managed holdings only. It cannot prove full-account equity.
    capital = None
    for row in holdings:
        row['weight'] = row['krw'] / total_purchase if total_purchase and row['krw'] is not None else None
    current_year = datetime.now(KST).year
    fills = sorted([f for m in markets for f in m['fills'] if f['trade_date'].startswith(str(current_year))], key=lambda f: f['at'], reverse=True)
    for fill in fills:
        fill['name'] = names.get(fill['symbol'], fill['symbol'])
    history_status = dict(year=current_year, complete=False)
    history = account = {}
    if snapshot_path:
        history = read_cache(Path(snapshot_path).with_name('trade_history.json'))
        account = read_cache(Path(snapshot_path))
        try:
            if history.get('schema') == 1 and history.get('year') == current_year and Path(history['root']).resolve() == resolved_root:
                from trade_history import attach_evidence, group_key
                broker_rows = history.get('fills', [])
                if history.get('complete'):
                    merged = attach_evidence(broker_rows, fills)
                    groups = {group_key(row) for row in broker_rows}
                    merged += [row for row in fills if group_key(row) not in groups]
                    fills = sorted(merged, key=lambda row: row['at'], reverse=True)
                history_status = {key:history.get(key) for key in ('year','complete','at','through','error','profit_complete','profit_unmatched')}
        except (OSError, ValueError, TypeError, KeyError):
            pass
    cached_names = cached_symbol_names(history, resolved_root, 'fills')
    cached_names.update(cached_symbol_names(account, resolved_root, 'holdings'))
    for row in (*holdings, *fills):
        if not row.get('name') or row['name'] == row['symbol']:
            row['name'] = cached_names.get((row['market'], row['symbol']), row['symbol'])
    result = dict(markets=markets, holdings=holdings, fills=fills, trade_history=history_status, capital=capital, system_version=source_version(str(resolved_root)),
                managed_capital=total_purchase + cash_total if total_purchase is not None and cash_total is not None else None,
                cash=cash_total, cash_at=cash.get('at', ''), fx=fx,
                full_account=False, evaluation_rates={},
                read_at=datetime.now().astimezone().isoformat(timespec='seconds'))
    if snapshot_path:
        from profit_history import read as read_profit_history
        result['profit_history'] = read_profit_history(root, Path(snapshot_path).with_name('profit_history.json'), fx)
    if snapshot_path:
        try:
            snapshot = account
            snapshot_age = age(snapshot.get('at'))
            if (snapshot.get('schema') != 1 or snapshot.get('complete') is not True
                    or Path(snapshot['root']).resolve() != resolved_root
                    or snapshot_age is None or not -5 <= snapshot_age <= 86400):
                return result
            fx = numeric(snapshot['fx'])
            krw, usd = numeric(snapshot['krw_cash']), numeric(snapshot['usd_cash'])
            if fx is None or fx <= 0 or krw is None or usd is None:
                return result
            result['profit_history'] = read_profit_history(root, Path(snapshot_path).with_name('profit_history.json'), fx)
            modes = {(h['market'], h['symbol']): h['mode'] for h in holdings}
            local = {(h['market'], h['symbol']): h for h in holdings}
            full = []
            seen = set()
            for source in snapshot['holdings']:
                row = dict(source)
                identity = (row['market'], row['symbol'])
                if identity in seen or row['market'] not in ('domestic', 'us'):
                    return result
                seen.add(identity)
                for field in ('purchase', 'evaluation', 'quantity'):
                    row[field] = numeric(row[field])
                    if row[field] is None or row[field] < 0:
                        return result
                row['book_krw'] = numeric(row.get('purchase_krw')) if 'purchase_krw' in row else row['purchase'] * (fx if row['market'] == 'us' else 1)
                row['krw'] = row['purchase'] * (fx if row['market'] == 'us' else 1)
                if row['krw'] is None or row['krw'] < 0: return result
                if row['book_krw'] is None or row['book_krw'] < 0: return result
                row['evaluation_krw'] = row['evaluation'] * (fx if row['market'] == 'us' else 1)
                evidence = local.get(identity, {})
                row['entry'] = evidence.get('entry') if evidence.get('quantity') == row['quantity'] else None
                row['atr_levels'] = evidence.get('atr_levels') if evidence.get('quantity') == row['quantity'] else None
                market_record=next((m for m in markets if m.get('market')==row['market']),None)
                row['ats_managed'] = (row['symbol'] in market_record.get('managed_symbols',[])) if market_record and market_record.get('available') else None
                if evidence.get('ats_managed'):row['ats_managed']=True
                row['mode'] = modes.get(identity, '전략')
                row['at'] = snapshot['at']
                if not row.get('name') or row['name'] == row['symbol']:
                    row['name'] = cached_names.get(identity, row['symbol'])
                full.append(row)
            evaluation_total = sum(h['evaluation_krw'] for h in full)
            total = sum(h['krw'] for h in full)
            for row in full:
                row['weight'] = row['krw'] / total if total else None
                row['evaluation_weight'] = row['evaluation_krw'] / evaluation_total if evaluation_total else None
            rates = {key:numeric(snapshot.get('evaluation_rates',{}).get(key)) for key in ('domestic','us')}
            # The total and the visible history share exactly the same merged rows.
            from decimal import Decimal
            sales = [row for row in result['fills'] if row['side'] == 'SELL']
            missing = sum(numeric(row.get('profit')) is None for row in sales)
            domestic_profit = sum((Decimal(str(row['profit'])) for row in sales
                                  if row['market'] == 'domestic' and numeric(row.get('profit')) is not None), Decimal(0))
            us_profit = sum((Decimal(str(row['profit'])) for row in sales
                            if row['market'] == 'us' and numeric(row.get('profit')) is not None), Decimal(0))
            total_profit = domestic_profit + us_profit * Decimal(str(fx))
            result['annual_profit'] = dict(
                complete=bool(history_status.get('complete') and history_status.get('profit_complete') and not missing),
                year=current_year, source='displayed_trade_history', domestic_krw=str(domestic_profit),
                us_usd=str(us_profit), us_krw=str(us_profit * Decimal(str(fx))),
                total_krw=str(total_profit), fx=fx, fx_at=snapshot.get('fx_at', ''),
                sales=len(sales), missing=missing, at=history_status.get('at'),
                stale=bool(history_status.get('error') or (age(snapshot.get('fx_at')) or 0) >= 3600))
            result.update(holdings=full, capital=sum(h['book_krw'] for h in full) + krw + usd * fx, cash=krw + usd * fx,
                          cash_at=snapshot['at'], fx=fx, fx_at=snapshot.get('fx_at',''), full_account=True, evaluation_rates=rates)
            full_names = dict(snapshot.get('names', {}))
            holding_names = {(h['market'], h['symbol']): h['name'] for h in full if h['name'] != h['symbol']}
            for fill in result['fills']:
                name = holding_names.get((fill['market'], fill['symbol'])) or full_names.get(fill['symbol'])
                if isinstance(name, str) and name.strip() and name.strip() != fill['symbol']:
                    fill['name'] = name.strip()
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return result
