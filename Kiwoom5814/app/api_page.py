"""API connections and their per-account ATS selections."""
from collections import Counter
from dataclasses import replace
import tkinter as tk
from tkinter import messagebox, ttk

from api_accounts import (accounts, delete_account, env_values, fingerprint, kis_env_account,
                          kis_env_accounts, save_account, save_env_metadata, validate_alias)
from api_watchlists import (default_selection, kis_groups, kiwoom_connection,
                            kiwoom_groups, save_default_selection)
from ats_models import DEFAULT_MODEL, available_models
import dashboard_theme as theme
from hangul_entry import offer_hangul
from kis_control import alive
from kis_managed import ManagedKIS
from kis_snapshot import guard
from managed_snapshots import cached, collect, domestic_balance


ENV_LABELS = {'live': '실전', 'demo': '모의'}


def style_controls(root):
    style = ttk.Style(root)
    c = theme.color
    background, edge, text, muted = map(c, ('#031522', '#12435C', '#EDF5FC', '#8DA6B9'))
    field, hover = c('#0C2B42'), c('#145166')
    style.configure('API.TCombobox', padding=(8, 6), fieldbackground=field,
                    background=field, bordercolor=edge, lightcolor=field, darkcolor=field,
                    arrowcolor=text, foreground=text, arrowsize=13)
    style.map('API.TCombobox',
              fieldbackground=[('disabled', background), ('readonly', field)],
              foreground=[('disabled', muted), ('readonly', text)],
              arrowcolor=[('disabled', muted)], bordercolor=[('focus', muted)])
    style.configure('API.TButton', padding=(9, 6), font=('맑은 고딕', 10),
                    foreground=text, background=edge, bordercolor=edge,
                    lightcolor=edge, darkcolor=edge, focuscolor=muted)
    style.map('API.TButton', foreground=[('disabled', muted)],
              background=[('disabled', field), ('pressed', field), ('active', hover)])


