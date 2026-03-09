"""
Webhook Sunucusu

ENDPOINT'LER:
  POST /webhook/signalA   → Kaynak A sinyali (TradingView İndikatör 1)
  POST /webhook/signalB   → Kaynak B sinyali (TradingView İndikatör 2)
  GET  /signal-status     → Anlık tampon durumu
  GET  /health            → Bot sağlık kontrolü
  GET  /api/trades        → Dashboard: kapalı işlemler + açık MTM
  GET  /api/equity        → Dashboard: aylık PnL
  GET  /portfolio         → Açık pozisyon özeti
  GET  /                  → React dashboard (dist/index.html)

SINYAL TOPLAMA PENCERESİ:
  • Her gün 17:30'da scheduler tarafından açılır
  • 17:35'e kadar A ve/veya B sinyalleri toplanır
  • 17:35'te kapanır → signal_engine.decide_trade_list() çağrılır
  • Sonuç signal_queue'ya atılır → 17:40'ta alım emirleri gönderilir

ÜÇ DURUM:
  Durum 1 — Hem A hem B geldi  → A ∩ B kesişimi filtreden geçirilir
  Durum 2 — Sadece A veya B   → Gelen listenin tamamı filtreden geçirilir
  Durum 3 — Hiç gelmedi       → Log uyarısı, o gün işlem yok
"""

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, date, time as dtime
from flask import Flask, request, jsonify, send_from_directory

import config
from database import Signal, Trade, PortfolioPosition
from signal_engine import parse_raw_signal, decide_trade_list


# ─────────────────────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────────────────────

def _add_cors_headers(response):
    origin = getattr(config, "CORS_ORIGIN", "*")
    response.headers["Access-Control-Allow-Origin"]  = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# ─────────────────────────────────────────────────────────────────────────────
# SİNYAL TAMPONU — 17:30–17:35 toplama penceresi
# ─────────────────────────────────────────────────────────────────────────────

