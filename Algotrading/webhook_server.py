"""
Webhook Sunucusu

TRADİNGVIEW ENTEGRASYONU:
─────────────────────────────────────────────────────────────────────────────
Pine Script alert_message formatı:
  {"secret": "...", "data": "{GARAN | RSI: 74.20 | ROC: 1.82 | Chg%: 9.95%} {THYAO | ...}"}

İki ayrı alert, iki ayrı URL:
  İndikatör 1 → POST /webhook/signalA
  İndikatör 2 → POST /webhook/signalB

HIZLI YANITLAMA (TradingView timeout önleme):
  Sinyal gelir → anında {"status": "ok"} döner
  Parse + filtre + karar → arka planda (thread) işlenir

SINYAL TOPLAMA PENCERESİ:
  17:30 → scheduler.open_collection_window() → COLLECTING modu
  17:35 → timer kapanır → decide_trade_list() → signal_queue'ya at
  17:40 → scheduler queue'yu okur → ListPositions → alım emirleri

ENDPOINT'LER:
  POST /webhook/signalA     → Kaynak A (production)
  POST /webhook/signalB     → Kaynak B (production)
  POST /webhook/testA       → Kaynak A test (pencere bypass)
  POST /webhook/testB       → Kaynak B test (pencere bypass)
  POST /webhook/open-window → Pencereyi manuel aç
  GET  /signal-status       → Tampon durumu
  GET  /health              → Bot sağlık
  GET  /api/trades          → Dashboard: işlemler + MTM
  GET  /api/equity          → Dashboard: aylık PnL
  GET  /portfolio           → Açık pozisyonlar
  GET  /                    → React dashboard
"""

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, date, time as dtime
from flask import Flask, request, jsonify, send_from_directory

import config
from database import Signal, Trade, PortfolioPosition
from signal_engine import parse_tv_data, decide_trade_list


# ─────────────────────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────────────────────

def _add_cors(response):
    origin = getattr(config, "CORS_ORIGIN", "*")
    response.headers["Access-Control-Allow-Origin"]  = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# ─────────────────────────────────────────────────────────────────────────────
# SİNYAL TAMPONU
# ─────────────────────────────────────────────────────────────────────────────

