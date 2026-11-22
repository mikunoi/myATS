"""KIS domestic reads for registered accounts, with separate tokens and caches."""
import json
from pathlib import Path
import re
import sys
import time

from api_accounts import save_json, account_dir, private_dir, fingerprint
from kis_portfolio import PortfolioClient, KISError


class ManagedKIS(PortfolioClient):
    MARKET = {
        **PortfolioClient.MARKET,
        'calendar': ('chk-holiday', 'CTCA0903R'),
        'group': ('intstock-stocklist-by-group', 'HHKCM113004C6'),
        'chart': ('inquire-daily-itemchartprice', 'FHKST03010100'),
        'quote': ('inquire-price', 'FHKST01010100'),
        'book': ('inquire-asking-price-exp-ccn', 'FHKST01010200'),
    }

    def __init__(self, root, profile):
        self.root = Path(root).resolve()
        self.profile = profile
        self.identity = profile.credentials_id
        self.key, self.secret = profile.app_key, profile.app_secret
        self.account, self.product = profile.number, profile.product
        self.cache_dir = private_dir(account_dir(root, profile))
        # OAuth and API pacing are app-key scoped even when two accounts share a key.
        self.transport_dir = private_dir(self.cache_dir.parent / ('transport-' + fingerprint(profile.mode, self.key)))
        self.cache_path = self.transport_dir / 'token.json'
        self.token_identity = fingerprint(profile.mode, self.key, self.secret)
        module_root = Path(__file__).resolve().parent.parent/'ats'
        if not (module_root/'src/platform_support.py').is_file():
            module_root = self.root
        if str(module_root) not in sys.path:
            sys.path.insert(0, str(module_root))
        from src.platform_support import FileLock
        self.lock_class = FileLock
        self.token = None
        self.token_expiry = 0
        self.last_request = 0.0
        self.guard = None
        if profile.mode == 'demo':
            self.HOST = 'https://openapivts.koreainvestment.com:29443'
        self.READS = {
            'domestic': (PortfolioClient.READS['domestic'][0],
                         'VTTC8434R' if profile.mode == 'demo' else 'TTTC8434R'),
            'domestic_orders': (PortfolioClient.READS['domestic_orders'][0],
                                'VTTC0081R' if profile.mode == 'demo' else 'TTTC0081R'),
            'domestic_orders_old': (PortfolioClient.READS['domestic_orders'][0],
                                    'VTSC9215R' if profile.mode == 'demo' else 'CTSC9215R'),
        }

    def authenticate(self):
        if self.token and time.time() + 300 < self.token_expiry:
            return
        with self.lock_class(self.transport_dir/'token.lock', timeout=20):
            self._load_or_issue_token()

    def _load_or_issue_token(self):
        self.token = None
        try:
            cached = json.loads(self.cache_path.read_text(encoding='utf-8'))
            expiry = float(cached['expires_at'])
            if cached['identity'] == self.token_identity and time.time() + 300 < expiry:
                self.token, self.token_expiry = cached['token'], expiry
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        body, _ = self._exchange('/oauth2/tokenP', payload={
            'grant_type': 'client_credentials', 'appkey': self.key, 'appsecret': self.secret})
        token = body.get('access_token')
        expiry = time.time() + min(float(body.get('expires_in', 0)), 86400)
        if not isinstance(token, str) or not token or expiry <= time.time() + 300:
            raise KISError('KIS_AUTH_' + self._safe_code(body.get('error_code')))
        save_json(self.cache_path, dict(identity=self.token_identity, token=token, expires_at=expiry))
        self.token, self.token_expiry = token, expiry

    def _exchange(self, path, *, params=None, payload=None, headers=None):
        with self.lock_class(self.transport_dir/'request.lock', timeout=20):
            schedule = self.transport_dir/'pace.json'
            try:
                shared = json.loads(schedule.read_text(encoding='utf-8'))
                previous = float(shared['monotonic'])
                if 0 <= time.monotonic() - previous < 2:
                    self.last_request = max(self.last_request, previous)
            except (OSError, ValueError, TypeError, KeyError):
                pass
            try:
                return super()._exchange(path, params=params, payload=payload, headers=headers)
            finally:
                save_json(schedule, {'monotonic': self.last_request})

    def headers(self, tr_id):
        self.authenticate()
        return {'authorization': 'Bearer ' + self.token, 'appkey': self.key,
                'appsecret': self.secret, 'tr_id': tr_id, 'custtype': 'P'}

    def market_day(self, day):
        rows = self.market_read('calendar', BASS_DT=day, CTX_AREA_FK='', CTX_AREA_NK='')['output']
        matching = [row for row in rows if row['bass_dt'] == day]
        if len(matching) != 1 or matching[0]['opnd_yn'] not in ('Y','N'):
            raise KISError('KIS_MARKET_DAY_UNAVAILABLE')
        return matching[0]['opnd_yn'] == 'Y'

    def watchlist(self, group, expected_code=''):
        if self.profile.mode != 'live' or not self.profile.hts_id:
            raise KISError('KIS_WATCHLIST_LIVE_HTS_REQUIRED')
        matches = [code for code, name in self.watchlist_groups(self.profile.hts_id)
                   if name == group and (not expected_code or code == expected_code)]
        if len(matches) != 1:
            raise KISError('KIS_WATCHLIST_GROUP_MISSING_OR_AMBIGUOUS')
        code = matches[0]
        body = self.market_read('group', TYPE='1', FID_ETC_CLS_CODE='4',
                                USER_ID=self.profile.hts_id, INTER_GRP_CODE=code, DATA_RANK='',
                                INTER_GRP_NAME='', HTS_KOR_ISNM='', CNTG_CLS_CODE='')
        stocks = {}
        for row in body['output2']:
            symbol = str(row['jong_code']).strip()
            if re.fullmatch(r'[0-9A-Z]{6}', symbol):
                stocks.setdefault(symbol, str(row.get('hts_kor_isnm') or symbol))
        if not stocks:
            raise KISError('KIS_WATCHLIST_EMPTY')
        return code, stocks


def watchlist_client(root, profile):
    if profile.mode == 'live':
        return ManagedKIS(root, profile)
    from api_accounts import accounts
    sources = [p for p in accounts(root) if p.mode == 'live' and p.enabled
               and p.hts_id and p.hts_id == profile.hts_id]
    if not sources:
        raise KISError('KIS_WATCHLIST_REQUIRES_LINKED_LIVE_PROFILE')
    return ManagedKIS(root, sources[0])
