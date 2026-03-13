"""
Görev Zamanlayıcı

Günlük iş akışı:
  10:05  Sabah TP/SL yenileme (emirler bir önceki günden iptal edilmiş olur)
  17:30  Sinyal toplama penceresi AÇILIR
  17:35  Pencere KAPANIR (webhook_server içinden otomatik)
         → decide_trade_list() → Durum 1/2/3 kararı → queue'ya at
  17:40  Queue okunur → ListPositions (ApiCommands:1) → MTM fiyatlar
         → Alım emirleri (ApiCommands:3) gönderilir
  17:55  Gün sonu: 2-gün sınırını aşan pozisyonlar kapatılır
  18:05  İşlem günü sayacı artırılır
"""

import time
import queue
from datetime import datetime, date
from typing import Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import config
from database import Trade


class TaskScheduler:

    TIMEZONE = pytz.timezone("Europe/Istanbul")

    def __init__(self, portfolio_manager, matriks_client,
                 signal_queue: queue.Queue, db_logger, webhook_server):
        self.pm             = portfolio_manager
        self.client         = matriks_client
        self.signal_queue   = signal_queue
        self.log            = db_logger
        self.webhook_server = webhook_server

        self._scheduler:         BackgroundScheduler = BackgroundScheduler(timezone=self.TIMEZONE)
        self._open_orders_cache: List[dict]          = []
        self._current_prices:    Dict[str, float]    = {}
        self._cached_cash:       float               = 0.0

        self._setup_api_callbacks()

    # ─────────────────────────────────────────
    # BAŞLAT / DURDUR
    # ─────────────────────────────────────────

    @staticmethod
    def _hm(t: str) -> tuple:
        """'HH:MM' stringini (hour, minute) tuple'ına çevirir."""
        h, m = t.strip().split(":")
        return int(h), int(m)

    def start(self):
        """
        Tüm görev saatleri config.py'den okunur.
        Değiştirmek için config.py'yi düzenle, botu yeniden başlat.

        config.py'deki ilgili satırlar:
          MORNING_REFRESH_TIME  = "10:05"
          SIGNAL_WINDOW_START   = "17:30"
          ORDER_SEND_TIME       = "17:40"
          EOD_CLOSE_TIME        = "17:55"
          DAY_INCREMENT_TIME    = "18:05"
        """
        t_morning = self._hm(getattr(config, "MORNING_REFRESH_TIME", "10:05"))
        t_window  = self._hm(getattr(config, "SIGNAL_WINDOW_START",  "17:30"))
        t_orders  = self._hm(getattr(config, "ORDER_SEND_TIME",      "17:40"))
        t_eod     = self._hm(getattr(config, "EOD_CLOSE_TIME",       "17:55"))
        t_dayinc  = self._hm(getattr(config, "DAY_INCREMENT_TIME",   "18:05"))

        jobs = [
            ("morning_refresh", t_morning[0], t_morning[1], self._task_morning_refresh,       "Sabah TP/SL"),
            ("open_window",     t_window[0],  t_window[1],  self._task_open_signal_window,    "Pencere Aç"),
            ("send_buy_orders", t_orders[0],  t_orders[1],  self._task_send_buy_orders,       "Alım Emirleri"),
            ("eod_close",       t_eod[0],     t_eod[1],     self._task_end_of_day_close,      "Gün Sonu"),
            ("increment_days",  t_dayinc[0],  t_dayinc[1],  self._task_increment_trading_days,"Gün Sayacı"),
        ]
        for jid, hr, mn, fn, name in jobs:
            self._scheduler.add_job(
                func=fn,
                trigger=CronTrigger(hour=hr, minute=mn, timezone=self.TIMEZONE),
                id=jid, name=name, replace_existing=True,
            )
        self._scheduler.start()

        wstart = getattr(config, "SIGNAL_WINDOW_START", "17:30")
        wend   = getattr(config, "SIGNAL_WINDOW_END",   "17:35")
        self.log.log("INFO", "SCHEDULER_START",
                     f"Zamanlayıcı başlatıldı → "
                     f"{getattr(config, 'MORNING_REFRESH_TIME', '10:05')} TP/SL | "
                     f"{wstart} Pencere Aç | "
                     f"{wend} Pencere Kapat | "
                     f"{getattr(config, 'ORDER_SEND_TIME', '17:40')} Emirler | "
                     f"{getattr(config, 'EOD_CLOSE_TIME', '17:55')} EOD")

    def stop(self):
        self._scheduler.shutdown(wait=False)
        self.log.log("INFO", "SCHEDULER_STOP", "Zamanlayıcı durduruldu.")

    # ─────────────────────────────────────────
    # 10:05 — Sabah TP/SL yenileme
    # ─────────────────────────────────────────

    def _task_morning_refresh(self):
        self.log.log("INFO", "TASK_MORNING", "Sabah TP/SL yenileme başlıyor.")
        try:
            self.client.request_waiting_orders()
            time.sleep(2)
            self.pm.refresh_all_tp_sl_orders(self._open_orders_cache)
            self.log.log("INFO", "TASK_MORNING", "Sabah TP/SL yenileme tamamlandı.")
        except Exception as e:
            self.log.log("ERROR", "TASK_MORNING_ERR", f"Sabah yenileme hatası: {e}")

    # ─────────────────────────────────────────
    # 17:30 — Sinyal toplama penceresini aç
    # ─────────────────────────────────────────

    def _task_open_signal_window(self):
        """
        webhook_server'ın SignalCollector'ını COLLECTING moduna alır.
        17:35'te collector otomatik kapanır ve decide_trade_list() çağrılır.

        Logda göreceğin satır:
          [INFO] WINDOW_OPEN  [17:30] Sinyal toplama penceresi AÇILDI.
        """
        self.log.log("INFO", "TASK_WINDOW",
                     "[17:30] Sinyal toplama penceresi açılıyor. "
                     "TradingView alert'leri /webhook/signalA ve /webhook/signalB'ye gönderilmeli.")
        self.webhook_server.open_collection_window()

    # ─────────────────────────────────────────
    # 17:40 — Mark-to-Market + Alım emirleri
    # ─────────────────────────────────────────

    def _task_send_buy_orders(self):
        """
        Adım 1 — ListPositions (ApiCommands:1):
          Açık pozisyonların LastPx değerleri çekilir.
          → self._current_prices güncellenir
          → webhook_server._live_prices güncellenir (Dashboard MTM)
          → /api/trades ve /api/equity anlık fiyatları yansıtır

        Adım 2 — Alım emirleri:
          Queue'dan sinyal okunur (decide_trade_list sonucu).
          Portföy filtresi uygulanır.
          Bütçe: nakit / bugün_satılan_sayısı (0 ise MAX_POSITIONS fallback)
          ApiCommands:3 emirleri gönderilir.
        """
        # ── Adım 1: MTM fiyatları güncelle ────────────────────────────────
        self.log.log("INFO", "TASK_MTM",
                     "[17:40] ListPositions ile MTM fiyatları güncelleniyor.")
        self.client.request_positions()
        time.sleep(2)   # on_positions_response callback'ini bekle
        self.log.log("INFO", "TASK_MTM",
                     f"MTM fiyatları: {self._current_prices}")

        # ── Adım 2: Queue'dan sinyal al ────────────────────────────────────
        latest = None
        while not self.signal_queue.empty():
            try:
                latest = self.signal_queue.get_nowait()
            except queue.Empty:
                break

        if not latest:
            self.log.log(
                "WARNING", "TASK_BUY_NO_SIGNAL",
                "[17:40] Queue boş. Olası sebepler: "
                "(1) 17:30–17:35 arasında hiç sinyal gelmedi (Durum 3), "
                "(2) İki listede ortak hisse yok, "
                "(3) Filtreden geçen hisse yok.",
            )
            return

        trade_list    = latest.get("symbols", [])
        decision_code = latest.get("decision_code", "?")

        self.log.log(
            "INFO", "TASK_BUY_START",
            f"[17:40] Karar: {decision_code.upper()} | Alınacak: {trade_list}",
            extra_data={"decision": decision_code, "trade_list": trade_list},
        )

        # Portföy filtresi
        selected = self.pm.filter_new_signals(trade_list)
        if not selected:
            self.log.log("INFO", "TASK_BUY_SKIP",
                         "Portföy filtresi sonrası alınacak hisse kalmadı.")
            return

        # Nakit
        available_cash = self._get_available_cash()
        if available_cash <= 0:
            self.log.log("ERROR", "TASK_BUY_NO_CASH",
                         f"Yetersiz nakit: {available_cash:.2f} TL")
            return

        # Bütçe dilimi
        today_sold    = self._today_sold_count()
        budget_per_st = self.pm.calculate_budget_per_stock(available_cash, today_sold)

        # Emirleri gönder
        bought = []
        for symbol in selected:
            try:
                price = self._current_prices.get(symbol)
                if not price or price <= 0:
                    self.log.log("WARNING", "TASK_BUY_NO_PRICE",
                                 f"{symbol} için fiyat yok, atlanıyor.", symbol=symbol)
                    continue

                qty = self.pm.calculate_quantity(budget_per_st, price)
                if qty <= 0:
                    continue

                ok = self.client.send_new_order(
                    symbol=symbol, price=price, quantity=qty,
                    order_side=config.ORDER_SIDE_BUY,
                    order_type=config.ORDER_TYPE,
                    time_in_force=config.TIME_IN_FORCE,
                    transaction_type=config.TRANSACTION_TYPE,
                )

                if ok:
                    pos = self.pm.open_position(symbol=symbol, price=price,
                                                quantity=qty, budget=budget_per_st)
                    if pos:
                        time.sleep(1)
                        self.pm.send_tp_sl_orders(pos)
                        bought.append(symbol)
                        self.log.log(
                            "INFO", "TASK_BUY_OK",
                            f"{symbol} alındı: {qty} lot @ {price:.2f} TL "
                            f"(Bütçe: {budget_per_st:,.2f} TL)",
                            symbol=symbol,
                        )

            except Exception as e:
                self.log.log("ERROR", "TASK_BUY_ERR",
                             f"{symbol} alım hatası: {e}", symbol=symbol)

        if self.webhook_server:
            self.webhook_server.mark_signal_processed(bought)

        self.log.log("INFO", "TASK_BUY_DONE",
                     f"[17:40] Tamamlandı. Alınan: {bought}",
                     extra_data={"bought": bought,
                                 "skipped": [s for s in selected if s not in bought]})

    # ─────────────────────────────────────────
    # 17:55 — Gün sonu kapanış
    # ─────────────────────────────────────────

    def _task_end_of_day_close(self):
        self.log.log("INFO", "TASK_EOD", "Gün sonu kapanış kontrolü.")
        try:
            expired = self.pm.get_expired_positions()
            if not expired:
                self.log.log("INFO", "TASK_EOD", "Süresi dolan pozisyon yok.")
                return
            self.pm.close_expired_positions_eod(self._current_prices)
            self.log.log("INFO", "TASK_EOD",
                         f"{len(expired)} pozisyon gün sonu kapatıldı.")
        except Exception as e:
            self.log.log("ERROR", "TASK_EOD_ERR", f"Gün sonu hatası: {e}")

    # ─────────────────────────────────────────
    # 18:05 — İşlem günü sayacı
    # ─────────────────────────────────────────

    def _task_increment_trading_days(self):
        try:
            self.pm.increment_trading_days()
        except Exception as e:
            self.log.log("ERROR", "TASK_DAY_ERR", f"Gün sayacı hatası: {e}")

    # ─────────────────────────────────────────
    # API CALLBACK'LERİ
    # ─────────────────────────────────────────

    def _setup_api_callbacks(self):

        # ApiCommands:2 — Bekleyen emirler
        def on_orders(data):
            self._open_orders_cache = data.get("OrderApiModels", [])
            self.log.log("INFO", "CB_ORDERS",
                         f"{len(self._open_orders_cache)} bekleyen emir.")
        self.client.register_callback(2, on_orders)

        # ApiCommands:1 — ListPositions (mark-to-market)
        #
        # Döküman: ListPositions yanıtı
        # {
        #   "ApiCommands": 1,
        #   "Positions": [
        #     {"Symbol":"GARAN","LastPx":63.10,"AvgCost":62.20,"Qty":100,...}
        #   ]
        # }
        def on_positions(data):
            positions = data.get("Positions", [])
            if not positions:
                self.log.log("WARNING", "CB_POSITIONS_EMPTY",
                             "ListPositions boş döndü.")
                return
            updated = {}
            for pos in positions:
                sym = pos.get("Symbol", "")
                px  = pos.get("LastPx",  0.0)
                if sym and px:
                    self._current_prices[sym] = px          # Emir fiyat cache
                    self.webhook_server.update_live_price(sym, px)  # Dashboard MTM
                    updated[sym] = px
            self.log.log(
                "INFO", "CB_POSITIONS_MTM",
                f"MTM güncellendi: {updated}",
                extra_data={"prices": updated},
            )
        self.client.register_callback(1, on_positions)

        # ApiCommands:8 — Hesap bakiyesi
        def on_account(data):
            info = data.get("Informations", [])
            cash = self._parse_balance(info)
            if cash is not None:
                self._cached_cash = cash
                self.log.log("INFO", "CB_CASH",
                             f"Nakit güncellendi: {cash:,.2f} TL")
            else:
                self.log.log("WARNING", "CB_CASH_MISS", "BAL kodu bulunamadı.")
        self.client.register_callback(8, on_account)

        # Broadcast — Anlık emir/pozisyon değişiklikleri
        def on_broadcast(data):
            cmd = data.get("ApiCommands")
            if cmd == 3:
                self.pm.handle_order_status_change(data)
            elif cmd == 4:
                sym = data.get("Symbol", "")
                px  = data.get("LastPx", 0.0)
                if sym and px:
                    self._current_prices[sym] = px
                    self.webhook_server.update_live_price(sym, px)
        self.client.set_broadcast_callback(on_broadcast)

    # ─────────────────────────────────────────
    # YARDIMCILAR
    # ─────────────────────────────────────────

    @staticmethod
    def _parse_balance(informations: list) -> float | None:
        for code in ["BAL", "ISL"]:
            for item in informations:
                if item.get("Code", "").upper() == code:
                    try:
                        raw     = item.get("Value", "")
                        cleaned = raw.replace(",", ".")
                        parts   = cleaned.split(".")
                        if len(parts) > 2:
                            cleaned = "".join(parts[:-1]) + "." + parts[-1]
                        return float(cleaned)
                    except (ValueError, AttributeError):
                        continue
        return None

    def _get_available_cash(self) -> float:
        self.client.request_account_info()
        time.sleep(2)
        cash = self._cached_cash
        if cash <= 0:
            self.log.log("ERROR", "CASH_ZERO",
                         "Nakit sıfır — MatriksIQ'da hesap giriş yapılmış mı?")
        else:
            self.log.log("INFO", "CASH_OK", f"Kullanılabilir nakit: {cash:,.2f} TL")
        return cash

    def _today_sold_count(self) -> int:
        session = self.pm.Session()
        try:
            today_start = datetime.combine(date.today(), datetime.min.time())
            return int(
                session.query(Trade)
                .filter(Trade.is_closed == True, Trade.sell_date >= today_start)
                .count()
            )
        except Exception as e:
            self.log.log("WARNING", "SOLD_COUNT_ERR",
                         f"Satış sayısı alınamadı: {e}. MAX_POSITIONS fallback.")
            return 0
        finally:
            session.close()

    def update_price(self, symbol: str, price: float):
        self._current_prices[symbol] = price

    def manual_trigger(self, task_name: str):
        tasks = {
            "morning":    self._task_morning_refresh,
            "open_window":self._task_open_signal_window,
            "buy_orders": self._task_send_buy_orders,
            "eod":        self._task_end_of_day_close,
        }
        if task_name in tasks:
            self.log.log("INFO", "MANUAL", f"Manuel tetikleme: {task_name}")
            tasks[task_name]()
        else:
            self.log.log("WARNING", "MANUAL", f"Bilinmeyen görev: {task_name}")
