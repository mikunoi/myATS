"""Display-only broker aggregation. No network calls or writes to ATS state."""
import calendar
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from refresh_schedule import KST


def merge(data, root, snapshot_path):
    from data_reader import age, numeric
    from kis_portfolio import PortfolioClient, KISError
    for row in data['holdings']+data['fills']:
        row.setdefault('broker','kiwoom')
    data['cash_by_broker']={'kiwoom':data.get('cash') if data.get('full_account') else None}
    data['portfolio_note']='키움'
    data['cash_rows']=[]
    disabled = False
    try:
        client=PortfolioClient(root)
    except (OSError,ValueError,KISError) as exc:
        disabled = str(exc) == 'KIS_DEFAULT_LIVE_ACCOUNT_DISABLED'
        client=None
    if client is None and not disabled and Path(snapshot_path).with_name('kis_snapshot.json').exists():
        data['cash_by_broker']['kis']=None
        data['portfolio_note']='한투 설정 확인 필요'
        data['capital']=None;data['cash']=None
        data.get('annual_profit',{})['complete']=False
        data['trade_history']['complete']=False
        data.get('profit_history',{})['complete']=False
        for key in ('months','years'):
            for row in data.get('profit_history',{}).get(key,[]):
                row.update(complete=False,domestic=None,us=None,total=None)
    if client:
        data['cash_by_broker']['kis']=None
        data['portfolio_note']='키움 · 한투 조회 대기'
        try:
            cached=json.loads(Path(snapshot_path).with_name('kis_snapshot.json').read_text(encoding='utf-8'))
            if cached.get('schema')!=1 or cached.get('root')!=str(Path(root).resolve()) or cached.get('identity')!=client.identity:
                raise ValueError('Source mismatch')
            balance=cached.get('balance') or {}
            elapsed=age(balance.get('at'))
            if not balance.get('complete') or elapsed is None or not -5<=elapsed<=86400:
                raise ValueError('Missing or expired snapshot')
            fx=numeric(data.get('fx'))
            if fx is None or fx<=0:
                raise ValueError('Missing FX')
            # Validate the entire source before publishing any partial totals.
            krw,usd=numeric(balance['krw_cash']),numeric(balance['usd_cash'])
            if krw is None or usd is None:
                raise ValueError('Invalid cash')
            incoming=[];seen=set()
            for src in balance['holdings']:
                row=dict(src);identity=(row['market'],row['symbol'])
                if identity in seen or row['market'] not in ('domestic','us'):
                    raise ValueError('Invalid holdings')
                seen.add(identity)
                for k in ('quantity','purchase','evaluation'):
                    row[k]=numeric(row[k])
                    if row[k] is None or row[k]<0:
                        raise ValueError('Invalid holding amount')
                rate=fx if row['market']=='us' else 1
                row.update(broker='kis',account_source=client.identity,entry=None,atr_levels=None,mode='—',
                    krw=row['purchase']*rate,book_krw=row['purchase']*rate,
                    evaluation_krw=row['evaluation']*rate,at=balance['at'])
                incoming.append(row)
            kis_cash=krw+usd*fx
            data['holdings']+=incoming
            data['cash_by_broker']['kis']=kis_cash
            data['kis_at']=balance['at']
            data['kis_cash_basis']='원화 D+2 · 달러 미결제 반영'
            stale=bool(cached.get('error') or elapsed>=3600)
            data['portfolio_note']='키움 + 한투'+(' · 한투 이전 자료/갱신 대기' if stale else '')
            if data.get('full_account'):
                data['cash']+=kis_cash
                data['capital']+=sum(r['book_krw'] for r in incoming)+kis_cash
            merge_history(data,cached,fx)
        except (OSError,ValueError,TypeError,KeyError,ArithmeticError):
            data['capital']=None;data['cash']=None
            if data.get('annual_profit'):
                data['annual_profit']['complete']=False
            data['trade_history']['complete']=False
            data.get('profit_history',{})['complete']=False
            for key in ('months','years'):
                for row in data.get('profit_history',{}).get(key,[]):
                    row.update(complete=False,domestic=None,us=None,total=None)
    # All displayed sources must be confirmed before an integrated chart is drawn.
    cash=data.get('cash')
    if not data.get('full_account') or cash is None:
        data['chart_rows']=[]
        return data
    # Negative net cash is a liability, not a negative pie wedge.
    chart_cash=max(0,cash)
    purchase=sum(r['krw'] for r in data['holdings'])+chart_cash
    evaluation=sum(r.get('evaluation_krw',0) for r in data['holdings'])+chart_cash
    for row in data['holdings']:
        row['weight']=row['krw']/purchase if purchase else None
        row['evaluation_weight']=row.get('evaluation_krw',0)/evaluation if evaluation else None
    for market in ('domestic','us'):
        held=[r for r in data['holdings'] if r['market']==market]
        cost=sum(r['krw'] for r in held)
        data['evaluation_rates'][market]=(sum(r.get('evaluation_krw',0) for r in held)/cost-1) if cost else None
    data['chart_rows']=list(data['holdings'])
    if chart_cash:
        data['chart_rows'].append(dict(symbol='CASH',name='현금',is_cash=True,krw=chart_cash,
            evaluation_krw=chart_cash,weight=chart_cash/purchase,evaluation_weight=chart_cash/evaluation,
            cash_by_broker=data['cash_by_broker'],cash_basis=data.get('kis_cash_basis',''),
            status=data['portfolio_note']))
    if cash<0:
        data['portfolio_note']+=' · 현금부채는 원형차트 제외'
    return data


