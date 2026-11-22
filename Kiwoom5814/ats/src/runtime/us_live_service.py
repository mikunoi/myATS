"""Polling runtime for Kiwoom US equities, scheduled in America/New_York."""
import asyncio
import hashlib
import json
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from src.telegram.error_messages import order_blocked_message
from src.runtime.close_phases import CloseEvaluationPhases
from src.runtime.us_schedule import KST, session_close, bedtime_due
from src.runtime.us_preview import USPreview
from src.runtime.universe import trading_universe


def eastern_now(utc_now=None):
    """US Eastern time with statutory DST, without an external tzdata package."""
    utc_now = utc_now or datetime.now(timezone.utc)
    year = utc_now.year
    march = datetime(year, 3, 8, 7, tzinfo=timezone.utc)
    dst_start = march + timedelta(days=(6 - march.weekday()) % 7)
    november = datetime(year, 11, 1, 6, tzinfo=timezone.utc)
    dst_end = november + timedelta(days=(6 - november.weekday()) % 7)
    offset = -4 if dst_start <= utc_now < dst_end else -5
    return utc_now.astimezone(timezone(timedelta(hours=offset)))


class USEvaluationClock:
    def __init__(self): self.last_date=None; self.retry_after=None
    @staticmethod
    def regular_market_open(now=None):
        now=now or eastern_now()
        close = session_close(now)
        return close is not None and time(9,30)<=now.time()<close
    def due(self, now):
        now=now
        if not self.regular_market_open(now) or now.time()<time(15,0) or self.last_date==now.date(): return False
        if self.retry_after and now<self.retry_after:return False
        self.last_date=now.date();self.retry_after=None;return True
    def retry(self,now): self.last_date=None;self.retry_after=now+timedelta(seconds=60)


