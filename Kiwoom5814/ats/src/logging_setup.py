import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from src.config import Secrets


class Redact(logging.Filter):
    def __init__(self, secrets: Secrets):
        super().__init__()
        self.values = sorted((v for v in vars(secrets).values() if v), key=len, reverse=True)

    def filter(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        for value in self.values:
            message = message.replace(value, "[REDACTED]")
        record.msg, record.args = message, ()
        return True


def configure_logging(path: Path, secrets: Secrets):
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_trader")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    for handler in (RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
                    logging.StreamHandler()):
        handler.addFilter(Redact(secrets))
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger
