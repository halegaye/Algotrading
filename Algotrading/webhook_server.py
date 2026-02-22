"""
Webhook Sunucusu — TradingView sinyallerini dinler.

ÇİFT SİNYAL FİLTRELEME MANTIĞI
─────────────────────────────────────────────────────────────────────────────
TradingView'den iki FARKLI alarm kaynağı (Source A ve Source B) gelir.
Her iki listede de ORTAK olan hisseler (intersection) alım listesine alınır.

Akış:
  1. İlk sinyal gelir (A veya B) → zamanlayıcı başlar (config.SIGNAL_WINDOW_SECONDS)
  2. Pencere içinde ikinci sinyal gelir → kesişim hesaplanır → queue'ya gönderilir
  3. Pencere dolarsa ve ikinci sinyal gelmezse → o gün işlem yapılmaz

TradingView Alert Message formatı:
  Kaynak A: {"secret": "...", "source": "A", "symbols": ["GARAN", "THYAO", ...]}
  Kaynak B: {"secret": "...", "source": "B", "symbols": ["GARAN", "ASELS", ...]}
─────────────────────────────────────────────────────────────────────────────
"""

import json
import threading
from datetime import datetime, date
from flask import Flask, request, jsonify

import config
from database import Signal


class SignalBuffer:
    """
    Aynı gün içinde gelen A ve B sinyallerini thread-safe biçimde saklar.
    Bekleme penceresi dolduğunda ya da her iki sinyal de geldiğinde
    kesişim hesaplanır ve callback tetiklenir.

    Durum geçişleri:
      IDLE → (ilk sinyal gelir) → WAITING → (ikinci gelir) → DONE
                                           → (süre dolar)  → EXPIRED
    """

    STATE_IDLE    = "idle"
    STATE_WAITING = "waiting"
    STATE_DONE    = "done"
    STATE_EXPIRED = "expired"

    def __init__(self, window_seconds: int, on_intersection_ready, log):
        self.window_seconds       = window_seconds
        self.on_intersection_ready = on_intersection_ready  # callback(symbols: list)
        self.log                  = log

        self._lock    = threading.Lock()
        self._state   = self.STATE_IDLE
        self._day     = None
        self._signals = {}   # {"A": [...], "B": [...]}
        self._timer   = None

    # ── Dışa açık metot ──────────────────────────────────────────────────

    def receive(self, source: str, symbols: list) -> dict:
        """
        Yeni sinyal geldiğinde çağrılır.
        Returns: Webhook yanıtına eklenecek tampon durum bilgisi.
        """
        today = date.today()

        with self._lock:
            # Gün değiştiyse sıfırla
            if self._day != today:
                self._reset_unlocked()
                self._day = today

            # Aynı kaynaktan tekrar gelirse güncelle
            if source in self._signals:
                self.log.log(
                    "WARNING", "SIGNAL_DUPLICATE",
                    f"Kaynak {source} bu gun zaten geldi, sembol listesi guncelleniyor.",
                    extra_data={"old": self._signals[source], "new": symbols}
                )

            self._signals[source] = symbols
            self.log.log(
                "INFO", "SIGNAL_BUFFER",
                f"Kaynak {source} alindi: {symbols} | "
                f"Mevcut: {list(self._signals.keys())} | Durum: {self._state}",
            )

            # Her iki kaynak da geldi mi?
            if config.SIGNAL_SOURCE_A in self._signals and \
               config.SIGNAL_SOURCE_B in self._signals:
                self._cancel_timer_unlocked()
                return self._compute_and_fire_unlocked()

            # İlk sinyal → bekleme penceresi başlat
            if self._state == self.STATE_IDLE:
                self._state = self.STATE_WAITING
                self._start_timer_unlocked()
                return {
                    "buffer_status": "waiting",
                    "message": (
                        f"Kaynak {source} alindi. "
                        f"Diger kaynak icin {self.window_seconds} saniye bekleniyor."
                    ),
                    "received_sources": list(self._signals.keys()),
                }

            return {
                "buffer_status": "waiting",
                "message":       f"Kaynak {source} guncellendi, digeri bekleniyor.",
                "received_sources": list(self._signals.keys()),
            }

    def status(self) -> dict:
        with self._lock:
            return {
                "state":            self._state,
                "day":              str(self._day),
                "received_sources": list(self._signals.keys()),
                "symbols_A":        self._signals.get(config.SIGNAL_SOURCE_A, []),
                "symbols_B":        self._signals.get(config.SIGNAL_SOURCE_B, []),
            }

    # ── İç metotlar (lock altında) ────────────────────────────────────────

    def _start_timer_unlocked(self):
        self._timer = threading.Timer(self.window_seconds, self._on_window_expired)
        self._timer.daemon = True
        self._timer.start()
        self.log.log("INFO", "SIGNAL_WINDOW_START",
                     f"Bekleme penceresi acildi: {self.window_seconds} saniye.")

    def _cancel_timer_unlocked(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _compute_and_fire_unlocked(self) -> dict:
        """
        Kesişim hesaplar, callback'i arka planda çalıştırır.
        Lock altında çağrılmalıdır.
        """
        list_a = set(self._signals.get(config.SIGNAL_SOURCE_A, []))
        list_b = set(self._signals.get(config.SIGNAL_SOURCE_B, []))

        # Sırayı Kaynak A'nın orijinal sırasına göre koru
        original_a   = self._signals.get(config.SIGNAL_SOURCE_A, [])
        intersection = [s for s in original_a if s in list_b]

        self._state = self.STATE_DONE

        self.log.log(
            "INFO", "SIGNAL_INTERSECTION",
            f"Kesisim: A={sorted(list_a)} n B={sorted(list_b)} = {intersection}",
            extra_data={
                "source_a":     sorted(list_a),
                "source_b":     sorted(list_b),
                "intersection": intersection,
            }
        )

        if not intersection:
            self.log.log(
                "WARNING", "SIGNAL_NO_INTERSECTION",
                "Iki listede ortak hisse bulunamadi. O gun islem yapilmayacak."
            )
            return {
                "buffer_status": "done",
                "intersection":  [],
                "message":       "Ortak hisse yok, islem yapilmayacak.",
            }

        threading.Thread(
            target=self.on_intersection_ready,
            args=(intersection,),
            daemon=True
        ).start()

        return {
            "buffer_status": "done",
            "intersection":  intersection,
            "message":       f"{len(intersection)} ortak hisse: {intersection}",
        }

    def _reset_unlocked(self):
        self._cancel_timer_unlocked()
        self._state   = self.STATE_IDLE
        self._signals = {}
        self._timer   = None

    def _on_window_expired(self):
        """Timer callback — lock dışında çalışır."""
        with self._lock:
            if self._state != self.STATE_WAITING:
                return
            missing = (
                config.SIGNAL_SOURCE_B
                if config.SIGNAL_SOURCE_A in self._signals
                else config.SIGNAL_SOURCE_A
            )
            self._state = self.STATE_EXPIRED
            self.log.log(
                "WARNING", "SIGNAL_WINDOW_EXPIRED",
                f"Bekleme penceresi doldu ({self.window_seconds} sn). "
                f"Kaynak '{missing}' gelmedi. O gun islem yapilmayacak.",
                extra_data={"received": list(self._signals.keys()), "missing": missing}
            )


# ─────────────────────────────────────────────────────────────────────────────

class WebhookServer:
    """
    Flask tabanlı webhook sunucusu.
    Gelen sinyalleri SignalBuffer'a iletir, kesişim hazır olunca queue'ya atar.
    """

    def __init__(self, signal_queue, Session, db_logger, host: str, port: int):
        self.signal_queue = signal_queue
        self.Session      = Session
        self.log          = db_logger
        self.host         = host
        self.port         = port
        self.app          = Flask(__name__)

        self.buffer = SignalBuffer(
            window_seconds        = config.SIGNAL_WINDOW_SECONDS,
            on_intersection_ready = self._push_intersection_to_queue,
            log                   = db_logger,
        )
        self._register_routes()

    def _push_intersection_to_queue(self, symbols: list):
        """SignalBuffer kesişimi hesapladığında bu metot arka planda çağrılır."""
        self.signal_queue.put({
            "received_at": datetime.utcnow().isoformat(),
            "symbols":     symbols,
            "source":      "intersection",
        })
        self.log.log("INFO", "SIGNAL_QUEUED",
                     f"Kesisim queue'ya eklendi: {symbols}",
                     extra_data={"symbols": symbols})

    def _register_routes(self):

        @self.app.route("/health", methods=["GET"])
        def health():
            return jsonify({
                "status":        "ok",
                "timestamp":     datetime.utcnow().isoformat(),
                "signal_buffer": self.buffer.status(),
            }), 200

        @self.app.route("/webhook", methods=["POST"])
        def webhook():
            return self._handle_webhook(request)

        @self.app.route("/portfolio", methods=["GET"])
        def portfolio_status():
            from database import PortfolioPosition, Trade
            session = self.Session()
            try:
                positions     = session.query(PortfolioPosition).filter_by(is_active=True).all()
                recent_trades = session.query(Trade).filter_by(is_closed=True)\
                                       .order_by(Trade.sell_date.desc()).limit(10).all()
                return jsonify({
                    "active_positions": [
                        {"symbol": p.symbol, "entry_price": p.entry_price,
                         "quantity": p.quantity, "take_profit": p.take_profit_price,
                         "stop_loss": p.stop_loss_price, "days_held": p.trading_days_held,
                         "entry_date": p.entry_date.isoformat() if p.entry_date else None}
                        for p in positions
                    ],
                    "recent_trades": [
                        {"symbol": t.symbol, "pnl_percent": t.pnl_percent,
                         "exit_reason": t.exit_reason,
                         "sell_date": t.sell_date.isoformat() if t.sell_date else None}
                        for t in recent_trades
                    ],
                    "signal_buffer": self.buffer.status(),
                }), 200
            finally:
                session.close()

        @self.app.route("/signal-status", methods=["GET"])
        def signal_status():
            """Anlık tampon durumu (izleme/debug için)."""
            return jsonify(self.buffer.status()), 200

    def _handle_webhook(self, req):
        try:
            data = req.get_json(silent=True)
            if not data:
                return jsonify({"error": "Invalid JSON"}), 400

            # Güvenlik
            if data.get("secret", "") != config.WEBHOOK_SECRET:
                self.log.log("WARNING", "WEBHOOK_AUTH_FAIL", "Gecersiz secret.",
                             extra_data={"ip": req.remote_addr})
                return jsonify({"error": "Unauthorized"}), 401

            # Kaynak doğrulaması
            source = str(data.get("source", "")).strip().upper()
            valid  = {config.SIGNAL_SOURCE_A.upper(), config.SIGNAL_SOURCE_B.upper()}
            if source not in valid:
                self.log.log("WARNING", "WEBHOOK_BAD_SOURCE",
                             f"Gecersiz source: '{source}'. Beklenen: {valid}")
                return jsonify({"error": "Invalid source", "expected": list(valid)}), 400

            # Sembol listesi
            symbols = data.get("symbols", [])
            if not isinstance(symbols, list) or not symbols:
                return jsonify({"error": "symbols field required"}), 400
            symbols = [s.strip().upper() for s in symbols if isinstance(s, str) and s.strip()]

            self.log.log("INFO", "WEBHOOK_RECEIVED",
                         f"Kaynak={source} | Semboller={symbols}",
                         extra_data={"source": source, "symbols": symbols})

            self._save_signal(data, symbols)
            buffer_result = self.buffer.receive(source, symbols)

            return jsonify({
                "status":           "accepted",
                "source":           source,
                "symbols_received": symbols,
                "timestamp":        datetime.utcnow().isoformat(),
                **buffer_result,
            }), 200

        except Exception as e:
            self.log.log("ERROR", "WEBHOOK_ERROR", f"Webhook hatasi: {e}")
            return jsonify({"error": "Internal server error"}), 500

    def _save_signal(self, raw_data: dict, symbols: list):
        session = self.Session()
        try:
            session.add(Signal(
                received_at  = datetime.utcnow(),
                raw_payload  = json.dumps(raw_data, ensure_ascii=False),
                symbols      = ",".join(symbols),
                processed    = False,
            ))
            session.commit()
        except Exception as e:
            session.rollback()
            self.log.log("ERROR", "SIGNAL_SAVE_ERROR", f"Kayit hatasi: {e}")
        finally:
            session.close()

    def mark_signal_processed(self, symbols_bought: list):
        session = self.Session()
        try:
            signal = session.query(Signal).order_by(Signal.id.desc()).first()
            if signal:
                signal.processed      = True
                signal.symbols_bought = ",".join(symbols_bought)
                session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    def run(self):
        self.log.log("INFO", "WEBHOOK_START",
                     f"Webhook sunucusu baslatildi: {self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
