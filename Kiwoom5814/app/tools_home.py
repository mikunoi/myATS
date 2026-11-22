"""Two local calculation tools; no trading actions."""
import tkinter as tk
from average_cost import PALETTES, Card, SoftButton
import dashboard_theme as theme


def build(app):
    p=PALETTES.get(theme.CURRENT,PALETTES['default'])
    root=tk.Frame(app.content,bg=p['page']);root._owns_theme=True
    root.pack(fill='both',expand=True,padx=(0,12))
    def label(parent,text,size=12,muted=False):
        return tk.Label(parent,text=text,bg=parent.cget('bg'),fg=p['muted' if muted else 'text'],font=('맑은 고딕',size,'bold' if size>=18 else 'normal'),anchor='w',justify='left')
    label(root,'투자 도구',24).pack(anchor='w',pady=(8,8))
    label(root,'계산할 도구를 선택하세요. 보유종목 자료로 바로 시작할 수 있어요.',12,True).pack(anchor='w',pady=(0,24))
    for name,description,detail in (
        ('평단관리','추가 매수하면 평균단가가 얼마나 바뀔까요?','보유종목 불러오기 · 매수 회차 추가 · 예상 평균단가 비교'),
        ('비중계산','내 자금의 몇 퍼센트를 투자할까요?','비중 ↔ 금액 계산 · 보유종목 목표 비중 · 추가하거나 줄일 금액')):
        card=Card(root,p);card.pack(fill='x',pady=(0,18))
        label(card.body,name,20).pack(anchor='w')
        label(card.body,description,14).pack(anchor='w',pady=(8,8))
        label(card.body,detail,11,True).pack(anchor='w')
        SoftButton(card.body,name+' 열기',lambda title=name:app.page(title),p,primary=True,width=170,height=46).pack(anchor='e',pady=(12,0))
    label(root,'계산 전용 도구이며 실제 주문이나 계좌에는 반영되지 않습니다.',11,True).pack(anchor='w')
    return root