def build(dashboard):
    parent, root = dashboard.content, dashboard.ats_root
    style_controls(dashboard.root)
    toolbar = tk.Frame(parent, bg=parent.cget('bg'))
    toolbar.pack(fill='x', pady=(6, 5))
    back = dashboard.button(toolbar, '‹  설정', lambda: dashboard.page('설정'))
    back.config(bg='#061E30', padx=10, pady=7)
    back.pack(side='left', padx=(0, 14))
    dashboard.label(toolbar, 'API 관리', 19).pack(side='left')
    dashboard.label(parent, '.env에서 계좌를 추가하거나 제거한 뒤 API 연결을 눌러 반영하세요.',
                    10, '#AFCFE2').pack(anchor='w', pady=(0, 14))
    table = tk.Frame(parent, bg=parent.cget('bg'), highlightbackground='#12435C', highlightthickness=1)
    table.pack(fill='x', pady=(0, 10))
    columns = (
        ('계좌별칭', 122, 2), ('계좌번호', 108, 1), ('환경', 56, 0),
        ('ATS 적용', 318, 5), ('연결상태', 117, 2),
    )
    for column, (title, minimum, weight) in enumerate(columns):
        table.columnconfigure(column, minsize=minimum, weight=weight)
        label = dashboard.label(table, title, 10, '#AFCFE2')
        label.config(bg='#0C2B42', font=('맑은 고딕', 10, 'bold'), padx=12, pady=9)
        label.grid(row=0, column=column, sticky='nsew')
    status = dashboard.label(parent, '', 10, '#AFCFE2')
    status.pack(anchor='w')
    selection_help = dashboard.label(parent, '', 10, '#8DA6B9')
    selection_help.pack(anchor='w')
    rows, profiles = {}, {}
    selected = ''

    def select(identity):
        nonlocal selected
        selected = identity
        update_actions()

    def update_actions():
        row = rows.get(selected)
        edit_button.config(state='normal' if row else 'disabled')
        selection_help.config(text='.env 연결 · 계좌 설정을 바꾼 뒤 API 연결로 목록을 갱신하세요.'
                              if row and 'source_id' in row.settings else '')
        for identity, row in rows.items():
            row.highlight(identity == selected)

    def show_row(index, identity, columns, **settings):
        row = rows.get(identity)
        if row is None or row.settings != settings:
            if row:
                row.destroy()
            row = rows[identity] = AssignmentRow(
                dashboard, table, root, identity, settings, select, status, reload_table)
        row.layout(index, columns)

    def reload_table(report_errors=True):
        if not table.winfo_exists():
            return
        previous_rows = table.grid_size()[1]
        profiles.clear()
        errors = []

        def read_setting(read, fallback, label):
            try:
                return read()
            except (OSError, ValueError):
                errors.append(label)
                return fallback

        identities = []
        data = dashboard.data or {}
        values = read_setting(lambda: env_values(root), {}, '.env')
        kiwoom = kiwoom_connection(root, values)
        number, credentials = kiwoom
        if len(number) >= 8 and number.isascii() and number.isdigit() and (
            credentials.kiwoom_app_key and credentials.kiwoom_secret_key
        ):
            alias = values.get('KIWOOM_ALIAS') or '키움 5814'
            kiwoom_id = fingerprint('kiwoom', number)
            identities.append('kiwoom')
            show_row(1, 'kiwoom', (alias, '••••' + number[-4:], '실전',
                                 '최근 조회 있음' if data.get('full_account') else '기본 연결'),
                     source_id=kiwoom_id, connection=kiwoom, alias=alias, assignment=read_setting(
                         lambda: default_selection(root, kiwoom_id), ('', '', ''), '키움 ATS 선택'),
                     issue='')
        env_profiles, invalid = kis_env_accounts(values)
        if invalid:
            errors.append('한투 ' + ', '.join('기본 계좌' if not slot else slot[1:] + '번 계좌' for slot in invalid))
        env_ids = {profile.identity for profile in env_profiles.values()}
        registered = read_setting(lambda: accounts(root), (), '기존 API 계좌')
        profiles.update((p.identity, p) for p in registered if p.identity not in env_ids)

        def show_kis(identity, profile, **settings):
            identities.append(identity)
            is_default_live = identity == 'legacy-kis' and profile.mode == 'live'
            state = cached(root, profile) if not is_default_live else {}
            balance = state.get('balance') or {}
            if not profile.enabled:
                summary, issue = '연결 해제', '연결 해제'
            else:
                issue = ('모의 API는 관심종목 그룹 조회 미지원' if profile.mode == 'demo'
                         else '' if profile.hts_id else
                         '.env에 KIS_HTS_ID' + settings['env_suffix'] + ' 필요' if 'env_suffix' in settings
                         else '수정에서 한투 HTS ID 입력')
                if is_default_live:
                    summary = '최근 조회 있음' if data.get('kis_at') else '기본 연결'
                elif state.get('balance_error') or state.get('fills_error'):
                    summary = '갱신 확인 필요'
                elif balance:
                    summary = balance['at'][5:16].replace('T', ' ') + ' 조회'
                else:
                    summary = '첫 조회 대기'
            show_row(len(identities), identity, (profile.alias, profile.masked,
                     ENV_LABELS[profile.mode], summary), profile=profile, issue=issue, **settings)

        for suffix, profile in env_profiles.items():
            identity = 'env-kis' + suffix if suffix else 'legacy-kis'
            source = fingerprint('legacy-kis', profile.identity, profile.hts_id)
            show_kis(identity, profile, source_id=source, env_suffix=suffix, assignment=read_setting(
                lambda: default_selection(root, source), ('', '', ''), '한투 ATS 선택'))
        for identity, profile in profiles.items():
            show_kis(identity, profile, assignment=(profile.group_code, profile.group,
                     profile.model_id or (DEFAULT_MODEL if profile.ats else '')))
        for identity in tuple(rows):
            if identity not in identities:
                rows.pop(identity).destroy()
        for index in range(1, max(previous_rows, len(identities) + 1)):
            table.rowconfigure(index, minsize=63 if index <= len(identities) else 0)
        select(selected if selected in rows else '')
        if report_errors and errors:
            status.config(text='설정 확인 필요 · ' + ', '.join(errors))
        return errors

    def sync_env():
        errors = reload_table(report_errors=False)
        if errors is None:
            return
        status.config(text=('설정 확인 필요 · ' + ', '.join(errors)) if errors else
                           (f'.env 반영 완료 · 표에 계좌 {len(rows)}개' if rows else
                            '.env에 등록된 API 계좌가 없습니다. 설정에서 인증 정보를 입력하세요.'))
        dashboard.refresh()

    def edit_selected():
        row = rows.get(selected)
        if not row:
            return
        if 'source_id' in row.settings:
            try:
                edit_env(dashboard, root, sync_env, row.identity, row.settings)
            except (OSError, ValueError):
                status.config(text='.env 설정을 읽을 수 없습니다. API 연결을 다시 눌러 주세요.')
        elif profile := profiles.get(selected):
            edit_registered(dashboard, root, sync_env, profile)

    buttons = tk.Frame(toolbar, bg=parent.cget('bg'))
    buttons.pack(side='right')
    add_button = dashboard.button(buttons, 'API 연결', sync_env)
    edit_button = dashboard.button(buttons, '수정', edit_selected)
    for button in (add_button, edit_button):
        button.config(padx=14, pady=7, disabledforeground='#8DA6B9')
        button.pack(side='left', padx=(6, 0))
    edit_button.config(bg='#061E30')
    dashboard.api_list_refresh = reload_table
    reload_table()


