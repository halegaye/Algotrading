"""
Sinyal Motoru — TradingView Parse, Teknik Filtre, Karar Mantığı

TRADİNGVIEW'DEN GELEN FORMAT:
─────────────────────────────────────────────────────────────────────────────
Pine Script alert_message şu şekilde gönderir:

  {"secret": "xxx", "data": "{GARAN | RSI: 74.20 | ROC: 1.82 | Chg%: 9.95%} {THYAO | RSI: 78.10 | ROC: 2.15 | Chg%: 5.00%}"}

  Dikkat:
  • data alanı bir STRING — JSON içinde JSON değil
  • Her hisse {…} bloğu içinde
  • Alan adlarında boşluk olabilir: "RSI: 74.20" veya "RSI:74.20"
  • Chg% sonunda % işareti olabilir
  • Süslü parantez, pipe, boşluk karışık gelir

PARSE ADIMLARI:
  1. data string'inden {…} bloklarını regex ile çek
  2. Her bloğu | ile ayır → sembol + alan:değer çiftleri
  3. RSI, ROC, Chg% değerlerini float'a çevir

FİLTRELEME SIRASI:
  1. RSI süzgeci  : 72.00 ≤ RSI ≤ 83.00
  2. Tavan eleme  : Chg% 9.90–10.00 → ÇIKAR (likidite riski)
  3. ROC sıralama : büyükten küçüğe
  4. İlk 5 al

KARAR:
  Çift kaynak (A ve B) → A ∩ B → filtrele
  Tek kaynak           → gelen liste → filtrele
  Veri yok             → boş, log uyarısı
"""

import re
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STRING TEMİZLEME
# ─────────────────────────────────────────────────────────────────────────────

def clean_tradingview_data(raw: str) -> str:
    """
    TradingView'den gelen data string'ini parse'a hazırlar.

    Yapılan temizlikler:
      1. Unicode boşluklar → normal boşluk  (\u00a0, \u202f, \t, \r vs.)
      2. Kaçış karakterleri decode  (\\n → \n, \\\" → " vs.)
      3. Başı/sonu boşluk kırpma
      4. Kontrol karakterlerini sil

    Süslü parantezlere ve pipe karakterlerine DOKUNULMAZ — parse bunlara ihtiyaç duyar.
    """
    if not raw:
        return ""

    # Unicode boşluk türlerini normal boşlukla değiştir
    # \u00a0 = non-breaking space, \u202f = narrow no-break space, vb.
    raw = re.sub(
        r'[\u00a0\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5'
        r'\u180e\u2000-\u200f\u202f\u205f\u2060\u2061\u2062'
        r'\u2063\u2064\u206a-\u206f\u3000\ufeff\uffa0]',
        ' ', raw
    )

    # Tab, carriage return → boşluk
    raw = re.sub(r'[\t\r]', ' ', raw)

    # Birden fazla boşluk → tek boşluk (bloklar arasında)
    # NOT: Blok içindeki tek boşlukları koruyoruz (alan adlarında boşluk olabilir)
    raw = re.sub(r' {3,}', ' ', raw)

    # Yazdırılamayan kontrol karakterlerini temizle (0x00–0x1F, 0x7F, 0x80–0x9F)
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]', '', raw)

    return raw.strip()


# ─────────────────────────────────────────────────────────────────────────────
# SINYAL PARSE — {SYM | RSI: val | ROC: val | Chg%: val%} formatı
# ─────────────────────────────────────────────────────────────────────────────

# {…} bloklarını yakalayan regex
_BLOCK_RE = re.compile(r'\{([^}]+)\}')

# Alan:Değer çiftlerini yakalayan regex — boşluklu alan adlarını destekler
# Örnek: "RSI: 74.20", "Chg%: 9.95%", "ROC:1.82"
_FIELD_RE = re.compile(
    r'(RSI|ROC|Chg%|CHG%|Chg|CHG|Change%|CHANGE%)\s*:\s*([\-\d.]+)\s*%?',
    re.IGNORECASE,
)