class USLiveRuntimeService:
    def __init__(self, settings, client, coordinator, store, telegram, symbols, names, logger,
                 notion_sync=None, entry_symbols=None):
        self.settings,self.client,self.coordinator,self.store=settings,client,coordinator,store
        self.telegram,self.symbols,self.names,self.logger=telegram,tuple(symbols),names,logger
        self.entry_symbols=frozenset(self.symbols if entry_symbols is None else entry_symbols)
        self.clock=USEvaluationClock(); self.daily={};self.weekly={}
        self.close_phases=CloseEvaluationPhases(store,"us",time(15,0), buy_time=time(15,10), buy_retry_time=time(15,30))
        self.notion_sync=notion_sync

    def label(self,s): return f"{self.names.get(s,s)}({s})"

    def has_active_orders(self):
        return any(entry["status"] in self.coordinator.ACTIVE
                   for entry in self.coordinator.journal.values())

    def addons_allowed(self, now):
        return (self.store.control()=="RUNNING" and self.clock.regular_market_open(now)
                and not self.close_phases.buys_paused(now)
                and self.close_phases.due(now,True) is None)

    async def refresh(self,symbol):
        date=eastern_now().strftime("%Y%m%d")
        daily=await asyncio.to_thread(self.client.candles,symbol,"daily",date,True,None)
        weekly=await asyncio.to_thread(self.client.candles,symbol,"weekly",date,True,None)
        self.daily[symbol]=self.coordinator.strategy.indicators(daily,"DAILY")
        self.weekly[symbol]=self.coordinator.strategy.indicators(weekly,"WEEKLY")

    async def initialize(self):
        for symbol in self.symbols:
            await self.refresh(symbol)
            if len(self.daily[symbol])<2 or len(self.weekly[symbol])<2 or self.daily[symbol][-1].atr is None:
                raise RuntimeError("US indicators are not ready")

    async def refresh_buy_priority(self):
        raw = await asyncio.to_thread(self.client.synchronized_watchlist,
            self.settings.max_watchlist + len(self.settings.excluded_symbols),
            "(5814)감시종목", allow_empty=True)
        entries, symbols = trading_universe(raw, self.coordinator.broker.snapshot().positions,
            self.coordinator.states, self.settings.excluded_symbols, self.settings.max_watchlist)
        self.entry_symbols, self.symbols = frozenset(entries), tuple(symbols)
        self.coordinator.broker.managed_symbols = frozenset(symbols)
        self.client.managed_symbols = frozenset(symbols)
        self.store.set_system_value("us_watchlist_cache", json.dumps(
            {symbol:self.client.exchanges[symbol] for symbol in raw}))

    def buy_attempted(self, symbol, day):
        return any(e.get("symbol")==symbol and e.get("side")=="BUY" and e.get("date")==day
                   for e in self.coordinator.journal.values())

    @staticmethod
    def display_time(now, hour=None, minute=0):
        target = now if hour is None else now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return f"{target.astimezone(KST):%m-%d %H:%M} 한국시간({target:%m-%d %H:%M} 미국 동부시간)"

    def display_cash(self, amount):
        amount = Decimal(amount)
        usd = f"{amount:,.2f}USD"
        provider = getattr(self.coordinator.broker, "unified_capital", None)
        cached = getattr(provider, "cached", None) or {}
        try:
            rate = Decimal(str(cached.get("usd_krw", "0")))
            if rate.is_finite() and rate > 0:
                return f"{amount * rate:,.0f}원({usd})"
        except (ValueError, ArithmeticError):
            pass
        return f"원화 환산 불가({usd})"

    def cash_notice(self, timing, rows, available, *, shortage=True):
        required = sum((Decimal(item["required"]) for _, item in rows), Decimal(0))
        title = "예수금 부족 안내" if shortage else "예수금 점검"
        suffix = f"({timing})" if timing else ""
        lines = [f"[해외] {title}{suffix}",
                 f"통합예수금 {self.display_cash(available)} / 필요 {self.display_cash(required)}"]
        if rows:
            lines.extend(["", "<진입대기 리스트>"])
            for index, (symbol, item) in enumerate(rows, 1):
                lines.append(f"{index}) {symbol}: {item['quantity']:,}주, {self.display_cash(item['required'])}")
        else:
            lines.append("진입·추가매수 후보 없음")
        return "\n".join(lines)

    def notify_shortfalls(self, now, phase, rows):
        if not rows:
            return
        from src.runtime.us_preview import integrated_cash_available
        available = integrated_cash_available(self.coordinator)
        required = sum((Decimal(item["required"]) for _, item in rows), Decimal(0))
        message = self.cash_notice("", rows, available, shortage=required > available)
        orderable = min(Decimal(item["available"]) for _, item in rows)
        if orderable < available:
            message += f"\nUSD 주문가능액 {self.display_cash(orderable)} · 환전/통합증거금 확인 필요"
        self.telegram.notify(f"us-cash-shortfall:{now.date()}:{phase}", message)

    async def evaluate_close(self, now, phase="SELL"):
        if self.store.control()=="STOP": return
        buy_phase = phase in {"BUY", "BUY_RETRY"}
        if buy_phase:
            for completion, action in self.coordinator.sync_orders(force_capital_refresh=True):
                self.telegram.notify(f"us-fill:{completion.order_id}",
                    f"[해외] {self.label(completion.request.symbol)} {action} 체결 {completion.filled_quantity}주")
            await self.refresh_buy_priority()
            self.coordinator.cash_shortfalls.clear()
        self.close_phases.begin(now, phase)
        day = now.date().isoformat()
        shortfalls=[]
        for symbol in self.symbols:
            if self.close_phases.symbol_done(now,phase,symbol): continue
            if phase=="BUY_RETRY" and self.buy_attempted(symbol,day):
                self.close_phases.complete_symbol(now,phase,symbol)
                continue
            issue=f"evaluation_issue:{day}:{phase}:{symbol}"
            if self.coordinator._has_open_order(symbol) or self.store.system_value("manual_reference_pending:"+symbol, ""):
                self.store.set_system_value(issue,"SYMBOL_PENDING")
                continue
            if buy_phase and self.store.system_value(f"evaluation_issue:{day}:SELL:{symbol}", ""):
                self.store.set_system_value(issue,"SELL_EVALUATION_PENDING")
                continue
            try:
                await self.refresh(symbol)
                if len(self.daily[symbol])<2 or len(self.weekly[symbol])<2:
                    raise RuntimeError("US indicators unavailable")
                price=Decimal((await asyncio.to_thread(self.client.quote,symbol)).current_price)
            except Exception:
                self.store.set_system_value(issue,"DATA_UNAVAILABLE")
                continue
            if self.store.control()=="STOP": return
            if not self.clock.regular_market_open(): raise RuntimeError("US session ended during evaluation")
            if not self.coordinator.gate.orders_allowed(is_buy=buy_phase):
                raise RuntimeError("Account gate not ready")
            try:
                order_id=self.coordinator.evaluate_close(symbol,self.daily[symbol],self.weekly[symbol],price,day,
                    allow_buys=buy_phase,allow_sells=phase=="SELL",allow_new_entries=symbol in self.entry_symbols)
            except Exception:
                self.store.set_system_value(issue,"ORDER_CHECK_REQUIRED")
                raise
            if order_id: self.telegram.notify("us-order:"+order_id, f"[해외] {self.label(symbol)} 전략 주문 #{order_id}")
            shortage=self.coordinator.cash_shortfalls.pop(symbol,None) if buy_phase else None
            if shortage is not None: shortfalls.append((symbol,shortage))
            self.close_phases.complete_symbol(now,phase,symbol)
            self.store.delete_system_value(issue)
        self.notify_shortfalls(now,phase,shortfalls)
        self.close_phases.complete(now,phase)
        if phase=="BUY_RETRY" and not shortfalls:
            self.telegram.notify(f"us-buy-retry:{day}",f"[해외] {self.display_time(now)} 매수 재평가 완료. 주문·자료 대기 여부는 점검 기록을 확인하세요.")

    async def bedtime_report(self, now):
        key=f"us-bedtime:{now.date()}"
        if not bedtime_due(now) or self.store.system_value(key,""): return
        retry=self.store.system_value(key+":retry","")
        if retry and now < datetime.fromisoformat(retry): return
        try:
            if session_close(now) is None:
                message=f"[해외] {self.display_time(now)} 취침 전 점검: 오늘 미국 휴장 또는 거래일 자료 미확인으로 가매수 평가를 보류합니다."
            else:
                raw=await asyncio.to_thread(self.client.synchronized_watchlist,
                    self.settings.max_watchlist+len(self.settings.excluded_symbols),"(5814)감시종목",allow_empty=True)
                symbols=tuple(s for s in raw if s not in self.settings.excluded_symbols)
                snapshot=self.coordinator.broker.sync_account()  # Read-only account refresh; no order reconciliation.
                preview=USPreview(self.coordinator,snapshot,symbols)
                failed=[]
                deferred=[]
                for symbol in symbols:
                    if self.store.control()=="STOP": return
                    self.store.set_system_value("service_last_heartbeat:us",datetime.now(timezone.utc).isoformat())
                    if preview._has_open_order(symbol) or self.store.system_value("manual_reference_pending:"+symbol,""):
                        deferred.append(symbol)
                        continue
                    try:
                        date=now.strftime("%Y%m%d")
                        daily=await asyncio.to_thread(self.client.candles,symbol,"daily",date,True,None)
                        weekly=await asyncio.to_thread(self.client.candles,symbol,"weekly",date,True,None)
                        daily=self.coordinator.strategy.indicators(daily,"DAILY")
                        weekly=self.coordinator.strategy.indicators(weekly,"WEEKLY")
                        if len(daily)<2 or len(weekly)<2 or daily[-1].atr is None or weekly[-1].atr is None:
                            raise RuntimeError("Preview indicators unavailable")
                        quote=await asyncio.to_thread(self.client.quote,symbol)
                        preview.evaluate(symbol,daily,weekly,quote.current_price,now.date().isoformat())
                    except Exception:
                        failed.append(symbol)
                rows = [(r["symbol"], r) for r in preview.rows]
                shortage = any(r["status"] == "예수금 부족" for r in preview.rows)
                lines = [self.cash_notice("취침전", rows, preview.available, shortage=shortage)]
                capped = [r["symbol"] for r in preview.rows if r["status"] == "자금사용 상한"]
                if capped: lines.append("자금사용 상한: " + ", ".join(capped))
                if failed: lines.append("자료 미확인: " + ", ".join(failed))
                if deferred: lines.append("미체결·대사 대기: " + ", ".join(deferred))
                if session_close(now)==time(13): lines.append("조기폐장일: 마감 전 정규평가 생략")
                lines.append("가매수 참고치")
                message="\n".join(lines)
            self.telegram.notify(key,message)
            self.store.set_system_value(key,"QUEUED")
        except Exception as error:
            self.store.set_system_value(key+":retry",(now+timedelta(minutes=5)).isoformat())
            self.logger.error("US bedtime preview unavailable: %s",type(error).__name__)
            self.telegram.notify(key+":error","[해외] 취침 전 예수금 점검 자료 조회 실패. 23시대에 다시 확인합니다.")

    def scheduled_check(self, now):
        if session_close(now)!=time(16): return
        windows=(("SELL_PRE",time(14,55),time(15)),("SELL_POST",time(15,3),time(15,5)),
                 ("BUY_PRE",time(15,5),time(15,10)),("BUY_POST",time(15,13),time(15,30)))
        for name,start,end in windows:
            key=f"us-evaluation-check:{now.date()}:{name}"
            if start<=now.time()<end and not self.store.system_value(key,""):
                try:
                    for completion,action in self.coordinator.sync_orders(force_capital_refresh=True):
                        self.telegram.notify(f"us-fill:{completion.order_id}",
                            f"[해외] {completion.request.symbol} {action} 체결 {completion.filled_quantity}주")
                    phase="SELL" if name.startswith("SELL") else "BUY"
                    issues=self.store.system_values(f"evaluation_issue:{now.date()}:{phase}:")
                    done=self.store.system_value(self.close_phases.key(now,phase+":done"),"")
                    status="점검 필요" if self.has_active_orders() or issues or (name.endswith("POST") and not done) else "완료"
                    self.store.set_system_value(key,status+" "+now.isoformat())
                    if status!="완료": self.telegram.notify(key,f"[해외] {name} 점검: 평가 미완료 또는 미체결·자료 확인 대기")
                except Exception:
                    self.telegram.notify(key+":error",f"[해외] {name} 계좌 점검 실패. 거래 가능 여부 재확인 필요")
                    raise

    async def supervise(self):
        self.store.set_system_value("us_runtime_revision","2026-10-22-us-integrated-cash-v5")
        self.store.set_system_value("us_connection_state","CONNECTED")
        lifecycle=self.store.system_value("lifecycle_start_pending","")
        if lifecycle:
            message="업데이트 완료" if lifecycle=="UPDATE_COMPLETE" else "시스템 재개"
            self.telegram.notify(f"us-lifecycle-start:{eastern_now().isoformat(timespec='seconds')}",
                "[해외] "+message)
            self.store.delete_system_value("lifecycle_start_pending")
            self.store.delete_system_value("lifecycle_stop_reason")
        failure_delay=5
        consecutive_failures=0
        while self.store.control()!="STOP":
            cycle_delay=5
            now=eastern_now()
            self.store.set_system_value("service_last_heartbeat:us",datetime.now(timezone.utc).isoformat())
            await self.bedtime_report(now)
            self.telegram.retry_pending()
            if not self.clock.regular_market_open(now) and not self.has_active_orders():
                self.store.set_system_value("us_connection_state","IDLE_OFF_SESSION")
                await asyncio.sleep(60)
                continue
            try:
                if self.clock.regular_market_open(now) and any(
                        symbol not in self.daily or symbol not in self.weekly
                        for symbol in self.symbols):
                    await self.initialize()
                self.coordinator.reconcile_uncertain_submissions()
                completed=list(self.coordinator.sync_orders())
                self.store.set_system_value("us_connection_state","CONNECTED")
                self.store.set_system_value("account_last_success:us",datetime.now(timezone.utc).isoformat())
                self.store.set_system_value("account_sync_error:us","")
                if self.clock.regular_market_open(now):
                    self.coordinator.retry_confirmed_unsubmitted(now.date().isoformat())
                    self.coordinator.submit_ready_us_market_replacements(now.date().isoformat())
                pending=[s for s in self.symbols if self.store.system_value("manual_reference_pending:"+s,"")]
                if pending:
                    refs={}
                    for s in pending:
                        await self.refresh(s); state=self.coordinator.states.get(s); mode=state.mode if state else "DAILY"
                        pts=self.weekly[s] if mode=="WEEKLY" else self.daily[s]
                        if pts and pts[-1].atr is not None: refs[s]=(mode,pts[-1].atr)
                    completed.extend(self.coordinator.sync_orders(refs))
                for completion,action in completed:
                    self.telegram.notify(f"us-fill:{completion.order_id}",
                        f"[해외] {self.label(completion.request.symbol)} {action} 체결 {completion.filled_quantity}주 #{completion.order_id}")
                now=eastern_now()
                # Reload the broker watchlist before the 15:00 evaluation so
                # same-day additions and removals are reflected.
                if (now.weekday()<5 and now.time()>=time(14,45)
                        and self.store.system_value("us_watchlist_sync_date","")!=now.date().isoformat()):
                    self.store.set_system_value("us_watchlist_sync_date",now.date().isoformat())
                    return
                self.scheduled_check(now)
                phase=self.close_phases.due(now,self.clock.regular_market_open(now))
                if self.addons_allowed(now):
                    for s in self.symbols:
                        if not self.addons_allowed(eastern_now()): break
                        state=self.coordinator.states.get(s)
                        pts=self.weekly.get(s) if state and state.mode=="WEEKLY" else self.daily.get(s)
                        if state and pts and pts[-1].atr is not None:
                            price=Decimal((await asyncio.to_thread(self.client.quote,s)).current_price)
                            tick=eastern_now()
                            if not self.addons_allowed(tick): break
                            order_id=self.coordinator.evaluate_addon(s,price,pts[-1].atr,tick.date().isoformat())
                            if order_id:self.telegram.notify("us-order:"+order_id,
                                f"[해외] {self.label(s)} 추가매수 주문 #{order_id}")
                now=eastern_now()
                phase=self.close_phases.due(now,self.clock.regular_market_open(now))
                if phase: await self.evaluate_close(now,phase)
                if self.clock.regular_market_open(now):
                    self.coordinator.replace_stale_us_limits()
                close=session_close(now)
                if close is not None and now.time()>=close: self.coordinator.cancel_remaining()
                if self.notion_sync:
                    try:
                        self.notion_sync.manual_holdings.sync(
                            self.coordinator.broker.snapshot().positions,
                            set(self.coordinator.broker.managed_symbols) | set(self.coordinator.states),
                            "US", lambda symbol: symbol)
                    except Exception as error:
                        self.logger.error("US manual holdings reporting pending: %s", type(error).__name__)
                self.telegram.retry_pending()
                recovery=self.store.system_value("auto_recovery_pending:us","")
                if recovery:
                    self.telegram.notify("auto-recovery:us:"+recovery,"[해외] 자동 복구 완료")
                    self.store.delete_system_value("auto_recovery_pending:us")
                consecutive_failures=0
                failure_delay=5
                if not self.symbols:
                    await asyncio.sleep(60)
                    return
            except Exception as error:
                consecutive_failures+=1
                self.clock.retry(now)
                self.close_phases.retry(eastern_now())
                self.coordinator.gate.disconnected(); self.store.set_system_value("us_connection_state","ERROR")
                self.store.set_system_value("account_sync_error:us",type(error).__name__+": "+str(error)[:200])
                self.logger.error("US reconciliation/evaluation blocked: %s",str(error))
                fingerprint=hashlib.sha256((type(error).__name__+":"+str(error)).encode()).hexdigest()[:12]
                if consecutive_failures>=2:
                    self.telegram.notify("us-error:"+eastern_now().strftime("%Y%m%d")+":"+fingerprint,
                        order_blocked_message("[해외]", error))
                    self.store.set_system_value("auto_recovery_pending:us",fingerprint)
                self.client.disconnect()
                if hasattr(self.client,"unified_peer"):self.client.unified_peer.disconnect()
                cycle_delay=failure_delay;failure_delay=min(failure_delay*2,60)
            await asyncio.sleep(cycle_delay)
        self.store.set_system_value("us_connection_state","DISCONNECTED")
        if self.store.system_value("lifecycle_stop_notified","0")!="1":
            reason=self.store.system_value("lifecycle_stop_reason","SYSTEM")
            message="업데이트 중" if reason=="UPDATE" else "시스템 중지"
            self.telegram.notify(f"us-lifecycle-stop:{eastern_now().isoformat(timespec='seconds')}",
                "[해외] "+message)
            self.store.set_system_value("lifecycle_stop_notified","1")
