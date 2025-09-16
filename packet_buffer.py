# packet_buffer.py - AKILLI PAKET TAMPONLAMA

from typing import Optional, Dict
from collections import OrderedDict
import time
from aiortc.rtp import RtpPacket


class PacketBuffer:
    """
    Akıllı paket tamponlama - jitter kompanzasyonu ve sıralama
    """

    def __init__(self,
                 target_delay_ms: int = 100,
                 max_delay_ms: int = 500,
                 reorder_tolerance: int = 5):
        """
        target_delay_ms: Hedef gecikme (jitter buffer)
        max_delay_ms: Maksimum gecikme (timeout)
        reorder_tolerance: Kaç paket geriye kadar reorder tolere edilir
        """
        self.target_delay = target_delay_ms
        self.max_delay = max_delay_ms
        self.reorder_tolerance = reorder_tolerance

        # Ana buffer - OrderedDict for efficient FIFO
        self.buffer: OrderedDict[int, RtpPacket] = OrderedDict()

        # Sıra takibi
        self.next_seq = None
        self.highest_seq = None

        # Zaman takibi
        self.first_packet_time = None
        self.last_pop_time = None
        self.last_cleanup_time = time.time()

        # Adaptif jitter buffer
        self.jitter_estimator = 0.0
        self.jitter_variance = 0.0
        self.alpha = 0.125  # Smoothing factor

        # İstatistikler
        self.stats = {
            'packets_buffered': 0,
            'packets_played': 0,
            'packets_dropped': 0,
            'packets_reordered': 0,
            'buffer_depth_ms': 0,
            'avg_jitter_ms': 0
        }

    def push(self, packet: RtpPacket) -> bool:
        """
        Paketi buffer'a ekler
        Returns: Başarılı ekleme True, drop edildi False
        """
        seq = packet.sequence_number

        # İlk paket
        if self.next_seq is None:
            self.next_seq = seq
            self.highest_seq = seq
            self.first_packet_time = time.time()

        # Duplicate kontrolü
        if seq in self.buffer:
            return False

        # Çok eski paket kontrolü
        if self.next_seq is not None and seq < self.next_seq - self.reorder_tolerance:
            self.stats['packets_dropped'] += 1
            return False

        # Buffer'a ekle
        self.buffer[seq] = packet
        self.stats['packets_buffered'] += 1

        # Reorder tespit
        if self.highest_seq is not None and seq < self.highest_seq:
            self.stats['packets_reordered'] += 1

        # En yüksek sequence number'ı güncelle
        if self.highest_seq is None or seq > self.highest_seq:
            self.highest_seq = seq

        # Jitter hesaplama
        self._update_jitter(packet)

        # Periyodik temizlik
        if time.time() - self.last_cleanup_time > 1.0:
            self._cleanup()
            self.last_cleanup_time = time.time()

        return True

    def pop(self) -> Optional[RtpPacket]:
        """
        Sıradaki paketi döndürür (sıralı playback için)
        """
        if self.next_seq is None:
            return None

        # Buffer'da yeterli paket var mı kontrol et
        if not self._is_ready_to_play():
            return None

        # Sıradaki paketi ara
        if self.next_seq in self.buffer:
            packet = self.buffer.pop(self.next_seq)
            self.stats['packets_played'] += 1
            self.next_seq = (self.next_seq + 1) & 0xFFFF
            self.last_pop_time = time.time()
            return packet

        # Paket kayıp - atla
        self.next_seq = (self.next_seq + 1) & 0xFFFF

        # Buffer'daki en küçük sequence'a atla
        if self.buffer:
            min_seq = min(self.buffer.keys())
            if min_seq > self.next_seq:
                # Büyük boşluk var, reset
                self.next_seq = min_seq

        return None

    def pop_batch(self, max_count: int = 10) -> list:
        """
        Birden fazla paketi sıralı olarak döndürür
        """
        packets = []
        for _ in range(max_count):
            packet = self.pop()
            if packet:
                packets.append(packet)
            else:
                break
        return packets

    def _is_ready_to_play(self) -> bool:
        """
        Buffer'ın playback için hazır olup olmadığını kontrol eder
        """
        if not self.buffer:
            return False

        # İlk paket için bekle
        if self.first_packet_time:
            elapsed = (time.time() - self.first_packet_time) * 1000
            if elapsed < self.target_delay:
                return False

        # Adaptif jitter buffer
        if self.stats['avg_jitter_ms'] > 0:
            required_depth = min(
                self.target_delay + 2 * self.stats['avg_jitter_ms'],
                self.max_delay
            )
            if self.get_depth_ms() < required_depth:
                return False

        return True

    def get_depth_ms(self) -> int:
        """
        Buffer derinliğini milisaniye olarak hesaplar
        """
        if len(self.buffer) < 2:
            return 0

        # RTP timestamp'lerden hesapla (90kHz clock varsayımı)
        timestamps = [p.timestamp for p in self.buffer.values()]
        if timestamps:
            depth = (max(timestamps) - min(timestamps)) / 90
            self.stats['buffer_depth_ms'] = int(depth)
            return int(depth)

        return 0

    def get_depth_packets(self) -> int:
        """
        Buffer'daki paket sayısını döndürür
        """
        return len(self.buffer)

    def _update_jitter(self, packet: RtpPacket):
        """
        Jitter tahminini günceller (RFC 3550)
        """
        if self.last_pop_time is None:
            return

        # Arrival time difference
        arrival_delta = time.time() - self.last_pop_time

        # RTP timestamp difference (90kHz clock)
        if hasattr(self, '_last_rtp_timestamp'):
            rtp_delta = (packet.timestamp - self._last_rtp_timestamp) / 90000.0

            # Jitter calculation
            diff = abs(arrival_delta - rtp_delta) * 1000  # Convert to ms

            # Exponential moving average
            self.jitter_estimator = (1 - self.alpha) * self.jitter_estimator + self.alpha * diff
            self.jitter_variance = (1 - self.alpha) * self.jitter_variance + self.alpha * abs(
                diff - self.jitter_estimator)

            self.stats['avg_jitter_ms'] = self.jitter_estimator

        self._last_rtp_timestamp = packet.timestamp

    def _cleanup(self):
        """
        Eski paketleri temizler (timeout)
        """
        if not self.buffer:
            return

        current_time = time.time()
        max_age_seconds = self.max_delay / 1000.0

        # Timestamp bazlı temizlik
        if self.highest_seq is not None:
            cutoff_seq = self.next_seq - self.reorder_tolerance if self.next_seq else 0

            to_remove = []
            for seq, packet in self.buffer.items():
                # Çok eski sequence number
                if seq < cutoff_seq:
                    to_remove.append(seq)

            for seq in to_remove:
                del self.buffer[seq]
                self.stats['packets_dropped'] += 1

        # Buffer overflow koruması
        max_packets = 100
        if len(self.buffer) > max_packets:
            # En eski paketleri sil
            while len(self.buffer) > max_packets // 2:
                seq = next(iter(self.buffer))
                del self.buffer[seq]
                self.stats['packets_dropped'] += 1

    def reset(self):
        """
        Buffer'ı sıfırlar
        """
        self.buffer.clear()
        self.next_seq = None
        self.highest_seq = None
        self.first_packet_time = None
        self.last_pop_time = None
        self.jitter_estimator = 0.0
        self.jitter_variance = 0.0

    def get_stats(self) -> Dict:
        """
        Buffer istatistiklerini döndürür
        """
        self.stats['current_packets'] = len(self.buffer)
        self.stats['current_depth_ms'] = self.get_depth_ms()

        if self.stats['packets_buffered'] > 0:
            self.stats['reorder_rate'] = (
                    self.stats['packets_reordered'] / self.stats['packets_buffered']
            )
            self.stats['drop_rate'] = (
                    self.stats['packets_dropped'] / self.stats['packets_buffered']
            )

        return self.stats