"""Shared display-only YTD dividend totals and explicitly defined reference yield."""
from datetime import datetime
from decimal import Decimal
from refresh_schedule import KST


def summary(data):
    year=datetime.now(KST).year
    months=[]
    views=data.get('profit_views',{})
    by_month={r['period']:r for r in views.get('combined',{}).get('months',[])}
    broker_months={b:{r['period']:r for r in views.get(b,{}).get('months',[])} for b in ('kiwoom','kis')}
    known=Decimal(0);any_known=False;complete=True
    for month in range(1,datetime.now(KST).month+1):
        key=f'{year}-{month:02d}';row=by_month.get(key,{})
        confirmed=any(broker_months[b].get(key,{}).get('dividend_complete') for b in broker_months)
        value=Decimal(str(row.get('dividend_known',0))) if confirmed else None
        if value is not None:known+=value;any_known=True
        done=bool(row.get('dividend_complete'));complete=complete and done
        months.append(dict(month=month,value=float(value) if value is not None else None,complete=done))
    cost=None
    if data.get('full_account') and data.get('cash') is not None:
        cost=sum((Decimal(str(r['book_krw'])) for r in data.get('holdings',[])),Decimal(0))
    return dict(year=year,amount=float(known) if any_known else None,complete=complete,
                rate=float(known/cost) if any_known and cost and cost>0 else None,
                cost=float(cost) if cost is not None else None,months=months,
                note='세후 수령 배당 · 해외 현재 환율 환산'+(' · 일부 계좌/기간 미확인' if not complete else ''),
                definition='투자배당률 = 올해 확인 배당 ÷ 현재 보유종목 총매입액 (연환산 아님)')