class SignalCollector:
    """
    17:30–17:35 penceresi boyunca A ve B sinyallerini toplar.

    Durum:
      IDLE       → pencere kapalı, sinyal reddedilir
      COLLECTING → pencere açık, sinyal kabul edilir
      DECIDED    → karar verildi, sinyal reddedilir
    """

    STATE_IDLE       = "idle"
    STATE_COLLECTING = "collecting"
    STATE_DECIDED    = "decided"

    @staticmethod
    def _parse_time(t: str) -> dtime:
        h, m = t.strip().split(":")
        return dtime(int(h), int(m), 0)

    @property
    def WINDOW_START(self) -> dtime:
        return self._parse_time(getattr(config, "SIGNAL_WINDOW_START", "17:30"))

    @property
    def WINDOW_END(self) -> dtime:
        return self._parse_time(getattr(config, "SIGNAL_WINDOW_END", "17:35"))

    def __init__(self, on_decision_ready, db_logger):
        self.on_decision_ready = on_decision_ready
        self.log   = db_logger
        self._lock  = threading.Lock()
        self._state = self.STATE_IDLE
        self._day   = None
        self._data_a: list[dict] | None = None
        self._data_b: list[dict] | None = None
        self._timer = None

    def open_window(self):
        """Scheduler config'deki SIGNAL_WINDOW_START saatinde çağırır."""
        import pytz
        today = datetime.now(pytz.timezone("Europe/Istanbul")).date()
        with self._lock:
            self._maybe_reset(today)
            if self._state == self.STATE_IDLE:
                self._state = self.STATE_COLLECTING
                self._start_timer(today)
                self.log.log(
                    "INFO", "WINDOW_OPEN",
                    f"[{config.SIGNAL_WINDOW_START}] Sinyal toplama penceresi AÇILDI. "
                    f"{config.SIGNAL_WINDOW_END}'e kadar /webhook/signalA ve /webhook/signalB bekleniyor.",
                )

    def receive(self, source: str, parsed_items: list[dict],
                bypass: bool = False) -> dict:
        """
        Parse edilmiş sinyal listesini alır.
        bypass=True → pencere kontrolü yapılmaz (test modu).
        """
        import pytz
        tz    = pytz.timezone("Europe/Istanbul")
        now   = datetime.now(tz)
        today = now.date()
        now_t = now.time().replace(second=0, microsecond=0)

        with self._lock:
            self._maybe_reset(today)

            if not bypass:
                if now_t < self.WINDOW_START:
                    self.log.log("WARNING", "SIGNAL_EARLY",
                                 f"[{source}] Pencere açılmadan önce geldi ({now_t}). Reddedildi.")
                    return {"status": "rejected", "reason": "window_not_open",
                            "window_opens": str(self.WINDOW_START)}

                if now_t >= self.WINDOW_END or self._state == self.STATE_DECIDED:
                    self.log.log("WARNING", "SIGNAL_LATE",
                                 f"[{source}] Pencere kapandıktan sonra geldi. Reddedildi.")
                    return {"status": "rejected", "reason": "window_closed"}

            # Pencere kapalıysa bypass ile aç
            if self._state == self.STATE_IDLE:
                self._state = self.STATE_COLLECTING
                self._start_timer(today)
                self.log.log("INFO", "WINDOW_AUTO_OPEN",
                             f"[{source}] {'Test: ' if bypass else ''}Pencere açıldı.")

            # Kaydет
            if source == "A":
                self._data_a = parsed_items
            else:
                self._data_b = parsed_items

            syms = [i['symbol'] for i in parsed_items]
            self.log.log(
                "INFO", f"SIGNAL_{source}_RECV",
                f"[{source}] {len(parsed_items)} hisse: {syms} | "
                f"A={'✓' if self._data_a is not None else '…'} "
                f"B={'✓' if self._data_b is not None else '…'}",
                extra_data={"source": source, "symbols": syms}
            )

            # İkisi de geldiyse hemen karar ver
            if self._data_a is not None and self._data_b is not None:
                self._cancel_timer()
                return self._decide_unlocked()

            other = "B" if source == "A" else "A"
            return {
                "status":   "collected",
                "source":   source,
                "waiting":  other,
                "message":  f"Kaynak {source} alındı. {other} bekleniyor ({self.WINDOW_END}).",
            }

    def status(self) -> dict:
        with self._lock:
            return {
                "state":       self._state,
                "day":         str(self._day),
                "window_start": str(self.WINDOW_START),
                "window_end":   str(self.WINDOW_END),
                "a_received":  self._data_a is not None,
                "b_received":  self._data_b is not None,
                "source_a":    [i['symbol'] for i in self._data_a] if self._data_a else [],
                "source_b":    [i['symbol'] for i in self._data_b] if self._data_b else [],
            }

    # ── İç ───────────────────────────────────────────────────────────────

    def _start_timer(self, today):
        import pytz
        tz    = pytz.timezone("Europe/Istanbul")
        now   = datetime.now(tz)
        end_dt = tz.localize(datetime.combine(today, self.WINDOW_END))
        delay  = max(1, (end_dt - now).total_seconds())
        self._timer = threading.Timer(delay, self._on_close)
        self._timer.daemon = True
        self._timer.start()
        self.log.log("INFO", "WINDOW_TIMER",
                     f"Pencere kapanma sayacı: {delay:.0f} sn ({self.WINDOW_END})")

    def _cancel_timer(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _on_close(self):
        with self._lock:
            if self._state != self.STATE_COLLECTING:
                return
            if not self._data_a and not self._data_b:
                self._state = self.STATE_DECIDED
                self.log.log("WARNING", "SIGNAL_NO_DATA",
                             f"[{self.WINDOW_END}] Pencere kapandı — HİÇ VERİ GELMEDİ. "
                             "TradingView alert'lerini kontrol et.")
                return
            self._decide_unlocked()

    def _decide_unlocked(self) -> dict:
        self._state = self.STATE_DECIDED
        trade_list, code = decide_trade_list(
            source_a=self._data_a,
            source_b=self._data_b,
            db_logger=self.log,
        )
        if trade_list:
            threading.Thread(
                target=self.on_decision_ready,
                args=(trade_list, code),
                daemon=True,
            ).start()
        return {"status": "decided", "decision_code": code, "trade_list": trade_list}

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
        self.signal_queue  = signal_queue
        self.Session       = Session
        self.log           = db_logger
        self.host          = host
        self.port          = port
        self._live_prices: dict[str, float] = {}  # MTM fiyat cache

        self.app = Flask(__name__, static_folder=_DIST_DIR, static_url_path="")
        self.collector = SignalCollector(
            on_decision_ready=self._on_decision,
            db_logger=db_logger,
        )
        self.app.after_request(_add_cors)

        @self.app.route("/<path:p>", methods=["OPTIONS"])
        @self.app.route("/",         methods=["OPTIONS"])
        def _opt(*a, **kw):
            from flask import Response
            return Response(status=204)

        self._register_routes()

    # ── Karar callback ────────────────────────────────────────────────────

    def _on_decision(self, trade_list: list[str], code: str):
        self.signal_queue.put({
            "received_at":   datetime.utcnow().isoformat(),
            "symbols":       trade_list,
            "decision_code": code,
        })
        self.log.log("INFO", "SIGNAL_QUEUED",
                     f"[{code.upper()}] Queue'ya eklendi: {trade_list}",
                     extra_data={"symbols": trade_list, "decision": code})

    # ── Dış arayüz ───────────────────────────────────────────────────────

    def open_collection_window(self):
        self.collector.open_window()

    def update_live_price(self, symbol: str, price: float):
        self._live_prices[symbol] = price

    def mark_signal_processed(self, bought: list[str]):
        session = self.Session()
        try:
            sig = session.query(Signal).order_by(Signal.id.desc()).first()
            if sig:
                sig.processed      = True
                sig.symbols_bought = ",".join(bought)
                session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    # ── Route'lar ─────────────────────────────────────────────────────────

    def _register_routes(self):

        # ── Sağlık ───────────────────────────────────────────────────────
        @self.app.route("/health", methods=["GET"])
        def health():
            return jsonify({
                "status":    "ok",
                "timestamp": datetime.utcnow().isoformat(),
                "host":      self.host,
                "port":      self.port,
                "collector": self.collector.status(),
            }), 200

        @self.app.route("/signal-status", methods=["GET"])
        def signal_status():
            return jsonify(self.collector.status()), 200

        # ── Production webhook'ları ────────────────────────────────────────
        @self.app.route("/webhook/signalA", methods=["POST"])
        def wa():
            return self._handle(request, "A", bypass=False)

        @self.app.route("/webhook/signalB", methods=["POST"])
        def wb():
            return self._handle(request, "B", bypass=False)

        # ── Test webhook'ları (pencere bypass) ────────────────────────────
        @self.app.route("/webhook/testA", methods=["POST"])
        def ta():
            return self._handle(request, "A", bypass=True)

        @self.app.route("/webhook/testB", methods=["POST"])
        def tb():
            return self._handle(request, "B", bypass=True)

        # ── Manuel pencere aç ─────────────────────────────────────────────
        @self.app.route("/webhook/open-window", methods=["POST"])
        def open_win():
            self.collector.open_window()
            return jsonify({"status": "ok", "collector": self.collector.status()}), 200

        # ── Dashboard API ─────────────────────────────────────────────────
        @self.app.route("/api/trades", methods=["GET"])
        def api_trades():
            return self._api_trades()

        @self.app.route("/api/equity", methods=["GET"])
        def api_equity():
            return self._api_equity()

        @self.app.route("/portfolio", methods=["GET"])
        def portfolio():
            return self._api_portfolio()

        # ── React statik ──────────────────────────────────────────────────
        @self.app.route("/", methods=["GET"])
        def root():
            idx = os.path.join(_DIST_DIR, "index.html")
            if os.path.isfile(idx):
                return send_from_directory(_DIST_DIR, "index.html")
            return jsonify({
                "status":    "dashboard_not_built",
                "message":   "dist/index.html bulunamadı.",
                "hint":      "npm run build → dist/ → C:\\matriks_bot\\dashboard\\dist\\",
                "endpoints": ["/webhook/signalA", "/webhook/signalB",
                              "/api/trades", "/api/equity",
                              "/signal-status", "/health"],
            }), 200

        @self.app.route("/<path:fname>", methods=["GET"])
        def static_files(fname):
            fp = os.path.join(_DIST_DIR, fname)
            if os.path.isfile(fp):
                return send_from_directory(_DIST_DIR, fname)
            idx = os.path.join(_DIST_DIR, "index.html")
            if os.path.isfile(idx):
                return send_from_directory(_DIST_DIR, "index.html")
            return jsonify({"error": "Not found"}), 404

    # ── Webhook işleme ─────────────────────────────────────────────────────

    def _handle(self, req, source: str, bypass: bool):
        """
        TradingView'den gelen isteği işler.

        Akış:
          1. JSON oku
          2. Secret doğrula
          3. Anında {"status":"ok"} yanıtı hazırla
          4. Parse + filtre + tampon işlemini arka planda başlat
          5. Yanıtı dön (TradingView timeout almaz)
        """
        try:
            data = req.get_json(silent=True)

            # JSON parse edilemedi — ham body'yi dene
            if data is None:
                try:
                    raw_body = req.get_data(as_text=True)
                    import json as _json
                    data = _json.loads(raw_body)
                except Exception:
                    self.log.log("ERROR", "WEBHOOK_JSON_ERR",
                                 f"[{source}] JSON parse hatası. Body: {req.get_data(as_text=True)[:200]}")
                    return jsonify({"error": "Invalid JSON"}), 400

            # Auth
            if data.get("secret", "") != config.WEBHOOK_SECRET:
                self.log.log("WARNING", "WEBHOOK_AUTH",
                             f"[{source}] Geçersiz secret. IP={req.remote_addr}")
                return jsonify({"error": "Unauthorized"}), 401

            # ── Anında OK dön — TradingView timeout almaz ─────────────────
            # İşlemi arka planda yürüt
            threading.Thread(
                target=self._process_signal_bg,
                args=(data, source, bypass),
                daemon=True,
            ).start()

            return jsonify({"status": "ok"}), 200

        except Exception as e:
            self.log.log("ERROR", "WEBHOOK_ERR", f"[{source}] Hata: {e}")
            return jsonify({"error": "Internal server error"}), 500

    def _process_signal_bg(self, data: dict, source: str, bypass: bool):
        """
        Arka planda çalışan parse + filtre + tampon işlemi.
        TradingView zaten "ok" yanıtını almıştır.
        """
        try:
            parsed_items = []
            parse_info   = {}

            # ── Mod 1: "data" alanı — TradingView Pine Script formatı ────
            # {"secret":"...","data":"{GARAN | RSI: 74.20 | ROC: 1.82 | Chg%: 9.95%} ..."}
            data_str = data.get("data", "")
            if data_str:
                parsed_items = parse_tv_data(data_str)
                parse_info   = {
                    "mode":      "tv_data",
                    "raw_len":   len(data_str),
                    "parsed":    len(parsed_items),
                    "symbols":   [i['symbol'] for i in parsed_items],
                    "details":   parsed_items,
                }
                self.log.log(
                    "INFO", f"PARSE_{source}",
                    f"[{source}] data parse: {len(parsed_items)} hisse → "
                    f"{[i['symbol'] for i in parsed_items]}",
                    extra_data=parse_info,
                )

            # ── Mod 2: "raw" alanı — pipe formatı ────────────────────────
            # {"secret":"...","raw":"GARAN|RSI:74.20|..."}
            elif data.get("raw", ""):
                parsed_items = parse_tv_data(data["raw"])
                parse_info   = {"mode": "raw", "symbols": [i['symbol'] for i in parsed_items]}

            # ── Mod 3: "symbols" alanı — direkt liste ────────────────────
            # {"secret":"...","symbols":["GARAN","THYAO"]}
            elif data.get("symbols"):
                raw_list = data["symbols"]
                if isinstance(raw_list, list):
                    parsed_items = [
                        {"symbol": s.strip().upper(), "rsi": None, "roc": None, "chg": None}
                        for s in raw_list if isinstance(s, str) and s.strip()
                    ]
                    parse_info = {"mode": "list", "symbols": [i['symbol'] for i in parsed_items]}

            if not parsed_items:
                self.log.log("WARNING", f"PARSE_{source}_EMPTY",
                             f"[{source}] Parse sonrası sembol listesi boş. "
                             f"data='{str(data.get('data',''))[:100]}'")
                return

            # DB'ye kaydet
            self._save_signal(data, [i['symbol'] for i in parsed_items], source)

            # Tampona gönder
            self.collector.receive(source, parsed_items, bypass=bypass)

        except Exception as e:
            self.log.log("ERROR", f"BG_PROCESS_{source}", f"Arka plan hatası: {e}")

    def _save_signal(self, raw_data: dict, symbols: list[str], source: str):
        session = self.Session()
        try:
            session.add(Signal(
                received_at = datetime.utcnow(),
                raw_payload = json.dumps({**raw_data, "_source": source},
                                         ensure_ascii=False)[:4000],
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

    # ── Dashboard API implementasyonları ──────────────────────────────────

    def _api_trades(self):
        """
        GET /api/trades?month=YYYY-MM&include_open=1

        Response: Record<string, Trade[]>
        Açık pozisyonlar mark-to-market fiyatıyla dahil edilir.
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

            if include_open:
                today_str = date.today().isoformat()
                positions = session.query(PortfolioPosition).filter_by(is_active=True).all()

                if month_filter:
                    y_f, m_f = month_filter.split("-")
                    if not (str(date.today().year) == y_f and
                            f"{date.today().month:02d}" == m_f):
                        positions = []

                for pos in positions:
                    live_px = self._live_prices.get(pos.symbol, pos.entry_price)
                    pnl_pct = round(
                        ((live_px - pos.entry_price) / pos.entry_price) * 100, 4
                    ) if pos.entry_price else 0.0
                    grouped[today_str].append({
                        "symbol":     pos.symbol,
                        "buyAt":      pos.entry_date.strftime("%Y-%m-%dT%H:%M") if pos.entry_date else None,
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
            self.log.log("ERROR", "API_TRADES", f"/api/trades: {e}")
            return jsonify({"error": "Internal server error"}), 500
        finally:
            session.close()

    def _api_equity(self):
        """
        GET /api/equity

        PnL Formülü: (kapanan PnL toplamı + açık MTM toplamı) / MAX_POSITIONS
        Response: [{month, pnlPct, tradeCount, openCount}]
        """
        session = self.Session()
        try:
            from sqlalchemy import extract, func

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
                avg = round(float(row.sum_pnl) / config.MAX_POSITIONS, 4)
                result.append({
                    "month":      f"{int(row.yr):04d}-{int(row.mo):02d}",
                    "pnlPct":     avg,
                    "tradeCount": int(row.cnt),
                    "openCount":  0,
                })

            # Mevcut ay: açık MTM ekle
            positions = session.query(PortfolioPosition).filter_by(is_active=True).all()
            if positions:
                open_pnl = sum(
                    ((self._live_prices.get(p.symbol, p.entry_price) - p.entry_price)
                     / p.entry_price) * 100
                    for p in positions if p.entry_price
                )
                this_month = date.today().strftime("%Y-%m")
                this_y, this_m = date.today().year, date.today().month

                from sqlalchemy import func as f2
                closed_sum = (
                    session.query(f2.sum(Trade.pnl_percent))
                    .filter(Trade.is_closed == True, Trade.pnl_percent != None,
                            extract("year",  Trade.sell_date) == this_y,
                            extract("month", Trade.sell_date) == this_m)
                    .scalar()
                ) or 0.0

                combined = round((closed_sum + open_pnl) / config.MAX_POSITIONS, 4)
                existing = next((r for r in result if r["month"] == this_month), None)
                if existing:
                    existing["pnlPct"]    = combined
                    existing["openCount"] = len(positions)
                else:
                    result.append({
                        "month":      this_month,
                        "pnlPct":     combined,
                        "tradeCount": 0,
                        "openCount":  len(positions),
                    })

            return jsonify(result), 200
        except Exception as e:
            self.log.log("ERROR", "API_EQUITY", f"/api/equity: {e}")
            return jsonify({"error": "Internal server error"}), 500
        finally:
            session.close()

    def _api_portfolio(self):
        session = self.Session()
        try:
            positions = session.query(PortfolioPosition).filter_by(is_active=True).all()
            trades    = session.query(Trade).filter_by(is_closed=True) \
                               .order_by(Trade.sell_date.desc()).limit(10).all()
            return jsonify({
                "active_positions": [{
                    "symbol":         p.symbol,
                    "entry_price":    p.entry_price,
                    "live_price":     self._live_prices.get(p.symbol, p.entry_price),
                    "quantity":       p.quantity,
                    "take_profit":    p.take_profit_price,
                    "stop_loss":      p.stop_loss_price,
                    "days_held":      p.trading_days_held,
                    "entry_date":     p.entry_date.isoformat() if p.entry_date else None,
                    "unrealized_pct": round(
                        ((self._live_prices.get(p.symbol, p.entry_price) - p.entry_price)
                         / p.entry_price) * 100, 2
                    ) if p.entry_price else 0.0,
                } for p in positions],
                "recent_trades": [t.to_dashboard_trade() for t in trades],
                "collector":     self.collector.status(),
                "live_prices":   self._live_prices,
            }), 200
        finally:
            session.close()

    def run(self):
        self.log.log(
            "INFO", "WEBHOOK_START",
            f"Webhook sunucusu başlatıldı: {self.host}:{self.port} | "
            f"TradingView URL'leri: "
            f"http://IP:{self.port}/webhook/signalA  "
            f"http://IP:{self.port}/webhook/signalB",
        )
        self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
