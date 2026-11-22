import argparse
from datetime import date
from pathlib import Path
from uuid import uuid4
from src.config import load_settings, load_secrets, kiwoom_secrets
from src.database.store import Store
from src.logging_setup import configure_logging
from src.broker.kiwoom import KiwoomClient
from src.broker.http import KiwoomError


def main():
    parser = argparse.ArgumentParser(description="StockTrader offline foundation checks")
    parser.add_argument("command", choices=["init-db", "check", "stop", "kiwoom-auth-check",
                                            "watchlist-check", "watchlist-groups-check", "account-check",
                                            "paper-status", "resume-paper"])
    parser.add_argument("--market-data-check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    settings, secrets = load_settings(root), load_secrets(root)
    logger = configure_logging(root / settings.log_path, secrets)
    store = Store(root / settings.database_path)
    try:
        store.migrate()
        if args.command == "stop":
            store.set_control("STOP")
        if args.command == "resume-paper":
            store.set_control("RUNNING")
        if args.command == "paper-status":
            counts = {table: store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("orders", "fills", "positions")}
            logger.info("Paper status; control=%s; orders=%d; fills=%d; positions=%d",
                        store.control(), counts["orders"], counts["fills"], counts["positions"])
        if args.command in {"kiwoom-auth-check", "watchlist-check", "watchlist-groups-check", "account-check"}:
            client = KiwoomClient(kiwoom_secrets(secrets, settings.kiwoom_environment),
                                  environment=settings.kiwoom_environment)
            try:
                client.connect()
                logger.info("Kiwoom authentication OK; order execution unavailable")
                if args.command == "watchlist-groups-check":
                    groups = client.watchlist_groups()
                    target = next((group for group in groups if group.name == settings.watchlist_group_name), None)
                    target_items = client.watchlist_group(target.code) if target else ()
                    logger.info("Kiwoom watchlist read OK; groups=%d; target_found=%s; target_items=%d",
                                len(groups), target is not None, len(target_items))
                if args.command == "watchlist-check":
                    groups = client.watchlist_groups()
                    symbols = client.synchronized_watchlist(settings.max_watchlist,
                                                            settings.watchlist_group_name, groups)
                    logger.info("Kiwoom watchlist read OK; groups=%d; unique_symbols=%d", len(groups), len(symbols))
                if args.command == "account-check":
                    account = client.account_number()
                    overview = client.account_overview()
                    holdings = client.holdings()
                    orders = client.open_orders()
                    logger.info("Kiwoom account read OK; account_format=%s; cash_fields_valid=%s; holdings=%d; open_orders=%d",
                                account.isdigit(), overview.cash >= 0 and overview.orderable_cash >= 0,
                                len(holdings), len(orders))
                if args.market_data_check:
                    groups = client.watchlist_groups()
                    symbols = client.synchronized_watchlist(settings.max_watchlist,
                                                            settings.watchlist_group_name, groups)
                    for symbol in symbols:
                        quote = client.quote(symbol)
                        book = client.order_book(symbol)
                        base_date = date.today().strftime("%Y%m%d")
                        daily = client.candles(symbol, "daily", base_date)
                        weekly = client.candles(symbol, "weekly", base_date)
                        logger.info("Market data read OK; symbol_valid=%s; price_valid=%s; ask_levels=%d; bid_levels=%d; daily=%d; weekly=%d",
                                    quote.symbol == symbol, quote.current_price >= 0,
                                    len(book.asks), len(book.bids), len(daily), len(weekly))
            except KiwoomError as error:
                logger.error("%s", error)
                raise SystemExit(1) from None
            finally:
                client.disconnect()
        store.record(str(uuid4()), "system", {"command": args.command, "mode": "paper"})
        logger.info("Offline check OK; broker=paper; control=%s; execution unavailable", store.control())
    finally:
        store.close()


if __name__ == "__main__":
    main()
