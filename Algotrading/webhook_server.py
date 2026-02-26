"""
Webhook Sunucusu — TradingView sinyallerini dinler + Dashboard API'si.

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

DASHBOARD API ENDPOINTLERİ
─────────────────────────────────────────────────────────────────────────────
  GET /api/trades   → Record<string, Trade[]>  (Dashboard MOCK_TRADES formatı)
  GET /api/equity   → {month, pnlPct}[]        (Dashboard MOCK_EQUITY_SERIES formatı)
  GET /             → React build dist/index.html (otomatik açılır)

CORS
─────────────────────────────────────────────────────────────────────────────
  Flask-CORS bağımlılığı olmadan manuel after_request hook ile eklenir.
  Geliştirme: * (tüm originler)
  Production: config.CORS_ORIGIN ile kısıtlanabilir.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, date
from flask import Flask, request, jsonify, send_from_directory

import config
from database import Signal, Trade


# ─────────────────────────────────────────────────────────────────────────────
# CORS — Flask-CORS olmadan manuel
# ─────────────────────────────────────────────────────────────────────────────

def _add_cors_headers(response):
    """Tüm yanıtlara CORS başlıkları ekler."""
    origin = getattr(config, "CORS_ORIGIN", "*")
    response.headers["Access-Control-Allow-Origin"]  = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL BUFFER — Çift kaynak bekleme penceresi
# ─────────────────────────────────────────────────────────────────────────────

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
        self.window_seconds        = window_seconds
        self.on_intersection_ready = on_intersection_ready
        self.log                   = log

        self._lock    = threading.Lock()
        self._state   = self.STATE_IDLE
        self._day     = None
        self._signals = {}
        self._timer   = None

    def receive(self, source: str, symbols: list) -> dict:
        today = date.today()
        with self._lock:
            if self._day != today:
                self._reset_unlocked()
                self._day = today

            if source in self._signals:
                self.log.log("WARNING", "SIGNAL_DUPLICATE",
                             f"Kaynak {source} bu gun zaten geldi, guncelleniyor.",
                             extra_data={"old": self._signals[source], "new": symbols})

            self._signals[source] = symbols
            self.log.log("INFO", "SIGNAL_BUFFER",
                         f"Kaynak {source} alindi: {symbols} | "
                         f"Mevcut: {list(self._signals.keys())} | Durum: {self._state}")

            if config.SIGNAL_SOURCE_A in self._signals and \
               config.SIGNAL_SOURCE_B in self._signals:
                self._cancel_timer_unlocked()
                return self._compute_and_fire_unlocked()

            if self._state == self.STATE_IDLE:
                self._state = self.STATE_WAITING
                self._start_timer_unlocked()
                return {
                    "buffer_status": "waiting",
                    "message": (f"Kaynak {source} alindi. "
                                f"Diger kaynak icin {self.window_seconds} saniye bekleniyor."),
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
        list_a = set(self._signals.get(config.SIGNAL_SOURCE_A, []))
        list_b = set(self._signals.get(config.SIGNAL_SOURCE_B, []))
        original_a   = self._signals.get(config.SIGNAL_SOURCE_A, [])
        intersection = [s for s in original_a if s in list_b]
        self._state  = self.STATE_DONE

        self.log.log("INFO", "SIGNAL_INTERSECTION",
                     f"Kesisim: A={sorted(list_a)} ∩ B={sorted(list_b)} = {intersection}",
                     extra_data={"source_a": sorted(list_a), "source_b": sorted(list_b),
                                 "intersection": intersection})

        if not intersection:
            self.log.log("WARNING", "SIGNAL_NO_INTERSECTION",
                         "Iki listede ortak hisse bulunamadi. O gun islem yapilmayacak.")
            return {"buffer_status": "done", "intersection": [],
                    "message": "Ortak hisse yok, islem yapilmayacak."}

        threading.Thread(target=self.on_intersection_ready,
                         args=(intersection,), daemon=True).start()

        return {"buffer_status": "done", "intersection": intersection,
                "message": f"{len(intersection)} ortak hisse: {intersection}"}

    def _reset_unlocked(self):
        self._cancel_timer_unlocked()
        self._state   = self.STATE_IDLE
        self._signals = {}
        self._timer   = None

    def _on_window_expired(self):
        with self._lock:
            if self._state != self.STATE_WAITING:
                return
            missing = (config.SIGNAL_SOURCE_B
                       if config.SIGNAL_SOURCE_A in self._signals
                       else config.SIGNAL_SOURCE_A)
            self._state = self.STATE_EXPIRED
            self.log.log("WARNING", "SIGNAL_WINDOW_EXPIRED",
                         f"Bekleme penceresi doldu ({self.window_seconds} sn). "
                         f"Kaynak '{missing}' gelmedi. O gun islem yapilmayacak.",
                         extra_data={"received": list(self._signals.keys()),
                                     "missing": missing})


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK + DASHBOARD API SUNUCUSU
# ─────────────────────────────────────────────────────────────────────────────

# React build çıktısının yolu (dist/index.html)
# Bot dizinine göre: C:\matriks_bot\dashboard\dist
_DIST_DIR = os.path.join(os.path.dirname(__file__), "dashboard", "dist")


class WebhookServer:
    """
    Flask tabanlı sunucu:
      • TradingView webhook sinyallerini alır (çift kaynak + kesişim)
      • Dashboard için /api/trades ve /api/equity endpoint'lerini sunar
      • React build'i statik dosya olarak servis eder
    """

    def __init__(self, signal_queue, Session, db_logger, host: str, port: int):
        self.signal_queue = signal_queue
        self.Session      = Session
        self.log          = db_logger
        self.host         = host
        self.port         = port
        self.app          = Flask(__name__,
                                  static_folder=_DIST_DIR,
                                  static_url_path="")

        self.buffer = SignalBuffer(
            window_seconds        = config.SIGNAL_WINDOW_SECONDS,
            on_intersection_ready = self._push_intersection_to_queue,
            log                   = db_logger,
        )

        # CORS tüm yanıtlara
        self.app.after_request(_add_cors_headers)

        # Preflight OPTIONS istekleri
        @self.app.route("/<path:path>", methods=["OPTIONS"])
        @self.app.route("/", methods=["OPTIONS"])
        def _options_handler(*args, **kwargs):
            from flask import Response
            return Response(status=204)

        self._register_routes()

    # ── Sinyal queue'ya at ────────────────────────────────────────────────

    def _push_intersection_to_queue(self, symbols: list):
        self.signal_queue.put({
            "received_at": datetime.utcnow().isoformat(),
            "symbols":     symbols,
            "source":      "intersection",
        })
        self.log.log("INFO", "SIGNAL_QUEUED",
                     f"Kesisim queue'ya eklendi: {symbols}",
                     extra_data={"symbols": symbols})

    # ── Flask route'ları ──────────────────────────────────────────────────

    def _register_routes(self):

        # ── Sağlık kontrolü ──────────────────────────────────────────────
        @self.app.route("/health", methods=["GET"])
        def health():
            return jsonify({
                "status":        "ok",
                "timestamp":     datetime.utcnow().isoformat(),
                "signal_buffer": self.buffer.status(),
            }), 200

        # ── TradingView webhook ───────────────────────────────────────────
        @self.app.route("/webhook", methods=["POST"])
        def webhook():
            return self._handle_webhook(request)

        # ── Sinyal tamponu durumu ─────────────────────────────────────────
        @self.app.route("/signal-status", methods=["GET"])
        def signal_status():
            return jsonify(self.buffer.status()), 200

        # ─────────────────────────────────────────────────────────────────
        # DASHBOARD API — /api/trades
        # ─────────────────────────────────────────────────────────────────
        @self.app.route("/api/trades", methods=["GET"])
        def api_trades():
            """
            Kapalı işlemleri satış tarihine göre gruplar.

            Query params:
              month=YYYY-MM  → sadece o ayın işlemleri (opsiyonel)

            Response (Dashboard MOCK_TRADES formatı):
            {
              "2026-01-05": [
                {
                  "symbol":    "THYAO",
                  "buyAt":     "2026-01-04T10:12",
                  "sellAt":    "2026-01-05T11:05",
                  "buyPrice":  245.5,
                  "sellPrice": 251.0,
                  "pnlPct":    2.24,
                  "exitReason": "take_profit"
                },
                ...
              ],
              "2026-01-06": [...]
            }
            """
            month_filter = request.args.get("month")  # "2026-01"

            session = self.Session()
            try:
                query = session.query(Trade).filter(Trade.is_closed == True)

                if month_filter:
                    try:
                        y, m = month_filter.split("-")
                        from sqlalchemy import extract
                        query = query.filter(
                            extract("year",  Trade.sell_date) == int(y),
                            extract("month", Trade.sell_date) == int(m),
                        )
                    except Exception:
                        return jsonify({"error": "month parametresi YYYY-MM formatında olmalı"}), 400

                trades = query.order_by(Trade.sell_date).all()

                # Satış gününe göre grupla
                grouped: dict[str, list] = defaultdict(list)
                for t in trades:
                    key = t.sell_date_key()
                    if key:
                        grouped[key].append(t.to_dashboard_trade())

                return jsonify(dict(grouped)), 200

            except Exception as e:
                self.log.log("ERROR", "API_TRADES_ERROR", f"/api/trades hatası: {e}")
                return jsonify({"error": "Internal server error"}), 500
            finally:
                session.close()

        # ─────────────────────────────────────────────────────────────────
        # DASHBOARD API — /api/equity
        # ─────────────────────────────────────────────────────────────────
        @self.app.route("/api/equity", methods=["GET"])
        def api_equity():
            """
            Aylık PnL toplamını ve kümülatif equity eğrisini döndürür.

            Response (Dashboard MOCK_EQUITY_SERIES formatı):
            [
              {"month": "2025-08", "pnlPct": 6.2},
              {"month": "2025-09", "pnlPct": -2.1},
              ...
            ]

            Not: pnlPct, o aydaki tüm işlemlerin pnl_percent ortalamasıdır.
            Gerçek portföy etkisi için ağırlıklı hesaplama ileride eklenebilir.
            """
            session = self.Session()
            try:
                from sqlalchemy import extract, func

                rows = (
                    session.query(
                        extract("year",  Trade.sell_date).label("year"),
                        extract("month", Trade.sell_date).label("month"),
                        func.avg(Trade.pnl_percent).label("avg_pnl"),
                        func.count(Trade.id).label("trade_count"),
                    )
                    .filter(Trade.is_closed == True, Trade.pnl_percent != None)
                    .group_by("year", "month")
                    .order_by("year", "month")
                    .all()
                )

                result = []
                for row in rows:
                    y   = int(row.year)
                    mo  = int(row.month)
                    key = f"{y:04d}-{mo:02d}"
                    result.append({
                        "month":       key,
                        "pnlPct":      round(float(row.avg_pnl), 4),
                        "tradeCount":  int(row.trade_count),
                    })

                return jsonify(result), 200

            except Exception as e:
                self.log.log("ERROR", "API_EQUITY_ERROR", f"/api/equity hatası: {e}")
                return jsonify({"error": "Internal server error"}), 500
            finally:
                session.close()

        # ─────────────────────────────────────────────────────────────────
        # MEVCUT PORTFÖY ENDPOINTİ (korundu)
        # ─────────────────────────────────────────────────────────────────
        @self.app.route("/portfolio", methods=["GET"])
        def portfolio_status():
            from database import PortfolioPosition
            session = self.Session()
            try:
                positions     = session.query(PortfolioPosition).filter_by(is_active=True).all()
                recent_trades = session.query(Trade).filter_by(is_closed=True)\
                                       .order_by(Trade.sell_date.desc()).limit(10).all()
                return jsonify({
                    "active_positions": [
                        {"symbol":     p.symbol,
                         "entry_price": p.entry_price,
                         "quantity":    p.quantity,
                         "take_profit": p.take_profit_price,
                         "stop_loss":   p.stop_loss_price,
                         "days_held":   p.trading_days_held,
                         "entry_date":  p.entry_date.isoformat() if p.entry_date else None}
                        for p in positions
                    ],
                    "recent_trades": [t.to_dashboard_trade() for t in recent_trades],
                    "signal_buffer": self.buffer.status(),
                }), 200
            finally:
                session.close()

        # ─────────────────────────────────────────────────────────────────
        # REACT DASHBOARD — Statik dosya servisi
        # ─────────────────────────────────────────────────────────────────
        @self.app.route("/", methods=["GET"])
        def serve_dashboard():
            """React build dist/index.html'i döndürür."""
            if os.path.isdir(_DIST_DIR) and os.path.isfile(
                    os.path.join(_DIST_DIR, "index.html")):
                return send_from_directory(_DIST_DIR, "index.html")
            # dist henüz build edilmemişse bilgilendirici mesaj
            return jsonify({
                "status":  "dashboard_not_built",
                "message": (
                    "React dashboard henüz build edilmedi. "
                    "C:\\matriks_bot\\dashboard dizininde "
                    "'npm run build' komutunu çalıştır."
                ),
                "api_endpoints": ["/api/trades", "/api/equity", "/health"],
            }), 200

        @self.app.route("/<path:filename>", methods=["GET"])
        def serve_static(filename):
            """React build'in JS/CSS/asset dosyalarını servis eder."""
            if os.path.isdir(_DIST_DIR):
                file_path = os.path.join(_DIST_DIR, filename)
                if os.path.isfile(file_path):
                    return send_from_directory(_DIST_DIR, filename)
            # SPA fallback: bilinmeyen yolları index.html'e yönlendir
            if os.path.isdir(_DIST_DIR) and os.path.isfile(
                    os.path.join(_DIST_DIR, "index.html")):
                return send_from_directory(_DIST_DIR, "index.html")
            return jsonify({"error": "Not found"}), 404

    # ── Webhook işleme ────────────────────────────────────────────────────

    def _handle_webhook(self, req):
        try:
            data = req.get_json(silent=True)
            if not data:
                return jsonify({"error": "Invalid JSON"}), 400

            if data.get("secret", "") != config.WEBHOOK_SECRET:
                self.log.log("WARNING", "WEBHOOK_AUTH_FAIL", "Gecersiz secret.",
                             extra_data={"ip": req.remote_addr})
                return jsonify({"error": "Unauthorized"}), 401

            source = str(data.get("source", "")).strip().upper()
            valid  = {config.SIGNAL_SOURCE_A.upper(), config.SIGNAL_SOURCE_B.upper()}
            if source not in valid:
                self.log.log("WARNING", "WEBHOOK_BAD_SOURCE",
                             f"Gecersiz source: '{source}'. Beklenen: {valid}")
                return jsonify({"error": "Invalid source", "expected": list(valid)}), 400

            symbols = data.get("symbols", [])
            if not isinstance(symbols, list) or not symbols:
                return jsonify({"error": "symbols field required"}), 400
            symbols = [s.strip().upper() for s in symbols
                       if isinstance(s, str) and s.strip()]

            self.log.log("INFO", "WEBHOOK_RECEIVED",
                         f"Kaynak={source} | Semboller={symbols}",
                         extra_data={"source": source, "symbols": symbols})

            self._save_signal(data, symbols)
            buffer_result = self.buffer.receive(source, symbols)

            return jsonify({
                "status":            "accepted",
                "source":            source,
                "symbols_received":  symbols,
                "timestamp":         datetime.utcnow().isoformat(),
                **buffer_result,
            }), 200

        except Exception as e:
            self.log.log("ERROR", "WEBHOOK_ERROR", f"Webhook hatasi: {e}")
            return jsonify({"error": "Internal server error"}), 500

    def _save_signal(self, raw_data: dict, symbols: list):
        session = self.Session()
        try:
            session.add(Signal(
                received_at = datetime.utcnow(),
                raw_payload = json.dumps(raw_data, ensure_ascii=False),
                symbols     = ",".join(symbols),
                processed   = False,
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
