"""Account-scoped read-only refresh and display aggregation."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from api_accounts import accounts, account_dir, env_values, kis_env_accounts, read_json, save_json
from kis_managed import ManagedKIS
from kis_portfolio import KISError
from kis_snapshot import guard, number, one, order_rows
from refresh_schedule import KST, automatic_due


def snapshot_path(root, profile):
    return account_dir(root, profile) / 'snapshot.json'


def cached(root, profile):
    try:
        value = read_json(snapshot_path(root, profile))
        if isinstance(value, dict) and value.get('credentials') == profile.credentials_id:
            return value
    except (OSError, ValueError):
        pass
    return {}


def domestic_balance(client):
    response = client.balance('domestic')
    summary = one(response['output2'])
    rows, seen = [], set()
    for row in response['output1']:
        quantity = number(row['hldg_qty'])
        if quantity == 0:
            continue
        symbol = str(row['pdno']).strip()
        purchase, evaluation = number(row['pchs_amt']), number(row['evlu_amt'])
        if not symbol or symbol in seen or quantity < 0 or min(purchase, evaluation) < 0:
            raise ValueError('Invalid balance')
        seen.add(symbol)
        rows.append(dict(broker='kis', market='domestic', symbol=symbol,
                         name=row.get('prdt_name') or symbol, quantity=quantity,
                         purchase=purchase, evaluation=evaluation))
    return dict(at=datetime.now(KST).isoformat(), cash=number(summary['prvs_rcdl_excc_amt']),
                holdings=rows)


def collect(root, profile, *, client=None):
    root = Path(root).resolve()
    client = ManagedKIS(root, profile) if client is None else client
    client.guard = lambda: guard(root)
    path = snapshot_path(root, profile)
    state = dict(cached(root, profile), credentials=profile.credentials_id,
                 attempt=datetime.now(KST).isoformat())
    try:
        state.update(balance=domestic_balance(client), balance_error='')
    except (KISError, OSError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
        state['balance_error'] = str(exc) if isinstance(exc, KISError) else type(exc).__name__
        save_json(path, state)
        return state
    save_json(path, state)
    now = datetime.now(KST)
    try:
        start = (now - timedelta(days=30)).strftime('%Y%m%d')
        fills = order_rows(client, 'domestic', start, now.strftime('%Y%m%d'), now)
        state.update(fills=fills, fills_at=now.isoformat(), fills_error='')
    except (KISError, OSError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
        state['fills_error'] = str(exc) if isinstance(exc, KISError) else type(exc).__name__
    save_json(path, state)
    return state


def refresh_due(root, frequent, force=False):
    if not Path(root).is_dir():
        return
    for profile in visible_accounts(root):
        state = cached(root, profile)
        if force or automatic_due(state.get('attempt'), frequent):
            try:
                collect(root, profile)
            except (OSError, ValueError, KISError):
                continue  # An unusable profile directory must not starve other accounts.


def visible_accounts(root, env_profiles=None):
    if env_profiles is None:
        env_profiles, _ = kis_env_accounts(env_values(root))
    identities = {p.identity for p in env_profiles.values()}
    managed = [p for slot, p in env_profiles.items() if slot or p.mode == 'demo']
    managed.extend(p for p in accounts(root) if p.enabled and p.identity not in identities)
    return tuple(managed)


def return_rate(rows):
    cost = sum((Decimal(str(r['krw'])) for r in rows), Decimal(0))
    value = sum((Decimal(str(r['evaluation_krw'])) for r in rows), Decimal(0))
    return float((value - cost) / cost) if cost > 0 else None


def apply(data, root):
    """Extend an existing dashboard with validated recent KIS account reads."""
    from data_reader import age, numeric
    try:
        values = env_values(root)
        env_profiles, _ = kis_env_accounts(values)
        profiles = visible_accounts(root, env_profiles)
    except (OSError, ValueError):
        data['portfolio_note'] += ' · API 연결 설정 확인 필요'
        return data
    default_kis = env_profiles.get('')
    kiwoom_alias = values.get('KIWOOM_ALIAS') or '키움 5814'
    for row in data['holdings'] + data['fills']:
        if row.get('broker') == 'kiwoom':
            row.setdefault('account_alias', kiwoom_alias)
        elif default_kis:
            row.setdefault('account_alias', default_kis.alias)
    live = [p for p in profiles if p.mode == 'live']
    if not live:
        return data
    base_cash, base_capital = data.get('cash'), data.get('capital')
    cash_by_broker = data['cash_by_broker']
    known_cash = [v for v in cash_by_broker.values() if v is not None]
    expected = len(cash_by_broker) + len(live)
    present = len(known_cash)
    new_cash, new_cost = 0, 0
    stale = any((age(data.get(key)) or 0) >= 3600 for key in ('cash_at','kis_at'))
    data['managed_kis_ready'] = False
    for profile in live:
        state = cached(root, profile)
        balance = state.get('balance') or {}
        elapsed = age(balance.get('at'))
        cash = numeric(balance.get('cash'))
        if elapsed is not None and -5 <= elapsed <= 86400 and cash is not None:
            rows = []
            try:
                for row in balance['holdings']:
                    cost, value, quantity = (numeric(row[k]) for k in ('purchase','evaluation','quantity'))
                    if None in (cost, value, quantity) or min(cost, value) < 0 or quantity <= 0:
                        raise ValueError('Invalid cached holding')
                    rows.append(dict(row, account_source=profile.identity, account_alias=profile.alias,
                        mode='—', at=balance['at'], krw=cost, book_krw=cost, evaluation_krw=value,
                        entry=None, atr_levels=None, weight=None, evaluation_weight=None))
            except (ValueError, KeyError, TypeError):
                stale = True
            else:
                data['holdings'].extend(rows)
                known_cash.append(cash)
                new_cash += cash
                new_cost += sum(r['krw'] for r in rows)
                stale |= bool(state.get('balance_error') or elapsed >= 3600)
                data['managed_kis_ready'] = True
                data['managed_kis_at'] = balance['at']
                present += 1
        else:
            stale = True
        fills_age = age(state.get('fills_at'))
        if fills_age is not None and -5 <= fills_age <= 86400:
            for fill in state.get('fills', []):
                if isinstance(fill, dict) and all(fill.get(k) for k in ('id','at','symbol')):
                    data['fills'].append(dict(fill, id=profile.identity+':'+fill['id'],
                                              account_source=profile.identity, account_alias=profile.alias))
    data['fills'].sort(key=lambda row: row['at'], reverse=True)
    data['trade_history'].update(complete=False, includes_recent_accounts=True)
    if data.get('annual_profit'):
        data['annual_profit'].update(trading_complete=False, complete=False)
    combined_history = data.get('profit_views', {}).get('combined', {})
    for key in ('months', 'years'):
        combined_history[key] = [
            dict(row, complete=False, total=None, domestic=None, us=None)
            for row in combined_history.get(key, [])
        ]
    if present == expected and base_cash is not None and base_capital is not None:
        data['cash'] = base_cash + new_cash
        data['capital'] = base_capital + new_cost + new_cash
        cash_by_broker['kis'] = (cash_by_broker.get('kis') or 0) + new_cash
    else:
        data.update(cash=None, capital=None, chart_rows=[])
    available = [r for r in data['holdings'] if data.get('full_account') or r.get('broker') != 'kiwoom']
    data['registered_totals'] = dict(cash=sum(known_cash) if known_cash else None,
                                     rate=return_rate(available), present=present,
                                     expected=expected, stale=stale)
    data['portfolio_note'] += f' · API 계좌 {present}/{expected} 확인'
    return data
