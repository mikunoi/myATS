"""Cash dividends only. Entitlements and forecast dividends are not receipts."""
import calendar
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from refresh_schedule import KST
from kis_snapshot import save


def source_id(root, broker):
    values={}
    for line in (Path(root)/'.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1);values[k.strip().lower()]=v.strip().strip('"').strip("'")
    keys=('kiwoom_expected_account',) if broker=='kiwoom' else ('kis_cano','kis_acnt_prdt_cd')
    parts=[values.get(k,'') for k in keys]
    if not all(parts):raise ValueError('Account identity unavailable')
    return hashlib.sha256((broker+'|'+ '|'.join(parts)).encode()).hexdigest()


def decimal(value):
    n=Decimal(str(value).replace(',','').strip())
    if not n.is_finite():raise ValueError('Invalid dividend amount')
    return n


def collect_kiwoom(root,path,transport,client,force=False):
    from trade_history import pages
    root=Path(root).resolve();path=Path(path);now=datetime.now(KST);today=now.strftime('%Y%m%d')
    identity=source_id(root,'kiwoom')
    try:
        result=json.loads(path.read_text(encoding='utf-8'))
        if result.get('identity')!=identity or result.get('root')!=str(root):result={}
    except (OSError,ValueError):result={}
    result.update(schema=1,root=str(root),identity=identity,error='',attempt_at=now.isoformat())
    periods=result.setdefault('periods',{})
    for year in (now.year,now.year-1):
        for month in range(now.month if year==now.year else 12,0,-1):
            key=f'{year}{month:02d}';start=key+'01'
            end=min(today,key+f'{calendar.monthrange(year,month)[1]:02d}')
            cached=periods.get(key,{})
            if not force and cached.get('complete') and end<today and cached.get('checked')==today and cached.get('through')==end:
                continue
            try:
                raw=pages(transport,client,'kt00015',dict(strt_dt=start,end_dt=end,tp='0',stk_cd='',
                    crnc_cd='',gds_tp='0',frgn_stex_code='',dmst_stex_tp='%',qry_sort_tp='2'),
                    'trst_ovrl_trde_prps_array')
                rows={}; unresolved=0
                for r in raw:
                    label=' '.join(str(r.get(k,'')) for k in ('rmrk_nm','trde_kind_nm','io_tp_nm'))
                    if not any(word in label for word in ('배당','분배')):continue
                    # Stock dividends and pending rights are not cash income.
                    direction=str(r.get('io_tp_nm','')).strip()
                    if direction in ('입고','출고'):continue
                    currency=str(r.get('crnc_cd','')).strip()
                    if currency not in ('KRW','USD') or direction not in ('입금','출금'):
                        unresolved+=1;continue
                    value=decimal(r['exct_amt' if currency=='KRW' else 'fc_exct_amt'])
                    # A reversal needs explicit outgoing direction; never guess its sign.
                    if any(w in label for w in ('취소','정정')) and direction!='출금':
                        unresolved+=1;continue
                    net=abs(value)*(1 if direction=='입금' else -1)
                    day=str(r['trde_dt'])
                    if not start<=day<=end:raise ValueError('Dividend outside period')
                    row=dict(id=day+':'+str(r['trde_no']),date=day,currency=currency,net=str(net),
                             symbol=str(r.get('stk_cd','')).strip(),name=r.get('stk_nm',''),
                             kind='dividend_receipt' if direction=='입금' else 'dividend_adjustment')
                    if row['id'] in rows and rows[row['id']]!=row:raise ValueError('Duplicate dividend conflict')
                    rows[row['id']]=row
                values=list(rows.values())
                periods[key]=dict(through=end,checked=today,at=datetime.now(KST).isoformat(),
                    complete=not unresolved,unresolved=unresolved,rows=values,
                    krw=str(sum((decimal(r['net']) for r in values if r['currency']=='KRW'),Decimal(0))),
                    usd=str(sum((decimal(r['net']) for r in values if r['currency']=='USD'),Decimal(0))))
                save(path,result)
            except Exception as exc:
                result['error']=type(exc).__name__;result['failed_period']=key
                save(path,result);return
    result['at']=datetime.now(KST).isoformat();save(path,result)


def apply(data,root,snapshot_path):
    """Enrich broker views without treating a missing source as zero dividends."""
    views=data.get('profit_views',{});now=datetime.now(KST);today=now.strftime('%Y%m%d')
    rate=decimal(data['fx']) if data.get('fx') else None
    sources={}
    for broker,filename in (('kiwoom','kiwoom_dividends.json'),('kis','kis_dividends.json')):
        try:
            raw=json.loads(Path(snapshot_path).with_name(filename).read_text(encoding='utf-8'))
            if raw.get('schema')!=1 or raw.get('identity')!=source_id(root,broker) or raw.get('root')!=str(Path(root).resolve()):raise ValueError('Wrong source')
            sources[broker]=raw
        except (OSError,ValueError,KeyError,TypeError):sources[broker]={}
    for broker in ('kiwoom','kis'):
        if sources[broker].get('error'):
            views.get(broker,{})['error']='배당 갱신 대기'
            views.get('combined',{})['error']='배당 갱신 대기'
        for row in views.get(broker,{}).get('months',[]):
            key=row['period'].replace('-','');y,m=int(key[:4]),int(key[4:])
            end=min(today,key+f'{calendar.monthrange(y,m)[1]:02d}')
            p=sources[broker].get('periods',{}).get(key,{})
            row['trading_total']=row.get('total')
            row['trading_complete']=row.get('complete',False)
            row['dividend']=None;row['dividend_known']=Decimal(0);row['dividend_complete']=False
            try:
                if p.get('through')==end and rate and p.get('complete'):
                    row['dividend']=decimal(p['krw'])+decimal(p['usd'])*rate
                    row['dividend_known']=row['dividend'];row['dividend_complete']=True
            except (ValueError,KeyError,TypeError,ArithmeticError):pass
            row['complete']=row['trading_complete'] and row['dividend_complete']
            row['total']=row['trading_total']+row['dividend'] if row['complete'] else None
    by_broker={b:{r['period']:r for r in views.get(b,{}).get('months',[])} for b in ('kiwoom','kis')}
    for row in views.get('combined',{}).get('months',[]):
        parts=[by_broker[b].get(row['period'],{}) for b in ('kiwoom','kis')]
        row['trading_total']=row.get('total');row['trading_complete']=row.get('complete',False)
        row['dividend_complete']=all(p.get('dividend_complete') for p in parts)
        row['dividend_known']=sum((p.get('dividend_known',Decimal(0)) for p in parts),Decimal(0))
        row['dividend']=row['dividend_known'] if row['dividend_complete'] else None
        row['complete']=row['trading_complete'] and row['dividend_complete']
        row['total']=row['trading_total']+row['dividend'] if row['complete'] else None
    for view in views.values():
        for year in view.get('years',[]):
            rows=[r for r in view.get('months',[]) if r['period'].startswith(year['period'])]
            for flag in ('trading_complete','dividend_complete','complete'):
                year[flag]=bool(rows) and all(r.get(flag) for r in rows)
            for key,flag in (('trading_total','trading_complete'),('dividend','dividend_complete'),('total','complete')):
                year[key]=sum((r[key] for r in rows),Decimal(0)) if year[flag] else None
            year['dividend_known']=sum((r.get('dividend_known',Decimal(0)) for r in rows),Decimal(0))
        view['complete']=all(r.get('complete') for r in view.get('months',[]))
    annual=data.get('annual_profit',{})
    rows=[r for r in views.get('combined',{}).get('months',[]) if r['period'].startswith(str(now.year))]
    if annual.get('year')==now.year and annual.get('total_krw') is not None:
        annual['trading_total_krw']=annual['total_krw'];annual['trading_complete']=annual.get('complete',False)
        annual['dividend_complete']=bool(rows) and all(r.get('dividend_complete') for r in rows)
        known=sum((r.get('dividend_known',Decimal(0)) for r in rows),Decimal(0))
        annual['dividend_known_krw']=str(known)
        annual['total_krw']=str(decimal(annual['trading_total_krw'])+known)
        annual['complete']=annual['trading_complete'] and annual['dividend_complete']
        annual['stale']=bool(annual.get('stale') or any(s.get('error') for s in sources.values()))
    return data