class AssignmentRow:
    def __init__(self, dashboard, table, root, identity, settings, select, status, refresh):
        self.dashboard, self.root, self.identity = dashboard, root, identity
        self.settings, self.status = settings, status
        self.refresh = refresh
        self.select = lambda _event=None: select(identity)
        self.loading = False
        self.groups = ()
        self.labels = [dashboard.label(table, '', 10) for _ in range(4)]
        self.labels[0].config(wraplength=112, font=('맑은 고딕', 10, 'bold'))
        self.labels[3].config(wraplength=103, fg='#AFCFE2')
        self.cell = tk.Frame(table, bg=table.cget('bg'))
        self.note = dashboard.label(self.cell, settings['issue'], size=9, color='#8DA6B9')
        self.group_var, self.model_var = tk.StringVar(master=table), tk.StringVar(master=table)
        self.models = available_models('kiwoom' if identity == 'kiwoom' else 'kis')
        self.model_ids = ('', *self.models)
        self.group = ttk.Combobox(self.cell, textvariable=self.group_var, width=13, state='disabled',
                                  font=('맑은 고딕', 10), style='API.TCombobox')
        self.model = ttk.Combobox(self.cell, textvariable=self.model_var, width=14,
                                  font=('맑은 고딕', 10), style='API.TCombobox',
                                  values=('미적용', *self.models.values()),
                                  state='disabled' if settings['issue'] else 'readonly')
        self.fetch_button = ttk.Button(self.cell, text='조회', width=4, command=self.fetch, style='API.TButton',
                                       state='disabled' if settings['issue'] else 'normal')
        self.cell.columnconfigure(0, weight=3)
        self.cell.columnconfigure(1, weight=2)
        self.cell.rowconfigure(0, weight=1)
        self.group.grid(row=0, column=0, sticky='ew', padx=(12, 8))
        self.model.grid(row=0, column=1, sticky='ew', padx=(0, 8))
        self.fetch_button.grid(row=0, column=2, sticky='ew', padx=(0, 12))
        self.note.grid(row=1, column=0, columnspan=3, sticky='w', padx=12, pady=(0, 7))
        self.set_note(settings['issue'])
        for widget in (*self.labels, self.cell, self.note, self.group, self.model, self.fetch_button):
            widget.bind('<Button-1>', self.select, add='+')
        for label in self.labels:
            label.config(padx=12, takefocus=True)
            label.bind('<space>', self.select)
            label.bind('<Return>', self.select)
        self.group.bind('<<ComboboxSelected>>', self.save)
        self.model.bind('<<ComboboxSelected>>', self.save)
        self.restore()

    def layout(self, index, columns):
        for column, (label, value) in enumerate(zip(self.labels, columns)):
            label.config(text=value)
            label.grid(row=index, column=column if column < 3 else 4, sticky='nsew', pady=(0, 1))
        self.cell.grid(row=index, column=3, sticky='nsew', pady=(0, 1))

    def destroy(self):
        for widget in (*self.labels, self.cell):
            widget.destroy()

    def highlight(self, active):
        bg = '#314B61' if active else '#061E30'
        for widget in (*self.labels, self.cell, self.note):
            widget.config(bg=bg)
            theme.widgets(widget)

    def set_note(self, message):
        self.note.config(text=message)
        if message:
            self.note.grid()
        else:
            self.note.grid_remove()

    def restore(self):
        code, name, model_id = self.settings['assignment']
        if (code, name) in self.groups:
            self.group.current(self.groups.index((code, name)))
        else:
            self.group_var.set(name or '관심종목 그룹')
        self.model_var.set(self.models.get(model_id, 'ATS 모델' if not model_id else '지원 종료 모델'))

    def fetch(self):
        if self.loading or self.settings['issue']:
            return
        self.select()
        self.loading = True
        self.fetch_button.config(state='disabled')
        self.group.config(state='disabled')
        self.set_note('관심종목 그룹 조회 중…')
        profile = self.settings.get('profile')

        def work():
            if self.identity == 'kiwoom':
                return kiwoom_groups(self.root, self.settings['connection'])
            return kis_groups(self.root, profile, legacy=self.identity == 'legacy-kis')

        def done(groups):
            if not self.cell.winfo_exists():
                return
            self.loading = False
            self.fetch_button.config(state='normal')
            self.groups = tuple(groups or ())
            counts = Counter(name for _, name in self.groups)
            duplicate_names = any(count > 1 for count in counts.values())
            labels = tuple(f'{name} ({code})' if duplicate_names else name for code, name in self.groups)
            self.group.config(values=labels, state='readonly' if labels else 'disabled')
            code, name, _ = self.settings['assignment']
            if (code, name) in self.groups:
                self.group.current(self.groups.index((code, name)))
            else:
                # Force an explicit re-selection if the old name/code no longer matches.
                self.group_var.set('')
            if groups is None:
                self.group_var.set(name or '관심종목 그룹')
                self.set_note(
                    '조회 실패 · 키움 인증 토큰 또는 조회 가능 시간 확인' if self.identity == 'kiwoom'
                    else '조회 실패 · 한투 API 설정과 조회 가능 시간 확인')
            elif name and (code, name) not in self.groups:
                self.set_note('저장된 그룹을 찾지 못했습니다. 다시 선택하세요.')
            elif labels:
                self.set_note('')
                if not self.group_var.get():
                    self.group_var.set('관심종목 그룹')
            else:
                self.group_var.set('관심종목 그룹')
                self.set_note('등록된 관심종목 그룹 없음')

        self.dashboard.submit_account_job(work, done)

    def save(self, event):
        self.select()
        index = self.group.current()
        model = self.model.current()
        if model == 0 and event.widget is self.model:
            assignment = ('', '', '')
        elif index >= 0 and model > 0 and not self.loading:
            assignment = (*self.groups[index], self.model_ids[model])
        else:
            self.status.config(text='관심종목 그룹을 조회한 뒤 그룹과 모델을 모두 골라 주세요.'
                               if index == -1 else '이 그룹에 적용할 ATS 모델을 골라 주세요.')
            return
        previous = self.settings['assignment']
        if assignment == previous:
            self.restore()
            self.status.config(text='')
            return
        code, name, model_id = assignment
        try:
            if 'source_id' in self.settings:
                if 'env_suffix' in self.settings:
                    if kis_env_account(env_values(self.root), self.settings['env_suffix']) != self.settings['profile']:
                        raise ValueError('한투 설정이 변경됐습니다. API 연결을 다시 눌러 주세요.')
                elif kiwoom_connection(self.root) != self.settings['connection']:
                    raise ValueError('키움 설정이 변경됐습니다. API 관리를 다시 열어 주세요.')
                save_default_selection(self.root, self.settings['source_id'], code, name, model_id)
            else:
                original = self.settings['profile']
                latest = next((p for p in accounts(self.root) if p.identity == original.identity), None)
                if latest != original:
                    raise ValueError('계좌 설정이 변경됐습니다. API 관리를 다시 열어 주세요.')
                if alive(self.root, latest):
                    raise ValueError('실행 중인 계좌는 ATS를 중지한 뒤 설정을 변경할 수 있습니다.')
                profile = replace(latest, group=name, group_code=code, model_id=model_id,
                                  ats=latest.ats and bool(model_id))
                save_account(self.root, profile, previous=profile.identity)
                self.settings['profile'] = profile
        except (OSError, ValueError) as error:
            self.restore()
            self.status.config(text=str(error) if isinstance(error, ValueError) else 'ATS 선택을 저장할 수 없습니다.')
            return
        self.settings['assignment'] = assignment
        self.restore()
        self.refresh()
        self.status.config(text='관심종목과 ATS 모델을 저장했습니다.' if model_id else 'ATS 선택을 해제했습니다.')


