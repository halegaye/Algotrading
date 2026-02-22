"""
Portföy Yöneticisi
Pozisyon açma/kapama, TP/SL hesaplama ve bütçe yönetimi.
"""

import json
from datetime import datetime, date
from typing import List, Optional, Dict, Tuple
from sqlalchemy.orm import Session

from database import PortfolioPosition, Trade, ExitReason
import config


class PortfolioManager:
    """
    Portföy durumunu veritabanı üzerinden yönetir.
    Tüm iş mantığı burada toplanır.
    """

    def __init__(self, Session, matriks_client, db_logger):
        self.Session = Session
        self.client = matriks_client
        self.log = db_logger

    # ─────────────────────────────────────────
    # PORTFÖY DURUMU SORGULAMA
    # ─────────────────────────────────────────

    def get_active_positions(self) -> List[PortfolioPosition]:
        """Aktif (açık) tüm pozisyonları döndürür."""
        session = self.Session()
        try:
            return session.query(PortfolioPosition).filter_by(is_active=True).all()
        finally:
            session.close()

    def get_active_position_count(self) -> int:
        """Aktif pozisyon sayısını döndürür."""
        session = self.Session()
        try:
            return session.query(PortfolioPosition).filter_by(is_active=True).count()
        finally:
            session.close()

    def get_available_slots(self) -> int:
        """Kaç yeni pozisyon açılabileceğini döndürür."""
        return max(0, config.MAX_POSITIONS - self.get_active_position_count())

    def get_position_by_symbol(self, symbol: str) -> Optional[PortfolioPosition]:
        """Sembol adına göre aktif pozisyonu bulur."""
        session = self.Session()
        try:
            return session.query(PortfolioPosition).filter_by(
                symbol=symbol, is_active=True
            ).first()
        finally:
            session.close()

    def is_symbol_in_portfolio(self, symbol: str) -> bool:
        """Sembol portföyde var mı?"""
        return self.get_position_by_symbol(symbol) is not None

    # ─────────────────────────────────────────
    # BÜTÇE HESAPLAMA
    # ─────────────────────────────────────────

    def calculate_budget_per_stock(
        self,
        available_cash: float,
        num_new_stocks: int
    ) -> float:
        """
        Mevcut nakit, alınacak hisse sayısına eşit bölünür.
        Örn: 10.000 TL nakit, 2 hisse -> her birine 5.000 TL
        """
        if num_new_stocks <= 0 or available_cash <= 0:
            return 0.0
        budget = available_cash / num_new_stocks
        self.log.log("INFO", "BUDGET_CALC",
                     f"Nakit={available_cash:.2f} TL / {num_new_stocks} hisse = "
                     f"{budget:.2f} TL/hisse")
        return budget

    def calculate_quantity(self, budget: float, price: float) -> float:
        """
        Verilen bütçe ve fiyata göre lot hesaplar.
        BIST'te tam lot olması gerektiğinden floor alınır.
        """
        if price <= 0:
            return 0.0
        qty = budget / price
        return float(int(qty))  # Tam lot

    def calculate_tp_sl_prices(
        self, entry_price: float
    ) -> Tuple[float, float]:
        """
        Giriş fiyatına göre TP ve SL fiyatlarını hesaplar.
        Returns: (take_profit_price, stop_loss_price)
        """
        tp_price = round(entry_price * (1 + config.TAKE_PROFIT_PCT / 100), 4)
        sl_price = round(entry_price * (1 - config.STOP_LOSS_PCT / 100), 4)
        return tp_price, sl_price

    # ─────────────────────────────────────────
    # POZİSYON AÇMA
    # ─────────────────────────────────────────

    def open_position(
        self,
        symbol: str,
        price: float,
        quantity: float,
        budget: float,
        order_id: str = None
    ) -> Optional[PortfolioPosition]:
        """
        Veritabanına yeni pozisyon açar ve Trade kaydı oluşturur.
        Gerçek emir MatriksAPIClient üzerinden ayrıca gönderilmeli.
        """
        if self.is_symbol_in_portfolio(symbol):
            self.log.log("WARNING", "POS_DUPLICATE",
                         f"{symbol} zaten portföyde.", symbol=symbol)
            return None

        tp_price, sl_price = self.calculate_tp_sl_prices(price)

        session = self.Session()
        try:
            position = PortfolioPosition(
                symbol=symbol,
                entry_date=datetime.utcnow(),
                entry_price=price,
                quantity=quantity,
                allocated_budget=budget,
                take_profit_price=tp_price,
                stop_loss_price=sl_price,
                trading_days_held=0,
                is_active=True
            )
            session.add(position)
            session.flush()  # ID al

            trade = Trade(
                position_id=position.id,
                symbol=symbol,
                buy_date=datetime.utcnow(),
                buy_price=price,
                quantity=quantity,
                buy_total=price * quantity,
                buy_order_id=order_id,
                is_closed=False
            )
            session.add(trade)
            session.commit()

            self.log.log("INFO", "POS_OPENED",
                         f"{symbol} pozisyonu açıldı. Fiyat={price} Adet={quantity} "
                         f"TP={tp_price} SL={sl_price}",
                         symbol=symbol,
                         extra_data={"price": price, "qty": quantity,
                                     "tp": tp_price, "sl": sl_price})

            session.refresh(position)
            return position

        except Exception as e:
            session.rollback()
            self.log.log("ERROR", "POS_OPEN_ERROR",
                         f"{symbol} pozisyon açma hatası: {e}", symbol=symbol)
            return None
        finally:
            session.close()

    # ─────────────────────────────────────────
    # POZİSYON KAPATMA
    # ─────────────────────────────────────────

    def close_position(
        self,
        symbol: str,
        sell_price: float,
        exit_reason: ExitReason,
        sell_order_id: str = None
    ) -> Optional[Trade]:
        """
        Pozisyonu kapatır, kar/zarar hesaplar, Trade kaydını günceller.
        """
        session = self.Session()
        try:
            position = session.query(PortfolioPosition).filter_by(
                symbol=symbol, is_active=True
            ).first()

            if not position:
                self.log.log("WARNING", "POS_NOT_FOUND",
                             f"{symbol} aktif pozisyonu bulunamadı.", symbol=symbol)
                return None

            trade = session.query(Trade).filter_by(
                position_id=position.id, is_closed=False
            ).first()

            sell_total = sell_price * position.quantity
            pnl_amount = sell_total - position.buy_total if hasattr(position, 'buy_total') \
                else sell_total - (position.entry_price * position.quantity)
            pnl_percent = ((sell_price - position.entry_price) / position.entry_price) * 100

            if trade:
                trade.sell_date = datetime.utcnow()
                trade.sell_price = sell_price
                trade.sell_total = sell_total
                trade.sell_order_id = sell_order_id
                trade.exit_reason = exit_reason.value
                trade.pnl_amount = round(pnl_amount, 2)
                trade.pnl_percent = round(pnl_percent, 4)
                trade.is_closed = True

            position.is_active = False
            session.commit()

            self.log.log("INFO", "POS_CLOSED",
                         f"{symbol} pozisyonu kapatıldı. "
                         f"SatışFiyatı={sell_price} PnL={pnl_percent:.2f}% "
                         f"Neden={exit_reason.value}",
                         symbol=symbol,
                         extra_data={"sell_price": sell_price,
                                     "pnl_pct": pnl_percent,
                                     "reason": exit_reason.value})
            return trade

        except Exception as e:
            session.rollback()
            self.log.log("ERROR", "POS_CLOSE_ERROR",
                         f"{symbol} pozisyon kapama hatası: {e}", symbol=symbol)
            return None
        finally:
            session.close()

    # ─────────────────────────────────────────
    # İŞLEM GÜNÜNÜ GÜNCELLEME
    # ─────────────────────────────────────────

    def increment_trading_days(self):
        """
        Her işlem günü sonunda çağrılır.
        Tüm aktif pozisyonların tutulma gün sayısını 1 artırır.
        """
        session = self.Session()
        try:
            positions = session.query(PortfolioPosition).filter_by(is_active=True).all()
            for pos in positions:
                pos.trading_days_held += 1
            session.commit()
            self.log.log("INFO", "TRADING_DAYS_INC",
                         f"{len(positions)} pozisyonun tutulma günü artırıldı.")
        except Exception as e:
            session.rollback()
            self.log.log("ERROR", "TRADING_DAYS_ERROR", f"Gün artırma hatası: {e}")
        finally:
            session.close()

    def get_expired_positions(self) -> List[PortfolioPosition]:
        """
        Maksimum tutulma süresini aşmış pozisyonları döndürür.
        """
        session = self.Session()
        try:
            return session.query(PortfolioPosition).filter(
                PortfolioPosition.is_active == True,
                PortfolioPosition.trading_days_held >= config.MAX_HOLD_DAYS
            ).all()
        finally:
            session.close()

    # ─────────────────────────────────────────
    # SINYAL FİLTRELEME
    # ─────────────────────────────────────────

    def filter_new_signals(self, intersection_symbols: List[str]) -> List[str]:
        """
        Çift sinyal kesişiminden gelen listeyi portföy durumuna göre filtreler.
        Bu metot NewOrder (ApiCommands: 3) sürecine GİRMEDEN HEMEN ÖNCE çalışır.

        Uygulanan kurallar (sırayla):
        ┌──────────────────────────────────────────────────────────────────┐
        │ 1. Zaten portföyde olan hisseler listeden çıkarılır.             │
        │ 2. Kesişim listesi boş slot sayısıyla kırpılır.                  │
        │    Örnek: 2 boş slot, 3 kesişim → sadece ilk 2 alınır.          │
        │ 3. Kesişimde sadece 1 hisse varsa, sadece o 1 alınır.            │
        │    Diğer slot nakit olarak bırakılır (zorla doldurulmaz).        │
        │ 4. Kesişim tamamen boşsa işlem yapılmaz.                         │
        └──────────────────────────────────────────────────────────────────┘

        Args:
            intersection_symbols: SignalBuffer'ın hesapladığı kesişim listesi.
                                  Sırası Kaynak A'nın orijinal sırasına göre korunur.

        Returns:
            Alım emri gönderilecek sembol listesi (boş olabilir).
        """
        # ── 1. Boş slot kontrolü ─────────────────────────────────────────
        available_slots = self.get_available_slots()

        if available_slots == 0:
            self.log.log(
                "INFO", "SIGNAL_FILTER",
                "Portföy dolu (boş slot yok). Kesişim listesi işlenmeyecek.",
                extra_data={"intersection": intersection_symbols}
            )
            return []

        if not intersection_symbols:
            self.log.log(
                "INFO", "SIGNAL_FILTER",
                "Kesişim listesi boş. O gün işlem yapılmayacak."
            )
            return []

        # ── 2. Portföyde zaten olanları çıkar ────────────────────────────
        filtered = []
        for symbol in intersection_symbols:
            if self.is_symbol_in_portfolio(symbol):
                self.log.log(
                    "INFO", "SIGNAL_SKIP",
                    f"{symbol} zaten portföyde, atlaniyor.",
                    symbol=symbol
                )
            else:
                filtered.append(symbol)

        if not filtered:
            self.log.log(
                "INFO", "SIGNAL_FILTER",
                "Kesisimdeki tüm hisseler zaten portföyde. Yeni pozisyon açılmayacak.",
                extra_data={"intersection": intersection_symbols}
            )
            return []

        # ── 3. Boş slot kadar kırp (örn: 2 slot, 3 hisse → ilk 2) ───────
        # Kural: Kesişimde 1 hisse varsa sadece o alınır, slot zorla doldurulmaz.
        # Kural: Kesişimde slot'tan fazla hisse varsa ilk N tanesi alınır.
        selected = filtered[:available_slots]

        self.log.log(
            "INFO", "SIGNAL_FILTER",
            f"Kesisim={intersection_symbols} | "
            f"Portföyde_olmayan={filtered} | "
            f"Bos_slot={available_slots} | "
            f"Secilen={selected} "
            f"({'slot dolduruldu' if len(filtered) >= available_slots else 'nakit bosluk birakiliyor'})",
            extra_data={
                "intersection":  intersection_symbols,
                "after_portfolio_filter": filtered,
                "available_slots": available_slots,
                "selected":      selected,
                "cash_gap":      available_slots - len(selected),
            }
        )

        return selected

    # ─────────────────────────────────────────
    # TP/SL EMİRLERİ
    # ─────────────────────────────────────────

    def send_tp_sl_orders(self, position: PortfolioPosition) -> bool:
        """
        Bir pozisyon için şartlı Kar Al (TP) ve Zarar Durdur (SL) satış emirleri gönderir.

        Döküman Sayfa 14 - Yeni Emir İsteği parametrelerine göre:
        ┌─────────────────┬──────────────────────────────────────────────────────┐
        │ Alan            │ Açıklama                                             │
        ├─────────────────┼──────────────────────────────────────────────────────┤
        │ OrderType       │ "2" = Limit emir (fiyat belirtilmiş satış)           │
        │ StopPx          │ Şart fiyatı — bu fiyata ulaşınca emir devreye girer  │
        │ Price           │ Gerçek işlem fiyatı (limit seviyesi)                 │
        │ TimeInForce     │ "1" = İptale kadar geçerli (GoodTillCancel)          │
        └─────────────────┴──────────────────────────────────────────────────────┘

        TP Mantığı:
          StopPx = take_profit_price  → fiyat bu seviyeye çıkınca tetiklenir
          Price  = take_profit_price  → limit fiyat (aynı seviyeden sat)

        SL Mantığı:
          StopPx = stop_loss_price    → fiyat bu seviyeye düşünce tetiklenir
          Price  = stop_loss_price    → limit fiyat (kaymayı sınırlamak için SL'den biraz altı)
          SL'de küçük bir slippage payı bırakılır (Price = SL * 0.995) ki emir dolsun.
        """

        # ── TAKE PROFIT EMRİ ──────────────────────────────────────────────────
        # StopPx: TP seviyesine ulaşınca şart devreye girer
        # Price:  Aynı fiyattan limit satış
        tp_sent = self.client.send_new_order(
            symbol=position.symbol,
            price=position.take_profit_price,
            quantity=position.quantity,
            order_side=config.ORDER_SIDE_SELL,
            order_type="2",                          # Limit emir
            time_in_force="1",                       # GoodTillCancel — gün sonunda silinmez
            transaction_type=config.TRANSACTION_TYPE,
            stop_px=position.take_profit_price       # Şart fiyatı = TP fiyatı
        )

        # ── STOP LOSS EMRİ ───────────────────────────────────────────────────
        # StopPx: SL seviyesine düşünce şart devreye girer
        # Price:  SL'den %0.5 aşağı → emir mutlaka dolsun (slippage payı)
        sl_limit_price = round(position.stop_loss_price * 0.995, 4)

        sl_sent = self.client.send_new_order(
            symbol=position.symbol,
            price=sl_limit_price,
            quantity=position.quantity,
            order_side=config.ORDER_SIDE_SELL,
            order_type="2",                          # Limit emir
            time_in_force="1",                       # GoodTillCancel
            transaction_type=config.TRANSACTION_TYPE,
            stop_px=position.stop_loss_price         # Şart fiyatı = SL fiyatı
        )

        status_tp = "✓" if tp_sent else "✗"
        status_sl = "✓" if sl_sent else "✗"

        self.log.log(
            "INFO", "TPSL_SENT",
            f"{position.symbol} şartlı emirler → "
            f"TP[{status_tp}] StopPx={position.take_profit_price} Limit={position.take_profit_price} | "
            f"SL[{status_sl}] StopPx={position.stop_loss_price} Limit={sl_limit_price}",
            symbol=position.symbol,
            extra_data={
                "tp_stop_px": position.take_profit_price,
                "tp_limit":   position.take_profit_price,
                "sl_stop_px": position.stop_loss_price,
                "sl_limit":   sl_limit_price,
                "qty":        position.quantity
            }
        )
        return tp_sent and sl_sent

    def refresh_all_tp_sl_orders(self, open_orders: List[dict]):
        """
        Sabah rutini: Tüm açık pozisyonların TP/SL emirlerini yeniler.
        Önce mevcut emirleri iptal et, sonra yeniden gönder.
        """
        active_positions = self.get_active_positions()
        self.log.log("INFO", "TPSL_REFRESH",
                     f"{len(active_positions)} pozisyon için TP/SL yenileniyor.")

        for position in active_positions:
            # Pozisyona ait bekleyen emirleri iptal et
            for order in open_orders:
                if order.get("Symbol") == position.symbol:
                    self.client.send_cancel_order(order)
                    self.log.log("INFO", "ORDER_CANCEL",
                                 f"{position.symbol} eski emir iptal edildi.",
                                 symbol=position.symbol)

            # Yeni TP/SL gönder
            self.send_tp_sl_orders(position)

    # ─────────────────────────────────────────
    # GÜN SONU KAPANIŞ
    # ─────────────────────────────────────────

    def close_expired_positions_eod(self, current_prices: Dict[str, float]):
        """
        2 günü dolmuş pozisyonları piyasa fiyatından kapatır.
        current_prices: {sembol: güncel_fiyat} dict'i
        """
        expired = self.get_expired_positions()
        self.log.log("INFO", "EOD_CHECK",
                     f"Süresi dolan {len(expired)} pozisyon gün sonu kapatılacak.")

        for position in expired:
            current_price = current_prices.get(position.symbol)
            if not current_price:
                self.log.log("WARNING", "EOD_NO_PRICE",
                             f"{position.symbol} için güncel fiyat bulunamadı.",
                             symbol=position.symbol)
                # Piyasa fiyatından (0 fiyat ile) emir gönder - aracı kurum anlık fiyattan işler
                current_price = 0.0

            # Piyasa satış emri gönder
            self.client.send_new_order(
                symbol=position.symbol,
                price=current_price,
                quantity=position.quantity,
                order_side=config.ORDER_SIDE_SELL,
                order_type="1",             # 1 = Piyasa emri
                time_in_force=config.TIME_IN_FORCE,
                transaction_type=config.TRANSACTION_TYPE
            )

            # Pozisyonu DB'de kapat (fiyat gerçek satış sonrası güncellenir)
            self.close_position(
                symbol=position.symbol,
                sell_price=current_price,
                exit_reason=ExitReason.TIME_LIMIT
            )

    # ─────────────────────────────────────────
    # BROADCAST HANDLER (Durum değişiklikleri)
    # ─────────────────────────────────────────

    def handle_order_status_change(self, data: dict):
        """
        MatriksIQ'dan gelen emir durum değişikliklerini işler.
        OrdStatus '2' = Gerçekleşti
        """
        ord_status = data.get("OrdStatus", "")
        symbol = data.get("Symbol", "")
        order_side = data.get("OrderSide", -1)
        avg_px = data.get("AvgPx", 0.0)
        filled_qty = data.get("FilledQty", 0.0)

        if ord_status == "2":  # Gerçekleşti
            if order_side == config.ORDER_SIDE_SELL - 1:  # OrderSide SELL=2, Side SELL=1
                position = self.get_position_by_symbol(symbol)
                if position:
                    # Hangi emir gerçekleşti? TP mi SL mi?
                    if avg_px >= position.take_profit_price * 0.99:
                        reason = ExitReason.TAKE_PROFIT
                    elif avg_px <= position.stop_loss_price * 1.01:
                        reason = ExitReason.STOP_LOSS
                    else:
                        reason = ExitReason.MANUAL

                    self.close_position(
                        symbol=symbol,
                        sell_price=avg_px,
                        exit_reason=reason,
                        sell_order_id=data.get("OrderID")
                    )
