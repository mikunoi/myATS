"""Read-only monthly broker realized P/L archive, starting January 2024."""
import calendar
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from trade_history import KST, pages, realized_row


def months(now):
    return [(y, m) for y in range(2024, now.year + 1)
            for m in range(1, (now.month if y == now.year else 12) + 1)]


def collect(root, path, transport, domestic, overseas, *, force=False, now=None):
    now = now or datetime.now(KST)
    today = now.strftime('%Y%m%d')
    path = Path(path)
    try:
        old = json.loads(path.read_text(encoding='utf-8'))
        if old.get('root') != str(root.resolve()) or old.get('schema') != 1:
            old = {}
    except (OSError, ValueError):
        old = {}
    periods = old.get('periods', {})
    refresh_started = old.get('refresh_started') if old.get('refresh_active') else None
    refresh_started = refresh_started or now.isoformat()
    result = dict(schema=1, root=str(root.resolve()), start='20240101', through=today,
                  attempt_at=now.isoformat(), periods=periods, error='', complete=False,
                  refresh_started=refresh_started, refresh_active=True)

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, path)

    # Current months first, so a slow historical backfill cannot delay today's figures.
    for year, month in reversed(months(now)):
        start = f'{year}{month:02d}01'
        end = min(today, f'{year}{month:02d}{calendar.monthrange(year, month)[1]:02d}')
        for market, client in (('domestic', domestic), ('us', overseas)):
            key = market + ':' + start[:6]
            cached = periods.get(key, {})
            if force and cached.get('through') == end and cached.get('at', '') >= refresh_started:
                continue
            if not force and end < today and cached.get('through') == end and cached.get('checked') == today:
                continue
            try:
                api = 'ka10073' if market == 'domestic' else 'ust21530'
                body = dict(strt_dt=start, end_dt=end,
                            **({'stk_cd': ''} if market == 'domestic' else {'fc_krw_tp': '0'}))
                raw = pages(transport, client, api, body,
                            'dt_stk_rlzt_pl' if market == 'domestic' else 'result_list')
                rows = [realized_row(r, market) for r in raw]
                if any(not start <= r['trade_date'].replace('-', '') <= end for r in rows):
                    raise ValueError('Profit outside requested month')
                periods[key] = dict(through=end, checked=today, at=datetime.now(KST).isoformat(),
                                    profit=str(sum((Decimal(str(r['profit'])) for r in rows), Decimal(0))),
                                    rows=rows, source=api, complete=True)
                save()  # Preserve progress when ATS protection interrupts the next request.
            except Exception as error:
                result.update(error=type(error).__name__, failed_period=key)
                save()
                return result
    result.update(complete=True, refresh_active=False, at=datetime.now(KST).isoformat())
    save()
    return result


def read(root, path, fx, now=None):
    now = now or datetime.now(KST)
    result = dict(months=[], years=[], complete=False, at='', error='')
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('schema') != 1 or data.get('root') != str(Path(root).resolve()):
            return result
        periods = data.get('periods', {})
        result.update(at=data.get('at', data.get('attempt_at', '')), error=data.get('error', ''))
        rate = Decimal(str(fx)) if fx is not None else None
        if rate is not None and (not rate.is_finite() or rate <= 0):
            rate = None
        for year, month in months(now):
            key = f'{year}{month:02d}'
            end = min(now.strftime('%Y%m%d'), key + f'{calendar.monthrange(year, month)[1]:02d}')
            d, u = (periods.get(m + ':' + key, {}) for m in ('domestic', 'us'))
            valid = all(p.get('complete') and p.get('through') == end for p in (d, u))
            krw = Decimal(d['profit']) if d.get('complete') else None
            usd = Decimal(u['profit']) if u.get('complete') else None
            if any(v is not None and not v.is_finite() for v in (krw, usd)):
                raise ValueError('Invalid profit amount')
            total = krw + usd * rate if valid and krw is not None and usd is not None and rate else None
            result['months'].append(dict(period=f'{year}-{month:02d}', domestic=krw, us=usd,
                                         total=total, complete=total is not None,
                                         at=min((p.get('at', '') for p in (d, u)), default='')))
        for year in range(2024, now.year + 1):
            rows = [r for r in result['months'] if r['period'].startswith(str(year))]
            complete = bool(rows) and all(r['complete'] for r in rows)
            result['years'].append(dict(period=str(year), complete=complete,
                **{k: sum((r[k] for r in rows), Decimal(0)) if complete else None for k in ('domestic', 'us', 'total')},
                at=min((r['at'] for r in rows), default='')))
        result['complete'] = bool(data.get('complete')) and all(r['complete'] for r in result['months'])
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        result['error'] = '자료 확인 필요'
    return result
