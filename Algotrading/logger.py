"""
Loglama yöneticisi - Hem dosyaya hem konsola hem de veritabanına log yazar.
"""

import logging
import os
import json
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Optional


def setup_logger(log_level: str, log_file: str, max_bytes: int, backup_count: int) -> logging.Logger:
    """
    Ana logger'ı yapılandırır.
    """
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger("trading_bot")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Konsol handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Dosya handler (döngüsel)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


class DBLogger:
    """
    Kritik olayları veritabanına da kaydeder.
    """
    def __init__(self, Session, logger: logging.Logger):
        self.Session = Session
        self.logger = logger

    def log(
        self,
        level: str,
        event: str,
        message: str,
        symbol: Optional[str] = None,
        extra_data: Optional[dict] = None
    ):
        """
        Hem Python logger'a hem veritabanına yazar.
        """
        from database import BotLog

        log_fn = getattr(self.logger, level.lower(), self.logger.info)
        log_msg = f"[{event}] {message}"
        if symbol:
            log_msg = f"[{event}][{symbol}] {message}"
        log_fn(log_msg)

        try:
            session = self.Session()
            entry = BotLog(
                timestamp=datetime.utcnow(),
                level=level.upper(),
                event=event,
                message=message,
                symbol=symbol,
                extra_data=json.dumps(extra_data) if extra_data else None
            )
            session.add(entry)
            session.commit()
        except Exception as e:
            self.logger.error(f"[DB_LOG_ERROR] Veritabanına log yazılamadı: {e}")
        finally:
            session.close()