def edit_env(dashboard, root, on_saved, identity, settings):
    values = env_values(root)
    is_kiwoom = identity == 'kiwoom'
    suffix = settings.get('env_suffix', '')
    profile = settings.get('profile')
    window = tk.Toplevel(dashboard.root)
    window.title('계좌 설정 수정')
    window.transient(dashboard.root)
    frame = ttk.Frame(window, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='계좌 설정 수정', font=('맑은 고딕', 14, 'bold')).grid(
        row=0, columnspan=2, sticky='w', pady=(0, 14))
    ttk.Label(frame, text='계좌별칭').grid(row=1, column=0, sticky='w', padx=(0, 16))
    alias = tk.StringVar(master=window, value=settings['alias'] if is_kiwoom else profile.alias)
    entry = ttk.Entry(frame, textvariable=alias, width=29)
    entry.grid(row=1, column=1, sticky='ew', pady=3)
    offer_hangul(entry, frame, row=1, column=2, sticky='w', padx=(8, 0))
    ttk.Label(frame, text='환경').grid(row=2, column=0, sticky='w')
    environment = ttk.Combobox(frame, values=tuple(ENV_LABELS.values()), width=27,
                               state='disabled' if is_kiwoom else 'readonly')
    environment.current(0 if is_kiwoom or profile.mode == 'live' else 1)
    environment.grid(row=2, column=1, sticky='ew', pady=3)
    note = ttk.Label(frame, wraplength=360, text=(
        '기본 키움 5814는 실전 서비스 계좌에 연결됩니다.' if is_kiwoom else
        '환경에 맞는 한투 계좌번호와 API 키는 설정의 .env 편집에서 바꾸세요.'))
    note.grid(row=3, columnspan=3, sticky='w', pady=(6, 12))

    def save():
        new_alias = alias.get().strip()
        try:
            validate_alias(new_alias)
            if is_kiwoom:
                if kiwoom_connection(root) != settings['connection']:
                    raise ValueError('키움 API 설정이 변경됐습니다. API 연결을 다시 눌러 주세요.')
                changes = {'KIWOOM_ALIAS': new_alias} if new_alias != settings['alias'] else {}
            else:
                if kis_env_account(values, suffix) != profile:
                    raise ValueError('한투 API 설정이 변경됐습니다. API 연결을 다시 눌러 주세요.')
                mode = tuple(ENV_LABELS)[environment.current()]
                changes = {}
                if new_alias != profile.alias:
                    changes['KIS_ALIAS' + suffix] = new_alias
                if mode != profile.mode:
                    updated = replace(profile, mode=mode)
                    env_profiles, _ = kis_env_accounts(values)
                    others = [p for slot, p in env_profiles.items() if slot != suffix]
                    if any(p.identity == updated.identity for p in (*others, *accounts(root))):
                        raise ValueError('선택한 환경에 이미 등록된 계좌입니다.')
                    changes['KIS_ENV' + suffix] = mode
            if changes:
                save_env_metadata(root, values, changes)
        except (OSError, ValueError) as exc:
            note.config(text=str(exc) if isinstance(exc, ValueError) else '.env 설정을 저장할 수 없습니다.')
            return
        window.destroy()
        on_saved()

    buttons = ttk.Frame(frame)
    buttons.grid(row=4, columnspan=3, sticky='ew')
    ttk.Button(buttons, text='저장', command=save).pack(side='right', padx=(8, 0))
    ttk.Button(buttons, text='닫기', command=window.destroy).pack(side='right')
    window.grab_set()
    entry.focus_set()


