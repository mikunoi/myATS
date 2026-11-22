"""Portfolio dashboard with explicit ATS lifecycle controls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

from refresh_schedule import automatic_due, protected_reason, KST
import dashboard_theme as theme
from scroll_support import bind_drag, scroll_page, SlimScrollbar
from data_reader import VERSION, read_dashboard, age
from desktop_platform import (read_preferences, startup_enabled,
                              set_startup, ensure_startup, create_tray, preferences_dir, run_elevated)
from installer import install, validate_target, MANIFEST

APP_NAME = 'myATS'

BG, PANEL, TEXT, MUTED, EDGE = '#031522', '#061E30', '#EDF5FC', '#8DA6B9', '#12435C'
PALETTE = ['#6B8D93', '#849888', '#9F9279', '#8587A0', '#A27F83', '#7296A7', '#939EAD', '#737F9A', '#9A8E9A', '#728A7F']


def money(value, currency='원'):
    return '확인 대기' if value is None else f'{value:,.0f}{currency}'


def timestamp(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime('%m/%d %H:%M:%S')
    except (ValueError, TypeError):
        return '확인 대기'


class Dashboard:
    def __init__(self, root, ats_root, smoke=None):
        self.root, self.ats_root = root, Path(ats_root).resolve()
        self.prefs = read_preferences()
        if os.name != "nt":
            self.prefs.update(minimize_to_tray=False, start_with_windows=False)
        theme.select(self.prefs.get("dashboard_theme","default"))
        self.data = None
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='dashboard-readonly')
        self.control_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dashboard-control')
        self.snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dashboard-account')
        self.account_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='myats-accounts')
        self.managed_refresh_pending = self.managed_refresh_busy = False
        self.api_list_refresh = None
        self.maintenance_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dashboard-maintenance')
        self.events, self.loading, self.closed = queue.Queue(), False, False
        self.snapshot_busy = False
        self.snapshot_pending = False
        self.snapshot_message = ""
        self.startup_busy = False
        self.lifecycle, self.control_busy, self.status_busy = 'checking', False, False
        self.process_states = None
        self.child_lock, self.snapshot_process = threading.Lock(), None
        self.snapshot_path = preferences_dir() / 'account_snapshot.json'
        self.tray, self.hover_after, self.refresh_after = None, None, None
        self.smoke = smoke
        root.title(APP_NAME)
        icon_path=Path(__file__).parent/'assets/ats-icon.png'
        if icon_path.is_file():
            self.app_icon=tk.PhotoImage(file=str(icon_path))
            root.iconphoto(True,self.app_icon)
        root.geometry(f'{min(1500,root.winfo_screenwidth()-60)}x{min(880,root.winfo_screenheight()-90)}')
        root.minsize(min(900, root.winfo_screenwidth() - 60), min(600, root.winfo_screenheight() - 90))
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW', self.close_requested)
        root.bind('<Unmap>', self.on_minimize)
        self.style()
        self.sidebar = tk.Frame(root, bg=PANEL, width=60)
        self.sidebar.pack(side='left', fill='y', padx=(10, 0), pady=12)
        self.sidebar.pack_propagate(False)
        self.flyout = tk.Frame(root, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        self.menu_button = self.nav('☰', self.show_nav, self.sidebar)
        self.nav('◫', lambda: self.page('운용현황'), self.sidebar)
        self.nav('↓', lambda: self.page('설치'), self.sidebar)
        self.nav('≡', lambda: self.page('전략구성'), self.sidebar)
        self.nav('＋', lambda: self.page('도구'), self.sidebar)
        self.nav('⚙', lambda: self.page('설정'), self.sidebar)
        self.nav('▣', self.export, self.sidebar)
        for text, callback in [('운용현황', lambda: self.page('운용현황')), ('설치', lambda: self.page('설치')),
                               ('전략구성', lambda: self.page('전략구성')), ('도구', lambda: self.page('도구')), ('설정', lambda: self.page('설정')), ('포폴 저장', self.export)]:
            self.nav(text, callback, self.flyout)
        self.flyout.bind('<Leave>', lambda e: self.hide_nav_later())
        self.sidebar.bind('<Leave>', lambda e: self.hide_nav_later())
        self.body = tk.Frame(root, bg=BG)
        self.body.pack(side='left', fill='both', expand=True, padx=18, pady=24)
        heading = tk.Frame(self.body, bg=BG)
        heading.pack(fill='x', pady=(0, 18))
        tk.Label(heading, text=APP_NAME, bg=BG, fg=TEXT, font=('맑은 고딕', 20, 'bold')).pack(side='left')
        self.refresh_icon=tk.Canvas(heading,width=38,height=38,bg=BG,highlightthickness=0,cursor='hand2')
        self.refresh_icon.pack(side='left',padx=10)
        self.refresh_icon.bind('<Button-1>',lambda e:self.manual_refresh())
        self.refresh_angle=0
        self.refresh_animating=False
        self.draw_refresh_icon()
        self.version_label = tk.Label(heading, text=f'대시보드 v{VERSION}', bg=BG, fg=MUTED)
        self.version_label.pack(side='right')
        self.content = tk.Frame(self.body, bg=BG)
        self.content.pack(fill='both', expand=True)
        self.footer = tk.Label(self.body, text='기록을 불러오는 중…', bg=BG, fg=MUTED, anchor='w')
        self.footer.pack(fill='x', pady=(12, 0))
        self.page('운용현황')
        root.after(100, self.poll_events)
        self.refresh()
        if not smoke:
            if os.environ.get('DISTRIBUTION_LAUNCHER'):
                from github_update import check
                root.after(2500, lambda: check(root, automatic=True))
            root.after(1500, self.snapshot_tick)
            root.after(2000, self.startup_tick)
        root.after(500, self.lifecycle_tick)
        if smoke:
            root.after(3500, self.smoke_complete)

    def style(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=30, borderwidth=0, font=('맑은 고딕', 10))
        style.configure('Treeview.Heading', background=EDGE, foreground=TEXT, font=('맑은 고딕', 10), padding=8)
        style.map('Treeview', background=[('selected', '#314B61')])
        style.configure('Vertical.TScrollbar', background=EDGE, troughcolor=PANEL, bordercolor=PANEL, arrowcolor=MUTED)

    def nav(self, text, command, parent):
        from PIL import Image, ImageDraw, ImageTk
        from dashboard_view import icon
        button = tk.Button(parent, text=text, command=command, bg=PANEL, fg=TEXT, bd=0,
                           activebackground=EDGE, activeforeground=TEXT, font=('맑은 고딕', 13), padx=12, pady=16)
        if parent == self.sidebar:
            background = '#0A3852' if text == '◫' else PANEL
            tile = Image.new('RGB', (40, 40), background)
            icon(ImageDraw.Draw(tile), {'☰':'menu', '◫':'chart', '↓':'download', '⚙':'settings', '≡':'strategy', '＋':'calculator'}.get(text, 'wallet'), 8, 8)
            button._theme_source_image=tile
            button.nav_image = ImageTk.PhotoImage(theme.image(tile))
            button.configure(image=button.nav_image, text='', bg=background, pady=12, height=50)
        button.pack(fill='x')
        if parent == self.sidebar:
            if text == '☰':
                button.bind('<Enter>', lambda e: self.show_nav())
                button.bind('<Leave>', lambda e: self.hide_nav_later())
            else:
                title = {'◫':'운용현황','↓':'설치','≡':'전략구성','＋':'도구','⚙':'설정','▣':'포폴 저장'}.get(text,text)
                button.bind('<Enter>', lambda e, b=button, t=title: self.show_tab_name(b,t))
                button.bind('<Leave>', lambda e: self.hide_tab_name())
                button.configure(command=lambda: (self.hide_tab_name(), command()))
                if text == '＋':
                    self.tools_button=button
                    button.bind('<Enter>',lambda e:self.show_tools_menu(button))
                    button.bind('<Leave>',lambda e:self.hide_tools_later())
        return button

    def hide_tab_name(self):
        tip=getattr(self,'tab_tip',None)
        if tip is not None:
            tip.destroy();self.tab_tip=None

    def show_tab_name(self,button,title):
        self.hide_tools()
        self.flyout.place_forget()
        self.hide_tab_name()
        self.tab_tip=tk.Label(self.root,text=title,bg=theme.color(PANEL),fg=theme.color(TEXT),
                              font=('맑은 고딕',11),padx=12,pady=8,bd=1,relief='solid')
        self.tab_tip.place(x=74,y=button.winfo_rooty()-self.root.winfo_rooty()+8)
        self.tab_tip.lift()

    def hide_tools(self):
        pending=getattr(self,'tools_hide_after',None)
        if pending:
            self.root.after_cancel(pending);self.tools_hide_after=None
        panel=getattr(self,'tools_popup',None)
        if panel is not None:panel.place_forget()

    def keep_tools(self,event=None):
        pending=getattr(self,'tools_hide_after',None)
        if pending:
            self.root.after_cancel(pending);self.tools_hide_after=None

    def hide_tools_later(self,event=None):
        self.keep_tools()
        def check():
            self.tools_hide_after=None
            widget=self.root.winfo_containing(self.root.winfo_pointerx(),self.root.winfo_pointery())
            while widget:
                if widget in (getattr(self,'tools_button',None),getattr(self,'tools_popup',None)):return
                widget=getattr(widget,'master',None)
            self.hide_tools()
        self.tools_hide_after=self.root.after(240,check)

    def show_tools_menu(self,button):
        self.keep_tools()
        self.hide_tab_name()
        self.flyout.place_forget()
        previous=getattr(self,'tools_popup',None)
        if previous is not None:previous.destroy()
        panel=tk.Frame(self.root,bg=theme.color(PANEL),highlightbackground=theme.color(EDGE),highlightthickness=1)
        self.tools_popup=panel
        for title in ('평단관리','비중계산'):
            item=tk.Button(panel,text=title,anchor='w',bd=0,padx=20,pady=14,font=('맑은 고딕',12),
                           bg=theme.color(PANEL),fg=theme.color(TEXT),activebackground=theme.color(EDGE),
                           activeforeground=theme.color(TEXT),command=lambda name=title:self.page(name))
            item.pack(fill='x')
            item.bind('<Enter>',self.keep_tools)
            item.bind('<Leave>',self.hide_tools_later)
        panel.bind('<Enter>',self.keep_tools)
        panel.bind('<Leave>',self.hide_tools_later)
        panel.place(x=button.winfo_rootx()-self.root.winfo_rootx()+button.winfo_width(),
                    y=button.winfo_rooty()-self.root.winfo_rooty(),width=180)
        panel.lift()

    def show_nav(self):
        self.hide_tools()
        self.hide_tab_name()
        self.flyout.place(x=70, y=12, width=170)
        self.flyout.lift()

    def hide_nav_later(self):
        def hide():
            widget = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
            while widget:
                if widget in (self.menu_button, self.flyout):
                    return
                widget = getattr(widget, 'master', None)
            self.flyout.place_forget()
        self.root.after(160, hide)

    def button(self, parent, text, command):
        result = tk.Button(parent, text=text, command=command, bg=EDGE, fg=TEXT, bd=0,
                           activebackground='#37516A', activeforeground=TEXT, padx=18, pady=10, font=('맑은 고딕', 10))
        return result

    def page(self, title):
        self.hide_tools()
        self.hide_tab_name()
        self.current_page = title
        self.api_list_refresh = None
        self.flyout.place_forget()
        for child in self.content.winfo_children():
            child.destroy()
        if title == '운용현황':
            self.overview()
            if self.data:
                self.render()
        elif title == '전략구성':
            self.strategy_page()
        else:
            outer=self.content
            self.content=scroll_page(outer,BG)
            try:
                if title in ('평단관리', '비중계산'):
                    self.button(self.content, '‹  도구 선택', lambda: self.page('도구')).pack(anchor='w', pady=(0, 10))
                if title == '도구':
                    from tools_home import build
                    build(self)
                elif title == '평단관리':
                    from average_cost import build
                    build(self)
                elif title == '비중계산':
                    from allocation_tool import build
                    build(self)
                elif title == '설치':self.install_page()
                elif title == 'API 관리':
                    from api_page import build
                    build(self)
                else:self.settings_page()
            finally:self.content=outer
        theme.style(self.root)
        theme.widgets(self.root)

    def label(self, parent, text, size=11, color=TEXT):
        label = tk.Label(parent, text=text, bg=BG, fg=color,
                         font=('맑은 고딕', size), anchor='w', justify='left')
        return label

    def overview(self):
        from dashboard_view import DashboardView
        self.dashboard_view = DashboardView(self.content, lambda:self.control_ats('Start'), lambda:self.control_ats('Stop'), on_refresh=self.manual_refresh)
        self.dashboard_view.pack(fill='both', expand=True)

    def refresh(self):
        if self.closed or self.loading:
            return
        if self.refresh_after:
            self.root.after_cancel(self.refresh_after)
            self.refresh_after = None
        self.loading = True
        root = self.ats_root
        future = self.executor.submit(read_dashboard, root, self.snapshot_path)
        future.add_done_callback(lambda f: self.events.put(('data', f)))

    def draw_refresh_icon(self):
        self.refresh_icon.delete('all')
        angle=self.refresh_angle
        self.refresh_icon.create_arc(8,8,30,30,start=angle+35,extent=285,style='arc',outline=theme.color('#73BCD8'),width=2)
        a=math.radians(angle+35)
        x,y=19+11*math.cos(a),19-11*math.sin(a)
        self.refresh_icon.create_polygon(x,y,x-5*math.cos(a)-3*math.sin(a),y+5*math.sin(a)-3*math.cos(a),
                                         x+3*math.sin(a),y+3*math.cos(a),fill=theme.color('#73BCD8'))

    def animate_refresh(self):
        if self.closed:return
        self.refresh_angle=(self.refresh_angle-25)%360
        self.draw_refresh_icon()
        if self.loading or self.snapshot_busy or self.status_busy:
            self.root.after(70,self.animate_refresh)
        else:
            self.refresh_animating=False
            self.refresh_angle=0
            self.draw_refresh_icon()

    def manual_refresh(self):
        self.refresh()
        self.snapshot_tick(reschedule=False,force=True)
        self.lifecycle_tick(reschedule=False)
        if not self.refresh_animating:
            self.refresh_animating=True
            self.animate_refresh()

    def lifecycle_tick(self, reschedule=True):
        if self.closed:
            return
        if not self.status_busy and not self.control_busy:
            from ats_control import invoke
            self.status_busy = True
            self.render()
            future = self.executor.submit(invoke, self.ats_root, 'Status')
            future.add_done_callback(lambda f:self.events.put(('lifecycle',f)))
        if reschedule:
            self.root.after(10000,self.lifecycle_tick)

    def startup_tick(self):
        if self.closed:
            return
        if startup_enabled(self.prefs) and not self.startup_busy:
            self.startup_busy = True
            future = self.maintenance_executor.submit(ensure_startup, self.ats_root)
            future.add_done_callback(lambda f: self.events.put(('startup', f)))
        self.root.after(300000, self.startup_tick)

    def control_ats(self, action):
        if self.control_busy or self.status_busy:
            return
        if action == 'Start' and self.lifecycle in ('running','stopping','unknown','checking','not_ready'):
            messagebox.showinfo('ATS', '프로세스가 실행 중이거나 상태 확인이 필요합니다. 좌측 국내·해외 상태를 확인하세요. 실행 중인 ATS는 이 버튼으로 중복 시작하지 않습니다.')
            return
        message = ('키움 5814 국내·해외 ATS를 시작할까요?\n기존 사전검증을 통과하면 자동매매가 활성화됩니다.' if action=='Start' else
                   '키움 5814 국내·해외 ATS를 중지할까요?\n기존 미체결 주문의 취소는 별도로 확인해야 합니다.')
        if not messagebox.askyesno('ATS 시작 확인' if action=='Start' else 'ATS 중지 확인',message):
            return
        from ats_control import invoke
        self.control_busy=True
        self.render()
        future=self.control_executor.submit(invoke,self.ats_root,action)
        future.add_done_callback(lambda f:self.events.put(('control',f)))

    def submit_account_job(self, work, on_result):
        future = self.account_executor.submit(work)
        future.add_done_callback(lambda f: self.events.put(('account_job', (f, on_result))))

    def refresh_managed_accounts(self, force, protection):
        self.managed_refresh_pending |= force
        if protection or self.managed_refresh_busy or self.control_busy:
            return
        pending, root = self.managed_refresh_pending, self.ats_root
        self.managed_refresh_pending = False
        self.managed_refresh_busy = True
        frequent = self.prefs.get('account_reads', False)
        from managed_snapshots import refresh_due
        def complete(_result):
            self.managed_refresh_busy = False
            if self.api_list_refresh:
                self.api_list_refresh()
        self.submit_account_job(lambda: refresh_due(root, frequent, pending), complete)

    def poll_events(self):
        if self.closed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'data':
                    self.loading = False
                    try:
                        self.data = value.result()
                        if self.process_states is not None:
                            from ats_control import summarize
                            self.lifecycle = summarize(self.process_states, self.data.get('markets', []))
                        self.render()
                    except Exception:
                        self.footer.config(text='자료 조회 실패 · 5초 후 자동 재시도')
                    self.refresh_after = self.root.after(5000, self.refresh)
                elif kind == 'account_job':
                    future, on_result = value
                    try:
                        result = future.result()
                    except Exception:
                        result = None
                    on_result(result)
                    self.refresh()
                elif kind == 'show':
                    self.root.deiconify()
                    self.root.lift()
                elif kind == 'quit':
                    self.quit()
                    return
                elif kind == 'installed':
                    try:
                        result = value.result()
                        messagebox.showinfo('설치 완료', f"설치 위치: {result['target']}\n기존 ATS는 변경하지 않았습니다.\n대시보드 EXE를 실행하세요.")
                    except Exception as error:
                        messagebox.showerror('설치 실패', str(error))
                    self.page('설치')
                elif kind == 'packaged':
                    self.package_busy=False
                    try:
                        install_path, private_path=value.result()
                        messagebox.showinfo('패키지 생성 완료',
                            '설치 ZIP: '+install_path+'\n개인 자료 ZIP: '+private_path+
                            '\n\n개인 ZIP은 암호화되지 않았습니다. 안전한 장소에 보관하세요. 복원 방법은 ZIP 안의 안내문을 확인하세요.')
                    except Exception as error:
                        messagebox.showerror('패키지 생성 실패',str(error))
                    self.footer.config(text='패키지 생성 처리 완료')
                elif kind == 'snapshot':
                    self.snapshot_busy = False
                    if value:
                        self.snapshot_message = '계좌 상세 갱신 완료'
                        self.refresh()
                    else:
                        self.snapshot_message = '계좌/체결 일부 갱신 대기 · 증권사 연결 또는 ATS 보호시간 확인'
                    self.render()
                elif kind == 'lifecycle':
                    self.status_busy=False
                    try:
                        from ats_control import summarize
                        self.process_states = value.result()
                        self.lifecycle=summarize(self.process_states, (self.data or {}).get('markets',[]))
                    except Exception:
                        self.process_states = None
                        self.lifecycle='unknown'
                    self.render()
                elif kind == 'control':
                    self.control_busy=False
                    try:
                        value.result()
                        messagebox.showinfo('ATS', '요청을 처리했습니다. 프로세스·계좌 연결 상태를 다시 확인합니다.')
                    except Exception:
                        messagebox.showerror('ATS', '제어 실패 또는 일부만 처리되었습니다. 표시되는 국내·해외 상태를 확인하세요.')
                    self.refresh()
                    self.lifecycle_tick(reschedule=False)
                elif kind == 'startup':
                    self.startup_busy = False
                    try:
                        value.result()
                    except Exception:
                        self.footer.config(text='Windows 자동시작 등록 확인 실패 · 설정과 Windows 권한을 확인하세요.')
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def render(self):
        data = self.data
        if not data:
            return
        self.version_label.config(text=f"ATS {data['system_version']}  ·  v{VERSION}")
        self.footer.config(text=f"조회 {timestamp(data['read_at'])}  ·  계좌 기록 국내 {timestamp(data['markets'][0]['account_at'])} / 해외 {timestamp(data['markets'][1]['account_at'])}\n"
                               f"{data.get('portfolio_note','키움')} · 한투 {timestamp(data.get('kis_at',''))} · 계좌 상세/예수금 기록 {timestamp(data['cash_at'])}  ·  {'전체 계좌 기록' if data['full_account'] else '계좌 상세 조회 대기'} · {self.snapshot_message or '시작/중지는 버튼 확인 후 실행'}")
        if self.current_page == '운용현황':
            self.dashboard_view.update_data(dict(data,lifecycle='busy' if self.control_busy else ('checking' if self.status_busy else self.lifecycle)))

    def chart_rows(self):
        return [r for r in (self.data or {}).get('chart_rows', []) if r['weight'] is not None and r['weight'] > 0]

    def export(self):
        from manual_assets import apply
        combined=apply(self.data or {})
        rows=[r for r in combined.get('chart_rows',[]) if r.get('weight') is not None and r['weight']>0]
        if not rows:
            messagebox.showinfo('포폴 저장', '비중을 계산할 수 있는 계좌 기록이 없습니다.')
            return
        from export_chart import render_portfolio, FIELDS, DEFAULT_FIELDS
        import copy
        from PIL import Image, ImageTk
        # Capture once: saving writes exactly the image being previewed.
        export_data=copy.deepcopy(combined)
        rows=copy.deepcopy(rows)
        snapshot=render_portfolio(rows,export_data['read_at'],export_data['cash_at'],export_data)
        previous=getattr(self,'portfolio_preview',None)
        if previous is not None and previous.winfo_exists():previous.destroy()
        window=tk.Toplevel(self.root);self.portfolio_preview=window
        from popup_position import center_popup
        center_popup(window,self.root)
        window.title('포트폴리오 저장 미리보기');window.configure(bg=BG)
        window.transient(self.root)
        window.geometry(f'{min(1100,self.root.winfo_screenwidth()-70)}x{min(760,self.root.winfo_screenheight()-100)}')
        window.minsize(600,400)
        chooser=tk.Frame(window,bg=PANEL,width=270);chooser.pack(side='left',fill='y',padx=16,pady=16)
        chooser.pack_propagate(False)
        tk.Label(chooser,text='이미지 설정',font=('맑은 고딕',18,'bold'),bg=PANEL,fg=TEXT).pack(anchor='w',padx=16,pady=(18,14))
        selected={key:tk.BooleanVar(value=key in DEFAULT_FIELDS) for key in FIELDS}
        style=tk.StringVar(value='classic');basis=tk.StringVar(value='purchase');dividends=tk.BooleanVar(value=True)
        hide_capital=tk.BooleanVar(value=False);hide_profit=tk.BooleanVar(value=False)
        def rebuild():
            nonlocal snapshot
            snapshot=render_portfolio(rows,export_data['read_at'],export_data['cash_at'],export_data,
                dict(fields=[k for k,v in selected.items() if v.get()],style=style.get(),basis=basis.get(),dividends=dividends.get(),
                     hide_capital=hide_capital.get(),hide_profit=hide_profit.get()))
            resize(None)
            status.config(text='선택한 항목을 반영했습니다. 미리보기와 동일하게 저장합니다.')
        field_frame=tk.Frame(chooser,bg=BG);field_frame.pack(fill='x')
        privacy_frame=tk.Frame(chooser,bg=PANEL);privacy_frame.pack(before=field_frame,fill='x',pady=(0,8))
        for label,variable in (('총 운용자금 모자이크',hide_capital),('통합 실현손익 모자이크',hide_profit)):
            tk.Checkbutton(privacy_frame,text=label,variable=variable,command=rebuild,bg=PANEL,fg=TEXT,
                           selectcolor=BG,activebackground=PANEL,activeforeground=TEXT,font=('맑은 고딕',11)).pack(anchor='w',padx=12,pady=3)
        for index,(key,label) in enumerate(FIELDS.items()):
            tk.Checkbutton(field_frame,text=label,variable=selected[key],command=rebuild,bg=BG,fg=TEXT,selectcolor=PANEL,activebackground=BG,activeforeground=TEXT,font=('맑은 고딕',12)).grid(row=index,column=0,sticky='w',padx=12,pady=4)
        style_frame=tk.Frame(chooser,bg=BG);style_frame.pack(fill='x',pady=(6,0))
        for label,value in (('기존 가로형','classic'),('참고 세로형','modern')):
            tk.Radiobutton(style_frame,text=label,variable=style,value=value,command=rebuild,bg=BG,fg=TEXT,selectcolor=PANEL,font=('맑은 고딕',12)).pack(anchor='w',padx=12,pady=4)
        for label,value in (('매입 비중','purchase'),('평가 비중','evaluation')):
            tk.Radiobutton(style_frame,text=label,variable=basis,value=value,command=rebuild,bg=BG,fg=TEXT,selectcolor=PANEL,font=('맑은 고딕',12)).pack(anchor='w',padx=12,pady=4)
        tk.Checkbutton(style_frame,text='수령 배당 / 월별 그래프',variable=dividends,command=rebuild,bg=BG,fg=TEXT,selectcolor=PANEL,font=('맑은 고딕',11)).pack(anchor='w',padx=12,pady=10)
        controls=tk.Frame(window,bg=BG);controls.pack(side='bottom',fill='x',padx=15,pady=12)
        status=tk.Label(controls,text='선택 버튼은 저장 이미지에 포함되지 않습니다.',bg=BG,fg=MUTED,font=('맑은 고딕',11))
        status.pack(side='left')
        def save():
            path=filedialog.asksaveasfilename(parent=window,title='포트폴리오 이미지 저장',defaultextension='.png',
                initialfile='키움5814_포트폴리오.png',filetypes=[('PNG 이미지','*.png')])
            if not path:return
            try:
                snapshot.save(path,'PNG')
                status.config(text='저장 완료')
            except OSError:
                messagebox.showerror('저장 실패','저장 위치와 권한을 확인하세요.',parent=window)
        from average_cost import SoftButton, PALETTES
        palette=PALETTES.get(theme.CURRENT,PALETTES['default'])
        SoftButton(controls,'이미지 저장',save,palette,primary=True,width=145,height=46).pack(side='right',padx=8)
        SoftButton(controls,'닫기',window.destroy,palette,width=90,height=46).pack(side='right')
        frame=tk.Frame(window,bg=BG);frame.pack(fill='both',expand=True,padx=12,pady=(12,0))
        bar=SlimScrollbar(frame);bar.pack(side='right',fill='y')
        canvas=tk.Canvas(frame,bg=BG,highlightthickness=0,yscrollcommand=bar.set)
        canvas.pack(fill='both',expand=True);bar.config(command=canvas.yview)
        def resize(event):
            width=max(1,min(snapshot.width,event.width if event else canvas.winfo_width()))
            preview_key=(id(snapshot),width)
            if preview_key==getattr(canvas,'preview_key',None):return
            canvas.preview_key=preview_key
            size=(width,max(1,round(snapshot.height*width/snapshot.width)))
            canvas.preview_image=ImageTk.PhotoImage(snapshot.resize(size,Image.Resampling.LANCZOS))
            canvas.delete('all');canvas.create_image(0,0,image=canvas.preview_image,anchor='nw')
            canvas.config(scrollregion=(0,0,*size))
        canvas.bind('<Configure>',resize)
        canvas.bind('<MouseWheel>',lambda e:canvas.yview_scroll(-1 if e.delta>0 else 1,'units'))
        bind_drag(canvas)
        window.bind('<Escape>',lambda e:window.destroy())
        theme.widgets(window)

    def strategy_page(self):
        self.label(self.content,'전략구성 · 조회 전용',18).pack(anchor='w',pady=(5,12))
        candidates=[self.ats_root/'handoff/STRATEGY.md',self.ats_root.parent/'handoff/STRATEGY.md',
                    Path(__file__).resolve().parent.parent/'handoff/STRATEGY.md']
        path=next((p for p in candidates if p.is_file()),None)
        try:content=path.read_text(encoding='utf-8') if path else '전략 문서가 없습니다. 설치된 handoff 폴더를 확인하세요.'
        except OSError:content='전략 문서를 읽을 수 없습니다.'
        frame=tk.Frame(self.content,bg=BG);frame.pack(fill='both',expand=True)
        scrollbar=SlimScrollbar(frame);scrollbar.pack(side='right',fill='y')
        text=tk.Text(frame,bg=PANEL,fg=TEXT,wrap='word',bd=0,padx=20,pady=14,
                     font=('맑은 고딕',11),yscrollcommand=scrollbar.set)
        text.pack(fill='both',expand=True);scrollbar.config(command=text.yview)
        text.tag_configure('heading',font=('맑은 고딕',14,'bold'),foreground='#83C4D8',spacing1=12,spacing3=7)
        for line in content.splitlines():
            text.insert('end',line.lstrip('# ').strip()+'\n' if line.startswith('#') else line+'\n',
                        'heading' if line.startswith('#') else '')
        text.config(state='disabled')
        bind_drag(text)

    def api_settings(self):
        from desktop_platform import open_api_settings
        try:open_api_settings(self.ats_root)
        except (OSError,ValueError):messagebox.showerror('.env 편집','설정 파일을 열지 못했습니다. ATS 폴더와 텍스트 편집기를 확인하세요.')

    def settings_page(self):
        self.label(self.content,'대시보드 스타일 (선택 즉시 적용·저장)',13).pack(anchor='w',pady=(8,4))
        theme_var=tk.StringVar(value=theme.CURRENT)
        themes=tk.Frame(self.content,bg=BG);themes.pack(anchor='w',pady=(0,12))
        for key,label in theme.NAMES.items():
            tk.Radiobutton(themes,text=label,variable=theme_var,value=key,command=lambda:self.change_theme(theme_var.get()),bg=BG,fg=TEXT,selectcolor=PANEL).pack(side='left',padx=(0,12))
        self.label(self.content, '설정', 18).pack(anchor='w', pady=(5, 10))
        if os.environ.get('DISTRIBUTION_LAUNCHER'):
            from github_update import check
            self.button(self.content, 'GitHub 업데이트 확인', lambda: check(self.root)).pack(anchor='w', pady=(0,8))
        if os.name == "nt" and not os.environ.get('DISTRIBUTION_LAUNCHER'):
            self.button(self.content,'ats 설치 패키지 생성',self.package_clicked).pack(anchor='w',pady=(0,8))
        if os.name == 'nt' and not os.environ.get('DISTRIBUTION_LAUNCHER'):
            self.label(self.content,'설치 ZIP과 인증정보·거래 DB ZIP을 같은 위치에 저장합니다. 개인 ZIP은 암호화되지 않습니다.',10,MUTED).pack(anchor='w',pady=(0,8))
        api_buttons = tk.Frame(self.content, bg=BG)
        api_buttons.pack(anchor='w', pady=(0, 8))
        self.button(api_buttons, '.env 편집', self.api_settings).pack(side='left')
        self.button(api_buttons, 'API 관리', lambda: self.page('API 관리')).pack(side='left', padx=(8, 0))
        self.label(self.content,'.env를 로컬 편집기로 엽니다. 저장해도 실행 중인 ATS를 자동 재시작하지 않습니다.',10,MUTED).pack(anchor='w')
        self.tray_var = tk.BooleanVar(value=self.prefs.get('minimize_to_tray', True))
        self.start_var = tk.BooleanVar(value=startup_enabled(self.prefs))
        self.snapshot_var = tk.BooleanVar(value=self.prefs.get('account_reads', False))
        for title, variable in [('트레이로 최소화', self.tray_var), ('Windows 시작 시 실행', self.start_var),
                                ('계좌 상세 자동조회 (켜짐: 60초 / 꺼짐: 평일 08~20시 1시간, 공휴일 제외)', self.snapshot_var)]:
            if os.name != 'nt' and variable in (self.tray_var, self.start_var):
                continue
            tk.Checkbutton(self.content, text=title, variable=variable, bg=BG, fg=TEXT, selectcolor=PANEL,
                           activebackground=BG, activeforeground=TEXT, font=('맑은 고딕', 13)).pack(anchor='w', pady=5)
        if os.name == 'nt':
            self.label(self.content, 'Windows 로그인 후 대시보드와 이전에 가동 중이던 5814 ATS를 복구합니다.\nSTOP된 ATS는 시작하지 않습니다. 등록이 지워지면 실행 중인 대시보드가 다시 등록합니다.', 11, MUTED).pack(anchor='w', pady=16)
        self.label(self.content, '꺼짐: 한국시간 평일 08:00 이상~20:00 미만, 마지막 성공 조회 후 1시간마다 갱신(공휴일 제외).\n새로고침은 시간대와 무관하게 요청합니다. 평가·점검·미체결 우선으로 보류 후 재시도합니다.\n예수금·보유종목을 먼저 반영하고 체결내역을 이어 조회합니다. 환율은 1시간 캐시를 사용합니다.', 10, MUTED).pack(anchor='w')
        self.label(self.content, '조회할 ATS 폴더', 12).pack(anchor='w', pady=(18, 8))
        self.root_var = tk.StringVar(value=str(self.ats_root))
        tk.Entry(self.content, textvariable=self.root_var, width=95, bg=PANEL, fg=TEXT, insertbackground=TEXT).pack(anchor='w', ipady=8)
        actions=tk.Frame(self.content,bg=BG);actions.pack(anchor='w',pady=10)
        self.button(actions,'폴더 선택',self.choose_root).pack(side='left',padx=(0,8))
        self.button(actions,'설정 저장',self.save_settings).pack(side='left',padx=(0,8))
        self.button(actions,'대시보드 종료',self.quit).pack(side='left')

    def package_clicked(self):
        if getattr(self,'package_busy',False):
            messagebox.showinfo('패키지 생성','현재 생성 중입니다. 잠시 기다려 주세요.')
            return
        path=filedialog.asksaveasfilename(title='ATS 설치 패키지 저장',defaultextension='.zip',
            initialfile=f'Kiwoom5814-{VERSION}-{datetime.now():%Y%m%d-%H%M%S}.zip',filetypes=[('설치 패키지','*.zip')])
        if not path:return
        from portable_package import create_transfer_packages
        source=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent/'dist/Kiwoom5814'
        self.package_busy=True
        self.footer.config(text='설치·개인 자료 ZIP 생성 중…')
        future=self.maintenance_executor.submit(create_transfer_packages,source,self.ats_root,Path(__file__).parent,path,VERSION)
        future.add_done_callback(lambda f:self.events.put(('packaged',f)))

    def choose_root(self):
        path = filedialog.askdirectory(title='기존 ATS 폴더 선택')
        if path:
            self.root_var.set(path)

    def change_theme(self,name):
        # Deliberately separate from save_settings/set_startup and all ATS controls.
        from desktop_platform import save_preferences
        try:
            latest=read_preferences();latest['dashboard_theme']=name
            save_preferences(latest)
        except OSError:
            messagebox.showerror('스타일','스타일 저장에 실패했습니다.');return
        self.prefs['dashboard_theme']=name
        theme.select(name);theme.style(self.root);theme.widgets(self.root)
        self.draw_refresh_icon()
        if self.current_page=='운용현황':self.dashboard_view.redraw()

    def save_settings(self):
        path = Path(self.root_var.get()).resolve()
        if not path.is_dir():
            messagebox.showerror('설정', '존재하는 ATS 폴더를 선택하세요.')
            return
        try:
            updated = dict(self.prefs, minimize_to_tray=self.tray_var.get(), ats_root=str(path),
                           account_reads=self.snapshot_var.get(), start_with_windows=self.start_var.get())
            set_startup(self.start_var.get(), path, updated)
            self.prefs = updated
            self.ats_root = path
            messagebox.showinfo('설정', '저장했습니다.')
            self.refresh()
            self.snapshot_tick(reschedule=False)
        except OSError:
            messagebox.showerror('설정', '설정 저장에 실패했습니다. Windows 권한을 확인하세요.')

    def install_page(self):
        if os.name != 'nt':
            self.label(self.content, 'Linux 설치', 18).pack(anchor='w', pady=(5, 18))
            self.label(self.content, '설치 및 이전 절차는 패키지의 START_HERE.md를 확인하세요.', 12).pack(anchor='w')
            return
        self.label(self.content, '설치', 18).pack(anchor='w', pady=(5, 18))
        self.label(self.content, '대시보드와 편집 가능한 ATS 소스를 전용 폴더에 설치합니다.\n기존 계좌의 인증정보·거래기록은 포함하지 않습니다. 설치만으로 실거래가 시작되지 않습니다.', 12).pack(anchor='w', pady=5)
        self.install_var = tk.StringVar(value=str(Path(os.environ['LOCALAPPDATA']) / 'Programs' / 'Kiwoom5814'))
        tk.Entry(self.content, textvariable=self.install_var, width=95, bg=PANEL, fg=TEXT, insertbackground=TEXT).pack(anchor='w', pady=18, ipady=8)
        def choose():
            path = filedialog.askdirectory(title='전용 설치 폴더 선택')
            if path:
                self.install_var.set(path)
        self.button(self.content, '설치 위치 선택', choose).pack(anchor='w', pady=8)
        self.button(self.content, '설치 / 재설치', self.install_clicked).pack(anchor='w', pady=12)
        self.label(self.content, '재설치: 설치기 소유 파일을 교체하고 수정 파일·사용자 데이터는 백업합니다.\n다른 Windows에서 ATS를 가동하려면 IP 등록, 인증정보 입력, 기존 PC 중지, 상태 이전 및 연결 검증이 필요합니다.\n이 화면은 대시보드 설치만 수행하며 ATS 자동매매를 활성화하지 않습니다.', 11, MUTED).pack(anchor='w', pady=15)
        self.label(self.content, '현재 조회 경로: ' + str(self.ats_root), 10, MUTED).pack(anchor='w', pady=5)

    def snapshot_tick(self, reschedule=True, force=False):
        if self.closed:
            return
        if force:
            self.snapshot_pending = True
        last_at = None
        try:
            cached = json.loads(self.snapshot_path.read_text(encoding='utf-8'))
            if cached.get('complete') is True and Path(cached['root']).resolve() == self.ats_root:
                last_at = cached.get('at')
        except (OSError, ValueError, KeyError, TypeError):
            pass
        year_changed = False
        try:
            history = json.loads(self.snapshot_path.with_name('trade_history.json').read_text(encoding='utf-8'))
            now_kst = datetime.now(KST)
            year_changed = (Path(history['root']).resolve() == self.ats_root and
                            (history.get('year') != now_kst.year or
                             (now_kst.month == 1 and now_kst.day == 1 and
                              (not history.get('complete') or not history.get('profit_complete')))) and
                            8 <= now_kst.hour < 20)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # A new year's ledger is initialized once even on the January 1 holiday.
        due = self.snapshot_pending or year_changed or automatic_due(last_at, self.prefs.get('account_reads', False))
        try:
            from kis_portfolio import PortfolioClient
            kis_client = PortfolioClient(self.ats_root)
            try:
                kis_cache = json.loads(self.snapshot_path.with_name('kis_snapshot.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                kis_cache = {}
            valid = kis_cache.get('identity') == kis_client.identity and not kis_cache.get('error') and not kis_cache.get('history_error')
            kis_at = (kis_cache.get('balance') or {}).get('at') if valid else None
            due = due or automatic_due(kis_at, self.prefs.get('account_reads', False))
        except (OSError, ValueError, RuntimeError):
            pass
        protection = protected_reason()
        self.refresh_managed_accounts(force, protection)
        if due and protection:
            self.snapshot_message = protection
        elif due and (self.snapshot_busy or self.control_busy):
            self.snapshot_message = '계좌 갱신 대기 · 진행 중인 작업 종료 후 재시도'
        if due and not protection and not self.snapshot_busy and not self.control_busy:
            python = Path(sys.executable) if os.name != 'nt' else self.ats_root / '.venv/Scripts/python.exe'
            if os.name == 'nt' and not python.exists():
                python = self.ats_root / 'runtime/python.exe'
            if packaged_python := os.environ.get('MYATS_RUNTIME_PYTHON'):
                python = Path(packaged_python)
            if python.exists():
                self.snapshot_busy = True
                # Keep queued manual requests until dispatch. A failed read
                # must not repeat a forced history download every minute.
                refresh_history = self.snapshot_pending
                self.snapshot_pending = False
                self.snapshot_message = '계좌 예수금·보유종목 조회 중…'
                query_root = self.ats_root
                def work():
                    try:
                        with self.child_lock:
                            if self.closed:
                                return False
                            process = subprocess.Popen([str(python), str((Path(sys.executable).parent / 'app' if getattr(sys,'frozen',False) else Path(__file__).parent) / 'account_snapshot.py'),
                                '--root', str(query_root), '--output', str(self.snapshot_path)] + (['--refresh-history'] if refresh_history else []),
                                cwd=query_root, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            self.snapshot_process = process
                        try:
                            process.communicate(timeout=420)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.communicate()
                            return False
                        return process.returncode == 0
                    except (OSError, subprocess.TimeoutExpired):
                        return False
                    finally:
                        with self.child_lock:
                            self.snapshot_process = None
                future = self.snapshot_executor.submit(work)
                def completed(f):
                    try:success=bool(f.result())
                    except Exception:success=False
                    self.events.put(('snapshot', success))
                future.add_done_callback(completed)
            else:
                self.snapshot_message = '계좌 조회 실행 환경 없음 · 설치 폴더 확인 필요'
        self.render()
        if reschedule:
            self.root.after(60000, self.snapshot_tick)

    def install_clicked(self):
        source = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent / 'dist' / 'Kiwoom5814'
        if not (source / 'package.json').exists():
            messagebox.showerror('설치', '배포 패키지가 없습니다. build.ps1로 먼저 빌드하세요.')
            return
        try:
            target, previous = validate_target(self.install_var.get(), source)
            # Never replace a selected live ATS root or ancestor, even if it has a manifest.
            if target == self.ats_root or target in self.ats_root.parents:
                raise ValueError('현재 조회 중인 ATS 폴더는 재설치할 수 없습니다.')
        except ValueError as error:
            messagebox.showerror('설치', str(error))
            return
        if previous and not messagebox.askyesno('재설치 확인', '기존 설치 파일을 교체할까요?\n수정한 파일과 사용자 자료는 백업하여 보존합니다.'):
            return
        elevated = messagebox.askyesnocancel('설치 권한', '관리자 권한으로 설치할까요?\n예: Windows 관리자 승인 요청\n아니요: 현재 사용자 권한으로 설치')
        if elevated is None:
            return
        if elevated:
            try:
                run_elevated(['--install-target', str(target), '--package-source', str(source), '--reinstall-consent'] if previous else
                             ['--install-target', str(target), '--package-source', str(source)])
                messagebox.showinfo('설치', '관리자 설치 창에서 완료 결과를 확인하세요.')
            except OSError as error:
                messagebox.showerror('설치', str(error))
        else:
            future = self.maintenance_executor.submit(install, source, target, allow_reinstall=bool(previous))
            future.add_done_callback(lambda f: self.events.put(('installed', f)))

    def ensure_tray(self):
        if self.tray:
            return True
        try:
            self.tray = create_tray(lambda: self.events.put(('show', None)), lambda: self.events.put(('quit', None)))
            return True
        except Exception:
            messagebox.showwarning('트레이', '트레이를 사용할 수 없어 창을 유지합니다.')
            return False

    def on_minimize(self, event):
        if event.widget == self.root and self.root.state() == 'iconic' and self.prefs.get('minimize_to_tray', True):
            if self.ensure_tray():
                self.root.withdraw()

    def close_requested(self):
        if self.prefs.get('minimize_to_tray', True) and self.ensure_tray():
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        self.closed = True
        with self.child_lock:
            if self.snapshot_process and self.snapshot_process.poll() is None:
                self.snapshot_process.terminate()
        if self.tray:
            self.tray.stop()
        for executor in (self.executor, self.control_executor, self.snapshot_executor, self.maintenance_executor, self.account_executor):
            executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def smoke_complete(self):
        # Explicit test-only artifact, no startup or installation side effects.
        target = Path(self.smoke)
        target.mkdir(parents=True, exist_ok=True)
        from PIL import ImageGrab
        self.dashboard_view.rendered.save(target / 'dashboard-render.png')
        screenshot = False
        try:
            # Tk coordinates are logical under Windows display scaling; DWM returns physical bounds.
            import ctypes
            from ctypes import wintypes
            user32=ctypes.windll.user32
            user32.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
            user32.GetAncestor.restype=wintypes.HWND
            handle=user32.GetAncestor(self.root.winfo_id(),2)
            bounds=wintypes.RECT()
            result=ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(handle),9,ctypes.byref(bounds),ctypes.sizeof(bounds))
            if result != 0:
                raise OSError('Window bounds unavailable')
            ImageGrab.grab(bbox=(bounds.left,bounds.top,bounds.right,bounds.bottom)).save(target / 'dashboard.png')
            screenshot = True
        except OSError:
            pass  # A remote Windows session may not have a capturable desktop.
        if self.chart_rows():
            from export_chart import export_png
            export_png(self.chart_rows(), target / 'portfolio.png', self.data['read_at'], self.data['cash_at'],self.data)
        (target / 'smoke.json').write_text(json.dumps({'version': VERSION, 'loaded': self.data is not None,
            'holdings': len((self.data or {}).get('holdings', [])), 'fills': len((self.data or {}).get('fills', [])),
            'module_path':__file__, 'reader_path':sys.modules['data_reader'].__file__, 'capital':(self.data or {}).get('capital'), 'evaluation_rates':(self.data or {}).get('evaluation_rates'), 'annual_profit':(self.data or {}).get('annual_profit'), 'readonly_test': True, 'screenshot': screenshot, 'full_account': self.data.get('full_account') if self.data else False,
            'visible_holding_rows': self.dashboard_view.painter.visible_rows,
            'visible_fill_rows': self.dashboard_view.painter.visible_rows}), encoding='utf-8')
        self.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root')
    parser.add_argument('--install-target')
    parser.add_argument('--package-source')
    parser.add_argument('--reinstall-consent', action='store_true')
    parser.add_argument('--smoke-output')
    parser.add_argument('--autostart', action='store_true')
    args = parser.parse_args()
    root = tk.Tk()
    if os.name != 'nt':
        from platform_linux import lock_dashboard
        if args.install_target:
            parser.error('Use setup-steamos.sh on Linux.')
        lock = lock_dashboard()
        if lock is None:
            root.destroy()
            return
        with lock:
            default = os.environ.get('MYATS_WORKSPACE') or Path(__file__).resolve().parents[1] / 'ats'
            Dashboard(root, args.root or read_preferences().get('ats_root') or default, args.smoke_output)
            root.mainloop()
        return
    if args.install_target:
        root.withdraw()
        try:
            result = install(args.package_source, args.install_target, allow_reinstall=args.reinstall_consent)
            messagebox.showinfo('설치 완료', '설치 완료: ' + result['target'] + '\n대시보드 EXE를 실행하세요. ATS는 자동 시작하지 않습니다.')
        except Exception as error:
            messagebox.showerror('설치 실패', str(error))
        root.destroy()
        return
    # One dashboard per Windows login prevents duplicate polling and tray icons.
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, 'Local\\Kiwoom5814Dashboard' + (f'-test-{os.getpid()}' if args.smoke_output else ''))
    if ctypes.get_last_error() == 183:
        root.withdraw()
        if not args.autostart:
            messagebox.showinfo('대시보드', '이미 실행 중입니다. 작업표시줄 또는 트레이 아이콘을 확인하세요.')
        root.destroy()
        return
    preferences = read_preferences()
    default = Path(__file__).resolve().parent.parent
    if (default / 'ats').is_dir():
        default = default / 'ats'
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / 'data/kiwoom5814_live.sqlite3').is_file():
            default = ancestor
            break
    ats_root = args.root or preferences.get('ats_root') or os.environ.get('MYATS_WORKSPACE') or str(default)
    Dashboard(root, ats_root, args.smoke_output)
    root.mainloop()
    if mutex:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
