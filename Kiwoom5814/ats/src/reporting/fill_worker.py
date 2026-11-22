"""Durable fill outbox delivery, independent of trading and market hours."""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace

from src.broker.kiwoom import KiwoomClient
from src.broker.kiwoom_us import KiwoomUSLiveClient
from src.config import load_secrets, kiwoom_secrets
from src.database.store import Store
from src.reporting.notion_5814 import Notion5814Sync
from src.reporting.notion_5814_us import Notion5814USSync
from src.runtime.live import LiveTradingCoordinator
from src.reporting.account_schedule import sync_due


def sync_cash_once(root, store, sync, logger):
    """Only the domestic worker owns the consolidated cash row, also off-session."""
    now = datetime.now(timezone.utc)
    retry = store.system_value("notion_cash_next_attempt", "")
    if retry and now < datetime.fromisoformat(retry):
        return
    store.set_system_value("notion_cash_next_attempt", (now + timedelta(seconds=60)).isoformat())
    domestic = overseas = None
    try:
        secrets = load_secrets(root)
        credentials = kiwoom_secrets(secrets, "production")
        domestic = KiwoomClient(credentials)
        overseas = KiwoomUSLiveClient(credentials, expected_account=secrets.kiwoom_expected_account,
                                      authorize=lambda: False)
        if (domestic.account_number() != secrets.kiwoom_expected_account
                or overseas.account_number() != secrets.kiwoom_expected_account):
            raise RuntimeError("Account mismatch")
        krw = Decimal(domestic.account_overview().d2_estimated_cash)
        usd = Decimal(overseas.d2_estimated_cash_usd())
        rate = Decimal(overseas.exchange_rate())
        if not all(v.is_finite() for v in (krw, usd, rate)) or rate <= 0:
            raise ValueError("Invalid cash snapshot")
        if not sync.sync_cash(krw + usd * rate, verify=True):
            raise RuntimeError("Notion cash update unconfirmed")
        store.set_system_value("notion_cash_snapshot", json.dumps({
            "krw_d2": str(krw), "usd_d2": str(usd), "usd_krw": str(rate),
            "total_krw": str(krw + usd * rate), "at": now.isoformat()}))
        store.set_system_value("notion_cash_last_success", datetime.now(timezone.utc).isoformat())
        store.set_system_value("notion_cash_error", "")
    except Exception as error:
        store.set_system_value("notion_cash_error", type(error).__name__)
        logger.error("Notion cash retry: %s", type(error).__name__)
    finally:
        if domestic is not None:
            domestic.disconnect()
        if overseas is not None:
            overseas.disconnect()


def deliver(store, sync, coordinator, prefix):
    # Close the row even when the broker has not published realized P/L yet.
    sync.sync_pending()
    coordinator.enrich_notion_sells()
    sync.sync_pending()
    pending = sum(store.system_value(prefix + key, "") != "1"
                  for key, _ in store.events_by_category("notion_holding_update"))
    return pending


def run_once(root, market, logger, names=None):
    us = market == "us"
    store = Store(root / "data" / ("kiwoom5814_us_live.sqlite3" if us else "kiwoom5814_live.sqlite3"))
    client = None
    key = "notion_delivery:" + market + ":"
    try:
        config = json.loads((root / "config/notion_5814.mapping.json").read_text(encoding="utf-8"))
        if config.get("enabled") is not True:
            store.set_system_value(key + "status", "DISABLED")
            store.set_system_value(key + "error", "")
            return 0
        store.set_system_value(key + "status", "ENABLED")
        secrets = load_secrets(root)
        sync = (Notion5814USSync(root, secrets.notion_token, store, logger, names or {}, retry_seconds=0)
                if us else Notion5814Sync(root, secrets.notion_token, store, logger, retry_seconds=0,
                                         symbol_names=names))
        if not sync.configured:
            raise RuntimeError("Notion synchronization is not configured")
        prefix = "notion_5814_us_applied:" if us else "notion_5814_applied:"
        pending = [(k, json.loads(raw)) for k, raw in store.events_by_category("notion_holding_update")
                   if store.system_value(prefix + k, "") != "1"]
        # The client has no order authorization. It is used only for realized P/L.
        production = kiwoom_secrets(secrets, "production")
        client = (KiwoomUSLiveClient(production, expected_account=secrets.kiwoom_expected_account,
                                    authorize=lambda: False) if us else KiwoomClient(production))
        def enrich():
            if any(p.get("profit_pending") for _, p in pending):
                if client.account_number() != secrets.kiwoom_expected_account:
                    raise RuntimeError("Account mismatch")
                coordinator = object.__new__(LiveTradingCoordinator)
                coordinator.store = store
                coordinator.broker = SimpleNamespace(client=client)
                coordinator.enrich_notion_sells()
        try:
            remaining = deliver(store, sync, SimpleNamespace(enrich_notion_sells=enrich), prefix)
        finally:
            if not us:
                sync_cash_once(root, store, sync, logger)
        sync_due(store, sync, client, secrets.kiwoom_expected_account, market)
        store.set_system_value(key + "pending", str(remaining))
        store.set_system_value(key + "error", "" if remaining == 0 else "PENDING_RETRY")
        if remaining == 0:
            store.set_system_value(key + "last_success", datetime.now(timezone.utc).isoformat())
        return remaining
    except Exception as error:
        store.set_system_value(key + "error", type(error).__name__)
        logger.error("Notion fill delivery retry: %s", type(error).__name__)
        return 1
    finally:
        store.set_system_value(key + "heartbeat", datetime.now(timezone.utc).isoformat())
        if client is not None:
            client.disconnect()
        store.close()


async def run_with_reporting(service_coroutine, root, market, logger, names=None):
    stop = asyncio.Event()

    async def worker():
        while not stop.is_set():
            try:
                pending = await asyncio.to_thread(run_once, root, market, logger, names)
            except Exception as error:
                logger.error("Notion worker retry: %s", type(error).__name__)
                pending = 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=60 if pending else 15)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(worker())
    try:
        return await service_coroutine
    finally:
        # Finish any in-flight page creation before a replacement worker starts.
        stop.set()
        await task
