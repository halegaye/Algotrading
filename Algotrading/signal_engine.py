"""
Sinyal Motoru — Parse, Filtre ve Karar Mantığı

SORUMLULUK:
  Bu modül TradingView'den gelen ham sinyal verisini alır,
  teknik filtrelerden geçirir ve nihai alım listesini üretir.
  webhook_server.py ve scheduler.py bu modülü kullanır.

SINYAL FORMATI (TradingView alert message):
  Ham string modu:
    "GARAN|RSI:74.20|ROC:1.82|Chg%:9.95 THYAO|RSI:78.10|ROC:2.15|Chg%:9.98"

  Liste modu (basit):
    ["GARAN", "THYAO", "ASELS"]

FİLTRELEME SIRASI:
  1. RSI süzgeci     : 72.00 ≤ RSI ≤ 83.00 olanlar kalır
  2. Tavan eleme     : Chg% 9.90–10.00 olanlar listeden ÇIKARILIR
  3. ROC sıralama    : büyükten küçüğe
  4. İlk 5 seçilir   : config.MAX_POSITIONS kadar

KARAR MANTIĞI (SignalCollector):
  Durum 1 — Çift kaynak (A ve B):  kesişim (A ∩ B) → filtrele
  Durum 2 — Tek kaynak (A veya B): gelen liste       → filtrele
  Durum 3 — Veri yok:              boş liste dön, log'a yaz
"""

import re
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SINYAL PARSE
# ─────────────────────────────────────────────────────────────────────────────

def parse_raw_signal(raw: str) -> list[dict]:
    """
    TradingView alert metnini parse eder.

    Desteklenen format:
      "GARAN|RSI:74.20|ROC:1.82|Chg%:9.95 THYAO|RSI:78.10|ROC:2.15|Chg%:9.98"

    Tokenlar boşluk, virgül veya yeni satırla ayrılabilir.
    Her token: SEMBOL|ALAN:DEGER|ALAN:DEGER...

    Returns:
      [{"symbol":"GARAN","rsi":74.20,"roc":1.82,"chg":9.95}, ...]
      Parse edilemeyen tokenlar sessizce atlanır.
    """
    if not raw or not raw.strip():
        return []

    results = []
    tokens  = re.split(r'[\s,]+', raw.strip())

    for token in tokens:
        token = token.strip()
        if not token:
            continue

        parts  = [p.strip() for p in token.split('|')]
        symbol = parts[0].upper() if parts else ""

        # Sembol sadece harf içermeli (GARAN, THYAO vs.)
        if not symbol or not re.match(r'^[A-Z]{2,10}$', symbol):
            continue

        entry = {"symbol": symbol, "rsi": None, "roc": None, "chg": None}

        for part in parts[1:]:
            try:
                key, val_str = part.split(':', 1)
                key     = key.strip().upper().replace('%', '').replace(' ', '')
                val_str = val_str.strip().replace('%', '')
                val     = float(val_str)

                if key == 'RSI':
                    entry['rsi'] = val
                elif key == 'ROC':
                    entry['roc'] = val
                elif key in ('CHG', 'CHANGE', 'CHNG'):
                    entry['chg'] = val
            except (ValueError, AttributeError):
                continue

        results.append(entry)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# TEKNİK FİLTRE
# ─────────────────────────────────────────────────────────────────────────────

def apply_technical_filter(items: list[dict]) -> list[str]:
    """
    RSI → Tavan Eleme → ROC Sıralama → İlk 5

    Adım 1 — RSI Süzgeci:
      RSI değeri [RSI_MIN, RSI_MAX] aralığında olanlar geçer.
      RSI bilgisi yoksa (None) o hisse dahil edilir.

    Adım 2 — Tavan Eleme:
      Chg% [CHG_MIN, CHG_MAX] arasındakiler "tavan" sayılır ve
      listeden ÇIKARILIR. (Tavan hisselerde likidite riski yüksek.)

    Adım 3 — ROC Sıralama:
      Kalan hisseler ROC'a göre büyükten küçüğe sıralanır.
      ROC bilgisi yoksa en sona düşer.

    Adım 4 — İlk MAX_POSITIONS (5) seçilir.

    Returns:
      ["GARAN", "THYAO", ...] — sembol listesi (en fazla 5)
    """
    rsi_min  = getattr(config, 'RSI_MIN',  72.0)
    rsi_max  = getattr(config, 'RSI_MAX',  83.0)
    chg_min  = getattr(config, 'CHG_MIN',   9.90)
    chg_max  = getattr(config, 'CHG_MAX',  10.00)
    max_pick = getattr(config, 'MAX_POSITIONS', 5)

    # Adım 1: RSI filtresi
    after_rsi = []
    rsi_elenen = []
    for item in items:
        rsi = item.get('rsi')
        if rsi is None or (rsi_min <= rsi <= rsi_max):
            after_rsi.append(item)
        else:
            rsi_elenen.append(item['symbol'])

    # Adım 2: Tavan eleme
    after_tavan = []
    tavan_elenen = []
    for item in after_rsi:
        chg = item.get('chg')
        if chg is not None and chg_min <= chg <= chg_max:
            tavan_elenen.append(item['symbol'])
        else:
            after_tavan.append(item)

    # Adım 3: ROC sıralama (büyükten küçüğe, None en sona)
    after_tavan.sort(
        key=lambda x: x.get('roc') if x.get('roc') is not None else -999.0,
        reverse=True,
    )

    # Adım 4: İlk max_pick
    result = [item['symbol'] for item in after_tavan[:max_pick]]

    logger.info(
        f"[FİLTRE] Giriş:{len(items)} → "
        f"RSI_elenen:{rsi_elenen} → "
        f"Tavan_elenen:{tavan_elenen} → "
        f"ROC_sırası:{[i['symbol'] for i in after_tavan]} → "
        f"Seçilen:{result}"
    )
    return result


