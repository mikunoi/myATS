from desktop_fonts import korean_font
"""Opaque, margin-safe portfolio exports with user-selected columns."""
from pathlib import Path
import math
from functools import lru_cache
from datetime import datetime
from refresh_schedule import KST
from PIL import Image, ImageDraw, ImageFont
from dashboard_view import COLORS, PaletteDraw
from dividend_summary import summary
import dashboard_theme as theme

FIELDS={'broker':'증권사','name':'종목','symbol':'종목코드','purchase':'매입금액','weight':'매입비중','evaluation':'평가금액','evaluation_weight':'평가비중'}
DEFAULT_FIELDS=('broker','name','symbol','purchase','weight')
BG='#031522';FG='#E3EAF0';MUTED='#98A8B7';YELLOW='#C9B76D'

@lru_cache(maxsize=64)
def font(size,bold=False):
    return ImageFont.truetype(korean_font(bold),size)

def wrap(value,width,size):
    lines=[];line=''
    for char in str(value):
        if line and font(size).getlength(line+char)>width:lines.append(line);line=''
        line+=char
    return lines+[line]

def money(value):return '확인 대기' if value is None else f'{float(value):,.0f}원'
def percent(value):return '—' if value is None else f'{float(value):.2%}'

def stamp(value):
    try:return datetime.fromisoformat(str(value)).astimezone(KST).strftime('%Y-%m-%d %H:%M KST')
    except (ValueError,TypeError):return '확인 대기'