class SignalCollector:
    """
    17:30–17:35 arasında A ve/veya B sinyallerini toplar.

    Durum geçişleri:
      IDLE → (17:30 / open_window()) → COLLECTING
           → (17:35 / timer)         → DECIDED
           → (gün değişti)           → IDLE (otomatik reset)

    Pencere DIŞINDA gelen sinyaller reddedilir ve log'a yazılır.
    """

    STATE_IDLE       = "idle"
    STATE_COLLECTING = "collecting"
    STATE_DECIDED    = "decided"

    WINDOW_START = dtime(17, 30, 0)
    WINDOW_END   = dtime(17, 35, 0)

    def __init__(self, on_decision_ready, db_logger):
        self.on_decision_ready = on_decision_ready
        self.log   = db_logger

        self._lock    = threading.Lock()
        self._state   = self.STATE_IDLE
        self._day     = None
        self._data_a  = None   # parse edilmiş A listesi ya da None
        self._data_b  = None   # parse edilmiş B listesi ya da None
        self._timer   = None

    # ── Dışa açık ────────────────────────────────────────────────────────

    def open_window(self):
        """Scheduler 17:30'da çağırır. Toplama modunu açar."""
        import pytz
        today = datetime.now(pytz.timezone("Europe/Istanbul")).date()

        with self._lock:
            self._maybe_reset(today)
            if self._state == self.STATE_IDLE:
                self._state = self.STATE_COLLECTING
                self._start_timer(today)
                self.log.log(
                    "INFO", "WINDOW_OPEN",
                    "[17:30] Sinyal toplama penceresi AÇILDI. "
                    "17:35'e kadar /webhook/signalA ve /webhook/signalB bekleniyor.",
                )

    def receive(self, source: str, parsed_items: list[dict]) -> dict:
        """
        A veya B sinyali geldiğinde çağrılır.
        Pencere dışındaysa reddeder.
        """
        import pytz
        tz    = pytz.timezone("Europe/Istanbul")
        now   = datetime.now(tz)
        today = now.date()
        now_t = now.time().replace(second=0, microsecond=0)

        with self._lock:
            self._maybe_reset(today)

            # Pencere henüz açılmadı
            if now_t < self.WINDOW_START:
                self.log.log("WARNING", "SIGNAL_EARLY",
                             f"[{source}] Pencere açılmadan önce sinyal geldi ({now_t}). "
                             f"Reddedildi. Pencere: 17:30.")
                return {"status": "rejected", "reason": "window_not_open"}

            # Pencere kapandı veya karar verildi
            if now_t >= self.WINDOW_END or self._state == self.STATE_DECIDED:
                self.log.log("WARNING", "SIGNAL_LATE",
                             f"[{source}] Pencere kapandıktan sonra sinyal geldi ({now_t}). "
                             f"Reddedildi.")
                return {"status": "rejected", "reason": "window_closed"}

            # Pencere açık değilse (scheduler çalışmadıysa) otomatik aç
            if self._state == self.STATE_IDLE:
                self._state = self.STATE_COLLECTING
                self._start_timer(today)
                self.log.log("INFO", "WINDOW_AUTO_OPEN",
                             f"[{source}] İlk sinyal ile pencere otomatik açıldı.")

            # Veriyi kaydet
            if source == "A":
                self._data_a = parsed_items
                self.log.log(
                    "INFO", "SIGNAL_A_RECEIVED",
                    f"[A] {len(parsed_items)} hisse alındı: "
                    f"{[i['symbol'] for i in parsed_items]} | "
                    f"B durumu: {'BEKLENIYOR' if self._data_b is None else 'ALINDI'}",
                    extra_data={"symbols": [i['symbol'] for i in parsed_items],
                                "b_received": self._data_b is not None}
                )
            else:
                self._data_b = parsed_items
                self.log.log(
                    "INFO", "SIGNAL_B_RECEIVED",
                    f"[B] {len(parsed_items)} hisse alındı: "
                    f"{[i['symbol'] for i in parsed_items]} | "
                    f"A durumu: {'BEKLENIYOR' if self._data_a is None else 'ALINDI'}",
                    extra_data={"symbols": [i['symbol'] for i in parsed_items],
                                "a_received": self._data_a is not None}
                )

            # İkisi de geldi → hemen karar ver
            if self._data_a is not None and self._data_b is not None:
                self._cancel_timer()
                return self._make_decision_unlocked()

            return {
                "status":    "collected",
                "source":    source,
                "collected": self._collected_sources(),
                "waiting":   ["B"] if source == "A" else ["A"],
                "message":   f"Kaynak {source} alındı. "
                             f"{'B bekleniyor.' if source == 'A' else 'A bekleniyor.'} "
                             f"17:35'e kadar süre var.",
            }

    def status(self) -> dict:
        with self._lock:
            return {
                "state":           self._state,
                "day":             str(self._day),
                "window_start":    str(self.WINDOW_START),
                "window_end":      str(self.WINDOW_END),
                "source_a":        [i['symbol'] for i in self._data_a] if self._data_a else [],
                "source_b":        [i['symbol'] for i in self._data_b] if self._data_b else [],
                "a_received":      self._data_a is not None,
                "b_received":      self._data_b is not None,
            }

    # ── İç metotlar ──────────────────────────────────────────────────────

    def _collected_sources(self):
        sources = []
        if self._data_a is not None: sources.append("A")
        if self._data_b is not None: sources.append("B")
        return sources

    def _start_timer(self, today):
        import pytz
        tz       = pytz.timezone("Europe/Istanbul")
        now      = datetime.now(tz)
        close_dt = tz.localize(datetime.combine(today, self.WINDOW_END))
        delay    = max(1, (close_dt - now).total_seconds())

        self._timer = threading.Timer(delay, self._on_window_close)
        self._timer.daemon = True
        self._timer.start()
        self.log.log("INFO", "WINDOW_TIMER",
                     f"Pencere kapanma sayacı başlatıldı: {delay:.0f} sn (17:35)")

    def _cancel_timer(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _on_window_close(self):
        """17:35 timer callback — lock dışında çalışır."""
        with self._lock:
            if self._state != self.STATE_COLLECTING:
                return
            self._make_decision_unlocked()

    def _make_decision_unlocked(self) -> dict:
        """Karar ver ve callback'i ateşle. Lock altında çalışır."""
        self._state = self.STATE_DECIDED

        trade_list, decision_code = decide_trade_list(
            source_a  = self._data_a,
            source_b  = self._data_b,
            db_logger = self.log,
        )

        if trade_list:
            threading.Thread(
                target=self.on_decision_ready,
                args=(trade_list, decision_code),
                daemon=True,
            ).start()

        return {
            "status":        "decided",
            "decision_code": decision_code,
            "trade_list":    trade_list,
            "a_received":    self._data_a is not None,
            "b_received":    self._data_b is not None,
        }

    def _maybe_reset(self, today):
        if self._day != today:
            self._cancel_timer()
            self._state  = self.STATE_IDLE
            self._day    = today
            self._data_a = None
            self._data_b = None
            self._timer  = None


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK SUNUCUSU
# ─────────────────────────────────────────────────────────────────────────────

_DIST_DIR = os.path.join(os.path.dirname(__file__), "dashboard", "dist")


class WebhookServer:

    def __init__(self, signal_queue, Session, db_logger, host: str, port: int):
        self.signal_queue = signal_queue
        self.Session      = Session
        self.log          = db_logger
        self.host         = host
        self.port         = port

        # Mark-to-market fiyat cache: {sembol: son_fiyat}
        self._live_prices: dict[str, float] = {}

        self.app = Flask(__name__, static_folder=_DIST_DIR, static_url_path="")

        self.collector = SignalCollector(
            on_decision_ready = self._on_decision_ready,
            db_logger         = db_logger,
        )

        self.app.after_request(_add_cors_headers)

        @self.app.route("/<path:p>", methods=["OPTIONS"])
        @self.app.route("/",         methods=["OPTIONS"])
        def _options(*a, **kw):
            from flask import Response
            return Response(status=204)

        self._register_routes()

    # ── Karar callback ────────────────────────────────────────────────────

    def _on_decision_ready(self, trade_list: list[str], decision_code: str):
        """SignalCollector karar verince çağrılır → queue'ya at."""
        self.signal_queue.put({
            "received_at":   datetime.utcnow().isoformat(),
            "symbols":       trade_list,
            "decision_code": decision_code,
        })
        self.log.log(
            "INFO", "SIGNAL_QUEUED",
            f"[{decision_code.upper()}] Alım listesi queue'ya eklendi: {trade_list}",
            extra_data={"symbols": trade_list, "decision": decision_code},
        )

    # ── Dış arayüz ───────────────────────────────────────────────────────

    def open_collection_window(self):
        """Scheduler 17:30'da çağırır."""
        self.collector.open_window()

    def update_live_price(self, symbol: str, price: float):
        """Scheduler ListPositions callback'inden çağırır (mark-to-market)."""
        self._live_prices[symbol] = price

    def mark_signal_processed(self, symbols_bought: list[str]):
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

    # ── Flask route'ları ──────────────────────────────────────────────────

    def _register_routes(self):

        # ── Sağlık ───────────────────────────────────────────────────────
        @self.app.route("/health", methods=["GET"])
        def health():
            return jsonify({
                "status":    "ok",
                "timestamp": datetime.utcnow().isoformat(),
                "collector": self.collector.status(),
            }), 200

        # ── Sinyal durumu ─────────────────────────────────────────────────
        @self.app.route("/signal-status", methods=["GET"])
        def signal_status():
            return jsonify(self.collector.status()), 200

        # ── /webhook/signalA ─────────────────────────────────────────────
        @self.app.route("/webhook/signalA", methods=["POST"])
        def webhook_a():
            return self._handle_signal(request, source="A")

        # ── /webhook/signalB ─────────────────────────────────────────────
        @self.app.route("/webhook/signalB", methods=["POST"])
        def webhook_b():
            return self._handle_signal(request, source="B")

        # ── /api/trades ───────────────────────────────────────────────────
        @self.app.route("/api/trades", methods=["GET"])
        def api_trades():
            """
            Kapalı işlemler + açık pozisyonların mark-to-market değeri.

            Query params:
              month=YYYY-MM     sadece o ay (opsiyonel)
              include_open=0    açık pozisyonları dahil etme

            Response: {"2026-01-05": [{symbol,buyAt,sellAt,buyPrice,sellPrice,pnlPct,...}]}
            Açık pozisyonlarda sellAt=null, isOpen=true
            """
            month_filter = request.args.get("month")
            include_open = request.args.get("include_open", "1") != "0"

            session = self.Session()
            try:
                from sqlalchemy import extract

                q = session.query(Trade).filter(Trade.is_closed == True)
                if month_filter:
                    try:
                        y, m = month_filter.split("-")
                        q = q.filter(
                            extract("year",  Trade.sell_date) == int(y),
                            extract("month", Trade.sell_date) == int(m),
                        )
                    except Exception:
                        return jsonify({"error": "month YYYY-MM formatında olmalı"}), 400

                grouped: dict[str, list] = defaultdict(list)
                for t in q.order_by(Trade.sell_date).all():
                    key = t.sell_date_key()
                    if key:
                        grouped[key].append(t.to_dashboard_trade())

                # Açık pozisyonlar — mark-to-market
                if include_open:
                    today_str = date.today().isoformat()
                    positions = session.query(PortfolioPosition)\
                                       .filter_by(is_active=True).all()

                    if month_filter and positions:
                        y_f, m_f = month_filter.split("-")
                        if not (str(date.today().year) == y_f and
                                f"{date.today().month:02d}" == m_f):
                            positions = []  # Bu ay değil

                    for pos in positions:
                        live_px = self._live_prices.get(pos.symbol, pos.entry_price)
                        pnl_pct = round(
                            ((live_px - pos.entry_price) / pos.entry_price) * 100, 4
                        ) if pos.entry_price else 0.0

                        grouped[today_str].append({
                            "symbol":     pos.symbol,
                            "buyAt":      pos.entry_date.strftime("%Y-%m-%dT%H:%M")
                                          if pos.entry_date else None,
                            "sellAt":     None,
                            "buyPrice":   pos.entry_price,
                            "sellPrice":  live_px,
                            "pnlPct":     pnl_pct,
                            "exitReason": None,
                            "isOpen":     True,
                            "daysHeld":   pos.trading_days_held,
                        })

                return jsonify(dict(grouped)), 200

            except Exception as e:
                self.log.log("ERROR", "API_TRADES_ERR", f"/api/trades: {e}")
                return jsonify({"error": "Internal server error"}), 500
            finally:
                session.close()

        # ── /api/equity ───────────────────────────────────────────────────
        @self.app.route("/api/equity", methods=["GET"])
        def api_equity():
            """
            Aylık PnL — PnL Formülü:
              Günlük Kar/Zarar = (Kapanan PnL Toplamı + Açık MTM PnL Toplamı) / MAX_POSITIONS

            Response: [{"month":"2026-01","pnlPct":8.4,"tradeCount":12,"openCount":2}]
            """
            session = self.Session()
            try:
                from sqlalchemy import extract, func

                # Kapalı işlemler aylık özet
                rows = (
                    session.query(
                        extract("year",  Trade.sell_date).label("yr"),
                        extract("month", Trade.sell_date).label("mo"),
                        func.sum(Trade.pnl_percent).label("sum_pnl"),
                        func.count(Trade.id).label("cnt"),
                    )
                    .filter(Trade.is_closed == True, Trade.pnl_percent != None)
                    .group_by("yr", "mo")
                    .order_by("yr", "mo")
                    .all()
                )

                result = []
                for row in rows:
                    # Formül: toplam_pnl / MAX_POSITIONS
                    avg = round(float(row.sum_pnl) / config.MAX_POSITIONS, 4)
                    result.append({
                        "month":      f"{int(row.yr):04d}-{int(row.mo):02d}",
                        "pnlPct":     avg,
                        "tradeCount": int(row.cnt),
                        "openCount":  0,
                    })

                # Mevcut ay: açık pozisyonlar MTM dahil
                positions = session.query(PortfolioPosition)\
                                   .filter_by(is_active=True).all()
                if positions:
                    open_pnl_sum = sum(
                        ((self._live_prices.get(p.symbol, p.entry_price) - p.entry_price)
                         / p.entry_price) * 100
                        for p in positions if p.entry_price
                    )
                    # Bu ayın kapalı PnL toplamı
                    this_month = date.today().strftime("%Y-%m")
                    this_y, this_m = date.today().year, date.today().month
                    closed_sum = (
                        session.query(func.sum(Trade.pnl_percent))
                        .filter(
                            Trade.is_closed == True,
                            Trade.pnl_percent != None,
                            extract("year",  Trade.sell_date) == this_y,
                            extract("month", Trade.sell_date) == this_m,
                        )
                        .scalar()
                    ) or 0.0

                    # Formül: (kapanan_toplam + açık_mtm_toplam) / MAX_POSITIONS
                    combined_avg = round(
                        (closed_sum + open_pnl_sum) / config.MAX_POSITIONS, 4
                    )

                    existing = next((r for r in result if r["month"] == this_month), None)
                    if existing:
                        existing["pnlPct"]    = combined_avg
                        existing["openCount"] = len(positions)
                    else:
                        result.append({
                            "month":      this_month,
                            "pnlPct":     combined_avg,
                            "tradeCount": 0,
                            "openCount":  len(positions),
                        })

                return jsonify(result), 200

            except Exception as e:
                self.log.log("ERROR", "API_EQUITY_ERR", f"/api/equity: {e}")
                return jsonify({"error": "Internal server error"}), 500
            finally:
                session.close()

        # ── /portfolio ────────────────────────────────────────────────────
        @self.app.route("/portfolio", methods=["GET"])
        def portfolio():
            session = self.Session()
            try:
                positions = session.query(PortfolioPosition)\
                                   .filter_by(is_active=True).all()
                trades    = session.query(Trade).filter_by(is_closed=True)\
                                   .order_by(Trade.sell_date.desc()).limit(10).all()
                return jsonify({
                    "active_positions": [
                        {
                            "symbol":           p.symbol,
                            "entry_price":      p.entry_price,
                            "live_price":       self._live_prices.get(p.symbol, p.entry_price),
                            "quantity":         p.quantity,
                            "take_profit":      p.take_profit_price,
                            "stop_loss":        p.stop_loss_price,
                            "days_held":        p.trading_days_held,
                            "entry_date":       p.entry_date.isoformat() if p.entry_date else None,
                            "unrealized_pct":   round(
                                ((self._live_prices.get(p.symbol, p.entry_price)
                                  - p.entry_price) / p.entry_price) * 100, 2
                            ) if p.entry_price else 0.0,
                        }
                        for p in positions
                    ],
                    "recent_trades":  [t.to_dashboard_trade() for t in trades],
                    "collector":      self.collector.status(),
                    "live_prices":    self._live_prices,
                }), 200
            finally:
                session.close()

        # ── React statik ──────────────────────────────────────────────────
        @self.app.route("/", methods=["GET"])
        def serve_root():
            if os.path.isfile(os.path.join(_DIST_DIR, "index.html")):
                return send_from_directory(_DIST_DIR, "index.html")
            return jsonify({
                "status":   "dashboard_not_built",
                "message":  "C:\\matriks_bot\\dashboard\\dist\\index.html bulunamadı.",
                "hint":     "Yerel makinende 'npm run build' al, dist/ klasörünü sunucuya kopyala.",
                "endpoints": ["/webhook/signalA", "/webhook/signalB",
                              "/api/trades", "/api/equity", "/signal-status", "/health"],
            }), 200

        @self.app.route("/<path:fname>", methods=["GET"])
        def serve_static(fname):
            fp = os.path.join(_DIST_DIR, fname)
            if os.path.isfile(fp):
                return send_from_directory(_DIST_DIR, fname)
            idx = os.path.join(_DIST_DIR, "index.html")
            if os.path.isfile(idx):
                return send_from_directory(_DIST_DIR, "index.html")
            return jsonify({"error": "Not found"}), 404

    # ── Webhook işleme ────────────────────────────────────────────────────

    def _handle_signal(self, req, source: str):
        try:
            data = req.get_json(silent=True)
            if not data:
                return jsonify({"error": "Invalid JSON"}), 400

            # Auth
            if data.get("secret", "") != config.WEBHOOK_SECRET:
                self.log.log("WARNING", "WEBHOOK_AUTH",
                             f"[{source}] Geçersiz secret. IP: {req.remote_addr}")
                return jsonify({"error": "Unauthorized"}), 401

            # Sembol çözümleme
            parsed_items = []
            parse_mode   = "unknown"

            raw_str = data.get("raw", "")
            if raw_str:
                # Ham format: "GARAN|RSI:74.2|ROC:1.8|Chg%:9.95 ..."
                parsed_items = parse_raw_signal(raw_str)
                parse_mode   = "raw"
                self.log.log(
                    "INFO", f"WEBHOOK_{source}_PARSED",
                    f"[{source}] Ham parse: {len(parsed_items)} hisse → "
                    f"{[i['symbol'] for i in parsed_items]}",
                    extra_data={"mode": "raw", "items": parsed_items},
                )
            else:
                # Direkt sembol listesi: ["GARAN","THYAO",...]
                raw_list = data.get("symbols", [])
                if not isinstance(raw_list, list) or not raw_list:
                    return jsonify({"error": "symbols veya raw alanı gerekli"}), 400
                parsed_items = [
                    {"symbol": s.strip().upper(), "rsi": None, "roc": None, "chg": None}
                    for s in raw_list if isinstance(s, str) and s.strip()
                ]
                parse_mode = "list"
                self.log.log(
                    "INFO", f"WEBHOOK_{source}_LIST",
                    f"[{source}] Liste modu: {[i['symbol'] for i in parsed_items]}",
                    extra_data={"mode": "list", "symbols": [i['symbol'] for i in parsed_items]},
                )

            if not parsed_items:
                self.log.log("WARNING", f"WEBHOOK_{source}_EMPTY",
                             f"[{source}] Parse sonrası sembol listesi boş.")
                return jsonify({"status": "accepted_empty",
                                "message": "Sembol listesi boş"}), 200

            # Sinyal tampona gönder
            self._save_signal(data, [i['symbol'] for i in parsed_items], source)
            result = self.collector.receive(source, parsed_items)

            return jsonify({
                "status":      "accepted",
                "source":      source,
                "parse_mode":  parse_mode,
                "symbols":     [i['symbol'] for i in parsed_items],
                "timestamp":   datetime.utcnow().isoformat(),
                **result,
            }), 200

        except Exception as e:
            self.log.log("ERROR", f"WEBHOOK_{source}_ERR", f"Hata: {e}")
            return jsonify({"error": "Internal server error"}), 500

    def _save_signal(self, raw_data: dict, symbols: list[str], source: str):
        session = self.Session()
        try:
            session.add(Signal(
                received_at = datetime.utcnow(),
                raw_payload = json.dumps({**raw_data, "_source": source},
                                         ensure_ascii=False),
                symbols     = ",".join(symbols),
                processed   = False,
                notes       = f"source={source}",
            ))
            session.commit()
        except Exception as e:
            session.rollback()
            self.log.log("ERROR", "SIGNAL_SAVE", f"Kayıt hatası: {e}")
        finally:
            session.close()

    def run(self):
        self.log.log("INFO", "WEBHOOK_START",
                     f"Webhook sunucusu başlatıldı: {self.host}:{self.port} | "
                     f"Endpoint'ler: /webhook/signalA  /webhook/signalB")
        self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
