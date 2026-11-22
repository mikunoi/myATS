def tr(text):
    return text
"""Five explicitly saved scenarios, each with its original local asset snapshot."""
import os,json,uuid
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from tkinter import simpledialog,messagebox
import tkinter as tk
from tkinter import ttk

from desktop_platform import preferences_dir

PATH=preferences_dir()/'allocation-goals.json'

def read():
    if not PATH.exists():return []
    value=json.loads(PATH.read_text(encoding='utf-8'))
    if not isinstance(value,list):raise ValueError(tr('목표 저장 파일 형식이 올바르지 않습니다.'))
    return value[:5]

def save(view):
    try:
        if not view.plans:raise ValueError(tr('먼저 저장할 목표를 추가해 주세요.'))
        from allocation_tool import scenario
        scenario(view.data,view.plans,view.extra.get())
        name=simpledialog.askstring(tr('목표 저장'),tr('저장할 목표 이름'),initialvalue=datetime.now().strftime(tr('%m/%d %H:%M 목표')),parent=view.winfo_toplevel())
        if name is None:return
        item=dict(id=uuid.uuid4().hex,name=name.strip() or tr('목표'),at=datetime.now().isoformat(timespec='seconds'),data=view.data,plans=view.plans,extra=view.extra.get())
        items=[item]+read();items=items[:5]
        PATH.parent.mkdir(parents=True,exist_ok=True)
        tmp=PATH.with_suffix('.tmp');tmp.write_text(json.dumps(items,ensure_ascii=False,indent=2,default=str),encoding='utf-8');tmp.replace(PATH)
        messagebox.showinfo(tr('목표 저장'),tr('저장했습니다. 최근 5개까지 보관합니다.'),parent=view.winfo_toplevel())
    except Exception as e:messagebox.showerror(tr('목표 저장 실패'),str(e),parent=view.winfo_toplevel())

def load(view):
    try:items=read()
    except Exception as e:messagebox.showerror(tr('목표 불러오기'),str(e),parent=view.winfo_toplevel());return
    if not items:messagebox.showinfo(tr('목표 불러오기'),tr('저장한 목표가 없습니다.'),parent=view.winfo_toplevel());return
    parent=view.winfo_toplevel();p=view.p
    win=tk.Toplevel(parent);win.withdraw();win.title(tr('최근 목표 불러오기'));win.transient(parent);win.configure(bg=p['page'])
    tk.Label(win,text=tr('현재 API 연동 자산에 저장한 목표를 적용합니다. 실제 계좌는 변경하지 않습니다.'),bg=p['page'],fg=p['text'],font=('맑은 고딕',11)).pack(padx=18,pady=16)
    listing=tk.Listbox(win,height=7,bg=p['field'],fg=p['text'],selectbackground=p['button'],selectforeground='white',font=('맑은 고딕',12),exportselection=False)
    listing.pack(fill='both',expand=True,padx=18)
    for item in items:listing.insert('end',f"{item['at'].replace('T',' ')}  ·  {item['name']}")
    listing.selection_set(0)
    def restore():
        try:
            if not listing.curselection():return
            item=items[listing.curselection()[0]]
            from allocation_tool import scenario
            from manual_assets import apply
            current=apply(view.provider() or {})
            holdings=current.get('holdings',[])
            def identity(row):
                return (row.get('broker'),row.get('market'),row.get('symbol') or row.get('name'))
            plans=[]
            for saved in item['plans']:
                plan=dict(saved,key=tuple(saved['key']),value=Decimal(saved['value']))
                if plan['key'][0]=='holding':
                    original=item['data']['holdings'][plan['key'][1]]
                    matches=[i for i,row in enumerate(holdings) if identity(row)==identity(original)]
                    if len(matches)!=1:
                        raise ValueError('현재 보유종목에서 목표 종목을 식별할 수 없습니다: '+plan['name'])
                    plan['key']=('holding',matches[0])
                plans.append(plan)
            scenario(current,plans,item['extra'])
            view.data=current;view.holdings=list(holdings)
            view.finish_edit();view.plans=plans;view.extra.set(item['extra']);view.update_all()
            # Refresh selection labels against the same current snapshot before editing goals.
            labels=['새 종목 직접 입력']
            for row in holdings:
                broker={'kiwoom':'키움','kis':'한투','other':'기타'}.get(row.get('broker'),'키움')
                labels.append(broker+' · '+(row.get('name') or row.get('symbol','')))
            view.choices.config(values=labels);view.choices.current(0);view.choice_changed()
            view.form_status.config(text='현재 API 연동 자산 기준으로 목표를 불러왔습니다.')
            win.destroy()
        except Exception as e:messagebox.showerror(tr('목표 복원 실패'),str(e),parent=win)
    def rename():
        try:
            if not listing.curselection():return
            index=listing.curselection()[0];item=items[index]
            name=simpledialog.askstring(tr('목표 이름 수정'),tr('새 목표 이름'),initialvalue=item['name'],parent=win)
            if name is None:return
            name=name.strip()
            if not name:raise ValueError(tr('목표 이름을 입력해 주세요.'))
            latest=read()
            target=next((r for r in latest if r.get('id')==item['id']),None)
            if target is None:raise ValueError(tr('이 목표가 더 이상 저장되어 있지 않습니다. 창을 다시 열어 주세요.'))
            target['name']=name
            tmp=PATH.with_suffix('.tmp')
            tmp.write_text(json.dumps(latest,ensure_ascii=False,indent=2,default=str),encoding='utf-8');tmp.replace(PATH)
            item['name']=name
            listing.delete(index);listing.insert(index,f"{item['at'].replace('T',' ')}  ·  {name}")
            listing.selection_set(index)
        except Exception as e:messagebox.showerror(tr('목표 이름 수정 실패'),str(e),parent=win)
    actions=tk.Frame(win,bg=p['page']);actions.pack(pady=16)
    ttk.Button(actions,text=tr('이름 수정'),command=rename).pack(side='left',padx=6)
    ttk.Button(actions,text=tr('선택한 목표 불러오기'),command=restore).pack(side='left',padx=6)
    listing.bind('<Double-Button-1>',lambda e:restore())
    win.update_idletasks();w=win.winfo_reqwidth();h=win.winfo_reqheight()
    win.geometry(f'+{max(0,parent.winfo_rootx()+(parent.winfo_width()-w)//2)}+{max(0,parent.winfo_rooty()+(parent.winfo_height()-h)//2)}')
    win.deiconify();win.grab_set()