def broker_evaluation_rates(data):
    """Unrealized holding returns; both USD cost and value use display FX."""
    from data_reader import age
    result={}
    for broker in ('kiwoom','kis'):
        info=dict(combined=None,domestic=None,us=None,state='unavailable',stale=False)
        result[broker]=info
        available=data.get('full_account') if broker=='kiwoom' else bool(data.get('kis_at') or data.get('managed_kis_ready'))
        if not available:continue
        at=data.get('cash_at') if broker=='kiwoom' else data.get('kis_at')
        if broker=='kis' and not at and data.get('managed_kis_ready'):
            at=data.get('managed_kis_at')
        elapsed=age(at)
        info['stale']=elapsed is None or elapsed>=3600 or (broker=='kis' and '이전' in data.get('portfolio_note',''))
        rows=[r for r in data.get('holdings',[]) if r.get('broker')==broker]
        try:
            for key in ('combined','domestic','us'):
                selected=rows if key=='combined' else [r for r in rows if r['market']==key]
                cost=sum((Decimal(str(r['krw'])) for r in selected),Decimal(0))
                value=sum((Decimal(str(r['evaluation_krw'])) for r in selected),Decimal(0))
                if not cost.is_finite() or not value.is_finite() or cost<0 or value<0:raise ValueError()
                info[key]=float(value/cost-1) if cost>0 else None
            info['state']='empty' if not rows else 'available'
        except (ValueError,KeyError,TypeError,ArithmeticError):
            info.update(combined=None,domestic=None,us=None,state='unavailable')
    return result


def merge_history(data,cached,fx):
    from trade_history import attach_profits
    now=datetime.now(KST);today=now.strftime('%Y%m%d')
    periods=cached.get('periods',{})
    orders=cached.get('orders',{}) if cached.get('year')==now.year else {}
    fills=[];realized=[];complete=True
    for month in range(1,now.month+1):
        key=f'{now.year}{month:02d}'
        end=min(today,key+f'{calendar.monthrange(now.year,month)[1]:02d}')
        for market in ('domestic','us'):
            p=periods.get(market+':'+key,{})
            o=orders.get(market+':'+key,{})
            valid=all(x.get('complete') and x.get('through')==end for x in (p,o))
            complete=complete and valid
            if o.get('complete'):
                fills+=o.get('rows',[])
            if p.get('complete'):
                realized+=p.get('rows',[])
    fills,unmatched=attach_profits(fills,realized)
    for row in fills:
        row.update(broker='kis',reason='')
    data['fills']=sorted(data['fills']+fills,key=lambda r:r['at'],reverse=True)
    status=data['trade_history']
    status['complete']=bool(status.get('complete') and complete)
    status['profit_complete']=bool(status.get('profit_complete') and complete and not unmatched)
    if cached.get('history_error'):
        status['error']='한투 체결/손익 갱신 대기'
    annual=data.get('annual_profit',{})
    if annual:
        annual['complete']=bool(annual.get('complete') and complete and not unmatched)
        if annual.get('year')==now.year:
            d=sum(Decimal(str(r['profit'])) for r in fills if r['side']=='SELL' and r['market']=='domestic' and r.get('profit') is not None)
            u=sum(Decimal(str(r['profit'])) for r in fills if r['side']=='SELL' and r['market']=='us' and r.get('profit') is not None)
            # Include broker-reported non-sale charges once, outside fill rows.
            for market in ('domestic','us'):
                adjustment=Decimal(0)
                for month in range(1,now.month+1):
                    period=periods.get(f'{market}:{now.year}{month:02d}',{})
                    if period.get('complete'):
                        adjustment+=Decimal(str(period['profit']))-sum((Decimal(str(r['profit'])) for r in period.get('rows',[])),Decimal(0))
                if market=='domestic':d+=adjustment
                else:u+=adjustment
            annual['total_krw']=str(Decimal(annual['total_krw'])+d+u*Decimal(str(fx)))
            annual['domestic_krw']=str(Decimal(annual['domestic_krw'])+d)
            annual['us_usd']=str(Decimal(annual['us_usd'])+u)
            annual['us_krw']=str(Decimal(annual['us_usd'])*Decimal(str(fx)))
            annual['sales']+=sum(r['side']=='SELL' for r in fills)
            annual['missing']+=sum(r['side']=='SELL' and r.get('profit') is None for r in fills)
            annual['stale']=bool(annual.get('stale') or cached.get('history_error'))
    history=data.get('profit_history',{})
    for row in history.get('months',[]):
        key=row['period'].replace('-','');y,m=int(key[:4]),int(key[4:])
        end=min(today,key+f'{calendar.monthrange(y,m)[1]:02d}')
        p=[periods.get(market+':'+key,{}) for market in ('domestic','us')]
        valid=all(x.get('complete') and x.get('through')==end for x in p)
        if row.get('complete') and valid:
            row['domestic']+=Decimal(p[0]['profit']);row['us']+=Decimal(p[1]['profit'])
            row['total']=row['domestic']+row['us']*Decimal(str(fx))
        else:
            row.update(complete=False,domestic=None,us=None,total=None)
    for row in history.get('years',[]):
        months=[m for m in history.get('months',[]) if m['period'].startswith(row['period'])]
        valid=bool(months) and all(m.get('complete') for m in months)
        row.update(complete=valid,**{k:sum((m[k] for m in months),Decimal(0)) if valid else None for k in ('domestic','us','total')})
    history['complete']=bool(history.get('months')) and all(m['complete'] for m in history['months'])
    if cached.get('history_error'):
        history['error']='한투 손익 갱신 대기'


