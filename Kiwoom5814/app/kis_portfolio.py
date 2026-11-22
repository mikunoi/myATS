"""KIS portfolio connection. Only explicitly allowed balance GETs and OAuth POST.

Consumed only by the dashboard worker, never by an ATS.
Credentials come exclusively from the selected ATS root's .env.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib import request, parse, error

from api_accounts import KIS_FIELDS, env_values


class KISError(RuntimeError):
    pass


class PortfolioClient:
    HOST = 'https://openapi.koreainvestment.com:9443'
    MARKET = {'groups': ('intstock-grouplist', 'HHKCM113004C7')}
    READS = {
        'domestic': ('/uapi/domestic-stock/v1/trading/inquire-balance', 'TTTC8434R'),
        'us': ('/uapi/overseas-stock/v1/trading/inquire-balance', 'TTTS3012R'),
        'present': ('/uapi/overseas-stock/v1/trading/inquire-present-balance', 'CTRP6504R'),
        'cash': ('/uapi/overseas-stock/v1/trading/foreign-margin', 'TTTC2101R'),
        'domestic_orders': ('/uapi/domestic-stock/v1/trading/inquire-daily-ccld', 'TTTC0081R'),
        'domestic_orders_old': ('/uapi/domestic-stock/v1/trading/inquire-daily-ccld', 'CTSC9215R'),
        'us_orders': ('/uapi/overseas-stock/v1/trading/inquire-ccnl', 'TTTS3035R'),
        'domestic_profit': ('/uapi/domestic-stock/v1/trading/inquire-period-trade-profit', 'TTTC8715R'),
        'us_profit': ('/uapi/overseas-stock/v1/trading/inquire-period-profit', 'TTTS3039R'),
    }

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.guard = None
        values = env_values(root)
        fields = KIS_FIELDS
        mode = values.get('KIS_ENV') or 'live'
        if not any(values.get(k) for k in fields) or mode == 'demo':
            raise KISError('KIS_DEFAULT_LIVE_ACCOUNT_DISABLED')
        if mode != 'live':
            raise KISError('KIS_ACCOUNT_MODE_INVALID')
        if any(not values.get(k) for k in fields):
            raise KISError('KIS_REQUIRED_FIELD_MISSING')
        self.key, self.secret, self.account, self.product = (values[k] for k in fields)
        if not re.fullmatch(r'\d{8}', self.account) or not re.fullmatch(r'\d{2}', self.product):
            raise KISError('KIS_ACCOUNT_FORMAT_INVALID')
        self.identity = hashlib.sha256(('live|' + '|'.join(values[k] for k in fields)).encode()).hexdigest()
        from desktop_platform import preferences_dir
        self.cache_dir = preferences_dir() / 'private'
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.cache_dir / 'kis_token.json'
        self.token = None
        self.last_request = 0.0

    def _safe_code(self, value):
        text = str(value)
        return text if re.fullmatch(r'[A-Z][A-Z0-9_]{1,30}', text) else 'UNSPECIFIED'

    def _exchange(self, path, *, params=None, payload=None, headers=None):
        if self.guard:
            self.guard()
        time.sleep(max(0, 1.1 - (time.monotonic() - self.last_request)))
        if self.guard:
            self.guard()
        self.last_request = time.monotonic()
        url = self.HOST + path
        if params is not None:
            url += '?' + parse.urlencode(params)
        req = request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                              headers={'Content-Type': 'application/json; charset=utf-8', **(headers or {})},
                              method='POST' if payload is not None else 'GET')
        # Do not follow redirects with credentials or expose HTTP bodies/URLs in errors.
        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with request.build_opener(NoRedirect).open(req, timeout=15) as response:
                return json.loads(response.read()), response.headers
        except error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
                code = self._safe_code(body.get('error_code') or body.get('msg_cd'))
            except Exception:
                code = 'UNSPECIFIED'
            raise KISError(f'KIS_HTTP_{exc.code}_{code}') from None
        except (error.URLError, TimeoutError, OSError):
            raise KISError('KIS_CONNECTION_FAILED') from None
        except (ValueError, UnicodeError):
            raise KISError('KIS_RESPONSE_INVALID') from None

    def authenticate(self):
        # The portfolio refresh and API management may authenticate concurrently.
        module_root = Path(__file__).resolve().parent.parent / 'ats'
        if not (module_root / 'src/platform_support.py').is_file():
            module_root = self.root
        if str(module_root) not in sys.path:
            sys.path.insert(0, str(module_root))
        from src.platform_support import FileLock
        with FileLock(self.cache_path.with_suffix('.lock'), timeout=20):
            self._load_or_issue_token()

    def _load_or_issue_token(self):
        try:
            cached = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if cached['identity'] == self.identity and float(cached['expires_at']) > time.time() + 300:
                self.token = cached['token']
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        body, _ = self._exchange('/oauth2/tokenP', payload={
            'grant_type': 'client_credentials', 'appkey': self.key, 'appsecret': self.secret})
        token = body.get('access_token')
        if not isinstance(token, str) or not token:
            raise KISError('KIS_AUTH_' + self._safe_code(body.get('error_code')))
        expires = min(float(body.get('expires_in', 0)), 86400)
        if expires <= 300:
            raise KISError('KIS_TOKEN_EXPIRY_INVALID')
        self.token = token
        cached = {'identity': self.identity, 'token': token, 'expires_at': time.time() + expires}
        temp = self.cache_path.with_suffix('.tmp')
        temp.write_text(json.dumps(cached), encoding='utf-8')
        os.replace(temp, self.cache_path)

    def headers(self, tr_id):
        if not self.token:
            self.authenticate()
        return {'authorization': 'Bearer ' + self.token, 'appkey': self.key,
                'appsecret': self.secret, 'tr_id': tr_id, 'custtype': 'P'}

    def market_read(self, name, **params):
        if name not in self.MARKET:
            raise KISError('KIS_ENDPOINT_DENIED')
        path, tr_id = self.MARKET[name]
        body, headers = self._exchange('/uapi/domestic-stock/v1/quotations/' + path,
                                       params=params, headers=self.headers(tr_id))
        if str(body.get('rt_cd')) != '0' or (name != 'calendar' and headers.get('tr_cont', '') in ('M', 'F')):
            raise KISError('KIS_API_' + self._safe_code(body.get('msg_cd')))
        return body

    def watchlist_groups(self, hts_id):
        if not hts_id:
            raise KISError('KIS_WATCHLIST_LIVE_HTS_REQUIRED')
        rows = self.market_read('groups', TYPE='1', FID_ETC_CLS_CODE='00', USER_ID=hts_id).get('output2')
        if not isinstance(rows, list):
            raise KISError('KIS_WATCHLIST_RESPONSE_INVALID')
        groups = {}
        for row in rows:
            if not isinstance(row, dict):
                raise KISError('KIS_WATCHLIST_RESPONSE_INVALID')
            code, name = row.get('inter_grp_code'), row.get('inter_grp_name')
            if not isinstance(code, str) or not isinstance(name, str):
                raise KISError('KIS_WATCHLIST_RESPONSE_INVALID')
            code, name = code.strip(), name.strip()
            if not re.fullmatch(r'[A-Za-z0-9]{1,16}', code) or not 1 <= len(name) <= 80:
                raise KISError('KIS_WATCHLIST_RESPONSE_INVALID')
            if code in groups and groups[code] != name:
                raise KISError('KIS_WATCHLIST_RESPONSE_INVALID')
            groups[code] = name
        return tuple(groups.items())

    def read(self, name, params, continuation=''):
        if name not in self.READS:
            raise KISError('KIS_ENDPOINT_DENIED')
        path, tr_id = self.READS[name]
        body, headers = self._exchange(path, params={**params, 'CANO': self.account,
              'ACNT_PRDT_CD': self.product}, headers={**self.headers(tr_id), 'tr_cont': continuation})
        if str(body.get('rt_cd')) != '0':
            raise KISError('KIS_API_' + self._safe_code(body.get('msg_cd')))
        return body, headers.get('tr_cont', '')

    def balance(self, name):
        if name == 'domestic':
            params = dict(AFHR_FLPR_YN='N', OFL_YN='', INQR_DVSN='02', UNPR_DVSN='01',
                          FUND_STTL_ICLD_YN='N', FNCG_AMT_AUTO_RDPT_YN='N', PRCS_DVSN='00',
                          CTX_AREA_FK100='', CTX_AREA_NK100='')
            suffix = '100'
        elif name == 'us':
            params = dict(OVRS_EXCG_CD='NASD', TR_CRCY_CD='USD', CTX_AREA_FK200='', CTX_AREA_NK200='')
            suffix = '200'
        else:
            raise KISError('KIS_BALANCE_NAME_INVALID')
        rows, seen, continuation = [], set(), ''
        for _ in range(100):
            body, more = self.read(name, params, continuation)
            output = body.get('output1')
            if not isinstance(output, list):
                raise KISError('KIS_BALANCE_ROWS_INVALID')
            rows.extend(output)
            if more not in ('M', 'F'):
                return {'output1': rows, 'output2': body.get('output2'), 'complete': True}
            cursor = tuple(str(body.get('ctx_area_' + x + suffix, '')).strip() for x in ('fk', 'nk'))
            if not any(cursor) or cursor in seen:
                raise KISError('KIS_PAGINATION_STALLED')
            seen.add(cursor)
            for x, value in zip(('FK', 'NK'), cursor):
                params['CTX_AREA_' + x + suffix] = value
            continuation = 'N'
        raise KISError('KIS_PAGINATION_LIMIT')

    def pages(self, name, params, field='output1', suffix='100'):
        params = dict(params, **{'CTX_AREA_FK'+suffix: '', 'CTX_AREA_NK'+suffix: ''})
        rows, seen, continuation = [], set(), ''
        for _ in range(100):
            body, more = self.read(name, params, continuation)
            page = body.get(field)
            if not isinstance(page, list):
                raise KISError('KIS_HISTORY_ROWS_INVALID')
            rows.extend(page)
            if more not in ('M', 'F'):
                return rows, body.get('output2')
            cursor = tuple(str(body.get('ctx_area_'+x+suffix, '')).strip() for x in ('fk', 'nk'))
            if not any(cursor) or cursor in seen:
                raise KISError('KIS_PAGINATION_STALLED')
            seen.add(cursor)
            for x, value in zip(('FK', 'NK'), cursor):
                params['CTX_AREA_'+x+suffix] = value
            continuation = 'N'
        raise KISError('KIS_PAGINATION_LIMIT')
