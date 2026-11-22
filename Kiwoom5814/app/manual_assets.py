"""Local display assets; never used by ATS capital or orders."""
import copy
import math
from desktop_platform import read_preferences

def mode_key(row):
    import json
    return json.dumps([row.get('broker','kiwoom'),row.get('market',''),row.get('symbol',''),row.get('name','') if row.get('manual') else ''],ensure_ascii=False)

def editable_mode(row):
    return row.get('manual') or row.get('broker')=='kis' or row.get('ats_managed') is False

def apply(source):
    data=dict(source)
    # Only holdings are modified. Do not copy the complete annual fills ledger.
    rows=[dict(r) for r in data.get('holdings',[]) if not r.get('manual')]
    data['holdings']=rows
    base_capital=data.get('broker_capital',data.get('capital'))
    data['broker_capital']=base_capital
    preferences=read_preferences()
    for i,item in enumerate(preferences.get('manual_assets',[])):
        try:
            value=float(item['amount'])
            if not math.isfinite(value) or value<=0:continue
            supplied=item.get('evaluation_amount')
            evaluation=float(supplied) if supplied not in (None,'') else value
            if not math.isfinite(evaluation) or evaluation<0:continue
            rows.append(dict(symbol=str(item.get('code','')).strip(),name=str(item['name']),broker='other',market='other',
                quantity=1,mode=str(item.get('mode','')).strip() or '—',note=str(item.get('note','')),entry=None,krw=value,book_krw=value,evaluation_krw=evaluation,
                purchase=value,evaluation=evaluation,manual=True,manual_valuation_assumed=supplied in (None,'')))
        except (ValueError,TypeError,KeyError):continue
    overrides=preferences.get('holding_mode_overrides',{})
    for row in rows:
        if editable_mode(row) and mode_key(row) in overrides:row['mode']=overrides[mode_key(row)]
    manual_total=sum(r['book_krw'] for r in rows if r.get('manual'))
    data['manual_assets_total']=manual_total
    data['capital']=base_capital+manual_total if base_capital is not None else None
    if data.get('cash') is None:return data
    cash=max(0,data['cash'])
    cost=sum(r['krw'] for r in rows)+cash
    value=sum(r.get('evaluation_krw',0) for r in rows)+cash
    for row in rows:
        row['weight']=row['krw']/cost if cost else None
        row['evaluation_weight']=row.get('evaluation_krw',0)/value if value else None
    data['chart_rows']=list(rows)
    if cash:
        data['chart_rows'].append(dict(symbol='CASH',name='현금',is_cash=True,krw=cash,
            evaluation_krw=cash,weight=cash/cost,evaluation_weight=cash/value,
            cash_by_broker=data.get('cash_by_broker',{})))
    return data

def account_chart(source):
    data=dict(source)
    if data.get('cash') is None:
        data['chart_rows']=[];return data
    rows=[]
    for broker,name in (('kiwoom','키움 5814'),('kis','한투'),('other','기타')):
        held=[r for r in data.get('holdings',[]) if r.get('broker','kiwoom')==broker]
        cash=data.get('cash_by_broker',{}).get(broker,0)
        if cash is None:
            data['chart_rows']=[];return data
        value=sum(r.get('evaluation_krw',0) for r in held)+cash
        if value>0:rows.append(dict(symbol=broker,name=name,broker=broker,krw=value,evaluation_krw=value))
    total=sum(r['krw'] for r in rows)
    for row in rows:row['weight']=row['krw']/total
    data['chart_rows']=rows
    return data


def entry_cycle_chart(source):
    """Purchase-cost allocation by displayed entry cycle, with cash separate."""
    data=dict(source)
    data['chart_rows']=[]
    if source.get('cash') is None:return data
    groups={}
    for holding in source.get('holdings',[]):
        value=holding.get('krw')
        if value is None or not math.isfinite(value) or value<0:return data
        label=str(holding.get('mode') or '').strip()
        if label in ('','—','미분류'):label='미분류'
        row=groups.setdefault(label,dict(symbol='',name=label,cycle_group=True,krw=0,quantity=0))
        row['krw']+=value
        row['quantity']+=1
    rows=[groups[key] for key in sorted(groups) if groups[key]['krw']>0]
    cash=max(0,source['cash'])
    if cash:
        rows.append(dict(symbol='CASH',name='현금',is_cash=True,krw=cash,cash_by_broker=source.get('cash_by_broker',{})))
    total=sum(r['krw'] for r in rows)
    for row in rows:row['weight']=row['krw']/total if total else 0
    data['chart_rows']=rows
    return data