def profit_views(root, snapshot_path, fx):
    """Independent account histories; current and preceding calendar year only."""
    from profit_history import read
    from kis_portfolio import PortfolioClient
    now=datetime.now(KST); today=now.strftime('%Y%m%d')
    start_year=now.year-1
    kiwoom=read(root,Path(snapshot_path).with_name('profit_history.json'),fx,now)
    kiwoom_months={r['period']:r for r in kiwoom.get('months',[])}
    cached={}; kis_error='한투 자료 확인 대기'
    try:
        client=PortfolioClient(root)
        candidate=json.loads(Path(snapshot_path).with_name('kis_snapshot.json').read_text(encoding='utf-8'))
        if candidate.get('schema')==1 and candidate.get('root')==str(Path(root).resolve()) and candidate.get('identity')==client.identity:
            cached=candidate
            kis_error=cached.get('history_error','')
    except (OSError,ValueError,RuntimeError,TypeError):
        pass
    views={key:dict(months=[],years=[],error=error,at='') for key,error in (
        ('kiwoom',kiwoom.get('error','')),('kis',kis_error),('combined',kiwoom.get('error','') or kis_error))}
    rate=Decimal(str(fx)) if fx is not None else None
    if rate is not None and (not rate.is_finite() or rate<=0):rate=None
    for y in range(start_year,now.year+1):
        for m in range(1,(now.month if y==now.year else 12)+1):
            label=f'{y}-{m:02d}'; key=f'{y}{m:02d}'
            end=min(today,key+f'{calendar.monthrange(y,m)[1]:02d}')
            blank=dict(period=label,domestic=None,us=None,total=None,complete=False,at='')
            kw=dict(kiwoom_months.get(label,blank))
            ks=dict(blank)
            parts=[cached.get('periods',{}).get(market+':'+key,{}) for market in ('domestic','us')]
            try:
                if rate and all(x.get('complete') and x.get('through')==end for x in parts):
                    d,u=(Decimal(str(x['profit'])) for x in parts)
                    if not d.is_finite() or not u.is_finite():raise ValueError('Invalid profit')
                    ks.update(domestic=d,us=u,total=d+u*rate,complete=True,at=min(x.get('at','') for x in parts))
            except (ValueError,KeyError,TypeError,ArithmeticError):
                views['kis']['error']='한투 손익 자료 확인 필요'
                views['combined']['error']='한투 손익 자료 확인 필요'
            both=dict(blank)
            if kw.get('complete') and ks.get('complete'):
                both.update(complete=True,at=min(kw.get('at',''),ks.get('at','')),
                            **{k:kw[k]+ks[k] for k in ('domestic','us','total')})
            for name,row in (('kiwoom',kw),('kis',ks),('combined',both)):
                views[name]['months'].append(row)
    for view in views.values():
        for y in range(start_year,now.year+1):
            rows=[r for r in view['months'] if r['period'].startswith(str(y))]
            complete=bool(rows) and all(r.get('complete') for r in rows)
            view['years'].append(dict(period=str(y),complete=complete,
                at=min((r.get('at','') for r in rows),default=''),
                **{k:sum((r[k] for r in rows),Decimal(0)) if complete else None for k in ('domestic','us','total')}))
        view['complete']=all(r.get('complete') for r in view['months'])
        view['at']=min((r.get('at','') for r in view['months']),default='')
    return views
