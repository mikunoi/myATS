from desktop_fonts import korean_font
"""Shared native dashboard renderer: the preview uses exactly the same pixels as the GUI."""
import math
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from functools import lru_cache
from datetime import datetime
from refresh_schedule import KST
from PIL import Image, ImageDraw, ImageFont, ImageTk
import dashboard_theme as theme
from popup_position import center_popup
from scroll_support import SlimScrollbar

BG = '#031522'
PANEL = '#061E30'
EDGE = '#12435C'
TEXT = '#EDF5FC'
MUTED = '#8DA6B9'
GREEN = '#00D6AF'
BROKER_COLORS = {'kiwoom': '#A65D7B', 'kis': '#669DD1', 'other': '#D9B84F'}
COLORS = ['#4476A3', '#AC8054', '#588C68', '#826B9F', '#A66068', '#AC9858', '#538793', '#A56C87', '#7089A6', '#7C9080']


@lru_cache(maxsize=80)
def font(size, bold=False):
    return ImageFont.truetype(korean_font(bold), size)


def amount(value, suffix='원'):
    if value is None: return '—'
    return f'{value:,.2f}{suffix}' if suffix in ('$', ' USD') else f'{value:,.0f}{suffix}'


def clock(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime('%H:%M')
    except (TypeError, ValueError):
        return '—'


def icon(draw, kind, x, y, color=TEXT, s=22):
    line = lambda points: draw.line(points, fill=color, width=2)
    if kind == 'menu':
        for dy in (4, 11, 18): line([(x, y + dy), (x + s, y + dy)])
    elif kind == 'strategy':
        # Three adjustable sliders, visually distinct from the hamburger menu.
        for dy, knob in ((4,7),(12,17),(20,10)):
            line([(x,y+dy),(x+s,y+dy)])
            draw.rounded_rectangle((x+knob-3,y+dy-3,x+knob+3,y+dy+3),radius=2,fill=color)
    elif kind == 'calculator':
        draw.rounded_rectangle((x+2,y,x+22,y+25),radius=3,outline=color,width=2)
        draw.rectangle((x+6,y+4,x+18,y+9),outline=color,width=1)
        for dx in (6,12,18):
            for dy in (14,20):draw.ellipse((x+dx-1,y+dy-1,x+dx+1,y+dy+1),fill=color)
    elif kind == 'chart':
        for dx, height in ((1, 9), (9, 16), (17, 23)):
            draw.rounded_rectangle((x + dx, y + 23 - height, x + dx + 4, y + 23), radius=2, fill=color)
    elif kind == 'download':
        line([(x+11,y), (x+11,y+15)]); line([(x+5,y+9),(x+11,y+15),(x+17,y+9)])
        line([(x,y+15),(x,y+23),(x+22,y+23),(x+22,y+15)])
    elif kind == 'globe':
        draw.ellipse((x,y,x+s,y+s), outline=color,width=2)
        draw.ellipse((x+6,y,x+s-6,y+s), outline=color,width=1)
        line([(x,y+s/2),(x+s,y+s/2)])
    elif kind == 'candles':
        for dx, dy in ((2,6),(10,1),(18,4)):
            line([(x+dx+2,y+dy-3),(x+dx+2,y+dy+20)])
            draw.rectangle((x+dx,y+dy,x+dx+4,y+dy+13),fill=PANEL,outline=color,width=1)
    elif kind == 'settings':
        draw.ellipse((x+2,y+2,x+22,y+22),outline=color,width=2)
        draw.ellipse((x+8,y+8,x+16,y+16),outline=color,width=2)
        for a in range(0,360,45):
            r=math.radians(a); line([(x+12+10*math.cos(r),y+12+10*math.sin(r)),(x+12+14*math.cos(r),y+12+14*math.sin(r))])
    else:
        draw.rounded_rectangle((x,y+5,x+25,y+23),radius=3,outline=color,width=2)
        draw.rectangle((x+17,y+11,x+27,y+18),fill=BG,outline=color,width=2)
        line([(x+3,y+4),(x+17,y),(x+21,y+5)])


class PaletteDraw:
    """Resolve semantic colors before rasterization, preserving antialiasing."""
    def __init__(self, image, modern=False):
        self.draw = ImageDraw.Draw(image)
        self.modern = modern

    def __getattr__(self, name):
        method = getattr(self.draw, name)
        def paint(*args, **kwargs):
            if self.modern:
                for key in ('fill', 'outline'):
                    value = kwargs.get(key)
                    if isinstance(value, str) and value.startswith('#'):
                        kwargs[key] = theme.toss_color(value)
            return method(*args, **kwargs)
        return paint


class DashboardPainter:
    def paint(self, data, width, height, offsets=(0,0), hover=None, scroll_hover=None):
        # Use the real viewport. A fixed minimum bitmap used to overflow under Windows scaling.
        width, height = max(1,width), max(1,height)
        modern=(data.get("theme") or theme.CURRENT)=="toss"
        canvas_bg=theme.toss_color(BG) if modern else BG
        if min(width,height)<160:
            self.hits={};self.slices=[];self.row_hits=[];self.fill_hits=[];self.visible_rows=0;self.chart_geometry=(0,0,0)
            return Image.new('RGB',(width,height),canvas_bg)
        scale=min(1,width/1174,height/560)
        if scale<1:
            logical_w=math.ceil(width/scale); logical_h=math.ceil(height/scale)
            logical_hover=(hover[0]/scale,hover[1]/scale,hover[2]) if hover else None
            im=self.paint(data,logical_w,logical_h,offsets,logical_hover,scroll_hover)
            self.hits={key:tuple(v*scale for v in box) for key,box in self.hits.items()}
            self.row_hits=[(tuple(v*scale for v in box),row) for box,row in self.row_hits]
            self.fill_hits=[(tuple(v*scale for v in box),row) for box,row in self.fill_hits]
            self.chart_geometry=tuple(v*scale for v in self.chart_geometry)
            return im.resize((width,height),Image.Resampling.LANCZOS)
        im = Image.new('RGB',(width,height),canvas_bg)
        d=PaletteDraw(im,modern)
        self.hits={}; self.slices=[]; self.row_hits=[];self.fill_hits=[]
        terminal=(data.get("theme") or theme.CURRENT)=="matrix"
        modern=(data.get("theme") or theme.CURRENT)=="toss"
        def text(x,y,value,size=15,color=TEXT,bold=False,limit=None):
            value=str(value)
            if limit:
                while font(size,bold).getlength(value)>limit and len(value)>1: value=value[:-2]+'…'
            d.text((int(x),int(y)),value,font=font(size,bold),fill=color)
        def card(box,fill=PANEL,edge=EDGE):
            if not terminal:
                d.rounded_rectangle(tuple(map(int,box)),radius=18 if modern else 10,fill=fill,outline=EDGE if modern else edge,width=1)
                return
            x1,y1,x2,y2=map(int,box)
            d.rectangle((x1,y1,x2,y2),fill=fill,outline='#53765B',width=1)
            if y2-y1>90:
                for sy in range(y1+4,y2,6):
                    d.line((x1+2,sy,x2-2,sy),fill='#082218',width=1)
            length=11 if y2-y1>60 else 5
            for xx,dx in ((x1,1),(x2,-1)):
                for yy,dy in ((y1,1),(y2,-1)):
                    d.line(((xx,yy+dy*length),(xx,yy),(xx+dx*length,yy)),fill='#AAD789',width=2)
        def fit(x,y,value,maxwidth,size=30,color=TEXT,bold=True):
            while size>15 and font(size,bold).getlength(value)>maxwidth: size-=1
            text(x,y,value,size,color,bold)
        gap=18 if modern else 14
        actionw=252
        sw=(width-actionw-gap*2)//2
        markets=data.get('markets',[])
        for i,title in enumerate(('국내 ATS','해외 ATS')):
            x=i*(sw+gap); card((x,0,x+sw,70))
            icon(d,'candles' if i==0 else 'globe',x+18,23,'#65BFDD' if i else TEXT)
            text(x+56,21,title,19,bold=True)
            market=markets[i] if len(markets)>i else {}
            status=market.get('status','확인 중')
            color=GREEN if status=='연결 기록 정상' else '#B4C6D3'
            sx=x+sw*.47
            d.ellipse((sx,30,sx+8,38),fill=color)
            text(sx+16,22,status,15,color,limit=sw*.49-25)
            text(x+sw-125,51,'최근 확인 '+clock(market.get('account_at')),10,MUTED)
        ax=width-actionw
        state=data.get('lifecycle','checking')
        label={'running':'ATS 진행중','busy':'처리 중','checking':'ATS 확인중...'}.get(state,'ATS 시작')
        for index,(label,key,color) in enumerate(((label,'start','#063D36'),('ATS 중지','stop','#38222F'))):
            box=(ax+index*132,0,ax+index*132+120,70)
            card(box,color,'#27677F')
            checking = key == 'start' and state == 'checking'
            text(box[0]+(7 if checking else 22),13 if checking else 23,label,13 if checking else 17,bold=True)
            self.hits[key]=box
        # One horizontal KPI strip, as in the approved mockup.
        y=88; bottom=250
        card((0,y,width,bottom))
        fractions=(0,.24,.46,.66,1)
        for frac in fractions[1:-1]: d.line((width*frac,y+20,width*frac,bottom-20),fill=EDGE,width=1)
        x=22; icon(d,'wallet',x,y+52,'#AFCFE2')
        text(x+45,y+23,'현재 총 운용자금',17)
        fit(x+45,y+55,amount(data.get('capital')),width*.24-85,38)
        text(x+45,y+121,'매입원금 + 추정 예수금',13,MUTED)
        x=width*.24+20
        icon(d,'wallet',x,y+52,'#AFCFE2')
        registered = data.get('registered_totals')
        partial = registered and (registered['present'] < registered['expected'] or registered['stale'])
        cash_title = '추정 예수금 · 확인분' if partial else '통합 추정 예수금'
        shown_cash = registered['cash'] if registered else data.get('cash')
        text(x+45,y+23,cash_title,17,limit=width*.22-81)
        fit(x+45,y+55,amount(shown_cash),width*.22-81,38)
        cash_sources=data.get('cash_by_broker',{})
        cash_detail='키움 '+amount(cash_sources.get('kiwoom'))+' / 한투 '+amount(cash_sources.get('kis'))
        detail_size=13
        detail_width=width*.22-81
        while detail_size>11 and font(detail_size).getlength(cash_detail)>detail_width:
            detail_size-=1
        detail_x=x+45
        text(detail_x,y+121,cash_detail,detail_size,MUTED)
        x=width*.46+14; rates=data.get('broker_evaluation_rates',{})
        if registered:
            rates = dict(rates, combined={'combined': registered['rate'], 'stale': bool(partial)})
        rate_titles = (('combined','통합 평가수익률'), ('kis','한투 평가수익률')) if registered else (
            ('kiwoom','키움 종합 평가수익률'),('kis','한투 종합 평가수익률'))
        for i,(key,title) in enumerate(rate_titles):
            info=rates.get(key,{})
            rate=info.get('combined')
            value=('보유 없음' if info.get('state')=='empty' else '확인 대기') if rate is None else f'{rate:+.2%}'
            text(x,y+23+i*65,title,15,bold=True,limit=width*.20-110)
            value_size=14 if rate is None else 20
            text(width*.66-12-font(value_size,rate is not None).getlength(value),y+(24 if rate is None else 19)+i*65,value,value_size,MUTED if rate is None else (GREEN if rate>=0 else '#E58390'),rate is not None)
            percent=lambda v:'—' if v is None else f'{v:+.2%}'
            detail='국내 '+percent(info.get('domestic'))+' / 해외 '+percent(info.get('us'))
            if key == 'combined':
                detail = f"계좌 {registered['present']}/{registered['expected']} 확인"
            if info.get('stale'):detail+=' · 일부 자료'
            text(x,y+51+i*65,detail,13,MUTED,limit=width*.20-26)
        x=width*.66+10
        card((x,y+10,width-10,bottom-10),'#062A2C','#147568')
        text(x+18,y+20,'통합 실현손익',17)
        profit_box=(width-64,y+17,width-17,y+43)
        card(profit_box,'#145166'); text(width-56,y+22,'상세',12)
        self.hits['profit_details']=profit_box
        annual=data.get('annual_profit',{})
        profit=float(annual['total_krw']) if annual.get('year') == datetime.now(KST).year and annual.get('trading_complete',annual.get('complete')) and annual.get('total_krw') is not None else None
        split=x+(width-x)*.55
        fit(x+18,y+52,('손익 조회 대기' if profit is None else amount(profit)),split-x-28,28,GREEN if (profit or 0)>=0 else '#E58390')
        from dividend_summary import summary as dividend_summary
        dividends=dividend_summary(data)
        yellow='#C9B76D'
        dividend_title='수령배당'+(' (확인분)' if not dividends['complete'] else '')
        title_box=font(17).getbbox('통합 실현손익'); small_box=font(11).getbbox(dividend_title)
        title_center=y+20+(title_box[1]+title_box[3])/2
        text(split,title_center-(small_box[1]+small_box[3])/2,dividend_title,11,yellow)
        fit(split,y+52,amount(dividends['amount']),width-split-22,22,yellow)
        dividend_rate=dividends['rate']
        text(split,y+83,'투자배당률 '+('—' if dividend_rate is None else f'{dividend_rate:.2%}'),11,yellow)
        # Render two readable text lines inside the card; never squeeze a bitmap.
        caption=f'{datetime.now(KST).year}년 매매손익 + 세후 배당'
        detail='해외 금액 원화환산'
        if not annual.get('dividend_complete',False):detail+=' · 배당 일부 미확인'
        text(x+18,y+108,caption,13,MUTED,limit=width-x-44)
        text(x+18,y+128,detail,12,MUTED,limit=width-x-44)
        # Keep eight or more visible rows and balanced panel widths.
        py=270; ph=height-py-4
        leftw=int(width*.38); midw=int(width*.27); rightx=leftw+midw+gap*2
        boxes=[(0,py,leftw,height-4),(leftw+gap,py,leftw+gap+midw,height-4),(rightx,py,width,height-4)]
        for box in boxes: card(box)
        for box,title in zip(boxes,('보유종목 비중','계좌 비중 차트','최근 체결 내역')):
            icon(d,'chart',box[0]+16,py+21)
            text(box[0]+50,py+18,title,20,bold=True)
        detail_box=(leftw-90,py+17,leftw-14,py+45)
        card(detail_box,'#0C2B42'); text(leftw-80,py+23,'상세 보기',12)
        self.hits['holding_details']=detail_box
        add_box=(leftw-124,py+17,leftw-96,py+45)
        card(add_box,'#0C2B42'); text(leftw-117,py+21,'+',16)
        self.hits['assets']=add_box
        basis=data.get('chart_basis','purchase')
        for i,(label,key) in enumerate((('매입','purchase'),('평가','evaluation'),('계좌별','account'),('진입주기별','cycle'))):
            x=boxes[1][0]+14+i*(midw-28)/4
            box=(x,py+51,x+(midw-28)/4-4,py+78)
            card(box,('#B8DC9B' if terminal else '#145166') if basis==key else '#0C2B42')
            text(x+7,py+56,label,12,'#031522' if terminal and basis==key else TEXT)
            self.hits['chart_'+key]=box
        fx_label = f" (원달러: {data['fx']:,.0f}원)" if data.get('fx') else ' (원달러: 확인 대기)'
        from data_reader import age
        fx_age = age(data.get('fx_at'))
        if data.get('fx') and (fx_age is None or fx_age >= 3600):
            fx_label += ' · 환율 갱신 대기'
        text(16,py+54,'현금 포함 · '+('전체 계좌' if data.get('full_account') else 'ATS 기록 종목')+fx_label,11,MUTED)
        history=data.get('trade_history',{})
        history_label = f"{history.get('year',datetime.now().year)}년 · {len(data.get('fills',[])):,}건"
        if history.get('includes_recent_accounts'):
            history_label = f"계좌별 조회 범위 · {len(data.get('fills',[])):,}건"
        if not history.get('complete'):history_label+=' · 연간 조회 대기'
        elif history.get('error'):history_label+=' · 갱신 재시도'
        elif not history.get('profit_complete'):history_label+=' · 손익 조회 대기'
        text(rightx+16,py+54,history_label+' · 클릭하여 근거 보기',11,MUTED)
        tabley=py+80; rowheight=max(22,min(43,(height-tabley-42)//8)); rows=max(1,int((height-tabley-42)//rowheight))
        self.visible_rows=rows
        def table(box,columns,items,offset,kind):
            x1,_,x2,_=box; inner=x2-x1-30
            fs=max(12,min(16,int(inner/32)))
            d.rectangle((x1+12,tabley,x2-12,tabley+32),fill='#0C2B42')
            sort_keys=('name','mode','entry','krw','weight')
            for column_index,(frac,title) in enumerate(columns):
                label=title
                if kind=='holdings':
                    sort_key=sort_keys[column_index]
                    if data.get('holding_sort')==sort_key:label+=(' ▼' if data.get('holding_desc') else ' ▲')
                    right=columns[column_index+1][0] if column_index+1<len(columns) else 1
                    self.hits['sort_'+sort_key]=(x1+12+inner*frac,tabley,x1+12+inner*right,tabley+32)
                text(x1+18+inner*frac,tabley+7,label,12,MUTED,True)
            items=items or []
            offset=min(offset,max(0,len(items)-rows))
            for i,row in enumerate(items[offset:offset+rows]):
                yy=tabley+32+i*rowheight
                if i%2==0: d.rectangle((x1+12,yy,x2-12,yy+rowheight),fill=PANEL if modern else '#071A2A')
                d.line((x1+12,yy+rowheight,x2-12,yy+rowheight),fill='#102D3E')
                if kind=='holdings':
                    name=row.get('name',row['symbol']); suffix=f"({row['symbol']})" if row.get('symbol') else ''
                    short=name
                    while font(fs).getlength(short+suffix)>inner*.34-18 and len(short)>2:short=short[:-2]+'…'
                    dot_y=yy+rowheight/2
                    d.ellipse((x1+18,dot_y-3,x1+24,dot_y+3),fill=BROKER_COLORS.get(row.get('broker','kiwoom'),BROKER_COLORS['kiwoom']))
                    text(x1+28,yy+rowheight*.24,short+suffix,fs)
                    self.row_hits.append(((x1+12,yy,x1+18+inner*.34,yy+rowheight),row))
                    mode=row.get('mode','전략')
                    colors={'일봉':'#72D3F4','주봉':'#C79DE8','배당':'#7DC9A0','채권':'#AA82C4','전략':'#D9BE68'}
                    light_colors={'배당':'#21865B','채권':'#8055A0','전략':'#997313'}
                    color=light_colors.get(mode,colors.get(mode,MUTED)) if modern else colors.get(mode,MUTED)
                    bx=x1+18+inner*.34
                    bh=min(26,rowheight-6); by=yy+(rowheight-bh)/2
                    d.rounded_rectangle((bx,by,bx+40,by+bh),radius=5,outline=color,fill='#092A3E')
                    text(bx+6,yy+(rowheight-16)/2-1,mode,13,color)
                    entry = row.get('entry')
                    entry_text = f"{entry['count']}({entry['initial']:g})" if entry else '—'
                    text(x1+18+inner*.49,yy+rowheight*.24,entry_text,11,limit=inner*.20-4)
                    val=amount(row.get('krw'),'원')
                    text(x1+18+inner*.70,yy+rowheight*.24,val,max(12,fs-2),limit=inner*.20)
                    text(x1+18+inner*.91,yy+rowheight*.24,'—' if row.get('weight') is None else f"{row['weight']:.0%}",max(12,fs-2))
                else:
                    self.fill_hits.append(((x1+12,yy,x2-22,yy+rowheight),row))
                    name=row.get('name',row['symbol']); name=f"{name}({row['symbol']})" if name!=row['symbol'] else row['symbol']; side='매수' if row.get('side')=='BUY' else '매도'
                    if row.get('account_alias'):
                        name = row['account_alias'] + ' · ' + name
                    try:
                        stamp=datetime.fromisoformat(row.get('at','')).astimezone()
                        text(x1+18,yy+1,stamp.strftime('%m/%d'),9,MUTED)
                        if not row.get('date_only'):text(x1+18,yy+12,stamp.strftime('%H:%M'),9,MUTED)
                    except (ValueError,TypeError):text(x1+18,yy+4,'—',11,MUTED)
                    dot_x=x1+18+inner*.14
                    dot_y=yy+rowheight/2
                    d.ellipse((dot_x,dot_y-3,dot_x+6,dot_y+3),fill=BROKER_COLORS.get(row.get('broker','kiwoom'),BROKER_COLORS['kiwoom']))
                    text(dot_x+10,yy+rowheight*.24,name,fs,limit=inner*.24-15)
                    text(x1+18+inner*.39,yy+rowheight*.24,side,12,'#6AB7E6' if side=='매수' else '#E08D99')
                    text(x1+18+inner*.49,yy+rowheight*.24,f"{row.get('quantity',0):g}",12)
                    profit=row.get('profit'); val=amount(profit,'원' if row.get('market')=='domestic' else '$') if side=='매도' else '—'
                    text(x1+18+inner*.59,yy+rowheight*.24,val,11,GREEN if (profit or 0)>=0 else '#E58390',limit=inner*.23)
                    rate=row.get('rate'); text(x1+18+inner*.84,yy+rowheight*.24,f'{rate:+.2%}' if rate is not None and profit is not None else '—',11,MUTED)
            if not items: text(x1+25,tabley+75,'표시할 기록이 없습니다.',13,MUTED)
            if len(items)>rows:
                track=(height-tabley-44); thumb=max(24,track*rows/len(items)); sy=tabley+38+(track-thumb)*offset/max(1,len(items)-rows)
                bar_width=6.6 if scroll_hover==kind else 6
                d.rounded_rectangle((x2-3-bar_width,sy,x2-3,sy+thumb),radius=bar_width//2,fill='#729DB5' if scroll_hover==kind else '#537D97')
            self.hits[kind]=(x1,tabley,x2,height-4)
        table(boxes[0],[(0,'종목(코드)'),(.34,'진입주기'),(.49,'진입횟수'),(.70,'매입액'),(.91,'비중')],data.get('holdings',[]),offsets[0],'holdings')
        table(boxes[2],[(0,'일시'),(.14,'종목'),(.39,'구분'),(.49,'수량'),(.59,'실현손익'),(.84,'수익률')],data.get('fills',[]),offsets[1],'fills')
        # Antialiased chart drawn at 2x resolution.
        bx=boxes[1]; cx=(bx[0]+bx[2])/2; cy=py+80+(ph-105)/2; radius=min(midw*.43,(ph-100)*.48)
        chart=Image.new('RGBA',(int(radius*4+8),int(radius*4+8)),(0,0,0,0)); cd=PaletteDraw(chart,modern)
        r=radius*2; bounds=(4,4,4+2*r,4+2*r); start=-90
        weight_key='evaluation_weight' if basis=='evaluation' else 'weight'
        holdings=[dict(h,weight=h[weight_key],chart_basis=basis) for h in data.get('chart_rows',data.get('holdings',[])) if h.get(weight_key) is not None and h[weight_key]>0]
        for index,row in enumerate(holdings):
            extent=row['weight']*360
            slice_color=(BROKER_COLORS.get(row.get('broker'),COLORS[index%len(COLORS)])
                         if basis=='account' else theme.chart_color(index,row.get('is_cash'),COLORS[index%len(COLORS)],data.get('theme')))
            cd.pieslice(bounds,start,start+extent,fill=slice_color,outline=PANEL,width=2)
            self.slices.append((start,extent,row)); start+=extent
        cd.ellipse((4+r*.51,4+r*.51,4+r*1.49,4+r*1.49),fill=PANEL)
        chart=chart.resize((int(radius*2+4),int(radius*2+4)),Image.Resampling.LANCZOS)
        im.paste(chart,(int(cx-radius-2),int(cy-radius-2)),chart)
        d=PaletteDraw(im,modern)
        if not holdings:
            text(cx-font(14).getlength('자료 확인 대기')/2,cy-10,'자료 확인 대기',14,MUTED)
        for slice_index,(start,extent,row) in enumerate(self.slices):
            a=math.radians(start+extent/2)
            pct=row['weight']*100
            value=f'{pct:.0f}%' if pct>=5 else (f'{pct:.1f}%' if pct>=.1 else '<0.1%')
            if extent>=19 and math.radians(extent)*radius*.75>=font(15).getlength(value)+8:
                label_color=TEXT
                selected_theme=data.get('theme') or theme.CURRENT
                if selected_theme not in ('default','toss'):
                    shade=int(theme.chart_color(slice_index,row.get('is_cash'),COLORS[slice_index%len(COLORS)],selected_theme)[1:3],16)
                    label_color='#000000' if shade>=130 else '#FFFFFF'
                text(cx+math.cos(a)*radius*.75-font(15).getlength(value)/2,cy+math.sin(a)*radius*.75-10,value,15,label_color)
        if terminal:
            d.ellipse((cx-radius-5,cy-radius-5,cx+radius+5,cy+radius+5),outline='#527E50',width=1)
            for tick in range(0,360,15):
                a=math.radians(tick)
                d.line((cx+math.cos(a)*(radius+6),cy+math.sin(a)*(radius+6),cx+math.cos(a)*(radius+10),cy+math.sin(a)*(radius+10)),fill='#80B56E',width=1)
        self.chart_geometry=(cx,cy,radius)
        text(bx[0]+16,height-30,('평가 기준 '+clock(data.get('cash_at'))) if basis=='evaluation' else ('진입주기별 · 매입액 + 현금 기준' if basis=='cycle' else '종목 위에 마우스를 올려 상세 보기'),10,MUTED)
        if hover:
            px,pyh,row=hover; tw=280; tx=min(width-tw-10,max(8,px+12))
            lines=[]; line=''
            for char in (row.get('name','')+(f"({row['symbol']})" if row.get('symbol') else '')):
                if font(13,True).getlength(line+char)>tw-24:lines.append(line);line=''
                line+=char
            lines.append(line)
            if row.get('is_cash'):
                parts=row.get('cash_by_broker',{})
                details=['현금 합계  '+amount(row.get('krw')),
                         '키움 예수금  '+amount(parts.get('kiwoom'))]
                if 'kis' in parts:details.append('한투 예수금  '+amount(parts.get('kis')))
                details+=['비중  '+f"{row['weight']:.1%}",row.get('cash_basis',''),row.get('status','')]
            elif row.get('cycle_group'):
                details=['진입주기: '+row['name'],'매입액 합계  '+amount(row['krw']),'비중  '+f"{row['weight']:.1%}",f"포함 자산 {row['quantity']}개"]
            elif row.get('side') in ('BUY','SELL'):
                side='매수' if row['side']=='BUY' else '매도'
                currency='원' if row.get('market')=='domestic' else ' USD'
                try:stamp=datetime.fromisoformat(row.get('at','')).astimezone().strftime('%m/%d' if row.get('date_only') else '%m/%d %H:%M')
                except (ValueError,TypeError):stamp='시각 미확인'
                details=["계좌 "+(row.get('account_alias') or ('한투' if row.get('broker')=='kis' else '키움')),f"{stamp} · {side} {row.get('quantity',0):g}주",'가격  '+amount(row.get('price'),currency)]
                if side=='매도':
                    rate=row.get('rate')
                    details.append('손익  '+amount(row.get('profit'),currency)+(f' ({rate:+.2%})' if rate is not None else ''))
                if row.get('profit_source'): details.append('손익 출처: '+({'kis':'한투','other':'기타'}.get(row.get('broker'),'키움'))+' 종목별 실현손익')
                if row.get('aggregated'): details.append('같은 날 매도 합산')
                if row.get('rate_source') == 'net_profit_over_broker_cost': details.append('수익률: 순손익 ÷ 증권사 매입금액')
                details.append('근거: '+(row.get('reason') or ''))
            else:
                evaluation=row.get('chart_basis')=='evaluation'
                details=['증권사: '+({'kis':'한투','other':'기타'}.get(row.get('broker'),'키움')),('평가액  ' if evaluation else '매입액  ')+amount(row.get('evaluation_krw') if evaluation else row.get('krw')),'비중     '+(f"{row['weight']:.1%}" if row.get('weight') is not None else '—')]
                entry=row.get('entry')
                if row.get('broker')!='kis':details.append('진입횟수: '+(f"{entry['count']}회 · {entry['label']}주 (최초+추가)" if entry else '구분 기록 확인 대기'))
            if row.get('note'):details.append('비고: '+row['note'])
            wrapped=[]
            for detail in details:
                line=''
                for char in detail:
                    if font(12).getlength(line+char)>tw-24:wrapped.append(line);line=''
                    line+=char
                wrapped.append(line)
            th=22+len(lines)*20+len(wrapped)*21
            ty=max(8,min(height-th-5,pyh-th))
            card((tx,ty,tx+tw,ty+th),'#0D2C43','#39738F')
            for i,line in enumerate(lines):text(tx+12,ty+8+i*20,line,13,bold=True)
            for i,line in enumerate(wrapped):text(tx+12,ty+12+len(lines)*20+i*21,line,12)
        return im if modern else theme.image(im,data.get('theme'))


class DashboardView(tk.Canvas):
    def __init__(self,parent,on_start,on_stop,on_refresh=None):
        super().__init__(parent,bg=BG,highlightthickness=0)
        self.painter=DashboardPainter(); self.data={}; self.offsets=[0,0]; self.hover=None; self.scroll_hover=None; self.drag_key=None; self.selected_fill=None; self.drag_origin=None; self.drag_moved=False; self.drag_bar=False
        self.chart_basis='purchase'; self.details_window=None
        self.holding_sort=None; self.holding_desc=False
        self.profit_window=None; self.on_refresh=on_refresh; self.profit_scope='combined'
        self.commands={'start':on_start,'stop':on_stop,'holding_details':self.show_holding_details,'profit_details':self.show_profit_details,
                       'assets':self.manage_assets,'chart_cycle':lambda:self.set_chart_basis('cycle'),'chart_account':lambda:self.set_chart_basis('account'),
                       'chart_purchase':lambda:self.set_chart_basis('purchase'),
                       'chart_evaluation':lambda:self.set_chart_basis('evaluation')}; self.pending=None
        for sort_key in ('name','mode','entry','krw','weight'):
            self.commands['sort_'+sort_key]=lambda key=sort_key:self.sort_holdings(key)
        self.spinner_job=None; self.spinner_angle=0
        self.bind('<Configure>',lambda e:self.schedule())
        self.bind('<Button-1>',self.click)
        self.bind('<B1-Motion>',self.drag_scroll)
        self.bind('<ButtonRelease-1>',self.end_drag)
        self.bind('<MouseWheel>',self.scroll)
        self.bind('<Motion>',self.motion)
        self.bind('<Leave>',lambda e:self.clear_hover())
    def update_data(self,data):
        self.source_data=data
        from manual_assets import apply
        self.data=apply(data); self.redraw()
        self.update_holding_details()
        self.update_profit_details()
        if getattr(self,'_applied_theme',None)!=theme.CURRENT:
            theme.widgets(self.winfo_toplevel())
            self._applied_theme=theme.CURRENT
    def sort_holdings(self,key):
        self.holding_desc=not self.holding_desc if self.holding_sort==key else False
        self.holding_sort=key;self.offsets[0]=0
        self.hover=None;self.selected_fill=None
        self.redraw()

    def set_chart_basis(self, basis):
        self.chart_basis=basis
        self.hover=None; self.selected_fill=None
        self.redraw()

    def manage_assets(self):
        from desktop_platform import read_preferences, save_preferences
        window=tk.Toplevel(self); window.title('기타 자산 관리'); window.geometry('480x340')
        center_popup(window,self)
        listing=tk.Listbox(window); listing.pack(fill='both',expand=True,padx=12,pady=12)
        def refresh():
            listing.delete(0,'end')
            for row in read_preferences().get('manual_assets',[]):
                code=f" ({row['code']})" if row.get('code') else ''
                listing.insert('end',f"{row['name']}{code}  {row['amount']:,.0f}원")
            self.update_data(getattr(self,'source_data',self.data))
        def edit(index=None):
            rows=read_preferences().get('manual_assets',[])
            item=rows[index] if index is not None and index<len(rows) else {}
            dialog=tk.Toplevel(window);dialog.title('자산 추가' if index is None else '자산 수정')
            dialog.geometry('500x390');dialog.resizable(False,False)
            center_popup(dialog,self)
            form=tk.Frame(dialog);form.pack(fill='both',expand=True,padx=20,pady=16)
            form.columnconfigure(1,weight=1)
            fields={}
            for i,(key,label) in enumerate((('name','종목명'),('code','코드 (선택)'),('mode','진입주기'),('amount','매입액 (원)'),('evaluation_amount','현재 평가액 (원, 선택)'),('note','비고'))):
                tk.Label(form,text=label,anchor='w',font=('맑은 고딕',11)).grid(row=i,column=0,sticky='w',padx=(0,14),pady=8)
                fields[key]=tk.StringVar(value='' if item.get(key) is None else str(item.get(key,'')))
                entry=tk.Entry(form,textvariable=fields[key],font=('맑은 고딕',12));entry.grid(row=i,column=1,sticky='ew',pady=8)
                if i==0:dialog.after(100,entry.focus_set)
            def save():
                row={key:value.get().strip() for key,value in fields.items()}
                try:
                    row['amount']=float(row['amount'].replace(',',''))
                    raw_evaluation=row.get('evaluation_amount','').replace(',','')
                    row['evaluation_amount']=float(raw_evaluation) if raw_evaluation else None
                    if row['evaluation_amount'] is not None and (not math.isfinite(row['evaluation_amount']) or row['evaluation_amount']<0):raise ValueError()
                    if not row['name'] or not math.isfinite(row['amount']) or row['amount']<=0:raise ValueError()
                except ValueError:
                    messagebox.showerror('입력 확인','종목명·양수 매입액을 입력하세요. 평가액은 비워두거나 0 이상으로 입력하세요.',parent=dialog);return
                pref=read_preferences();current=pref.get('manual_assets',[])
                if index is None:current.append(row)
                elif index<len(current):current[index]=row
                pref['manual_assets']=current;save_preferences(pref);dialog.destroy();refresh()
            buttons=tk.Frame(dialog);buttons.pack(pady=(0,16))
            tk.Button(buttons,text='저장',width=10,command=save).pack(side='left',padx=6)
            tk.Button(buttons,text='취소',width=10,command=dialog.destroy).pack(side='left',padx=6)
            theme.widgets(dialog);dialog.grab_set()
        def selected_edit():
            selected=listing.curselection()
            if selected:edit(selected[0])
        def remove():
            selected=listing.curselection()
            if not selected:return
            pref=read_preferences();rows=pref.get('manual_assets',[])
            if selected[0]<len(rows):rows.pop(selected[0])
            pref['manual_assets']=rows;save_preferences(pref);refresh()
        buttons=tk.Frame(window);buttons.pack(fill='x',padx=12,pady=(0,12))
        for label,command in (('+ 추가',lambda:edit()),('선택 자산 수정',selected_edit),('선택 자산 삭제',remove)):
            tk.Button(buttons,text=label,command=command).pack(side='left',padx=4)
        listing.bind('<Double-1>',lambda event:selected_edit())
        refresh();theme.widgets(window)

    def show_holding_details(self):
        if self.details_window is not None and self.details_window.winfo_exists():
            self.details_window.lift(); return
        window=tk.Toplevel(self); self.details_window=window
        window.title('보유종목 상세'); window.configure(bg=BG)
        window.geometry('1280x480'); window.minsize(700,300); center_popup(window,self)
        self.detail_caption=tk.Label(window,bg=BG,fg=TEXT,anchor='w',font=('맑은 고딕',11))
        self.detail_caption.pack(fill='x',padx=16,pady=12)
        toolbar=tk.Frame(window,bg=BG)
        toolbar.pack(fill='x',padx=16,pady=(0,6))
        tk.Button(toolbar,text='진입주기 수정',command=self.edit_holding_mode,font=('맑은 고딕',10),bg=EDGE,fg=TEXT,activebackground='#314B61',activeforeground=TEXT,bd=0,padx=10,pady=4,cursor='hand2').pack(side='right')
        frame=tk.Frame(window,bg=BG);frame.pack(fill='both',expand=True,padx=16)
        columns=('broker','name','mode','entry','next_buy','atr_stop','qty','purchase','evaluation','weight','evaluation_weight')
        self.detail_table=ttk.Treeview(frame,columns=columns,show='headings')
        self._detail_holdings=None
        for key,title,width in zip(columns,('증권사','종목','진입주기','진입횟수 (최초+추가 주)','추매가','손절가','보유수량','매입액 (원)','평가액 (원)','매입비중','평가비중'),(65,190,90,160,110,110,80,130,130,85,85)):
            self.detail_table.heading(key,text=title); self.detail_table.column(key,width=width,anchor='w' if key=='name' else 'e',stretch=False)
        vertical=SlimScrollbar(frame,orient='vertical',command=self.detail_table.yview)
        horizontal=SlimScrollbar(frame,orient='horizontal',command=self.detail_table.xview)
        self.detail_table.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        self.detail_table.grid(row=0,column=0,sticky='nsew');vertical.grid(row=0,column=1,sticky='ns');horizontal.grid(row=1,column=0,sticky='ew')
        tk.Label(window,text='추가매수: 최근 진입가 + 진입 당시 ATR × 설정배수 이상 / 일봉 ATR 손절: 최근 진입가 − 3ATR 미만.\n가격 단위: 국내 원 · 해외 USD. 주봉은 기준선 매도 적용. 기록 부족·수량 불일치는 — 표시.\n표시 가격은 조건 기준값이며 주문·체결 보장이 아닙니다. 추가매수 횟수·자금 제한과 다른 매도 조건도 적용됩니다.',bg=BG,fg=MUTED,anchor='w',justify='left').pack(fill='x',padx=16,pady=12)
        self.update_holding_details()
        theme.widgets(window)

    def update_holding_details(self):
        if self.details_window is None or not self.details_window.winfo_exists():return
        self.detail_caption.configure(text='보유종목 상세 · 계좌 기준 '+clock(self.data.get('cash_at'))+' · 매입액/평가액은 원화 환산 · ATR 가격은 저장된 최근 진입 기준')
        tree=self.detail_table
        current_rows=self.data.get('holdings',[])
        if current_rows==getattr(self,'_detail_holdings',None):return
        self._detail_holdings=current_rows
        from manual_assets import mode_key
        selected_keys={mode_key(self.detail_rows[item]) for item in tree.selection() if item in getattr(self,'detail_rows',{})}
        position=tree.yview()
        tree.delete(*tree.get_children())
        self.detail_rows={}
        for row in self.data.get('holdings',[]):
            entry=row.get('entry')
            levels=row.get('atr_levels') or {}
            currency=' USD' if row.get('market')=='us' else '원'
            # Presentation rounding only; stored thresholds and ATS decisions are untouched.
            digits=2 if row.get('market')=='us' else 0
            level_price=lambda value: '—' if value is None else f'{value:,.{digits}f}'+currency
            next_buy=levels.get('addon_status') or level_price(levels.get('next_buy'))
            stop=levels.get('stop_status') or level_price(levels.get('stop'))
            weights=[f"{row[key]:.1%}" if row.get(key) is not None else '—' for key in ('weight','evaluation_weight')]
            item=tree.insert('', 'end',values=(({'kis':'한투','other':'기타'}.get(row.get('broker'),'키움')),(row.get('name','')+(f" ({row['symbol']})" if row.get('symbol') else '')),row.get('mode','—'),
                        f"{entry['count']}회 ({entry['label']}주)" if entry else '—',next_buy,stop,f"{row.get('quantity',0):g}",amount(row.get('krw'),''),amount(row.get('evaluation_krw'),''),*weights))
            self.detail_rows[item]=row
            if mode_key(row) in selected_keys:tree.selection_add(item)
        if position:tree.yview_moveto(position[0])

    def edit_holding_mode(self):
        from manual_assets import editable_mode, mode_key
        from desktop_platform import read_preferences, save_preferences
        selected=self.detail_table.selection()
        if not selected:
            messagebox.showinfo('진입주기 수정','먼저 표에서 수정할 종목을 선택해 주세요.',parent=self.details_window)
            return
        row=self.detail_rows.get(selected[0],{})
        if not editable_mode(row):
            messagebox.showinfo('진입주기','ATS 관리종목이거나 관리 여부 확인이 필요한 종목입니다.',parent=self.details_window)
            return
        dialog=tk.Toplevel(self.details_window);dialog.title('진입주기 수정');dialog.geometry('380x170')
        center_popup(dialog,self.details_window)
        tk.Label(dialog,text=row.get('name',row.get('symbol','')),font=('맑은 고딕',12)).pack(pady=12)
        value=tk.StringVar(value=row.get('mode',''))
        field=tk.Entry(dialog,textvariable=value,font=('맑은 고딕',13));field.pack(fill='x',padx=20)
        def save():
            mode=value.get().strip()
            if not mode or len(mode)>20:
                messagebox.showerror('입력 확인','진입주기는 1~20자로 입력하세요.',parent=dialog);return
            pref=read_preferences();pref.setdefault('holding_mode_overrides',{})[mode_key(row)]=mode
            save_preferences(pref);dialog.destroy();self.update_data(self.source_data)
        tk.Button(dialog,text='저장',command=save).pack(pady=12)
        theme.widgets(dialog);dialog.grab_set();field.focus_set()

    def show_profit_details(self):
        if self.profit_window is not None and self.profit_window.winfo_exists():
            self.profit_window.lift(); return
        window=tk.Toplevel(self); self.profit_window=window
        window.title('월별 · 연도별 실현손익'); window.geometry('1080x590'); window.minsize(980,400);window.configure(bg=BG); center_popup(window,self)
        top=tk.Frame(window,bg=BG);top.pack(fill='x',padx=16,pady=12)
        self.profit_caption=tk.Label(top,bg=BG,fg=TEXT,anchor='w',justify='left')
        self.profit_caption.pack(side='left',fill='x',expand=True)
        tk.Button(top,text='최신화',command=self.on_refresh,bg=PANEL,fg=TEXT).pack(side='right')
        selector=tk.Frame(window,bg=BG);selector.pack(fill='x',padx=16,pady=(0,6))
        self.profit_buttons={}
        for key,label in (('combined','통합손익'),('kiwoom','키움손익'),('kis','한투손익')):
            button=tk.Button(selector,text=label,command=lambda scope=key:self.set_profit_scope(scope),
                             bg=PANEL,fg=TEXT,relief='flat',padx=20,pady=8)
            button.pack(side='left',padx=(0,8));self.profit_buttons[key]=button
        tabs=ttk.Notebook(window);tabs.pack(fill='both',expand=True,padx=16,pady=6)
        self.profit_tables={}
        for key,title in (('months','월별'),('years','연도별')):
            frame=tk.Frame(tabs,bg=BG);tabs.add(frame,text=title)
            table=ttk.Treeview(frame,columns=('period','domestic','us','dividend','total','status'),show='headings')
            for field,label,width in zip(('period','domestic','us','dividend','total','status'),('기간','국내 매매손익 (원)','해외 매매손익 (USD)','세후 배당 (원)','매매+배당 (원)','자료 상태'),(90,165,165,150,180,180)):
                table.heading(field,text=label);table.column(field,width=width,anchor='e')
            bar=SlimScrollbar(frame,orient='vertical',command=table.yview);table.configure(yscrollcommand=bar.set)
            bar.pack(side='right',fill='y');table.pack(fill='both',expand=True)
            self.profit_tables[key]=table
        tk.Label(window,text='최근 2개 연도(전년도 1월 1일~현재) · 선택 계좌의 실현손익\n해외 손익은 표시 환율로 원화 환산합니다. 환율이 바뀌면 과거 통합 손익도 변합니다.\n배당은 실제 지급일·세후 수령액 기준입니다. 미확인 배당은 0원이 아니며 상단은 매매손익+확인된 배당만 표시합니다.\n투자배당률 = 올해 확인된 세후 배당 ÷ 현재 보유종목 총매입액 (연환산 아님).',bg=BG,fg=MUTED,justify='left').pack(fill='x',padx=16,pady=10)
        self.update_profit_details()
        theme.widgets(window)

    def set_profit_scope(self, scope):
        self.profit_scope=scope
        self.update_profit_details()

    def update_profit_details(self):
        if self.profit_window is None or not self.profit_window.winfo_exists():return
        data=self.data.get('profit_views',{}).get(self.profit_scope,{})
        title={'combined':'통합손익 (키움 + 한투)','kiwoom':'키움손익','kis':'한투손익'}[self.profit_scope]
        for key,button in self.profit_buttons.items():
            button.configure(bg='#145166' if key==self.profit_scope else PANEL)
        state='조회 완료' if data.get('complete') and not data.get('error') else '수집 중 / 갱신 대기'
        fx=self.data.get('fx')
        self.profit_caption.configure(text=title+' · '+str(datetime.now(KST).year-1)+'-01-01 ~ '+datetime.now(KST).strftime('%Y-%m-%d')+' · '+state+'\n환율 '+(amount(fx) if fx else '확인 대기')+' · 최근 완료 '+clock(data.get('at')))
        for key,table in self.profit_tables.items():
            position=table.yview();table.delete(*table.get_children())
            for row in reversed(data.get(key,[])):
                table.insert('', 'end',values=(row['period'],amount(row.get('domestic'),''),amount(row.get('us'),' USD'),amount(row.get('dividend'),''),amount(row.get('total'),''),'확인' if row.get('complete') else ('배당 확인 대기' if row.get('trading_complete') else '매매/배당 조회 대기')))
            if position:table.yview_moveto(position[0])
        theme.widgets(self.profit_window)

    def schedule(self):
        if self.pending: self.after_cancel(self.pending)
        self.pending=self.after(50,self.redraw)
    def redraw(self):
        if self.pending is not None:
            self.after_cancel(self.pending)
        self.pending=None
        if not self.winfo_exists():return
        # Compare render inputs, excluding the reader timestamp that is only shown in the footer.
        visible=dict(self.data)
        visible.pop('read_at',None)
        visible['markets']=[{k:m.get(k) for k in ('status','account_at')} for m in self.data.get('markets',[])]
        render_key=(visible,self.winfo_width(),self.winfo_height(),tuple(self.offsets),self.hover or self.selected_fill,self.scroll_hover,self.chart_basis,self.holding_sort,self.holding_desc,theme.CURRENT,int(datetime.now(KST).timestamp())//60)
        if render_key==getattr(self,'_last_render_key',None):return
        from manual_assets import account_chart, entry_cycle_chart
        rendered_data=account_chart(self.data) if self.chart_basis=='account' else entry_cycle_chart(self.data) if self.chart_basis=='cycle' else self.data
        rendered_data=dict(rendered_data,holding_sort=self.holding_sort,holding_desc=self.holding_desc)
        if self.holding_sort:
            key=self.holding_sort
            def value(row):
                if key=='entry':return (row.get('entry') or {}).get('count')
                result=row.get(key)
                return str(result).casefold() if key in ('name','mode') and result is not None else result
            rows=list(rendered_data.get('holdings',[]))
            known=[r for r in rows if value(r) is not None]
            missing=[r for r in rows if value(r) is None]
            rendered_data['holdings']=sorted(known,key=value,reverse=self.holding_desc)+missing
        self.rendered=self.painter.paint(dict(rendered_data,chart_basis=self.chart_basis),self.winfo_width(),self.winfo_height(),self.offsets,self.hover or self.selected_fill,self.scroll_hover)
        self._last_render_key=render_key
        self.photo=ImageTk.PhotoImage(self.rendered)
        self.delete('all'); self.create_image(0,0,image=self.photo,anchor='nw')
        if self.spinner_job is None: self.animate_status()
    def animate_status(self):
        self.spinner_job=None
        if not self.winfo_exists(): return
        self.delete('status-spinner')
        if self.data.get('lifecycle', 'checking') != 'checking': return
        box=self.painter.hits.get('start')
        if box:
            x=(box[0]+box[2])/2
            scale=(box[3]-box[1])/70
            self.create_arc(x-8*scale,box[1]+42*scale,x+8*scale,box[1]+58*scale,start=self.spinner_angle,extent=270,
                            style='arc',outline=theme.color('#73BCD8'),width=2,tags='status-spinner')
            self.spinner_angle=(self.spinner_angle-30)%360
        self.spinner_job=self.after(90,self.animate_status)
    def destroy(self):
        if self.spinner_job is not None: self.after_cancel(self.spinner_job)
        if self.pending is not None: self.after_cancel(self.pending)
        super().destroy()
    def hit(self,x,y):
        return next((key for key,(x1,y1,x2,y2) in self.painter.hits.items() if x1<=x<=x2 and y1<=y<=y2),None)
    def click(self,e):
        self.selected_fill=None; self.hover=None
        self.drag_key=None; self.drag_origin=None; self.drag_moved=False
        key=self.hit(e.x,e.y)
        callback=self.commands.get(key)
        if callback and (key not in ('start','stop') or self.data.get('lifecycle', 'checking') not in ('checking','busy')):callback()
        elif key in ('holdings','fills'):
            self.drag_key=key
            index=0 if key=='holdings' else 1
            self.drag_origin=(e.x,e.y,self.offsets[index])
            self.drag_bar=e.x>=self.painter.hits[key][2]-20
            if self.drag_bar:
                self.scroll_hover=key
                self.drag_scroll(e)
        self.redraw()

    def drag_scroll(self,e):
        key=self.drag_key
        if key not in ('holdings','fills') or self.drag_origin is None:return
        x1,y1,x2,y2=self.painter.hits[key]
        ox,oy,initial=self.drag_origin
        if not self.drag_bar and not self.drag_moved and abs(e.y-oy)<6:return
        self.drag_moved=True
        self.selected_fill=None;self.hover=None;self.scroll_hover=key
        index=0 if key=='holdings' else 1
        maximum=max(0,len(self.data.get(key,[]))-self.painter.visible_rows)
        if self.drag_bar:
            offset=round(maximum*min(1,max(0,(e.y-y1-32)/max(1,y2-y1-32))))
        else:
            # Drag content naturally: pulling up reveals later rows.
            row_height=max(1,(y2-y1-38)/max(1,self.painter.visible_rows))
            offset=initial+round((oy-e.y)/row_height)
        self.offsets[index]=min(maximum,max(0,offset))
        self.configure(cursor='fleur')
        self.redraw()

    def end_drag(self,e):
        moved=self.drag_moved
        if self.drag_key and not moved and not self.drag_bar:
            for (x1,y1,x2,y2),row in self.painter.fill_hits+self.painter.row_hits:
                if x1<=e.x<=x2 and y1<=e.y<=y2:
                    self.selected_fill=(e.x,e.y,row)
                    break
        self.drag_key=None;self.drag_origin=None;self.drag_moved=False;self.drag_bar=False
        self.hover=None;self.scroll_hover=None
        self.configure(cursor='')
        self.redraw()

    def scroll(self,e):
        self.selected_fill=None
        key=self.hit(e.x,e.y)
        if key not in ('holdings','fills'):return
        i=0 if key=='holdings' else 1
        maximum=max(0,len(self.data.get(key,[]))-self.painter.visible_rows)
        self.offsets[i]=min(maximum,max(0,self.offsets[i]+(-1 if e.delta>0 else 1)*2)); self.redraw()
    def motion(self,e):
        if self.drag_key:return
        self.configure(cursor='hand2' if self.hit(e.x,e.y) in self.commands or any(x1<=e.x<=x2 and y1<=e.y<=y2 for (x1,y1,x2,y2),row in self.painter.fill_hits) else '')
        key=self.hit(e.x,e.y)
        active=key if key in ('holdings','fills') and e.x>=self.painter.hits[key][2]-20 and len(self.data.get(key,[]))>self.painter.visible_rows else None
        changed=active!=self.scroll_hover
        self.scroll_hover=active
        cx,cy,r=self.painter.chart_geometry
        hover=None
        for (x1,y1,x2,y2),row in self.painter.row_hits:
            if x1<=e.x<=x2 and y1<=e.y<=y2:hover=(e.x,e.y,row);break
        if r*.5<=math.hypot(e.x-cx,e.y-cy)<=r:
            angle=math.degrees(math.atan2(e.y-cy,e.x-cx))
            for start,extent,row in self.painter.slices:
                if (angle-start)%360<extent: hover=(e.x,e.y,row);break
        if changed or (hover is None)!=(self.hover is None) or (hover and (not self.hover or hover[2]!=self.hover[2])):
            self.hover=hover;self.redraw()
    def clear_hover(self):
        if self.hover or self.scroll_hover:
            self.hover=None
            self.scroll_hover=self.drag_key
            self.redraw()
