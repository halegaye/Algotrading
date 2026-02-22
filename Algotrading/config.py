"""
Matriks IQ Trading Bot - Konfigürasyon Dosyası
Tüm ayarları buradan yapılandırın.
"""

# ─────────────────────────────────────────────
# MATRIKS IQ API BAĞLANTI AYARLARI
# ─────────────────────────────────────────────
MATRIKS_HOST = "127.0.0.1"       # MatriksIQ'nun çalıştığı host
MATRIKS_PORT = 18890              # MatriksIQ API portu

# Aracı kurum bilgileri (ListAccounts sorgusundan alın)
BROKAGE_ID = "7"                  # Aracı kurum ID'si
ACCOUNT_ID = "0~801949"           # Hesap numarası
EXCHANGE_ID = 4                   # 4 = Borsa İstanbul Spot

# ─────────────────────────────────────────────
# WEBHOOK SUNUCUSU AYARLARI
# ─────────────────────────────────────────────
WEBHOOK_HOST = "0.0.0.0"
WEBHOOK_PORT = 5000
WEBHOOK_SECRET = "your_webhook_secret_key"  # TradingView webhook güvenlik anahtarı

# ─────────────────────────────────────────────
# PORTFÖY VE RİSK YÖNETİMİ
# ─────────────────────────────────────────────
MAX_POSITIONS = 5          # Aynı anda taşınabilecek maksimum pozisyon sayısı
TAKE_PROFIT_PCT = 6.0      # Kar al yüzdesi
STOP_LOSS_PCT = 6.0        # Zarar durdur yüzdesi
MAX_HOLD_DAYS = 2          # Maksimum elde tutma günü (işlem günü)

# ─────────────────────────────────────────────
# ZAMANLAMA AYARLARI
# ─────────────────────────────────────────────
SIGNAL_CHECK_TIME = "17:30"       # TradingView sinyali bekleme saati
ORDER_SEND_TIME = "17:40"         # Alım emirlerinin gönderilme saati
EOD_CLOSE_TIME = "17:55"          # Gün sonu zorla kapanış saati (17:55 güvenli)
MORNING_REFRESH_TIME = "08:05"    # Sabah TP/SL yenileme saati

# ─────────────────────────────────────────────
# VERİTABANI AYARLARI
# ─────────────────────────────────────────────
DATABASE_URL = "sqlite:///trading_bot.db"  # SQLite (PostgreSQL için değiştirin)
# DATABASE_URL = "postgresql://user:pass@localhost/trading_bot"

# ─────────────────────────────────────────────
# LOGLAMA AYARLARI
# ─────────────────────────────────────────────
LOG_LEVEL = "INFO"                 # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "logs/trading_bot.log"  # Log dosyası yolu
LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
LOG_BACKUP_COUNT = 5

# ─────────────────────────────────────────────
# EMİR AYARLARI
# ─────────────────────────────────────────────
ORDER_TYPE = "2"                   # 2 = Limit emir
TIME_IN_FORCE = "0"                # 0 = Günlük
TRANSACTION_TYPE = "1"             # 1 = Normal
ORDER_SIDE_BUY = 1                 # OrderSide: 1 = Alış
ORDER_SIDE_SELL = 2                # OrderSide: 2 = Satış

# API bağlantı yeniden deneme ayarları
RECONNECT_DELAY = 5                # Saniye cinsinden yeniden bağlanma gecikmesi
MAX_RECONNECT_ATTEMPTS = 10        # Maksimum yeniden bağlanma denemesi
KEEPALIVE_INTERVAL = 30            # Saniye cinsinden keepalive gönderme aralığı

# Mesaj tipi (SetMessageType0 = JSON)
MESSAGE_TYPE = "SetMessageType0"

# ─────────────────────────────────────────────
# ÇİFT SİNYAL FİLTRELEME AYARLARI
# ─────────────────────────────────────────────
# TradingView'den iki FARKLI alarm kaynağı gelir.
# Her ikisinde de ortak olan hisseler (intersection) alım listesine alınır.
#
# TradingView'de iki ayrı alert oluştur, her biri farklı "source" değeri göndersin:
#   Alert 1 mesajı: {"secret":"...", "source":"A", "symbols":["GARAN","THYAO",...]}
#   Alert 2 mesajı: {"secret":"...", "source":"B", "symbols":["GARAN","ASELS",...]}
#
SIGNAL_SOURCE_A = "A"             # 1. alarm kaynağının "source" değeri
SIGNAL_SOURCE_B = "B"             # 2. alarm kaynağının "source" değeri

# Bekleme penceresi: İlk sinyal geldiğinde ikincisini bu kadar saniye bekle.
# 300 sn = 5 dakika (17:30'da ilk gelirse 17:35'e kadar bekler).
# Süre dolmadan ikinci gelmezse → o gün işlem yapılmaz.
SIGNAL_WINDOW_SECONDS = 300