def parse_tv_data(data_str: str) -> list[dict]:
    """
    TradingView alert data string'ini parse eder.

    Desteklenen format:
      "{GARAN | RSI: 74.20 | ROC: 1.82 | Chg%: 9.95%} {THYAO | RSI: 78.10 | ...}"

    Ayrıca pipe-only formatı da desteklenir (süslü parantez olmadan):
      "GARAN|RSI:74.20|ROC:1.82|Chg%:9.95 THYAO|RSI:78.10|..."

    Returns:
      [{"symbol": "GARAN", "rsi": 74.20, "roc": 1.82, "chg": 9.95}, ...]
      Parse edilemeyen bloklar sessizce atlanır.
    """
    if not data_str:
        return []

    cleaned = clean_tradingview_data(data_str)
    results = []

    # ── Mod 1: {…} blok formatı ──────────────────────────────────────────
    blocks = _BLOCK_RE.findall(cleaned)

    if blocks:
        for block in blocks:
            item = _parse_block(block)
            if item:
                results.append(item)
        if results:
            return results

    # ── Mod 2: Pipe-only formatı (süslü parantez yok) ────────────────────
    # "GARAN|RSI:74.20|ROC:1.82|Chg%:9.95 THYAO|..."
    tokens = re.split(r'[\s,]+', cleaned)
    for token in tokens:
        if '|' not in token:
            continue
        item = _parse_block(token.replace('|', ' | '))
        if item:
            results.append(item)

    return results


def _parse_block(block: str) -> Optional[dict]:
    """
    Tek bir blok string'ini parse eder.
    Örnek girdi: "GARAN | RSI: 74.20 | ROC: 1.82 | Chg%: 9.95%"
    """
    parts = [p.strip() for p in block.split('|')]
    if not parts:
        return None

    # İlk parça sembol
    symbol = parts[0].strip().upper()
    # Sembol: 2-10 büyük harf (rakam içerebilir: THYAO, AKBNK, vs.)
    if not symbol or not re.match(r'^[A-Z][A-Z0-9]{1,9}$', symbol):
        return None

    entry = {"symbol": symbol, "rsi": None, "roc": None, "chg": None}

    # Kalan parçaları tara
    remaining = ' | '.join(parts[1:])
    for match in _FIELD_RE.finditer(remaining):
        key     = match.group(1).upper().replace('%', '').replace('CHANGE', 'CHG')
        val_str = match.group(2).strip()
        try:
            val = float(val_str)
            if key == 'RSI':
                entry['rsi'] = val
            elif key == 'ROC':
                entry['roc'] = val
            elif key in ('CHG', 'CHNG'):
                entry['chg'] = val
        except ValueError:
            continue

    return entry


# ─────────────────────────────────────────────────────────────────────────────
# GERİYE DÖNÜK UYUMLULUK — eski parse_raw_signal ismiyle çağıranlar için
# ─────────────────────────────────────────────────────────────────────────────

def parse_raw_signal(raw: str) -> list[dict]:
    """parse_tv_data'nın alias'ı — eski kodu kırmamak için."""
    return parse_tv_data(raw)


# ─────────────────────────────────────────────────────────────────────────────
# TEKNİK FİLTRE
# ─────────────────────────────────────────────────────────────────────────────

