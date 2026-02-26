"""
Veritabanı modelleri - SQLAlchemy ORM

Dashboard uyumluluğu:
  Trade tablosu React Dashboard'un beklediği alanlarla birebir eşleştirilmiştir.
  Dashboard Trade tipi:
    { symbol, buyAt (ISO), sellAt (ISO), buyPrice, sellPrice }
  API'de pnlPct hesaplanıp eklenir.

Alan eşleştirmesi:
  DB: buy_date    ↔  Dashboard: buyAt   (ISO format: YYYY-MM-DDTHH:mm)
  DB: sell_date   ↔  Dashboard: sellAt
  DB: buy_price   ↔  Dashboard: buyPrice
  DB: sell_price  ↔  Dashboard: sellPrice
  DB: pnl_percent ↔  Dashboard: pnlPct (hesaplanmış, float)
"""

from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Boolean, Text, ForeignKey
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
import enum

Base = declarative_base()


class ExitReason(str, enum.Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS   = "stop_loss"
    TIME_LIMIT  = "time_limit"
    MANUAL      = "manual"


class PortfolioPosition(Base):
    """Açık pozisyonlar."""
    __tablename__ = "portfolio"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    symbol            = Column(String(20), nullable=False, unique=True, index=True)
    entry_date        = Column(DateTime, nullable=False, default=datetime.utcnow)
    entry_price       = Column(Float, nullable=False)
    quantity          = Column(Float, nullable=False)
    allocated_budget  = Column(Float, nullable=False)
    take_profit_price = Column(Float, nullable=True)
    stop_loss_price   = Column(Float, nullable=True)
    tp_order_id       = Column(String(50), nullable=True)
    sl_order_id       = Column(String(50), nullable=True)
    trading_days_held = Column(Integer, default=0)
    is_active         = Column(Boolean, default=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    trades = relationship("Trade", back_populates="position")

    def __repr__(self):
        return f"<Position {self.symbol} @ {self.entry_price} x {self.quantity}>"


class Trade(Base):
    """
    Tamamlanan işlemlerin kaydı.

    Dashboard /api/trades endpoint'i bu tabloyu okur ve şu formata dönüştürür:
    {
      "2026-01-05": [
        {
          "symbol":   "THYAO",
          "buyAt":    "2026-01-04T10:12",   ← buy_date ISO (dakika hassasiyeti)
          "sellAt":   "2026-01-05T11:05",   ← sell_date ISO
          "buyPrice":  245.5,               ← buy_price
          "sellPrice": 251.0,               ← sell_price
          "pnlPct":    2.24                 ← pnl_percent
        },
        ...
      ]
    }
    """
    __tablename__ = "trades"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    position_id   = Column(Integer, ForeignKey("portfolio.id"), nullable=True)
    symbol        = Column(String(20), nullable=False, index=True)

    # ── Alış — Dashboard: buyAt, buyPrice ────────────────────────────────
    buy_date      = Column(DateTime, nullable=False)        # → buyAt  (ISO YYYY-MM-DDTHH:mm)
    buy_price     = Column(Float,    nullable=False)        # → buyPrice
    quantity      = Column(Float,    nullable=False)
    buy_total     = Column(Float,    nullable=False)        # buy_price * quantity (TL)
    buy_order_id  = Column(String(50), nullable=True)

    # ── Satış — Dashboard: sellAt, sellPrice ─────────────────────────────
    sell_date     = Column(DateTime, nullable=True)         # → sellAt (ISO YYYY-MM-DDTHH:mm)
    sell_price    = Column(Float,    nullable=True)         # → sellPrice
    sell_total    = Column(Float,    nullable=True)         # sell_price * quantity (TL)
    sell_order_id = Column(String(50), nullable=True)

    # ── Sonuç — Dashboard: pnlPct ────────────────────────────────────────
    exit_reason   = Column(String(20), nullable=True)       # ExitReason string
    pnl_amount    = Column(Float,    nullable=True)         # Net TL kar/zarar
    pnl_percent   = Column(Float,    nullable=True)         # → pnlPct (yüzde, float)

    is_closed     = Column(Boolean, default=False)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    position = relationship("PortfolioPosition", back_populates="trades")

    # ── Dashboard serileştirme yardımcıları ──────────────────────────────

    @staticmethod
    def _iso_min(dt: datetime | None) -> str | None:
        """datetime → 'YYYY-MM-DDTHH:mm' (Dashboard beklediği format)."""
        if dt is None:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M")

    def to_dashboard_trade(self) -> dict:
        """
        Dashboard Trade tipine dönüştür.
        sellAt None ise (henüz kapanmamış) çağırma — /api/trades yalnızca
        is_closed=True kayıtları döndürür.
        """
        return {
            "symbol":    self.symbol,
            "buyAt":     self._iso_min(self.buy_date),
            "sellAt":    self._iso_min(self.sell_date),
            "buyPrice":  self.buy_price,
            "sellPrice": self.sell_price,
            "pnlPct":    round(self.pnl_percent, 4) if self.pnl_percent is not None else None,
            "exitReason": self.exit_reason,
        }

    def sell_date_key(self) -> str | None:
        """Satış tarihini 'YYYY-MM-DD' olarak döndür (gruplama anahtarı)."""
        if self.sell_date is None:
            return None
        return self.sell_date.strftime("%Y-%m-%d")

    def __repr__(self):
        pnl = f"{self.pnl_percent:.2f}%" if self.pnl_percent is not None else "open"
        return f"<Trade {self.symbol} PnL={pnl}>"


class Signal(Base):
    """TradingView sinyal logları."""
    __tablename__ = "signals"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    received_at    = Column(DateTime, default=datetime.utcnow)
    raw_payload    = Column(Text, nullable=True)
    symbols        = Column(Text, nullable=True)
    processed      = Column(Boolean, default=False)
    symbols_bought = Column(Text, nullable=True)
    notes          = Column(Text, nullable=True)

    def __repr__(self):
        return f"<Signal {self.received_at} symbols={self.symbols}>"


class BotLog(Base):
    """Bot operasyonel logları."""
    __tablename__ = "bot_logs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    timestamp  = Column(DateTime, default=datetime.utcnow)
    level      = Column(String(10), nullable=False)
    event      = Column(String(100), nullable=False)
    message    = Column(Text, nullable=True)
    symbol     = Column(String(20), nullable=True)
    extra_data = Column(Text, nullable=True)

    def __repr__(self):
        return f"<BotLog {self.timestamp} [{self.level}] {self.event}>"


def init_db(database_url: str):
    """Veritabanını başlatır, tabloları oluşturur veya günceller."""
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def get_session(Session):
    return Session()
