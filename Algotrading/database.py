"""
Veritabanı modelleri - SQLAlchemy ORM
"""

from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Boolean, Text, ForeignKey, Enum
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
import enum

Base = declarative_base()


class ExitReason(str, enum.Enum):
    TAKE_PROFIT = "take_profit"       # %6 kar al
    STOP_LOSS = "stop_loss"           # %6 zarar durdur
    TIME_LIMIT = "time_limit"         # 2 gün sınırı
    MANUAL = "manual"                 # Manuel çıkış


class PortfolioPosition(Base):
    """
    Mevcut açık pozisyonları tutar.
    """
    __tablename__ = "portfolio"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, unique=True, index=True)
    entry_date = Column(DateTime, nullable=False, default=datetime.utcnow)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    allocated_budget = Column(Float, nullable=False)   # Bu pozisyona ayrılan TL
    take_profit_price = Column(Float, nullable=True)
    stop_loss_price = Column(Float, nullable=True)
    tp_order_id = Column(String(50), nullable=True)    # MatriksIQ TP emir ID
    sl_order_id = Column(String(50), nullable=True)    # MatriksIQ SL emir ID
    trading_days_held = Column(Integer, default=0)     # Kaç işlem günü tutuldu
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # İlişki
    trades = relationship("Trade", back_populates="position")

    def __repr__(self):
        return f"<Position {self.symbol} @ {self.entry_price} x {self.quantity}>"


class Trade(Base):
    """
    Tamamlanan işlemlerin kaydı. Dashboard için ana tablo.
    """
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("portfolio.id"), nullable=True)
    symbol = Column(String(20), nullable=False, index=True)

    # Alış bilgileri
    buy_date = Column(DateTime, nullable=False)
    buy_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    buy_total = Column(Float, nullable=False)          # Alış toplam TL
    buy_order_id = Column(String(50), nullable=True)

    # Satış bilgileri
    sell_date = Column(DateTime, nullable=True)
    sell_price = Column(Float, nullable=True)
    sell_total = Column(Float, nullable=True)          # Satış toplam TL
    sell_order_id = Column(String(50), nullable=True)

    # Sonuç
    exit_reason = Column(String(20), nullable=True)    # ExitReason enum değeri
    pnl_amount = Column(Float, nullable=True)          # Net kar/zarar (TL)
    pnl_percent = Column(Float, nullable=True)         # Net kar/zarar (%)
    is_closed = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # İlişki
    position = relationship("PortfolioPosition", back_populates="trades")

    def __repr__(self):
        return f"<Trade {self.symbol} PnL={self.pnl_percent:.2f}%>"


class Signal(Base):
    """
    TradingView'den gelen sinyal logları.
    """
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    received_at = Column(DateTime, default=datetime.utcnow)
    raw_payload = Column(Text, nullable=True)          # Ham JSON verisi
    symbols = Column(Text, nullable=True)              # Virgülle ayrılmış semboller
    processed = Column(Boolean, default=False)
    symbols_bought = Column(Text, nullable=True)       # Gerçekte alınan semboller
    notes = Column(Text, nullable=True)

    def __repr__(self):
        return f"<Signal {self.received_at} symbols={self.symbols}>"


class BotLog(Base):
    """
    Bot operasyonel logları - kritik olaylar için.
    """
    __tablename__ = "bot_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    level = Column(String(10), nullable=False)         # INFO, WARNING, ERROR
    event = Column(String(100), nullable=False)        # Olay kodu
    message = Column(Text, nullable=True)
    symbol = Column(String(20), nullable=True)
    extra_data = Column(Text, nullable=True)           # JSON formatında ek veri

    def __repr__(self):
        return f"<BotLog {self.timestamp} [{self.level}] {self.event}>"


def init_db(database_url: str):
    """Veritabanını başlatır ve tabloları oluşturur."""
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def get_session(Session):
    """Context manager ile güvenli session döndürür."""
    return Session()
