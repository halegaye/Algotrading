"""
Görev Zamanlayıcı
APScheduler ile periyodik ve saatlik görevleri yönetir.
"""

import threading
import queue
from datetime import datetime, time as dtime
from typing import Optional, Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import config
from database import ExitReason


class TaskScheduler:
    """
    Trading bot görevlerini zamanlar:
    - 08:05: Sabah TP/SL yenileme
    - 17:30: TradingView sinyal bekleme penceresi
    - 17:40: Yeni alım emirlerini gönder
    - 17:55: Gün sonu 2-gün sınırı kapanışları
    """

    TIMEZONE = pytz.timezone("Europe/Istanbul")

    def __init__(
        self,
        portfolio_manager,
        matriks_client,
        signal_queue: queue.Queue,
        db_logger,
        webhook_server
    ):
        self.pm = portfolio_manager
        self.client = matriks_client
        self.signal_queue = signal_queue
        self.log = db_logger
        self.webhook_server = webhook_server

        self._scheduler = BackgroundScheduler(timezone=self.TIMEZONE)
        self._pending_signals: List[str] = []       # 17:30'da alınan semboller
        self._open_orders_cache: List[dict] = []    # Bekleyen emirler cache'i
        self._current_prices: Dict[str, float] = {} # Güncel fiyatlar

        # MatriksIQ callback kayıtları
        self._setup_api_callbacks()

    # ─────────────────────────────────────────
    # ZAMANLAYICI KURULUMU
    # ─────────────────────────────────────────

    def start(self):
        """Tüm zamanlanmış görevleri başlatır."""

        # 08:05 - Sabah TP/SL yenileme
        self._scheduler.add_job(
            func=self._task_morning_refresh,
            trigger=CronTrigger(hour=8, minute=5, timezone=self.TIMEZONE),
            id="morning_refresh",
            name="Sabah TP/SL Yenileme",
            replace_existing=True
        )

        # 17:30 - Sinyal işleme penceresi (queue'yu oku)
        self._scheduler.add_job(
            func=self._task_process_signals,
            trigger=CronTrigger(hour=17, minute=30, timezone=self.TIMEZONE),
            id="signal_process",
            name="Sinyal İşleme",
            replace_existing=True
        )

        # 17:40 - Alım emirlerini gönder
        self._scheduler.add_job(
            func=self._task_send_buy_orders,
            trigger=CronTrigger(hour=17, minute=40, timezone=self.TIMEZONE),
            id="send_buy_orders",
            name="Alım Emirleri",
            replace_existing=True
        )

        # 17:55 - Gün sonu kapanışları (2-gün sınırı)
        self._scheduler.add_job(
            func=self._task_end_of_day_close,
            trigger=CronTrigger(hour=17, minute=55, timezone=self.TIMEZONE),
            id="eod_close",
            name="Gün Sonu Kapanış",
            replace_existing=True
        )

        # Her gün 18:05'te işlem günü sayacını artır
        self._scheduler.add_job(
            func=self._task_increment_trading_days,
            trigger=CronTrigger(hour=18, minute=5, timezone=self.TIMEZONE),
            id="increment_days",
            name="İşlem Günü Sayacı",
            replace_existing=True
        )

        self._scheduler.start()
        self.log.log("INFO", "SCHEDULER_START", "Görev zamanlayıcı başlatıldı.")

    def stop(self):
        """Zamanlayıcıyı durdurur."""
        self._scheduler.shutdown(wait=False)
        self.log.log("INFO", "SCHEDULER_STOP", "Görev zamanlayıcı durduruldu.")

    # ─────────────────────────────────────────
    # GÖREV TANIMLARI
    # ─────────────────────────────────────────

    def _task_morning_refresh(self):
        """
        08:05 görevi: Tüm açık pozisyonların TP/SL emirlerini yeniler.
        Borsada günlük emirler silindiği için sabah tekrar gönderilmesi gerekir.
        """
        self.log.log("INFO", "TASK_MORNING_REFRESH", "Sabah TP/SL yenileme başlıyor.")
        try:
            # Bekleyen emirleri çek
            self.client.request_waiting_orders()
            # Callback'te gelen emirler _open_orders_cache'e yazılacak
            import time; time.sleep(2)  # API yanıtını bekle

            self.pm.refresh_all_tp_sl_orders(self._open_orders_cache)
            self.log.log("INFO", "TASK_MORNING_REFRESH", "Sabah TP/SL yenileme tamamlandı.")
        except Exception as e:
            self.log.log("ERROR", "TASK_MORNING_ERROR", f"Sabah yenileme hatası: {e}")

    def _task_process_signals(self):
        """
        17:30 görevi: Queue'daki sinyalleri okur ve filtreleyerek saklar.
        """
        self.log.log("INFO", "TASK_SIGNAL_PROCESS", "Sinyal işleme başlıyor.")
        self._pending_signals = []

        # Queue'daki en son sinyali al
        latest_signal = None
        while not self.signal_queue.empty():
            try:
                latest_signal = self.signal_queue.get_nowait()
            except queue.Empty:
                break

        if not latest_signal:
            self.log.log("WARNING", "TASK_NO_SIGNAL",
                         "17:30'da işlenecek sinyal bulunamadı.")
            return

        incoming_symbols = latest_signal.get("symbols", [])
        self.log.log("INFO", "TASK_SIGNAL_PROCESS",
                     f"Gelen semboller: {incoming_symbols}")

        # Portföy boşluğuna göre filtrele
        selected = self.pm.filter_new_signals(incoming_symbols)
        self._pending_signals = selected

        self.log.log("INFO", "TASK_SIGNAL_PROCESS",
                     f"17:40'ta alınacak semboller: {self._pending_signals}")

    def _task_send_buy_orders(self):
        """
        17:40 görevi: Filtrelenmiş semboller için alım emirlerini gönderir.
        """
        if not self._pending_signals:
            self.log.log("INFO", "TASK_BUY_SKIP", "Gönderilecek sinyal yok.")
            return

        self.log.log("INFO", "TASK_BUY_START",
                     f"Alım emirleri gönderiliyor: {self._pending_signals}")

        # Mevcut nakit bilgisini çek
        available_cash = self._get_available_cash()

        if available_cash <= 0:
            self.log.log("ERROR", "TASK_BUY_NO_CASH",
                         f"Yetersiz nakit: {available_cash:.2f} TL")
            return

        # O gün kaç hisse satıldığını bul → dilim hesabının temeli
        today_sold_count = self._today_sold_count()

        self.log.log(
            "INFO", "TASK_BUY_SOLD_COUNT",
            f"Bugun satilan hisse sayisi: {today_sold_count} "
            f"({'fallback: MAX_POSITIONS kullanilacak' if today_sold_count == 0 else 'bu sayi bolunen olacak'})",
            extra_data={"today_sold": today_sold_count,
                        "pending":    self._pending_signals}
        )

        budget_per_stock = self.pm.calculate_budget_per_stock(available_cash, today_sold_count)

        bought_symbols = []
        for symbol in self._pending_signals:
            try:
                # Güncel fiyatı al (önceki cache veya API'den)
                current_price = self._current_prices.get(symbol)
                if not current_price or current_price <= 0:
                    self.log.log("WARNING", "TASK_BUY_NO_PRICE",
                                 f"{symbol} için fiyat bilgisi yok, atlanıyor.",
                                 symbol=symbol)
                    continue

                quantity = self.pm.calculate_quantity(budget_per_stock, current_price)
                if quantity <= 0:
                    self.log.log("WARNING", "TASK_BUY_QTY_ZERO",
                                 f"{symbol} için lot hesaplanamadı.", symbol=symbol)
                    continue

                # Alım emri gönder
                success = self.client.send_new_order(
                    symbol=symbol,
                    price=current_price,
                    quantity=quantity,
                    order_side=config.ORDER_SIDE_BUY,
                    order_type=config.ORDER_TYPE,
                    time_in_force=config.TIME_IN_FORCE,
                    transaction_type=config.TRANSACTION_TYPE
                )

                if success:
                    # DB'ye pozisyon aç (gerçek emir karşılanınca da güncellenebilir)
                    position = self.pm.open_position(
                        symbol=symbol,
                        price=current_price,
                        quantity=quantity,
                        budget=budget_per_stock
                    )

                    if position:
                        # TP/SL emirlerini hemen gönder
                        import time; time.sleep(1)
                        self.pm.send_tp_sl_orders(position)
                        bought_symbols.append(symbol)

            except Exception as e:
                self.log.log("ERROR", "TASK_BUY_ERROR",
                             f"{symbol} alım emri hatası: {e}", symbol=symbol)

        # Webhook sunucusuna bildirimleri kaydet
        if self.webhook_server:
            self.webhook_server.mark_signal_processed(bought_symbols)

        self._pending_signals = []
        self.log.log("INFO", "TASK_BUY_DONE",
                     f"Alım emirleri tamamlandı: {bought_symbols}")

    def _task_end_of_day_close(self):
        """
        17:55 görevi: 2 işlem günü sınırını aşmış pozisyonları kapatır.
        """
        self.log.log("INFO", "TASK_EOD_START", "Gün sonu kapanış kontrolü başlıyor.")
        try:
            expired = self.pm.get_expired_positions()
            if not expired:
                self.log.log("INFO", "TASK_EOD_NO_EXPIRED", "Süresi dolan pozisyon yok.")
                return

            self.pm.close_expired_positions_eod(self._current_prices)
            self.log.log("INFO", "TASK_EOD_DONE",
                         f"{len(expired)} pozisyon gün sonu kapatıldı.")
        except Exception as e:
            self.log.log("ERROR", "TASK_EOD_ERROR", f"Gün sonu kapanış hatası: {e}")

    def _task_increment_trading_days(self):
        """18:05 görevi: İşlem günü sayacını artırır."""
        try:
            self.pm.increment_trading_days()
        except Exception as e:
            self.log.log("ERROR", "TASK_DAY_INC_ERROR", f"Gün artırma hatası: {e}")

    # ─────────────────────────────────────────
    # API CALLBACK KURULUMU
    # ─────────────────────────────────────────

    def _setup_api_callbacks(self):
        """MatriksIQ API callback'lerini kaydeder."""

        # Emirler sorgusu yanıtı (ApiCommands: 2)
        def on_orders_response(data: dict):
            orders = data.get("OrderApiModels", [])
            self._open_orders_cache = orders
            self.log.log("INFO", "API_ORDERS_RECEIVED",
                         f"{len(orders)} bekleyen emir alındı.")

        # ── HESAP BİLGİLERİ YANITI (ApiCommands: 8) ──────────────────────────
        # Döküman Sayfa 25 — Hesap Bilgileri Sorgusu çıktısı:
        # {
        #   "Informations": [
        #     { "Code": "BAL", "Key": "Bakiye",       "Value": "9574528.62", "DataType": 2 },
        #     { "Code": "ISL", "Key": "Islem limiti", "Value": "9574528.62", "DataType": 2 },
        #     { "Code": "OAL", "Key": "Overall",      "Value": "10620184",   "DataType": 2 },
        #     ...
        #   ],
        #   "ApiCommands": 8
        # }
        # "Code": "BAL" → kullanılabilir nakit bakiyesini verir.
        def on_account_info(data: dict):
            informations = data.get("Informations", [])
            if not informations:
                self.log.log("WARNING", "API_ACCT_EMPTY",
                             "Hesap bilgileri boş döndü.")
                return

            parsed_cash = self._parse_balance_from_info(informations)
            if parsed_cash is not None:
                self._cached_cash = parsed_cash
                self.log.log(
                    "INFO", "API_CASH_UPDATED",
                    f"Kullanılabilir nakit güncellendi: {parsed_cash:,.2f} TL",
                    extra_data={"balance": parsed_cash}
                )
            else:
                self.log.log("WARNING", "API_BAL_NOT_FOUND",
                             "Hesap bilgilerinde 'BAL' kodu bulunamadı. "
                             "Mevcut nakit değeri korunuyor.")

        # Emir/Pozisyon durum değişiklikleri — broadcast (sorgu yapılmadan gelen)
        def on_broadcast(data: dict):
            api_cmd = data.get("ApiCommands")
            if api_cmd == 3:    # Emir durum değişikliği
                self.pm.handle_order_status_change(data)
            elif api_cmd == 4:  # Pozisyon durum değişikliği
                symbol = data.get("Symbol", "")
                last_px = data.get("LastPx", 0.0)
                if symbol and last_px:
                    self._current_prices[symbol] = last_px

        self.client.register_callback(2, on_orders_response)
        self.client.register_callback(8, on_account_info)   # Hesap bilgileri
        self.client.set_broadcast_callback(on_broadcast)

    # ─────────────────────────────────────────
    # BAKİYE PARSE YARDIMCISI
    # ─────────────────────────────────────────

    @staticmethod
    def _parse_balance_from_info(informations: list) -> float | None:
        """
        Döküman Sayfa 25 — Hesap bilgileri yanıtındaki Informations listesini tarar.

        Öncelik sırası:
          1. Code == "BAL"  → Kullanılabilir nakit bakiye (birincil kaynak)
          2. Code == "ISL"  → İşlem limiti (BAL yoksa fallback)

        DataType == 2 olan alanlar sayısal değer içerir (string olarak gelir).

        Returns:
            float — bulunan bakiye değeri
            None  — hiçbir eşleşme bulunamazsa
        """
        priority_codes = ["BAL", "ISL"]   # ISL = işlem limiti, BAL yoksa kullan

        # Önce tam kod eşleşmesi ara
        for code in priority_codes:
            for item in informations:
                if item.get("Code", "").upper() == code:
                    raw_value = item.get("Value", "")
                    try:
                        # Gelen değer string: "9574528.62"
                        # Türkçe format varsa (nokta=binlik, virgül=ondalık) normalize et
                        cleaned = raw_value.replace(",", ".")
                        # Eğer birden fazla nokta varsa binlik ayracı temizle
                        parts = cleaned.split(".")
                        if len(parts) > 2:
                            # "9.574.528.62" → "9574528.62"
                            cleaned = "".join(parts[:-1]) + "." + parts[-1]
                        return float(cleaned)
                    except (ValueError, AttributeError):
                        continue  # Parse edilemedi, bir sonraki kodu dene

        return None

    # ─────────────────────────────────────────
    # YARDIMCI METODLAR
    # ─────────────────────────────────────────

    def _today_sold_count(self) -> int:
        """
        Bugün kapatılan (satılan) pozisyon sayısını veritabanından okur.

        Bu sayı dilim hesabının böleni olur:
          dilim = toplam_nakit / today_sold_count

        Eğer bugün hiç satış yoksa (bot yeni başlatıldı, portföy boştu)
        0 döner → calculate_budget_per_stock MAX_POSITIONS fallback'e geçer.
        """
        from database import Trade
        from datetime import date
        import sqlalchemy

        session = self.pm.Session()
        try:
            today_start = datetime.combine(date.today(), datetime.min.time())
            count = (
                session.query(Trade)
                .filter(
                    Trade.is_closed == True,
                    Trade.sell_date >= today_start,
                )
                .count()
            )
            return int(count)
        except Exception as e:
            self.log.log("WARNING", "SOLD_COUNT_ERROR",
                         f"Bugun satilan hisse sayisi alinamadi: {e}. "
                         f"Fallback: MAX_POSITIONS kullanilacak.")
            return 0
        finally:
            session.close()

    def _get_available_cash(self) -> float:
        """
        Kullanılabilir nakiti döndürür.

        Döküman Sayfa 25 — Hesap Bilgileri Sorgusu (ApiCommands: 7) tetiklenir.
        Yanıt on_account_info callback'ine düşer ve _cached_cash güncellenir.
        Callback tamamlanana kadar kısa bir bekleme uygulanır.

        Fallback: API'den değer gelmezse son önbellek değeri kullanılır.
        İlk başlatmada önbellek sıfır ise uyarı verilir.
        """
        import time

        # Güncel bakiyeyi çek
        self.client.request_account_info()
        time.sleep(2)   # API yanıtını bekle (callback _cached_cash'i doldurur)

        cash = getattr(self, "_cached_cash", 0.0)

        if cash <= 0:
            self.log.log(
                "ERROR", "API_NO_CASH",
                "Kullanılabilir nakit sıfır veya alınamadı. "
                "Alım emirleri gönderilemeyecek. "
                "MatriksIQ'da hesabın giriş yapılmış olduğunu kontrol edin."
            )
        else:
            self.log.log(
                "INFO", "API_CASH_READ",
                f"Kullanılabilir nakit: {cash:,.2f} TL"
            )

        return cash

    def update_price(self, symbol: str, price: float):
        """Harici kaynaktan fiyat günceller."""
        self._current_prices[symbol] = price

    def manual_trigger(self, task_name: str):
        """Belirtilen görevi manuel olarak tetikler (test için)."""
        tasks = {
            "morning_refresh": self._task_morning_refresh,
            "process_signals": self._task_process_signals,
            "send_buy_orders": self._task_send_buy_orders,
            "eod_close": self._task_end_of_day_close,
        }
        if task_name in tasks:
            self.log.log("INFO", "MANUAL_TRIGGER", f"Manuel tetikleme: {task_name}")
            tasks[task_name]()
        else:
            self.log.log("WARNING", "MANUAL_TRIGGER",
                         f"Bilinmeyen görev: {task_name}. "
                         f"Geçerli görevler: {list(tasks.keys())}")
