"""
Webhook Sunucusu - TradingView sinyallerini dinler.
Flask ile /webhook endpoint'i üzerinden gelen JSON payload'ı işler.
"""

import json
import hashlib
import hmac
from datetime import datetime
from flask import Flask, request, jsonify

import config
from database import Signal


class WebhookServer:
    """
    TradingView'den gelen webhook sinyallerini alır ve
    signal_queue'ya (threading.Queue) iletir.

    Beklenen TradingView payload formatı:
    {
        "secret": "your_secret",
        "symbols": ["GARAN", "THYAO", "ASELS", "EREGL", "KCHOL"]
    }
    """

    def __init__(self, signal_queue, Session, db_logger, host: str, port: int):
        self.signal_queue = signal_queue
        self.Session = Session
        self.log = db_logger
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        self._register_routes()

    def _register_routes(self):
        """Flask route'larını tanımlar."""

        @self.app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()}), 200

        @self.app.route("/webhook", methods=["POST"])
        def webhook():
            return self._handle_webhook(request)

        @self.app.route("/portfolio", methods=["GET"])
        def portfolio_status():
            """Basit portföy durumu endpoint'i (dashboard için)."""
            from database import PortfolioPosition, Trade
            session = self.Session()
            try:
                positions = session.query(PortfolioPosition).filter_by(is_active=True).all()
                recent_trades = session.query(Trade).filter_by(is_closed=True)\
                    .order_by(Trade.sell_date.desc()).limit(10).all()

                return jsonify({
                    "active_positions": [
                        {
                            "symbol": p.symbol,
                            "entry_price": p.entry_price,
                            "quantity": p.quantity,
                            "take_profit": p.take_profit_price,
                            "stop_loss": p.stop_loss_price,
                            "days_held": p.trading_days_held,
                            "entry_date": p.entry_date.isoformat() if p.entry_date else None
                        }
                        for p in positions
                    ],
                    "recent_trades": [
                        {
                            "symbol": t.symbol,
                            "pnl_percent": t.pnl_percent,
                            "exit_reason": t.exit_reason,
                            "sell_date": t.sell_date.isoformat() if t.sell_date else None
                        }
                        for t in recent_trades
                    ]
                }), 200
            finally:
                session.close()

    def _handle_webhook(self, req):
        """Gelen webhook isteğini doğrular ve işler."""
        try:
            data = req.get_json(silent=True)
            if not data:
                self.log.log("WARNING", "WEBHOOK_BAD_REQUEST", "Geçersiz JSON payload.")
                return jsonify({"error": "Invalid JSON"}), 400

            # Güvenlik doğrulaması
            incoming_secret = data.get("secret", "")
            if incoming_secret != config.WEBHOOK_SECRET:
                self.log.log("WARNING", "WEBHOOK_AUTH_FAIL",
                             "Geçersiz webhook secret.",
                             extra_data={"ip": req.remote_addr})
                return jsonify({"error": "Unauthorized"}), 401

            symbols = data.get("symbols", [])
            if not isinstance(symbols, list) or len(symbols) == 0:
                self.log.log("WARNING", "WEBHOOK_NO_SYMBOLS", "Sembol listesi boş veya geçersiz.")
                return jsonify({"error": "symbols field required"}), 400

            # Sembolleri büyük harfe çevir ve temizle
            symbols = [s.strip().upper() for s in symbols if isinstance(s, str)]

            self.log.log("INFO", "WEBHOOK_RECEIVED",
                         f"Sinyal alındı: {symbols}",
                         extra_data={"symbols": symbols, "count": len(symbols)})

            # Veritabanına kaydet
            self._save_signal(data, symbols)

            # Queue'ya ilet (ana thread işleyecek)
            self.signal_queue.put({
                "received_at": datetime.utcnow().isoformat(),
                "symbols": symbols
            })

            return jsonify({
                "status": "accepted",
                "symbols_received": symbols,
                "timestamp": datetime.utcnow().isoformat()
            }), 200

        except Exception as e:
            self.log.log("ERROR", "WEBHOOK_ERROR", f"Webhook işleme hatası: {e}")
            return jsonify({"error": "Internal server error"}), 500

    def _save_signal(self, raw_data: dict, symbols: list):
        """Sinyali veritabanına kaydeder."""
        session = self.Session()
        try:
            signal = Signal(
                received_at=datetime.utcnow(),
                raw_payload=json.dumps(raw_data, ensure_ascii=False),
                symbols=",".join(symbols),
                processed=False
            )
            session.add(signal)
            session.commit()
        except Exception as e:
            session.rollback()
            self.log.log("ERROR", "SIGNAL_SAVE_ERROR", f"Sinyal kayıt hatası: {e}")
        finally:
            session.close()

    def mark_signal_processed(self, symbols_bought: list):
        """En son sinyali işlenmiş olarak işaretler."""
        session = self.Session()
        try:
            signal = session.query(Signal).order_by(Signal.id.desc()).first()
            if signal:
                signal.processed = True
                signal.symbols_bought = ",".join(symbols_bought)
                session.commit()
        except Exception as e:
            session.rollback()
        finally:
            session.close()

    def run(self):
        """Flask sunucusunu başlatır."""
        self.log.log("INFO", "WEBHOOK_START",
                     f"Webhook sunucusu başlatıldı: {self.host}:{self.port}")
        self.app.run(
            host=self.host,
            port=self.port,
            debug=False,
            use_reloader=False
        )
