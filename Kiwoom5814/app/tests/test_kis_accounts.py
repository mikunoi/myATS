"""Synthetic account, KIS transport and durable order tests. No network access."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
ATS = APP.parent / 'ats'
sys.path[:0] = [str(APP), str(ATS)]
from api_accounts import (Account, accounts, account_dir, delete_account, env_values, save_account,
                          save_env_metadata, save_json)
from kis_execution import ExecutionClient
from kis_portfolio import KISError, PortfolioClient
from kis_runtime import run, runtime_types
from managed_snapshots import apply, collect, snapshot_path, visible_accounts
from refresh_schedule import KST
from src.database.store import Store
from src.market_data.models import OrderBookLevel
from src.orders.models import Side
from src.recovery.service import RecoveryGate
from src.runtime.live import LiveAccountBroker
from src.runtime.mock import PendingAction


class RegisteredAccountsTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        place = Path(temp.name)
        self.root, self.prefs = place/'ats', place/'preferences'
        self.root.mkdir()
        self.prefs.mkdir()
        preferences = patch('api_accounts.preferences_dir', return_value=self.prefs)
        preferences.start()
        self.addCleanup(preferences.stop)
        self.first = Account('첫 한투', 'live', '00000001', '01', 'key1', 'secret1',
                             'test-user', '그룹', '001', True)
        self.second = replace(self.first, alias='둘째 한투', number='00000002', app_key='key2')

    def test_separate_identity_and_local_file_permissions(self):
        save_account(self.root, self.first)
        save_account(self.root, self.second)
        self.assertEqual(set(accounts(self.root)), {self.first,self.second})
        self.assertNotEqual(account_dir(self.root,self.first),account_dir(self.root,self.second))
        with self.assertRaises(ValueError):
            save_account(self.root, replace(self.first, app_key='changed'))
        save_account(self.root, replace(self.first, app_key='changed'), previous=self.first.identity)
        self.assertEqual(len(accounts(self.root)),2)
        registry = account_dir(self.root,self.first).parent/'accounts.json'
        if os.name == 'posix':
            self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.first.app_secret,repr(self.first))
        (self.root/'.env').write_text('KIS_CANO=00000003\nKIS_ACNT_PRDT_CD=01\n')
        with self.assertRaises(ValueError):
            save_account(self.root, replace(self.first,number='00000003'))

    def test_remove_only_selected_profile_and_reject_stale_update(self):
        original_env = 'KIS_CANO=00000003\nKIS_ACNT_PRDT_CD=01\n'
        env = self.root / '.env'
        env.write_text(original_env)
        save_account(self.root, self.first)
        save_account(self.root, self.second)
        saved_snapshot = snapshot_path(self.root, self.first)
        save_json(saved_snapshot, {'credentials': self.first.credentials_id})
        updated = replace(self.first, app_secret='new-secret')
        save_account(self.root, updated, previous=self.first.identity)
        self.assertEqual(accounts(self.root), (updated, self.second))
        delete_account(self.root, updated.identity)
        self.assertEqual(accounts(self.root), (self.second,))
        self.assertEqual(env.read_text(), original_env)
        self.assertTrue(saved_snapshot.is_file())
        with self.assertRaises(ValueError):
            delete_account(self.root, self.first.identity)
        with self.assertRaises(ValueError):
            save_account(self.root, updated, previous=self.first.identity)
        self.assertEqual(accounts(self.root), (self.second,))

    def test_env_sync_routes_demo_and_removes_deleted_numbered_accounts(self):
        env = self.root / '.env'
        base = ('KIS_APP_KEY=base-key\nKIS_APP_SECRET=base-secret\n'
                'KIS_CANO=00000010\nKIS_ACNT_PRDT_CD=01\n')
        extra = ('KIS_APP_KEY_2=extra-key\nKIS_APP_SECRET_2=extra-secret\n'
                 'KIS_CANO_2=00000011\nKIS_ACNT_PRDT_CD_2=01\n')
        env.write_text(base + extra, encoding='utf-8')
        self.assertEqual([p.masked for p in visible_accounts(self.root)], ['••••0011-01'])
        before = env_values(self.root)
        save_env_metadata(self.root, before, {'KIS_ALIAS': '모의 한투', 'KIS_ENV': 'demo'})
        with self.assertRaisesRegex(KISError, 'KIS_DEFAULT_LIVE_ACCOUNT_DISABLED'):
            PortfolioClient(self.root)
        primary, secondary = visible_accounts(self.root)
        self.assertEqual((primary.mode, primary.alias, secondary.mode), ('demo', '모의 한투', 'live'))
        self.assertEqual(env_values(self.root)['KIS_APP_KEY'], before['KIS_APP_KEY'])
        with self.assertRaisesRegex(ValueError, '설정이 변경'):
            save_env_metadata(self.root, before, {'KIS_ENV': 'live'})
        env.write_text(base, encoding='utf-8')
        self.assertEqual(visible_accounts(self.root), ())

    def test_cash_return_and_fills_merge_with_duplicates_and_missing_data(self):
        demo = replace(self.first,mode='demo',alias='모의')
        for profile in (self.first,self.second,demo):
            save_account(self.root,profile)
        now = datetime.now(KST).isoformat()
        for profile, cash, cost, value in ((self.first,20,100,110),(self.second,10,200,160),(demo,900,100,800)):
            save_json(snapshot_path(self.root,profile),dict(credentials=profile.credentials_id,
                balance=dict(at=now,cash=cash,holdings=[dict(broker='kis',market='domestic',
                    symbol='005930',purchase=cost,evaluation=value,quantity=1,name='가상종목')]),
                fills_at=now,fills=[dict(id='same',at=now,trade_date=now[:10],symbol='005930')]))
        def base():
            return dict(cash=10,capital=310,full_account=True,cash_at=now, holdings=[
                dict(symbol='0046A0',broker='kiwoom',krw=300,evaluation_krw=360)],
                fills=[], cash_by_broker={'kiwoom':10}, portfolio_note='키움',trade_history={'complete':True})
        data = apply(base(), self.root)
        self.assertEqual(data['cash'],40)
        self.assertAlmostEqual(data['registered_totals']['rate'],.05)
        self.assertEqual(len({fill['id'] for fill in data['fills']}),2)
        self.assertEqual(data['registered_totals']['present'],3)
        save_account(self.root,replace(self.second,app_key='changed'),previous=self.second.identity)
        data = apply(base(),self.root)
        self.assertIsNone(data['cash'])
        self.assertEqual(data['registered_totals']['cash'],30)
        self.assertEqual(data['registered_totals']['present'],2)

    def test_read_only_snapshot_failure_keeps_previous_balance(self):
        client = SimpleNamespace(balance=lambda _name: {'output2':[{'prvs_rcdl_excc_amt':'125'}],
            'output1':[{'pdno':'005930','hldg_qty':'2','pchs_amt':'80','evlu_amt':'100'}]})
        with patch('managed_snapshots.order_rows',side_effect=KISError('KIS_HISTORY_UNAVAILABLE')):
            result = collect(self.root,self.first,client=client)
        self.assertEqual(result['balance']['cash'],125)
        self.assertEqual(result['fills_error'],'KIS_HISTORY_UNAVAILABLE')
        client.balance = lambda _name: (_ for _ in ()).throw(KISError('KIS_TEST_FAILURE'))
        result = collect(self.root,self.first,client=client)
        self.assertEqual(result['balance']['cash'],125)
        self.assertEqual(result['balance_error'],'KIS_TEST_FAILURE')

    def test_watchlist_respects_bound_group(self):
        client = ExecutionClient(self.root,self.first,lambda _action: False)
        with patch.object(client,'market_read',side_effect=[
            {'output2':[{'inter_grp_code':'001','inter_grp_name':'그룹'}]},
            {'output2':[{'jong_code':'005930','hts_kor_isnm':'가상종목'}]},
        ]) as read:
            self.assertEqual(client.watchlist('그룹','001'),('001',{'005930':'가상종목'}))
            self.assertEqual(read.call_args.kwargs['INTER_GRP_CODE'],'001')
        with patch.object(client,'market_read',return_value={
            'output2':[{'inter_grp_code':'002','inter_grp_name':'그룹'}]}):
            with self.assertRaises(KISError):
                client.watchlist('그룹','001')

    def test_live_start_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(RuntimeError,'KIS_LIVE_CONFIRMATION_REQUIRED'):
            run(self.root,self.first)

    def test_stop_during_request_pacing_blocks_post(self):
        running = True
        client = ExecutionClient(self.root,self.first,lambda _action: running)
        client.headers = lambda tr_id: {'tr_id':tr_id}
        def stop_during_wait(_duration):
            nonlocal running
            running = False
        with patch('kis_portfolio.time.sleep',side_effect=stop_during_wait), \
             patch('kis_portfolio.request.build_opener') as open_request:
            with self.assertRaisesRegex(KISError,'KIS_ORDER_DISABLED'):
                client._post('order-cash','0012U',{},'submit')
        open_request.assert_not_called()
        self.assertIsNone(client.guard)

    def test_cancel_uses_account_and_exact_broker_order_branch(self):
        client = ExecutionClient(self.root,self.first,lambda _action: True)
        owned = dict(ord_gno_brno='00001',odno='0000000145',pdno='005930',psbl_qty='2')
        other = dict(owned,ord_gno_brno='00002')
        client.pages = lambda *_args: ([owned,other],{})
        with patch.object(client,'_post',return_value='000010000000146') as post:
            self.assertEqual(client.cancel('000010000000145','005930',0),'000010000000146')
            _, code, payload, action = post.call_args.args
            self.assertEqual((code,action),('0013U','cancel'))
            self.assertEqual(payload['KRX_FWDG_ORD_ORGNO'],'00001')
            self.assertEqual(payload['ORGN_ODNO'],'0000000145')
        with self.assertRaisesRegex(KISError,'KIS_CANCEL_ORDER_UNAVAILABLE'):
            client.cancel('000010000000145','000660',0)

    def test_durable_submit_reconcile_and_uncertain_never_retry(self):
        _, _, _, load_settings, _, _, _, _, Coordinator, _, _ = runtime_types(ATS)
        settings = load_settings(ATS)
        for profile, error in ((self.first,False),(self.second,True)):
            client = ExecutionClient(self.root,profile,lambda _action: True)
            client.order_book = lambda _: SimpleNamespace(
                asks=(OrderBookLevel(1,100,100),),bids=(OrderBookLevel(1,99,100),))
            client.headers = lambda tr_id: {'tr_id':tr_id}
            client.read = lambda *_args: ({'output':{'nrcvb_buy_qty':'500'}},'')
            history = []
            positions = []
            requests = []
            def balance(_name):
                return dict(output1=positions, output2=[dict(dnca_tot_amt='1000000',prvs_rcdl_excc_amt='1000000')])
            def exchange(path, *, payload, headers):
                requests.append((path,payload,headers))
                if error:
                    raise KISError('KIS_CONNECTION_FAILED')
                return {'rt_cd':'0','output':{'KRX_FWDG_ORD_ORGNO':'00001','ODNO':'0000000145'}}, {}
            client.balance = balance
            client.history = lambda _day: history
            client._exchange = exchange
            storage = account_dir(self.root,profile)/'test.sqlite3'
            with closing(Store(storage)) as store:
                store.migrate()
                store.set_control('RUNNING')
                gate = RecoveryGate(store)
                broker = LiveAccountBroker(client,('005930',))
                coordinator = Coordinator(settings,None,broker,gate,store)
                coordinator.restore()
                day = datetime.now(KST).date().isoformat()
                key = day+':005930:INITIAL'
                if error:
                    with self.assertRaises(RuntimeError):
                        coordinator._submit('005930',Side.BUY,2,key,PendingAction('INITIAL',Decimal(10),'DAILY'))
                    coordinator.restore()
                    self.assertEqual(coordinator.journal[key]['status'],'UNCERTAIN')
                    self.assertEqual(coordinator._resolve_unsubmitted_uncertain_orders(),0)
                    self.assertIsNone(coordinator._submit('005930',Side.BUY,2,key,PendingAction('INITIAL',Decimal(10),'DAILY')))
                    self.assertEqual(len(requests),1)
                else:
                    response = coordinator._submit('005930',Side.BUY,2,key,PendingAction('INITIAL',Decimal(10),'DAILY'))
                    self.assertEqual(response,'000010000000145')
                    history.append(dict(ord_gno_brno='00001',odno='0000000145',pdno='005930',
                                        ord_qty='2',tot_ccld_qty='2',cncl_yn='N',rmn_qty='0',tot_ccld_amt='200'))
                    positions.append(dict(pdno='005930',hldg_qty='2',ord_psbl_qty='2',pchs_amt='200',pchs_avg_pric='100'))
                    self.assertEqual(len(coordinator.sync_orders()),1)
                    self.assertEqual(coordinator.journal[key]['status'],'FILLED')
                    self.assertEqual(coordinator.states['005930'].initial_quantity,2)
                    self.assertIsNone(coordinator._submit('005930',Side.BUY,2,key,PendingAction('INITIAL',Decimal(10),'DAILY')))
                path, payload, headers = requests[0]
                self.assertTrue(path.endswith('/order-cash'))
                self.assertEqual(payload['CANO'],profile.number)
                self.assertEqual(payload['ORD_DVSN'],'03')
                self.assertEqual(headers['tr_id'],'TTTC0012U')


if __name__ == '__main__':
    unittest.main()
