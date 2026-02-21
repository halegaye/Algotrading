# Matriks IQ Trading Bot

TradingView webhook sinyallerini alarak Matriks IQ API üzerinden otomatik işlem yapan Python botu.

---

## 📁 Proje Yapısı

```
matriks_bot/
├── config.py            # Tüm konfigürasyon ayarları
├── database.py          # SQLAlchemy modelleri (portfolio, trades, signals, bot_logs)
├── logger.py            # Loglama yöneticisi (dosya + DB)
├── matriks_client.py    # MatriksIQ soket API istemcisi
├── portfolio_manager.py # Portföy iş mantığı (açma/kapatma/TP-SL)
├── webhook_server.py    # Flask webhook sunucusu
├── scheduler.py         # APScheduler görev zamanlayıcı
├── main.py              # Ana başlangıç noktası
├── requirements.txt
└── README.md
```

---

## ⚙️ Kurulum

```bash
# 1. Bağımlılıkları yükle
pip install -r requirements.txt

# 2. config.py dosyasını düzenle
#    - BROKAGE_ID, ACCOUNT_ID, EXCHANGE_ID → MatriksIQ hesabınız
#    - WEBHOOK_SECRET → TradingView'de ayarlayacağınız gizli anahtar
#    - MATRIKS_HOST/PORT → MatriksIQ'nun çalıştığı makine

# 3. Botu başlat
python main.py
```

---

## 🔧 Konfigürasyon (config.py)

| Değişken | Açıklama | Varsayılan |
|---|---|---|
| `MATRIKS_HOST` | MatriksIQ host | `127.0.0.1` |
| `MATRIKS_PORT` | MatriksIQ API portu | `18890` |
| `BROKAGE_ID` | Aracı kurum ID | `"7"` |
| `ACCOUNT_ID` | Hesap numarası | `"0~801949"` |
| `EXCHANGE_ID` | Borsa ID (4=BIST) | `4` |
| `MAX_POSITIONS` | Max eş zamanlı pozisyon | `5` |
| `TAKE_PROFIT_PCT` | Kar al yüzdesi | `6.0` |
| `STOP_LOSS_PCT` | Zarar durdur yüzdesi | `6.0` |
| `MAX_HOLD_DAYS` | Maksimum tutma günü | `2` |
| `WEBHOOK_PORT` | Webhook sunucu portu | `5000` |

---

## 📡 TradingView Webhook Kurulumu

TradingView Alert ayarlarında Webhook URL'yi şu şekilde ayarlayın:

```
http://YOUR_SERVER_IP:5000/webhook
```

Webhook mesaj formatı (JSON):
```json
{
    "secret": "your_webhook_secret_key",
    "symbols": ["GARAN", "THYAO", "ASELS", "EREGL", "KCHOL"]
}
```

---

## 🕐 Günlük Çalışma Takvimi

| Saat | Görev |
|---|---|
| **08:05** | Sabah TP/SL yenileme (borsada günlük emirler sıfırlanır) |
| **17:30** | TradingView sinyali bekleme, portföy boşluğu analizi |
| **17:40** | Yeni alım emirlerini MatriksIQ'ya gönder |
| **17:55** | 2 günü dolan pozisyonları piyasa fiyatından kapat |
| **18:05** | İşlem günü sayacını artır |

---

## 📊 Veritabanı Tabloları

### `portfolio` — Aktif Pozisyonlar
| Alan | Açıklama |
|---|---|
| symbol | Hisse kodu |
| entry_date | Giriş tarihi |
| entry_price | Alış fiyatı |
| quantity | Adet |
| take_profit_price | TP fiyatı |
| stop_loss_price | SL fiyatı |
| trading_days_held | Kaç işlem günü tutuldu |

### `trades` — Tamamlanan İşlemler
| Alan | Açıklama |
|---|---|
| symbol | Hisse kodu |
| buy_date / sell_date | Alış/satış tarihleri |
| buy_price / sell_price | Fiyatlar |
| exit_reason | `take_profit` / `stop_loss` / `time_limit` / `manual` |
| pnl_amount | Kar/Zarar (TL) |
| pnl_percent | Kar/Zarar (%) |

### `signals` — TradingView Sinyalleri
### `bot_logs` — Operasyonel Loglar

---

## 🧪 Test Komutları

```bash
# Test sinyali gönder (queue'ya ekler)
python main.py --test-signal GARAN THYAO ASELS

# Manuel görev tetikle
python main.py --trigger morning_refresh
python main.py --trigger send_buy_orders
python main.py --trigger eod_close

# Webhook'u test et (ayrı terminal)
curl -X POST http://localhost:5000/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret": "your_webhook_secret_key", "symbols": ["GARAN", "THYAO"]}'

# Portföy durumunu görüntüle
curl http://localhost:5000/portfolio
```

---

## ⚠️ Önemli Notlar

1. **MatriksIQ açık olmalı**: Bot çalışmadan önce MatriksIQ masaüstü uygulamasının açık ve hesaba giriş yapılmış olması gerekir.

2. **TP/SL emir tipi**: BIST'te koşullu (stop) emirler her aracı kurumda desteklenmeyebilir. Şu anki implementasyon limit satış emirleri gönderir. Aracı kurum koşullu emir destekliyorsa `OrderType` ve `StopPx` alanları güncellenmeli.

3. **Fiyat verisi**: Bot şu an pozisyon durum değişikliklerinden (`LastPx`) fiyat bilgisi alır. Gerçek zamanlı fiyat için MatriksIQ'nun fiyat yayını entegrasyonu eklenebilir.

4. **Nakit hesabı**: `scheduler.py` → `_get_available_cash()` metodu şu an mock değer döndürüyor. `request_account_info()` API yanıtından `"BAL"` (Bakiye) alanı parse edilmeli.

5. **PostgreSQL**: Production ortamı için `config.py`'de `DATABASE_URL`'i PostgreSQL'e çevirin.

---

## 🔌 Dashboard Entegrasyonu

`/portfolio` endpoint'i (GET) JSON döndürür:
- Aktif pozisyonlar
- Son 10 tamamlanan işlem

İleride Streamlit, Grafana veya React dashboard'una `trades` ve `portfolio` tablolarını doğrudan bağlayabilirsiniz.