def apply_technical_filter(items: list[dict]) -> list[str]:
    """
    RSI süzgeci → Tavan eleme → ROC sıralama → İlk 5

    Adım 1 — RSI Süzgeci:
      [RSI_MIN=72, RSI_MAX=83] dışındakiler elenir.
      RSI=None ise o hisse dahil edilir (bilgi yok → elenmiyor).

    Adım 2 — Tavan Eleme:
      Chg% [CHG_MIN=9.90, CHG_MAX=10.00] → tavan hissesi → ÇIKARILIR.
      Likidite riski: tavan hissede açık pozisyon ertesi gün sıkışır.

    Adım 3 — ROC Sıralama:
      Kalan hisseler ROC'a göre büyükten küçüğe sıralanır.
      ROC=None → -999 (en sona düşer).

    Adım 4 — İlk config.MAX_POSITIONS (5) alınır.
    """
    rsi_min  = getattr(config, 'RSI_MIN',       72.0)
    rsi_max  = getattr(config, 'RSI_MAX',       83.0)
    chg_min  = getattr(config, 'CHG_MIN',        9.90)
    chg_max  = getattr(config, 'CHG_MAX',       10.00)
    max_pick = getattr(config, 'MAX_POSITIONS',  5)

    after_rsi    = []
    rsi_elenen   = []
    tavan_elenen = []
    after_tavan  = []

    # Adım 1: RSI
    for item in items:
        rsi = item.get('rsi')
        if rsi is None or (rsi_min <= rsi <= rsi_max):
            after_rsi.append(item)
        else:
            rsi_elenen.append(f"{item['symbol']}(RSI={rsi})")

    # Adım 2: Tavan eleme
    for item in after_rsi:
        chg = item.get('chg')
        if chg is not None and chg_min <= chg <= chg_max:
            tavan_elenen.append(f"{item['symbol']}(Chg%={chg})")
        else:
            after_tavan.append(item)

    # Adım 3: ROC sıralama
    after_tavan.sort(
        key=lambda x: x.get('roc') if x.get('roc') is not None else -999.0,
        reverse=True,
    )

    # Adım 4: İlk max_pick
    result = [item['symbol'] for item in after_tavan[:max_pick]]

    logger.info(
        f"[FİLTRE] Giriş={len(items)} | "
        f"RSI_elenen={rsi_elenen} | "
        f"Tavan_elenen={tavan_elenen} | "
        f"ROC_sırası={[i['symbol'] for i in after_tavan]} | "
        f"Seçilen={result}"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# KARAR MANTIĞI
# ─────────────────────────────────────────────────────────────────────────────

def decide_trade_list(
    source_a: Optional[list[dict]],
    source_b: Optional[list[dict]],
    db_logger=None,
) -> tuple[list[str], str]:
    """
    Çift / Tek / Veri Yok durumuna göre nihai alım listesini üretir.

    Returns:
      (sembol_listesi, karar_kodu)
      karar_kodu: "dual" | "single" | "empty"
    """
    def _log(level, event, msg, extra=None):
        if db_logger:
            db_logger.log(level, event, msg, extra_data=extra)
        else:
            getattr(logger, level.lower(), logger.info)(f"[{event}] {msg}")

    has_a = bool(source_a)
    has_b = bool(source_b)

    # ── Durum 3: Veri yok ────────────────────────────────────────────────
    if not has_a and not has_b:
        _log("WARNING", "SIGNAL_NO_DATA",
             "Durum 3 — VERİ YOK. 17:30–17:35 arasında hiçbir kaynaktan sinyal gelmedi.")
        return [], "empty"

    # ── Durum 1: Çift kaynak — kesişim ───────────────────────────────────
    if has_a and has_b:
        syms_a = {i['symbol'] for i in source_a}
        syms_b = {i['symbol'] for i in source_b}
        common = syms_a & syms_b

        _log("INFO", "SIGNAL_DUAL",
             f"Durum 1 — ÇİFT KAYNAK | A={sorted(syms_a)} | B={sorted(syms_b)} | "
             f"Kesişim={sorted(common)}",
             extra={"source_a": sorted(syms_a), "source_b": sorted(syms_b),
                    "intersection": sorted(common)})

        if not common:
            _log("WARNING", "SIGNAL_NO_INTERSECTION",
                 "Çift kaynak — ortak hisse YOK. Bugün işlem yapılmayacak.")
            return [], "empty"

        # Kesişim öğelerini A'dan al, eksik olanları B'den tamamla
        intersect = [i for i in source_a if i['symbol'] in common]
        a_syms    = {i['symbol'] for i in intersect}
        for i in source_b:
            if i['symbol'] in common and i['symbol'] not in a_syms:
                intersect.append(i)

        result = apply_technical_filter(intersect)
        _log("INFO", "SIGNAL_DUAL_RESULT",
             f"Çift kaynak filtre sonucu: {result}",
             extra={"decision": "dual", "symbols": result})
        return result, "dual"

    # ── Durum 2: Tek kaynak ───────────────────────────────────────────────
    items  = source_a if has_a else source_b
    which  = "A" if has_a else "B"
    miss   = "B" if has_a else "A"

    _log("INFO", "SIGNAL_SINGLE",
         f"Durum 2 — TEK KAYNAK | Kaynak {which} geldi, {miss} GELMEDİ. "
         f"{which} listesi kullanılıyor.",
         extra={"which": which, "missing": miss,
                "symbols": [i['symbol'] for i in items]})

    result = apply_technical_filter(items)
    _log("INFO", "SIGNAL_SINGLE_RESULT",
         f"Tek kaynak ({which}) filtre sonucu: {result}",
         extra={"decision": "single", "source": which, "symbols": result})
    return result, "single"
