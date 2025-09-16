# adaptive_controller.py - GELİŞMİŞ ADAPTİF BİTRATE CONTROLLER

import time
import numpy as np
from collections import deque
from resilience import FecHandler


class AdaptiveController:
    """
    Gelişmiş Adaptif Bitrate ve FEC Controller
    Ağ koşullarına göre dinamik ayarlama yapar
    """

    def __init__(self, fec_handler: FecHandler,
                 initial_bitrate: int = 2500000,  # 2.5 Mbps
                 min_bitrate: int = 500000,  # 500 Kbps
                 max_bitrate: int = 8000000):  # 8 Mbps

        self.fec_handler = fec_handler

        # Bitrate parametreleri
        self.current_bitrate = initial_bitrate
        self.min_bitrate = min_bitrate
        self.max_bitrate = max_bitrate

        # Ağ metrikleri
        self.rtt_samples = deque(maxlen=10)
        self.loss_samples = deque(maxlen=10)
        self.jitter_samples = deque(maxlen=10)
        self.bandwidth_samples = deque(maxlen=10)

        # Kontrol parametreleri
        self.increase_factor = 1.05  # %5 artış
        self.decrease_factor = 0.85  # %15 azalma
        self.stable_threshold = 5  # 5 sample stabil
        self.stable_count = 0

        # FEC adaptasyon parametreleri
        self.min_fec_ratio = 0.1  # %10 minimum FEC
        self.max_fec_ratio = 0.5  # %50 maksimum FEC

        # Zaman takibi
        self._last_check_time = time.time()
        self._last_adapt_time = time.time()

        # İstatistikler
        self._stats = {
            'packets_sent': 0,
            'packets_lost': 0,
            'bytes_sent': 0,
            'rtt_ms': 0,
            'jitter_ms': 0,
            'loss_rate': 0.0,
            'bandwidth_mbps': 0.0
        }

    def process_stats(self, stats):
        """
        aiortc veya transport katmanından gelen istatistikleri işler
        """
        if isinstance(stats, dict):
            # Paket kaybı
            if 'packetsLost' in stats and 'packetsSent' in stats:
                if stats['packetsSent'] > 0:
                    loss_rate = stats['packetsLost'] / stats['packetsSent']
                    self.loss_samples.append(loss_rate)
                    self._stats['loss_rate'] = loss_rate

            # RTT
            if 'roundTripTime' in stats:
                rtt_ms = stats['roundTripTime'] * 1000
                self.rtt_samples.append(rtt_ms)
                self._stats['rtt_ms'] = rtt_ms

            # Jitter
            if 'jitter' in stats:
                jitter_ms = stats['jitter'] * 1000
                self.jitter_samples.append(jitter_ms)
                self._stats['jitter_ms'] = jitter_ms

            # Bandwidth
            if 'bytesSent' in stats:
                current_time = time.time()
                if hasattr(self, '_last_bytes_sent'):
                    bytes_delta = stats['bytesSent'] - self._last_bytes_sent
                    time_delta = current_time - self._last_bytes_time
                    if time_delta > 0:
                        bandwidth_mbps = (bytes_delta * 8) / (time_delta * 1000000)
                        self.bandwidth_samples.append(bandwidth_mbps)
                        self._stats['bandwidth_mbps'] = bandwidth_mbps

                self._last_bytes_sent = stats['bytesSent']
                self._last_bytes_time = current_time

    def adapt(self):
        """
        Periyodik olarak çağrılarak adaptasyon mantığını çalıştırır
        Hem bitrate hem FEC seviyesini ayarlar
        """
        current_time = time.time()

        # Her 2 saniyede bir adapt et
        if current_time - self._last_adapt_time < 2.0:
            return

        self._last_adapt_time = current_time

        # Yeterli sample yoksa bekle
        if len(self.loss_samples) < 3:
            return

        # Metrikleri hesapla
        avg_loss = np.mean(self.loss_samples)
        avg_rtt = np.mean(self.rtt_samples) if self.rtt_samples else 50
        avg_jitter = np.mean(self.jitter_samples) if self.jitter_samples else 10

        print(f"[ADAPT] Metrics - Loss: {avg_loss:.2%}, RTT: {avg_rtt:.0f}ms, Jitter: {avg_jitter:.0f}ms")

        # Bitrate adaptasyonu
        new_bitrate = self._calculate_target_bitrate(avg_loss, avg_rtt, avg_jitter)

        # FEC adaptasyonu
        new_fec_ratio = self._calculate_target_fec(avg_loss, avg_rtt)

        # Değişiklikleri uygula
        if new_bitrate != self.current_bitrate:
            print(f"[ADAPT] Bitrate: {self.current_bitrate} -> {new_bitrate}")
            self.current_bitrate = new_bitrate

        if abs(new_fec_ratio - self.fec_handler.protection_level) > 0.02:
            print(f"[ADAPT] FEC ratio: {self.fec_handler.protection_level:.2f} -> {new_fec_ratio:.2f}")
            self.fec_handler.protection_level = new_fec_ratio

    def _calculate_target_bitrate(self, loss_rate: float, rtt: float, jitter: float) -> int:
        """
        Ağ koşullarına göre hedef bitrate hesaplar
        """
        target = self.current_bitrate

        # Ağır kayıp durumu - agresif azaltma
        if loss_rate > 0.10:  # %10+ kayıp
            target = int(self.current_bitrate * 0.7)  # %30 azalt
            self.stable_count = 0

        # Orta kayıp durumu - moderate azaltma
        elif loss_rate > 0.05:  # %5-10 kayıp
            target = int(self.current_bitrate * self.decrease_factor)
            self.stable_count = 0

        # Hafif kayıp durumu - küçük azaltma
        elif loss_rate > 0.02:  # %2-5 kayıp
            target = int(self.current_bitrate * 0.95)
            self.stable_count = 0

        # İyi koşullar - artırmayı dene
        elif loss_rate < 0.01 and rtt < 100 and jitter < 20:
            self.stable_count += 1
            if self.stable_count >= self.stable_threshold:
                # Bandwidth'e göre artır
                if self.bandwidth_samples:
                    current_usage = np.mean(self.bandwidth_samples)
                    if current_usage < self.current_bitrate * 0.8 / 1000000:
                        # Bandwidth kullanımı düşük, dikkatli artır
                        target = int(self.current_bitrate * 1.02)
                    else:
                        # Normal artış
                        target = int(self.current_bitrate * self.increase_factor)
                else:
                    target = int(self.current_bitrate * self.increase_factor)
                self.stable_count = 0

        # RTT ve jitter bazlı ek ayarlamalar
        if rtt > 200:  # Yüksek RTT
            target = int(target * 0.95)
        if jitter > 50:  # Yüksek jitter
            target = int(target * 0.95)

        # Limitleri uygula
        target = max(self.min_bitrate, min(self.max_bitrate, target))

        return target

    def _calculate_target_fec(self, loss_rate: float, rtt: float) -> float:
        """
        Paket kaybı oranına göre optimal FEC seviyesi hesaplar
        """
        # Base FEC ratio - kayıp oranının 1.5 katı
        base_fec = loss_rate * 1.5

        # RTT'ye göre ayarlama (yüksek RTT = daha fazla FEC)
        if rtt > 150:
            base_fec *= 1.2
        elif rtt < 50:
            base_fec *= 0.9

        # Özel durumlar
        if loss_rate > 0.15:  # %15+ kayıp
            # Çok yüksek kayıp - maksimum FEC
            target_fec = 0.4
        elif loss_rate > 0.10:  # %10-15 kayıp
            target_fec = max(0.3, base_fec)
        elif loss_rate > 0.05:  # %5-10 kayıp
            target_fec = max(0.2, base_fec)
        elif loss_rate > 0.02:  # %2-5 kayıp
            target_fec = max(0.15, base_fec)
        elif loss_rate > 0.01:  # %1-2 kayıp
            target_fec = max(0.1, base_fec)
        else:  # <%1 kayıp
            target_fec = 0.1  # Minimum FEC

        # Limitleri uygula
        target_fec = max(self.min_fec_ratio, min(self.max_fec_ratio, target_fec))

        return target_fec

    def get_current_settings(self) -> dict:
        """Mevcut adaptif ayarları döndürür"""
        return {
            'bitrate': self.current_bitrate,
            'fec_ratio': self.fec_handler.protection_level,
            'stats': self._stats
        }

    def force_adaptation(self, loss_rate: float):
        """Test için manuel adaptasyon tetikleme"""
        self.loss_samples.clear()
        for _ in range(5):
            self.loss_samples.append(loss_rate)

        if not self.rtt_samples:
            for _ in range(5):
                self.rtt_samples.append(50)

        if not self.jitter_samples:
            for _ in range(5):
                self.jitter_samples.append(10)

        self.adapt()