def symbols_to_filter(symbols: list[str]) -> list[str]:
    """
    Sembol listesini (RSI/ROC bilgisi olmadan) filtreden geçirir.
    RSI/ROC bilgisi olmadığı için sadece MAX_POSITIONS kadar kırpılır.
    Ham liste modunda kullanılır.
    """
    max_pick = getattr(config, 'MAX_POSITIONS', 5)
    result   = symbols[:max_pick]
    logger.info(f"[FİLTRE-LİSTE] Giriş:{len(symbols)} → Seçilen:{result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# KARAR MANTIĞI
# ─────────────────────────────────────────────────────────────────────────────

def decide_trade_list(
    source_a: Optional[list[dict]],   # None = gelmedi
    source_b: Optional[list[dict]],   # None = gelmedi
    db_logger=None,
) -> tuple[list[str], str]:
    """
    Çift / Tek / Veri Yok durumuna göre nihai alım listesini üretir.

    Args:
      source_a: A kaynağından parse edilmiş [{symbol,rsi,roc,chg},...] ya da None
      source_b: B kaynağından parse edilmiş [{symbol,rsi,roc,chg},...] ya da None
      db_logger: opsiyonel BotLogger (DB'ye log atmak için)

    Returns:
      (sembol_listesi, karar_kodu)
      karar_kodu:
        "dual"   — çift kaynak, kesişim kullanıldı
        "single" — tek kaynak, o liste kullanıldı
        "empty"  — veri yok veya filtreden sonra liste boş

    ─────────────────────────────────────────────────────────────────────
    Durum 1 — Çift Kaynak (A ve B ikisi de geldi):
      Her iki listede ORTAK olan semboller alınır (kesişim).
      Kesişim listesi teknik filtreden geçirilir.

    Durum 2 — Tek Kaynak (sadece A veya sadece B geldi):
      Gelen tek listenin tamamı teknik filtreden geçirilir.
      (Diğer kaynağın gelmemesi işlemi durdurmaz.)

    Durum 3 — Veri Yok (ikisi de gelmedi):
      Boş liste döner, log'a uyarı yazılır.
    ─────────────────────────────────────────────────────────────────────
    """
    def _log(level, event, msg, extra=None):
        if db_logger:
            db_logger.log(level, event, msg, extra_data=extra)
        else:
            getattr(logger, level.lower(), logger.info)(f"[{event}] {msg}")

    has_a = source_a is not None and len(source_a) > 0
    has_b = source_b is not None and len(source_b) > 0

    # ── Durum 3: Veri yok ────────────────────────────────────────────────
    if not has_a and not has_b:
        _log("WARNING", "SIGNAL_NO_DATA",
             "[KARAR] Durum 3 — VERİ YOK. "
             "17:30–17:35 arasında hiçbir kaynaktan sinyal gelmedi. "
             "Bugün işlem yapılmayacak.")
        return [], "empty"

    # ── Durum 1: Çift kaynak — kesişim ───────────────────────────────────
    if has_a and has_b:
        syms_a = {item['symbol'] for item in source_a}
        syms_b = {item['symbol'] for item in source_b}
        common = syms_a & syms_b

        _log("INFO", "SIGNAL_DUAL_SOURCE",
             f"[KARAR] Durum 1 — ÇİFT KAYNAK. "
             f"A={sorted(syms_a)} | B={sorted(syms_b)} | "
             f"Kesişim={sorted(common)}",
             extra={"source_a": sorted(syms_a), "source_b": sorted(syms_b),
                    "intersection": sorted(common)})

        if not common:
            _log("WARNING", "SIGNAL_NO_INTERSECTION",
                 "[KARAR] Çift kaynak ama ortak hisse YOK. "
                 "Bugün işlem yapılmayacak.")
            return [], "empty"

        # Kesişimdeki sembollerin detaylarını A listesinden al (ROC/RSI bilgisi için)
        intersect_items = [item for item in source_a if item['symbol'] in common]
        # B'de olup A'da olmayan bilgileri de ekle (ROC bilgisi B'de daha iyiyse)
        a_syms = {item['symbol'] for item in intersect_items}
        for item in source_b:
            if item['symbol'] in common and item['symbol'] not in a_syms:
                intersect_items.append(item)

        result = apply_technical_filter(intersect_items)
        _log("INFO", "SIGNAL_DUAL_RESULT",
             f"[KARAR] Çift kaynak filtre sonucu: {result}",
             extra={"decision": "dual", "symbols": result})
        return result, "dual"

    # ── Durum 2: Tek kaynak ───────────────────────────────────────────────
    source_items = source_a if has_a else source_b
    which        = "A" if has_a else "B"
    missing      = "B" if has_a else "A"

    _log("INFO", "SIGNAL_SINGLE_SOURCE",
         f"[KARAR] Durum 2 — TEK KAYNAK. "
         f"Kaynak {which} geldi, Kaynak {missing} GELMEDİ. "
         f"Kaynak {which}'nın listesi kullanılıyor.",
         extra={"which": which, "missing": missing,
                "symbols": [i['symbol'] for i in source_items]})

    result = apply_technical_filter(source_items)
    _log("INFO", "SIGNAL_SINGLE_RESULT",
         f"[KARAR] Tek kaynak ({which}) filtre sonucu: {result}",
         extra={"decision": "single", "source": which, "symbols": result})
    return result, "single"
