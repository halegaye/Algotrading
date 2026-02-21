"""
Matriks IQ Trading Bot - Ana Başlangıç Noktası
Tüm bileşenleri başlatır ve koordine eder.

Kullanım:
    python main.py                    # Normal başlatma
    python main.py --trigger morning  # Manuel görev tetikleme
    python main.py --test-signal      # Test sinyali gönder
"""

import sys
import signal
import queue
import threading
import argparse
import time
from datetime import datetime

import config
from database import init_db
from logger import setup_logger, DBLogger
from matriks_client import MatriksAPIClient
from portfolio_manager import PortfolioManager
from webhook_server import WebhookServer
from scheduler import TaskScheduler


def parse_args():
    parser = argparse.ArgumentParser(description="Matriks IQ Trading Bot")
    parser.add_argument(
        "--trigger",
        choices=["morning_refresh", "process_signals", "send_buy_orders", "eod_close"],
        help="Manuel görev tetikle"
    )
    parser.add_argument(
        "--test-signal",
        nargs="+",
        metavar="SYMBOL",
        help="Test sinyali gönder (örn: --test-signal GARAN THYAO)"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # ─────────────────────────────────────────
    # 1. LOGGER KURULUMU
    # ─────────────────────────────────────────
    logger = setup_logger(
        log_level=config.LOG_LEVEL,
        log_file=config.LOG_FILE,
        max_bytes=config.LOG_MAX_BYTES,
        backup_count=config.LOG_BACKUP_COUNT
    )
    logger.info("=" * 60)
    logger.info("  Matriks IQ Trading Bot Başlatılıyor")
    logger.info(f"  Tarih: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ─────────────────────────────────────────
    # 2. VERİTABANI KURULUMU
    # ─────────────────────────────────────────
    engine, Session = init_db(config.DATABASE_URL)
    db_logger = DBLogger(Session, logger)
    db_logger.log("INFO", "BOT_START", "Veritabanı bağlantısı kuruldu.")

    # ─────────────────────────────────────────
    # 3. MATRİKS IQ API İSTEMCİSİ
    # ─────────────────────────────────────────
    client = MatriksAPIClient(
        host=config.MATRIKS_HOST,
        port=config.MATRIKS_PORT,
        brokage_id=config.BROKAGE_ID,
        account_id=config.ACCOUNT_ID,
        exchange_id=config.EXCHANGE_ID,
        reconnect_delay=config.RECONNECT_DELAY,
        max_reconnect_attempts=config.MAX_RECONNECT_ATTEMPTS,
        keepalive_interval=config.KEEPALIVE_INTERVAL,
        db_logger=db_logger
    )

    # Bağlantı kur
    if not client.connect():
        db_logger.log("ERROR", "API_CONNECT_FAIL",
                      "MatriksIQ'ya bağlanılamadı. Bot durduruluyor.")
        sys.exit(1)

    # ─────────────────────────────────────────
    # 4. PORTFÖY YÖNETİCİSİ
    # ─────────────────────────────────────────
    portfolio_manager = PortfolioManager(Session, client, db_logger)
    db_logger.log("INFO", "PM_INIT",
                  f"Portföy yöneticisi hazır. "
                  f"Aktif pozisyon: {portfolio_manager.get_active_position_count()}/{config.MAX_POSITIONS}")

    # ─────────────────────────────────────────
    # 5. SİNYAL QUEUE'SU
    # ─────────────────────────────────────────
    signal_queue = queue.Queue()

    # ─────────────────────────────────────────
    # 6. WEBHOOK SUNUCUSU
    # ─────────────────────────────────────────
    webhook = WebhookServer(
        signal_queue=signal_queue,
        Session=Session,
        db_logger=db_logger,
        host=config.WEBHOOK_HOST,
        port=config.WEBHOOK_PORT
    )

    webhook_thread = threading.Thread(
        target=webhook.run,
        daemon=True,
        name="WebhookServer"
    )
    webhook_thread.start()

    # ─────────────────────────────────────────
    # 7. GÖREV ZAMANLAYICI
    # ─────────────────────────────────────────
    scheduler = TaskScheduler(
        portfolio_manager=portfolio_manager,
        matriks_client=client,
        signal_queue=signal_queue,
        db_logger=db_logger,
        webhook_server=webhook
    )
    scheduler.start()

    db_logger.log("INFO", "BOT_READY",
                  f"Bot hazır. Webhook: {config.WEBHOOK_HOST}:{config.WEBHOOK_PORT} | "
                  f"MatriksIQ: {config.MATRIKS_HOST}:{config.MATRIKS_PORT}")

    # ─────────────────────────────────────────
    # 8. MANUEL GÖREV / TEST MOD
    # ─────────────────────────────────────────
    if args.trigger:
        db_logger.log("INFO", "MANUAL_MODE", f"Manuel tetikleme modu: {args.trigger}")
        time.sleep(2)  # API yanıtı için bekle
        scheduler.manual_trigger(args.trigger)
        time.sleep(3)
        client.disconnect()
        scheduler.stop()
        sys.exit(0)

    if args.test_signal:
        db_logger.log("INFO", "TEST_MODE",
                      f"Test sinyali: {args.test_signal}")
        signal_queue.put({
            "received_at": datetime.utcnow().isoformat(),
            "symbols": args.test_signal
        })
        db_logger.log("INFO", "TEST_MODE",
                      "Test sinyali queue'ya eklendi. "
                      "Gerçek emir için 17:40'ı bekleyin ya da "
                      "'python main.py --trigger send_buy_orders' komutunu çalıştırın.")

    # ─────────────────────────────────────────
    # 9. GRACEFUL SHUTDOWN
    # ─────────────────────────────────────────
    shutdown_event = threading.Event()

    def handle_shutdown(signum, frame):
        db_logger.log("INFO", "BOT_SHUTDOWN", "Kapatma sinyali alındı. Bot durduruluyor...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Ana döngü - bot çalışmaya devam eder
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    finally:
        db_logger.log("INFO", "BOT_STOP", "Bot kapatılıyor.")
        scheduler.stop()
        client.disconnect()
        db_logger.log("INFO", "BOT_STOP", "Bot başarıyla durduruldu.")


if __name__ == "__main__":
    main()
