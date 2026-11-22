"""Low-priority portfolio reads. Kiwoom reuses ATS tokens; KIS has separate OAuth. No orders."""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys


def collect(root, previous=None, *, force_annual=False, history_path=None, publish=None, refresh_history=False):
    sys.path.insert(0, str(root))
    from src.config import load_secrets, kiwoom_secrets
    from src.broker.http import AuthTransport, KiwoomError
    from src.broker.kiwoom import KiwoomClient
    from src.broker.kiwoom_us import KiwoomUSLiveClient

    class DashboardTransport(AuthTransport):
        def __init__(self):
            super().__init__('production', timeout=5, minimum_interval=2.5)
            self.names = {}
            self.rates = {}
            self.calls = {}

        def issue_token(self, *args, **kwargs):
            workspace = os.environ.get('MYATS_WORKSPACE')
            if workspace and root == Path(workspace).resolve():
                return super().issue_token(*args, **kwargs)
            raise KiwoomError('Dashboard waits for an existing ATS token')

        def order(self, *args, **kwargs):
            raise KiwoomError('Dashboard orders are disabled')

        def read(self, path, api_id, token, body, **kwargs):
            # Yield during evaluation windows and unresolved orders, before every request.
            import sqlite3
            from contextlib import closing
            from refresh_schedule import protected_reason
            if protected_reason():
                raise KiwoomError('Dashboard yields to scheduled evaluations')
            for filename in ('kiwoom5814_live.sqlite3', 'kiwoom5814_us_live.sqlite3'):
                database = root / 'data' / filename
                if database.exists():
                    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
                        for (raw,) in db.execute("SELECT value FROM system_state WHERE key LIKE 'live_intent:%'"):
                            if json.loads(raw).get('status') in {'SENDING', 'UNCERTAIN', 'ACK', 'PARTIAL', 'CANCEL_SENT', 'PARTIAL_CANCELED'}:
                                raise KiwoomError('Dashboard yields to outstanding orders')
            if api_id not in {'ka00001', 'kt00001', 'kt00018', 'ust21070', 'ust21160', 'ust31301', 'ka10001', 'ka10074', 'ka10073', 'ust21530', 'kt00015', 'kt00007', 'ust21150'}:
                raise KiwoomError('Dashboard endpoint denied')
            self.calls[api_id]=self.calls.get(api_id,0)+1
            result = super().read(path, api_id, token, body, **kwargs)
            from decimal import Decimal
            if api_id in ('kt00018','ust21070'):
                field='tot_prft_rt' if api_id=='kt00018' else 'tot_pl_rt'
                value=Decimal(str(result[field]).replace(',',''))
                if not value.is_finite(): raise ValueError('Invalid account return')
                self.rates['domestic' if api_id=='kt00018' else 'us']=str(value/100)
            rows = result.get('acnt_evlt_remn_indv_tot', result.get('result_list', []))
            for row in rows if isinstance(rows, list) else []:
                symbol = str(row.get('stk_cd', '')).removeprefix('A') if api_id == 'kt00018' else str(row.get('stk_cd', ''))
                name = row.get('stk_nm') or row.get('kor_stk_nm')
                if symbol and name:
                    self.names[symbol] = str(name)
            return result

    secrets = load_secrets(root)
    if not secrets.kiwoom_expected_account:
        raise ValueError('Expected account is missing')
    credentials = kiwoom_secrets(secrets, 'production')
    transport = DashboardTransport()
    domestic = KiwoomClient(credentials, transport=transport)
    overseas = KiwoomUSLiveClient(credentials, expected_account=secrets.kiwoom_expected_account,
                                 authorize=lambda: False, transport=transport)
    try:
        if domestic.account_number() != secrets.kiwoom_expected_account:
            raise ValueError('Account mismatch')
        overview = domestic.account_overview()
        domestic_positions = domestic.holdings()
        foreign_positions = overseas.holdings()
        usd = overseas.d2_estimated_cash_usd()
        cached = previous or {}
        if cached.get('root') != str(root.resolve()):
            cached = {}
        from fx_cache import exchange_rate
        fx, fx_at = exchange_rate(overseas, cached)
        account_at = datetime.now(timezone.utc).isoformat()
        names = dict(cached.get('names', {})) if cached.get('root') == str(root.resolve()) else {}
        names.update(transport.names)
        # Resolve historical sold symbols too; cache them to avoid repeated quote requests.
        try:
            from data_reader import read_market
            recent = read_market(root, 'domestic')['fills']
            missing = list(dict.fromkeys(row['symbol'] for row in recent if row['symbol'] not in names))
            for symbol in missing[:8]:
                try:
                    names[symbol] = domestic.symbol_name(symbol)
                except Exception:
                    break  # A deferred name lookup must not discard the account snapshot.
        except Exception:
            pass
        annual = cached.get('annual_profit', {}) if cached.get('root') == str(root.resolve()) else {}
        today = datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d')
        # Legacy account summary is diagnostic only; the UI totals displayed fills.
        if force_annual:
            try:
                from decimal import Decimal
                evidence=[]
                def history(client, path, api, rows_key, date_key, profit_key, extra, start=None, end=None):
                    client.connect()
                    body={'strt_dt':start or today[:4]+'0101','end_dt':end or today, **extra}
                    rows=[]; seen=set(); key=None; expected=None
                    for _ in range(100):
                        try:
                            result=transport.read(path,api,client._token,body,cont_yn='Y' if key else None,next_key=key)
                        except KiwoomError as error:
                            if api=='ust21530' and not key and '[2000](571758:' in str(error):
                                evidence.append(dict(api=api,start=body['strt_dt'],end=body['end_dt'],no_records=True,broker_code='571758'))
                                return [],Decimal(0)
                            raise
                        if expected is None: expected=Decimal(str(result['rlzt_pl' if api=='ka10074' else 'tot_pl_amt']).replace(',',''))
                        page=result.get(rows_key)
                        if not isinstance(page,list): raise ValueError('Missing history rows')
                        for row in page:
                            if not body['strt_dt']<=str(row[date_key])<=body['end_dt']: raise ValueError('History outside period')
                            value=Decimal(str(row[profit_key]).replace(',',''))
                            if not value.is_finite(): raise ValueError('Invalid profit')
                        rows.extend(page)
                        key=result.get('_next_key')
                        if not key: break
                        if key in seen: raise ValueError('Repeated history page')
                        seen.add(key)
                    else: raise ValueError('Incomplete history')
                    total=sum((Decimal(str(r[profit_key]).replace(',','')) for r in rows),Decimal(0))
                    evidence.append(dict(api=api,start=body['strt_dt'],end=body['end_dt'],rows=len(rows),summary=str(expected),row_sum=str(total),difference=str(expected-total)))
                    if abs(total-expected)>max(Decimal('1'),Decimal(len(rows))): raise ValueError(f'{api} profit summary mismatch')
                    return rows,expected
                dr,dp=history(domestic,'/api/dostk/acnt','ka10074','dt_rlzt_pl','dt','tdy_sel_pl',{})
                import calendar
                ur=[]; up=Decimal(0)
                for year in (int(today[:4]),):
                    for month in (1,4,7,10):
                        start=f'{year}{month:02d}01'
                        if start>today:break
                        end=min(today,f'{year}{month+2:02d}{calendar.monthrange(year,month+2)[1]:02d}')
                        part,value=history(overseas,'/api/us/acnt','ust21530','result_list','sell_dt','pl_amt',{'fc_krw_tp':'1'},start,end)
                        ur.extend(part); up+=value
                annual=dict(complete=True,year=int(today[:4]),start=today[:4]+'0101',through=today,at=datetime.now(timezone.utc).isoformat(),
                            domestic_krw=str(dp),us_krw=str(up),total_krw=str(dp+up),
                            sources=['ka10074','ust21530:KRW'],domestic_days=len(dr),us_records=len(ur),evidence=evidence)
            except Exception as error:
                annual=dict(complete=False,year=int(today[:4]),start=today[:4]+'0101',through=today,error=type(error).__name__, reason=__import__('re').sub(r'[0-9]{8,}', '[redacted]', str(error))[:250])
        rows = []
        for market, holdings in [('domestic', domestic_positions), ('us', foreign_positions)]:
            for item in holdings:
                if item.quantity <= 0:
                    continue
                rows.append(dict(market=market, symbol=item.symbol, name=names.get(item.symbol, item.symbol),
                                 quantity=item.quantity, purchase=str(item.purchase_amount), purchase_krw=str(item.purchase_amount if market=='domestic' else item.purchase_amount_krw), evaluation=str(item.evaluation_amount)))
        result = dict(schema=1, complete=True, root=str(root.resolve()), at=account_at,
                    krw_cash=str(overview.d2_estimated_cash), usd_cash=str(usd), fx=str(fx), fx_at=fx_at, holdings=rows, names=names, evaluation_rates=transport.rates, annual_profit=annual, query_counts=transport.calls)
        # Publish account balances before the potentially long annual-history request.
        if publish:
            publish(result)
        if history_path:
            try:
                from dividend_history import collect_kiwoom
                collect_kiwoom(root, Path(history_path).with_name('kiwoom_dividends.json'),
                               transport, domestic, force=refresh_history)
            except Exception as error:
                result['dividend_error'] = type(error).__name__
        if history_path:
            try:
                from trade_history import collect as collect_history
                collect_history(root, history_path, transport, domestic, overseas, force=refresh_history)
            except Exception as error:
                result['history_error'] = type(error).__name__
        if history_path:
            from profit_history import collect as collect_profit_history
            collect_profit_history(root, Path(history_path).with_name('profit_history.json'), transport, domestic, overseas, force=refresh_history)
        return result
    finally:
        domestic.disconnect()
        overseas.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--force-annual',action='store_true')
    parser.add_argument('--refresh-history', action='store_true')
    args = parser.parse_args()
    target = Path(args.output)
    from concurrent.futures import ThreadPoolExecutor
    def kis_work():
        from kis_snapshot import collect as collect_kis
        from kis_portfolio import KISError
        try:
            return collect_kis(Path(args.root).resolve(), target.with_name('kis_snapshot.json'), force=args.refresh_history)
        except KISError as exc:
            return str(exc) in ('KIS_REQUIRED_FIELD_MISSING', 'KIS_DEFAULT_LIVE_ACCOUNT_DISABLED')
        except Exception:
            return False
    pool = ThreadPoolExecutor(max_workers=1)
    kis_future = pool.submit(kis_work)
    success = False
    try:
        try:
            previous = json.loads(target.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            previous = {}
        def publish(result):
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, target)
        result = collect(Path(args.root).resolve(), previous, force_annual=args.force_annual,
                         history_path=target.with_name('trade_history.json'), publish=publish, refresh_history=args.refresh_history)
        publish(result)
        success = True
    except Exception as error:
        # Do not print broker responses, account IDs, credentials or headers.
        print('SNAPSHOT_UNAVAILABLE:' + type(error).__name__)
    finally:
        kis_success = kis_future.result()
        pool.shutdown(wait=True)
    if not success or not kis_success:
        print('PORTFOLIO_REFRESH_PENDING')
        raise SystemExit(1)
    print('SNAPSHOT_OK')


if __name__ == '__main__':
    main()
