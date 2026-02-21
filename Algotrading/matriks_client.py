"""
Matriks IQ API İstemcisi
Socket üzerinden JSON mesajlaşma ile MatriksIQ'ya bağlanır.
"""

import socket
import json
import time
import threading
from typing import Optional, Callable, Dict, Any
from datetime import datetime


class MatriksAPIClient:
    """
    MatriksIQ API ile soket tabanlı haberleşme sağlar.
    Yeniden bağlanma, keepalive ve callback desteği içerir.
    """

    MSG_TERMINATOR = chr(11)  # char(11) - paket sonu işareti

    def __init__(
        self,
        host: str,
        port: int,
        brokage_id: str,
        account_id: str,
        exchange_id: int,
        reconnect_delay: int = 5,
        max_reconnect_attempts: int = 10,
        keepalive_interval: int = 30,
        db_logger=None
    ):
        self.host = host
        self.port = port
        self.brokage_id = brokage_id
        self.account_id = account_id
        self.exchange_id = exchange_id
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self.keepalive_interval = keepalive_interval
        self.db_logger = db_logger

        self._sock: Optional[socket.socket] = None
        self._connected = False
        self._reconnect_count = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # Callback fonksiyonları: ApiCommands değerine göre
        self._callbacks: Dict[int, Callable] = {}
        # Genel broadcast callback (durum değişiklikleri için)
        self._broadcast_callback: Optional[Callable] = None

        self._receiver_thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None

        # Gelen mesaj tamponu
        self._buffer = ""

    # ─────────────────────────────────────────
    # BAĞLANTI YÖNETİMİ
    # ─────────────────────────────────────────

    def connect(self) -> bool:
        """MatriksIQ'ya bağlan ve mesaj tipini ayarla."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(10)
            self._sock.connect((self.host, self.port))
            self._sock.settimeout(None)
            self._connected = True
            self._reconnect_count = 0
            self._log("INFO", "API_CONNECT", f"MatriksIQ'ya bağlandı: {self.host}:{self.port}")

            # Mesaj tipini JSON olarak ayarla
            self._sock.sendall(("SetMessageType0" + self.MSG_TERMINATOR).encode("utf-8"))
            time.sleep(0.5)

            self._start_receiver()
            self._start_keepalive()
            return True

        except Exception as e:
            self._connected = False
            self._log("ERROR", "API_CONNECT_FAIL", f"Bağlantı hatası: {e}")
            return False

    def disconnect(self):
        """Bağlantıyı güvenli şekilde kapat."""
        self._stop_event.set()
        self._connected = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._log("INFO", "API_DISCONNECT", "MatriksIQ bağlantısı kapatıldı.")

    def reconnect(self) -> bool:
        """Bağlantıyı yeniden kurmayı dener."""
        if self._reconnect_count >= self.max_reconnect_attempts:
            self._log("ERROR", "API_RECONNECT_FAIL", "Maksimum yeniden bağlanma denemesi aşıldı.")
            return False

        self._reconnect_count += 1
        self._log("WARNING", "API_RECONNECT",
                  f"Yeniden bağlanılıyor... Deneme {self._reconnect_count}/{self.max_reconnect_attempts}")

        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

        time.sleep(self.reconnect_delay)
        self._stop_event.clear()
        return self.connect()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ─────────────────────────────────────────
    # MESAJ GÖNDERME
    # ─────────────────────────────────────────

    def _send(self, payload: dict) -> bool:
        """JSON payload'ı soket üzerinden gönderir."""
        with self._lock:
            if not self._connected or not self._sock:
                self._log("ERROR", "API_SEND_FAIL", "Bağlantı yok, mesaj gönderilemedi.")
                return False
            try:
                message = json.dumps(payload, ensure_ascii=False) + self.MSG_TERMINATOR
                self._sock.sendall(message.encode("utf-8"))
                return True
            except Exception as e:
                self._log("ERROR", "API_SEND_ERROR", f"Gönderme hatası: {e}")
                self._connected = False
                threading.Thread(target=self.reconnect, daemon=True).start()
                return False

    # ─────────────────────────────────────────
    # API KOMUTLARI
    # ─────────────────────────────────────────

    def request_accounts(self) -> bool:
        """Kurum listesini sorgular (ApiCommands: 0)."""
        return self._send({"ApiCommands": 0})

    def request_positions(self) -> bool:
        """Pozisyon listesini sorgular (ApiCommands: 1)."""
        return self._send({
            "ApiCommands": 1,
            "BrokageId": self.brokage_id,
            "AccountId": self.account_id,
            "ExchangeId": self.exchange_id
        })

    def request_waiting_orders(self) -> bool:
        """Bekleyen emirleri sorgular (ApiCommands: 2)."""
        return self._send({
            "ApiCommands": 2,
            "BrokageId": self.brokage_id,
            "AccountId": self.account_id,
            "ExchangeId": self.exchange_id
        })

    def send_new_order(
        self,
        symbol: str,
        price: float,
        quantity: float,
        order_side: int,
        order_type: str = "2",
        time_in_force: str = "0",
        transaction_type: str = "1",
        stop_px: float = 0.0,
        include_after_session: bool = False
    ) -> bool:
        """Yeni emir gönderir (ApiCommands: 3)."""
        payload = {
            "ApiCommands": 3,
            "AccountId": self.account_id,
            "BrokageId": self.brokage_id,
            "Symbol": symbol,
            "Price": round(price, 4),
            "Quantity": float(quantity),
            "OrderQty": float(quantity),
            "LeavesQty": float(quantity),
            "OrderSide": order_side,
            "OrderType": order_type,
            "TimeInForce": time_in_force,
            "TransactionType": transaction_type,
            "StopPx": stop_px,
            "IncludeAfterSession": include_after_session,
            "OrdStatus": "0",
            "FilledQty": 0.0,
            "AvgPx": 0.0,
            "TradeDate": "0001-01-01T00:00:00",
            "TransactTime": "00:00:00",
            "ExpireDate": "0001-01-01T00:00:00",
            "Explanation": None,
            "OrderID": None,
            "OrderID2": None
        }
        self._log("INFO", "ORDER_SEND",
                  f"Emir gönderiliyor: {symbol} {'ALIŞ' if order_side == 1 else 'SATIŞ'} "
                  f"Fiyat={price} Adet={quantity}",
                  symbol=symbol)
        return self._send(payload)

    def send_cancel_order(self, order_data: dict) -> bool:
        """Emir iptali (ApiCommands: 4)."""
        payload = dict(order_data)
        payload["ApiCommands"] = 4
        return self._send(payload)

    def send_edit_order(self, order_data: dict) -> bool:
        """Emir düzeltme (ApiCommands: 5)."""
        payload = dict(order_data)
        payload["ApiCommands"] = 5
        return self._send(payload)

    def send_keepalive(self) -> bool:
        """Bağlantı durumu sorgular (ApiCommands: 6)."""
        return self._send({
            "ApiCommands": 6,
            "KeepAliveDate": datetime.now().isoformat()
        })

    def request_account_info(self) -> bool:
        """Hesap bilgilerini sorgular (ApiCommands: 7)."""
        return self._send({
            "ApiCommands": 7,
            "BrokageId": self.brokage_id,
            "AccountIdList": self.account_id,
            "ExchangeID": self.exchange_id
        })

    def request_filled_orders(self) -> bool:
        """Gerçekleşen emirleri sorgular (ApiCommands: 8)."""
        return self._send({
            "ApiCommands": 8,
            "BrokageId": self.brokage_id,
            "AccountId": self.account_id,
            "ExchangeId": self.exchange_id
        })

    def request_canceled_orders(self) -> bool:
        """İptal edilmiş emirleri sorgular (ApiCommands: 9)."""
        return self._send({
            "ApiCommands": 9,
            "BrokageId": self.brokage_id,
            "AccountId": self.account_id,
            "ExchangeId": self.exchange_id
        })

    # ─────────────────────────────────────────
    # CALLBACK KAYIT
    # ─────────────────────────────────────────

    def register_callback(self, api_command: int, callback: Callable):
        """Belirli bir ApiCommands değeri için callback kaydeder."""
        self._callbacks[api_command] = callback

    def set_broadcast_callback(self, callback: Callable):
        """Otomatik gelen mesajlar (durum değişiklikleri) için callback."""
        self._broadcast_callback = callback

    # ─────────────────────────────────────────
    # ALICI THREAD
    # ─────────────────────────────────────────

    def _start_receiver(self):
        """Mesaj alıcı thread'i başlatır."""
        self._stop_event.clear()
        self._receiver_thread = threading.Thread(
            target=self._receive_loop,
            daemon=True,
            name="MatriksReceiver"
        )
        self._receiver_thread.start()

    def _receive_loop(self):
        """Soket'ten sürekli veri okur ve parse eder."""
        while not self._stop_event.is_set() and self._connected:
            try:
                chunk = self._sock.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    self._log("WARNING", "API_DISCONNECT", "Sunucu bağlantıyı kapattı.")
                    self._connected = False
                    if not self._stop_event.is_set():
                        threading.Thread(target=self.reconnect, daemon=True).start()
                    break

                self._buffer += chunk
                # char(11) ile ayrılan mesajları işle
                while self.MSG_TERMINATOR in self._buffer:
                    msg_str, self._buffer = self._buffer.split(self.MSG_TERMINATOR, 1)
                    msg_str = msg_str.strip()
                    if msg_str:
                        self._handle_message(msg_str)

            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop_event.is_set():
                    self._log("ERROR", "API_RECEIVE_ERROR", f"Alma hatası: {e}")
                    self._connected = False
                    threading.Thread(target=self.reconnect, daemon=True).start()
                break

    def _handle_message(self, raw: str):
        """Gelen ham mesajı parse eder ve uygun callback'e yönlendirir."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._log("WARNING", "API_PARSE_ERROR", f"JSON parse hatası: {e} | Raw: {raw[:100]}")
            return

        api_cmd = data.get("ApiCommands")

        # Kayıtlı callback varsa çağır
        if api_cmd in self._callbacks:
            try:
                self._callbacks[api_cmd](data)
            except Exception as e:
                self._log("ERROR", "CALLBACK_ERROR",
                          f"ApiCommands={api_cmd} callback hatası: {e}")

        # Broadcast callback (emir/pozisyon durum değişiklikleri)
        if self._broadcast_callback:
            try:
                self._broadcast_callback(data)
            except Exception as e:
                self._log("ERROR", "BROADCAST_ERROR", f"Broadcast callback hatası: {e}")

    # ─────────────────────────────────────────
    # KEEPALIVE THREAD
    # ─────────────────────────────────────────

    def _start_keepalive(self):
        """Periyodik keepalive gönderen thread'i başlatır."""
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            daemon=True,
            name="MatriksKeepalive"
        )
        self._keepalive_thread.start()

    def _keepalive_loop(self):
        """Her N saniyede bir keepalive gönderir."""
        while not self._stop_event.is_set():
            time.sleep(self.keepalive_interval)
            if self._connected:
                self.send_keepalive()

    # ─────────────────────────────────────────
    # YARDIMCI
    # ─────────────────────────────────────────

    def _log(self, level: str, event: str, message: str, symbol: str = None):
        if self.db_logger:
            self.db_logger.log(level, event, message, symbol=symbol)
        else:
            print(f"[{level}][{event}] {message}")