def edit_registered(dashboard, root, on_saved, previous):
    window = tk.Toplevel(dashboard.root)
    window.title('API 정보 수정')
    window.transient(dashboard.root)
    frame = ttk.Frame(window, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='한투 API 정보 수정', font=('맑은 고딕', 15, 'bold')).grid(
        row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
    variables = {}
    fields = (
        ('alias', '계좌별칭'), ('mode', '환경'),
        ('number', '계좌 앞 8자리'), ('product', '상품코드 2자리'),
        ('app_key', '앱 키'), ('app_secret', '앱 시크릿'),
        ('hts_id', '한투 HTS ID'),
    )
    for index, (name, label) in enumerate(fields, 1):
        ttk.Label(frame, text=label).grid(row=index, column=0, sticky='w', padx=(0, 18), pady=3)
        secret_field = name in ('app_key', 'app_secret')
        value = '' if secret_field else getattr(previous, name)
        if name == 'mode':
            value = ENV_LABELS[value]
        variable = variables[name] = tk.StringVar(value=value)
        if name == 'mode':
            entry = ttk.Combobox(frame, textvariable=variable, values=tuple(ENV_LABELS.values()),
                                 state='readonly', width=38)
        else:
            entry = ttk.Entry(frame, textvariable=variable, width=40,
                              show='•' if name in ('app_key', 'app_secret', 'number') else '')
        if name in ('number', 'product'):
            entry.state(['readonly'])
        entry.grid(row=index, column=1, sticky='ew', pady=3)
        if name == 'alias':
            offer_hangul(entry, frame, row=index, column=2, sticky='w', padx=(8, 0))
    help_text = '환경을 바꾸려면 해당 환경의 앱 키와 시크릿을 입력하세요.'
    note = ttk.Label(frame, text=help_text, wraplength=460)
    note.grid(row=len(fields) + 1, column=0, columnspan=3, sticky='w', pady=(8, 10))
    busy = False

    def draft():
        values = {key: var.get().strip() for key, var in variables.items()}
        values['mode'] = next(mode for mode, label in ENV_LABELS.items() if label == values['mode'])
        mode_changed = values['mode'] != previous.mode
        if mode_changed and not (values['app_key'] and values['app_secret']):
            raise ValueError('환경에 맞는 앱 키와 시크릿을 입력하세요.')
        for key in ('app_key', 'app_secret'):
            values[key] = values[key] or getattr(previous, key)
        if mode_changed or values['hts_id'] != previous.hts_id:
            values.update(group='', group_code='', model_id='', ats=False)
        return replace(previous, **values)

    def test():
        nonlocal busy
        if busy:
            return
        try:
            profile = draft()
        except ValueError as exc:
            note.config(text=str(exc))
            return
        busy = True
        note.config(text='계좌 연결을 확인하는 중…')

        def work():
            client = ManagedKIS(root, profile)
            client.guard = lambda: guard(root)
            domestic_balance(client)
            return True

        def done(result):
            nonlocal busy
            busy = False
            if window.winfo_exists():
                note.config(text='계좌 연결을 확인했습니다.' if result else
                            '연결 테스트 실패 · 앱 키·계좌 또는 조회 가능 시간을 확인하세요.')

        dashboard.submit_account_job(work, done)

    def save():
        if busy:
            return
        try:
            profile = draft()
            if alive(root, previous) or (previous.identity != profile.identity and alive(root, profile)):
                raise ValueError('실행 중인 계좌입니다. ATS를 중지한 뒤 수정하세요.')
            if previous not in accounts(root):
                raise ValueError('계좌 설정이 변경됐습니다. API 관리를 다시 열어 주세요.')
            save_account(root, profile, previous=previous.identity, allow_mode_change=True)
        except (OSError, ValueError) as exc:
            note.config(text=str(exc))
            return
        window.destroy()
        on_saved()
        if profile.enabled:
            dashboard.submit_account_job(lambda: collect(root, profile), lambda _result: on_saved())
        dashboard.refresh()

    def remove():
        if busy or not messagebox.askyesno('API 연결 삭제',
                f'{previous.alias} ({previous.masked})의 API 연결을 삭제하시겠습니까?',
                default=messagebox.NO, parent=window):
            return
        try:
            if alive(root, previous):
                raise ValueError('실행 중인 계좌입니다. ATS를 중지한 뒤 삭제하세요.')
            if previous not in accounts(root):
                raise ValueError('계좌 설정이 변경됐습니다. API 관리를 다시 열어 주세요.')
            delete_account(root, previous.identity)
        except (OSError, ValueError) as exc:
            note.config(text=str(exc) if isinstance(exc, ValueError) else 'API 연결을 삭제할 수 없습니다.')
            return
        window.destroy()
        on_saved()

    buttons = ttk.Frame(frame)
    buttons.grid(row=len(fields) + 2, column=0, columnspan=3, sticky='ew')
    ttk.Button(buttons, text='연결 테스트', command=test).pack(side='left', padx=(0, 8))
    ttk.Button(buttons, text='연결 삭제', command=remove).pack(side='left')
    ttk.Button(buttons, text='저장', command=save).pack(side='right', padx=(8, 0))
    ttk.Button(buttons, text='닫기', command=window.destroy).pack(side='right')
    window.grab_set()