def render_portfolio(rows,read_at,cash_at,data=None,options=None):
    data=data or {};options=options or {}
    fields=[k for k in FIELDS if k in options.get('fields',DEFAULT_FIELDS)]
    modern=options.get('style','classic')=='modern'
    selected_theme=options.get('theme') or theme.CURRENT
    basis=options.get('basis','purchase');weight_key='evaluation_weight' if basis=='evaluation' else 'weight'
    width=1320 if modern else 1680;pad=48;chart_w=410 if modern else 530
    table_x=pad+chart_w+120;table_w=width-pad-table_x
    bg='#121315' if modern else BG
    sizes={'broker':.8,'name':2.2,'symbol':1.0,'purchase':1.35,'weight':1.0,'evaluation':1.35,'evaluation_weight':1.0}
    total=sum(sizes[k] for k in fields) or 1
    columns={k:table_w*sizes[k]/total for k in fields}
    fs=20 if len(fields)>5 else 23
    layout=[];cards=[]
    for index,row in enumerate(rows):
        values=dict(broker='현금' if row.get('is_cash') else {'kis':'한투','other':'기타'}.get(row.get('broker'),'키움'),
          name=row.get('name',''),symbol='—' if row.get('is_cash') else row.get('symbol',''),
          purchase=money(row.get('krw')),weight=percent(row.get('weight')),
          evaluation=money(row.get('evaluation_krw')),evaluation_weight=percent(row.get('evaluation_weight')))
        lines={k:wrap(values[k],max(24,columns[k]-18),fs) for k in fields}
        height=max(44,max((len(v) for v in lines.values()),default=1)*(fs+8)+16)
        layout.append((row,lines,height,index))
        card_lines=[]
        for key in fields:
            label=values[key] if key in ('broker','name','symbol') else FIELDS[key]+' '+values[key]
            card_lines.extend(wrap(label,table_w/2-38,18))
        cards.append((row,card_lines,max(50,len(card_lines)*26+24),index))
    top=320
    legend_h=sum(max(c[2] for c in cards[i:i+2]) for i in range(0,len(cards),2)) if modern else 56+sum(v[2] for v in layout)
    body_h=max(chart_w+55,legend_h)
    div=summary(data);div_y=top+body_h+46
    show_div=options.get('dividends',True)
    height=div_y+(440 if show_div else 30)+85
    light=selected_theme=='toss'
    if light:bg='#F2F4F6'
    image=Image.new('RGB',(width,height),bg);d=PaletteDraw(image,light)
    def text(x,y,value,size=22,color=FG,bold=False):d.text((int(x),int(y)),str(value),font=font(size,bold),fill=color)
    def mosaic(x,y,w,h):
        # Fixed opaque tiles replace the value; no sensitive glyphs are rendered.
        colors=('#CBD5E1','#DEE5ED','#B9C6D5','#D2DCE7') if light else ('#34495B','#52697B','#40586C','#61788A')
        brush=ImageDraw.Draw(image)
        for row,ty in enumerate(range(y,y+h,12)):
            for col,tx in enumerate(range(x,x+w,12)):
                brush.rectangle((tx,ty,min(tx+11,x+w-1),min(ty+11,y+h-1)),fill=colors[(row*3+col*7+row*col)%len(colors)])
    text(pad,32,'키움 · 한투 · 기타 포트폴리오',25,MUTED)
    text(pad,77,'총 운용자금',23,MUTED)
    if options.get('hide_capital',False):mosaic(pad,116,390,52)
    else:text(pad,108,money(data.get('capital')),48,bold=True)
    annual=data.get('annual_profit',{})
    pnl=annual.get('total_krw') if annual.get('trading_complete',annual.get('complete')) else None
    cost=sum(float(r.get('krw') or 0) for r in rows if not r.get('is_cash'))
    complete=all(r.get('evaluation_krw') is not None and r.get('krw') is not None for r in rows if not r.get('is_cash'))
    valuation=sum(float(r.get('evaluation_krw') or 0) for r in rows if not r.get('is_cash'))
    rate=(valuation/cost-1) if cost>0 and complete else None
    rate_label='—' if rate is None else f'{rate:+.2%}'
    if options.get('hide_profit',False):
        label='통합 실현손익  '
        text(pad,181,label,27,MUTED)
        mosaic(pad+round(font(27).getlength(label)),186,350,32)
    else:
        text(pad,181,'통합 실현손익  '+money(pnl)+' ('+rate_label+')',27,'#DE8B98' if pnl is not None and float(pnl)<0 else '#66C2AC')
    text(pad,224,'괄호: 보유자산 종합 평가수익률 · '+('평가액' if basis=='evaluation' else '매입액')+' + 현금 기준',18,MUTED)
    if any(r.get('manual') for r in rows):
        note='기타 자산 포함 · 입력한 원화 평가액으로 수익률 계산'
        if any(r.get('manual_valuation_assumed') for r in rows):note+=' · 평가액 미입력 자산은 매입액과 동일 가정'
        text(pad,262,note,19,MUTED)
    text(width-420,83,'해외 금액: 표시 환율로 원화 환산',17,MUTED)
    text(width-420,111,'운용자금: 매입원금 + 추정 예수금',17,MUTED)
    if not annual.get('dividend_complete'):text(width-420,140,'실현손익: 확인 배당 포함 · 일부 미확인',17,YELLOW)
    # Render donut at 2x then reduce once; no alpha fades or edge masks.
    size=chart_w;cx=pad+size/2;cy=top+body_h/2
    tile=Image.new('RGB',(size*2,size*2),bg);td=ImageDraw.Draw(tile)
    bounds=(4,4,size*2-4,size*2-4);start=-90
    for row,_,_,index in layout:
        weight=float(row.get(weight_key) or 0)
        if weight<=0:continue
        end=start+weight*360
        td.pieslice(bounds,start,end,fill=theme.chart_color(index,row.get('is_cash'),COLORS[index%len(COLORS)],selected_theme),outline=bg,width=4)
        start=end
    td.ellipse((size*.52,size*.52,size*1.48,size*1.48),fill=bg)
    start=-90;placed=[];outside=[]
    for row,_,_,index in layout:
        weight=float(row.get(weight_key) or 0)
        if weight<=0:continue
        extent=weight*360;label=f'{weight:.1%}' if weight>=.001 else '<0.1%'
        angle=math.radians(start+extent/2);found=False
        ink=FG
        if selected_theme!='default':
            shade=int(theme.chart_color(index,row.get('is_cash'),COLORS[index%len(COLORS)],selected_theme)[1:3],16)
            ink='#000000' if shade>=130 else '#FFFFFF'
        for fontsize in (40,36,32,28,24,22):
            if found:break
            for label_text in (label,label.replace('%','\n%')):
                if found:break
                label_font=font(fontsize,True)
                box=td.multiline_textbbox((0,0),label_text,font=label_font,spacing=0,align='center')
                w,h=box[2]-box[0],box[3]-box[1]
                for distance in (.75,.82,.67,.88,.59):
                    px=size+math.cos(angle)*size*distance;py=size+math.sin(angle)*size*distance
                    bounds_label=(px-w/2-4,py-h/2-4,px+w/2+4,py+h/2+4)
                    x1,y1,x2,y2=bounds_label
                    points=((x1,y1),(x2,y1),(x1,y2),(x2,y2),(px,y1),(px,y2),(x1,py),(x2,py))
                    inside=all(size*.49<=math.hypot(x-size,y-size)<=size-8 and
                               (math.degrees(math.atan2(y-size,x-size))-start)%360<=extent for x,y in points)
                    collision=any(not(x2<q[0] or x1>q[2] or y2<q[1] or y1>q[3]) for q in placed)
                    if inside and not collision:
                        td.multiline_text((px-(box[0]+box[2])/2,py-(box[1]+box[3])/2),label_text,font=label_font,fill=ink,spacing=0,align='center')
                        placed.append(bounds_label);found=True;break
        if not found:outside.append((angle,label))
        start+=extent
    image.paste(tile.resize((size,size),Image.Resampling.LANCZOS),(int(cx-size/2),int(cy-size/2)))
    d=PaletteDraw(image,light)
    # Short radial leaders, with vertical spacing on each side of the donut.
    for side in (-1,1):
        labels=sorted((cy+math.sin(a)*(size/2+18),a,label) for a,label in outside if (1 if math.cos(a)>=0 else -1)==side)
        last=top-24
        for natural,a,label in labels:
            ly=max(natural,last+20);last=ly
            ax=cx+math.cos(a)*(size/2-2);ay=cy+math.sin(a)*(size/2-2)
            ex=cx+math.cos(a)*(size/2+13);ey=cy+math.sin(a)*(size/2+13)
            endx=ex+side*10
            d.line(((ax,ay),(ex,ey),(endx,ly)),fill=MUTED,width=1)
            tx=endx+4 if side>0 else endx-4-font(14).getlength(label)
            tx=max(5,min(width-font(14).getlength(label)-5,tx))
            text(tx,ly-9,label,14,FG)
    if modern:
        y=top
        for i in range(0,len(cards),2):
            group=cards[i:i+2]
            for column,(row,lines,_,index) in enumerate(group):
                x=table_x+column*table_w/2
                d.ellipse((x,y+6,x+12,y+18),fill=theme.chart_color(index,row.get('is_cash'),COLORS[index%len(COLORS)],selected_theme))
                for j,line in enumerate(lines):text(x+22,y+j*26,line,18,FG if j==0 else MUTED)
            y+=max(c[2] for c in group)
    else:
        x=table_x;y=top
        for key in fields:
            for j,line in enumerate(wrap(FIELDS[key],columns[key]-18,17)):text(x+12,y+j*20,line,17,MUTED)
            x+=columns[key]
        y+=56
        for row,lines,row_h,index in layout:
            d.ellipse((table_x-22,y+7,table_x-10,y+19),fill=theme.chart_color(index,row.get('is_cash'),COLORS[index%len(COLORS)],selected_theme))
            x=table_x
            for key in fields:
                for j,line in enumerate(lines[key]):text(x+12,y+j*(fs+8),line,fs)
                x+=columns[key]
            y+=row_h
    if not fields:text(table_x,top+24,'표시 항목 미선택',20,MUTED)
    if show_div:
        text(pad,div_y,f"{div['year']}년 현재까지 받은 배당 (확인분)",29)
        text(pad,div_y+48,money(div['amount']),44,YELLOW,True)
        text(width-470,div_y+57,'투자배당률 '+percent(div['rate']),27,YELLOW)
        text(pad,div_y+110,div['note'],17,MUTED)
        text(pad,div_y+137,div['definition'],17,MUTED)
        baseline=div_y+372;bar_area=width-pad*2;step=bar_area/12
        vals=[abs(m['value']) for m in div['months'] if m['value'] is not None]
        maximum=max(vals,default=0) or 1
        by_month={m['month']:m for m in div['months']}
        for month in range(1,13):
            x=pad+(month-1)*step;row=by_month.get(month);value=row['value'] if row else None
            if row and value is not None:
                h=abs(value)/maximum*155
                if h>0:d.rounded_rectangle((int(x+10),int(baseline-h),int(x+step-10),baseline),radius=min(8,int(h/2)),fill=YELLOW if value>=0 else '#DE8B98')
                label=f'{value:,.0f}원'
                text(x+step/2-font(20,True).getlength(label)/2,baseline-h-32,label,20,FG,True)
            else:text(x+step/2-8,baseline-24,'—',15,MUTED)
            text(x+step/2-12,baseline+14,f'{month}월',16,MUTED)
        text(pad,baseline+49,'확인된 배당만 표시 · 미래 월은 미표시 · 음수는 배당 취소/조정',16,MUTED)
    text(pad,height-56,'잔고 조회 '+stamp(cash_at)+' · 화면 조회 '+stamp(read_at),15,MUTED)
    if selected_theme=='matrix':
        for x,dx in ((20,1),(width-20,-1)):
            for y,dy in ((20,1),(height-20,-1)):
                d.line(((x,y+dy*28),(x,y),(x+dx*28,y)),fill='#AAD789',width=3)
        d.line((pad,div_y-24,width-pad,div_y-24),fill='#527E50',width=1)
    return image if light else theme.image(image,selected_theme)

def export_png(rows,path,read_at,cash_at,data=None,options=None):
    render_portfolio(rows,read_at,cash_at,data,options).save(path,'PNG')
