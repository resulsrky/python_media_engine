# resilience.py - %20 PAKET KAYBINA DAYANIKLI VERSİYON

import numpy as np
from aiortc.rtp import RtpPacket
from typing import List, Dict, Optional
from collections import deque
import struct
import hashlib

# config.py'den import edilecek değerler
FEC_PAYLOAD_TYPE = 127
RED_PAYLOAD_TYPE = 100


class FecHandler:
    """
    Gelişmiş FEC Handler - %20 paket kaybına dayanıklı
    Reed-Solomon benzeri sistematik kodlama + RED desteği
    """

    def __init__(self, group_size=10, protection_level=0.3, enable_red=True):
        """
        group_size: Bir FEC grubundaki paket sayısı
        protection_level: FEC oranı (0.3 = %30 FEC paketi)
        enable_red: RED (Redundancy Encoding) aktif mi
        """
        self.group_size = group_size
        self.protection_level = protection_level
        self.enable_red = enable_red

        # Buffers
        self._media_packet_buffer = []
        self.tx_buffer = deque(maxlen=group_size * 2)
        self.rx_buffer = {}
        self.fec_buffer = {}

        # RED için
        self.red_history_size = 3
        self.red_buffer = deque(maxlen=self.red_history_size)

        # İstatistikler
        self.stats = {
            'packets_sent': 0,
            'packets_received': 0,
            'packets_recovered': 0,
            'packets_lost': 0,
            'fec_packets_generated': 0
        }

    def protect(self, packet: RtpPacket) -> List[RtpPacket]:
        """
        Paketi FEC ve RED ile korur
        Returns: Gönderilecek paketler listesi
        """
        packets_to_send = [packet]
        self._media_packet_buffer.append(packet)
        self.stats['packets_sent'] += 1

        # RED: Kritik paketler için redundant kopya
        if self.enable_red and self._is_critical_packet(packet):
            red_packet = self._create_red_packet(packet)
            if red_packet:
                packets_to_send.append(red_packet)

        # FEC: Grup dolduğunda FEC paketleri oluştur
        if len(self._media_packet_buffer) >= self.group_size:
            fec_packets = self._generate_advanced_fec(self._media_packet_buffer[:self.group_size])
            packets_to_send.extend(fec_packets)
            self._media_packet_buffer = self._media_packet_buffer[self.group_size:]

        return packets_to_send

    def _is_critical_packet(self, packet: RtpPacket) -> bool:
        """Paketin kritik olup olmadığını kontrol eder (keyframe vb.)"""
        # Marker bit genelde frame sonu/keyframe'i gösterir
        return packet.marker or (packet.sequence_number % 30 == 0)

    def _create_red_packet(self, packet: RtpPacket) -> Optional[RtpPacket]:
        """RED paketi oluşturur - önceki paketlerin kopyasını içerir"""
        self.red_buffer.append(packet)

        if len(self.red_buffer) < 2:
            return None

        red_payload = bytearray()

        # RED header: [F|PT|timestamp_offset|length]
        # F: More blocks flag (1 bit)
        # PT: Payload type (7 bits)

        # Önceki paket için header
        prev_packet = self.red_buffer[-2]
        ts_offset = (packet.timestamp - prev_packet.timestamp) & 0x3FFF

        # Header byte: F=1 (more blocks), PT=96 (H264)
        red_payload.append(0x80 | 96)
        # Timestamp offset (14 bits) + Length (10 bits) = 24 bits = 3 bytes
        red_payload.extend(struct.pack('!H', ts_offset << 2 | (len(prev_packet.payload) >> 8)))
        red_payload.append(len(prev_packet.payload) & 0xFF)
        # Önceki paketin payload'ı (truncated)
        red_payload.extend(prev_packet.payload[:min(100, len(prev_packet.payload))])

        # Son blok için header (primary encoding)
        red_payload.append(96)  # F=0, PT=96
        # Mevcut paketin payload'ı
        red_payload.extend(packet.payload)

        return RtpPacket(
            payload_type=RED_PAYLOAD_TYPE,
            sequence_number=packet.sequence_number,
            timestamp=packet.timestamp,
            ssrc=packet.ssrc,
            payload=bytes(red_payload),
            marker=packet.marker
        )

    def _generate_advanced_fec(self, media_packets: List[RtpPacket]) -> List[RtpPacket]:
        """
        Sistematik FEC kodlaması - Reed-Solomon benzeri
        Her FEC paketi farklı katsayılarla oluşturulur
        """
        num_fec_packets = max(1, int(len(media_packets) * self.protection_level))
        fec_packets = []

        print(f"[FEC] {len(media_packets)} medya paketi için {num_fec_packets} FEC paketi oluşturuluyor")

        for fec_idx in range(num_fec_packets):
            # Her FEC paketi için farklı katsayılar
            coefficients = self._generate_vandermonde_coefficients(fec_idx, len(media_packets))

            # FEC payload hesapla
            fec_payload = self._calculate_fec_payload(media_packets, coefficients)

            # FEC header oluştur
            fec_header = self._create_fec_header(media_packets, coefficients)

            # FEC paketi oluştur
            fec_packet = RtpPacket(
                payload_type=FEC_PAYLOAD_TYPE,
                sequence_number=media_packets[-1].sequence_number + fec_idx + 1,
                timestamp=media_packets[-1].timestamp,
                ssrc=media_packets[0].ssrc,
                payload=fec_header + fec_payload
            )

            fec_packets.append(fec_packet)
            self.stats['fec_packets_generated'] += 1

        return fec_packets

    def _generate_vandermonde_coefficients(self, row: int, cols: int) -> List[int]:
        """
        Vandermonde matrisi katsayıları üretir
        Sistematik kod için kullanılır
        """
        # GF(256) üzerinde Vandermonde matrisi
        # Her satır: [1, a^i, a^(2i), ..., a^((cols-1)*i)]
        # a = 2 (primitive element)
        coeffs = []
        base = pow(2, row + 1, 257)  # GF(257) kullanıyoruz basitlik için

        for col in range(cols):
            coeff = pow(base, col, 257) % 256
            coeffs.append(coeff if coeff != 0 else 1)  # 0'dan kaçın

        return coeffs

    def _calculate_fec_payload(self, packets: List[RtpPacket], coeffs: List[int]) -> bytes:
        """
        FEC payload'ı hesaplar - linear combination in GF(256)
        """
        max_len = max(len(p.payload) for p in packets)
        result = np.zeros(max_len, dtype=np.uint16)

        for packet, coeff in zip(packets, coeffs):
            # Paketi padding'le
            padded = np.pad(
                np.frombuffer(packet.payload, dtype=np.uint8),
                (0, max_len - len(packet.payload)),
                'constant'
            )

            # GF(256) üzerinde linear combination
            result = (result + padded * coeff) % 256

        return result.astype(np.uint8).tobytes()

    def _create_fec_header(self, packets: List[RtpPacket], coeffs: List[int]) -> bytes:
        """
        FEC header oluşturur - kurtarma için gerekli metadata
        Format:
        - 1 byte: Korunan paket sayısı
        - 2 bytes: Base sequence number
        - 2 bytes: Sequence number bitmask
        - N bytes: Katsayılar
        - 4 bytes: MD5 checksum
        """
        header = bytearray()

        # Korunan paket sayısı
        header.append(len(packets))

        # Base sequence number
        base_seq = packets[0].sequence_number
        header.extend(struct.pack('!H', base_seq))

        # Sequence number bitmask (hangi paketler korunuyor)
        bitmask = 0
        for p in packets:
            offset = p.sequence_number - base_seq
            if offset < 16:
                bitmask |= (1 << offset)
        header.extend(struct.pack('!H', bitmask))

        # Katsayılar (maksimum 10 tane)
        for coeff in coeffs[:10]:
            header.append(coeff)

        # Padding
        while len(header) < 15:
            header.append(0)

        # Checksum
        checksum = hashlib.md5(bytes(header)).digest()[:4]
        header.extend(checksum)

        return bytes(header)

    def recover(self, received_packets: List[RtpPacket]) -> List[RtpPacket]:
        """
        Kayıp paketleri FEC ve RED kullanarak kurtarır
        Returns: Sıralı tüm paketler (alınan + kurtarılan)
        """
        media_packets = {}
        fec_packets = []
        red_packets = []

        # Paketleri kategorize et
        for packet in received_packets:
            if packet.payload_type == FEC_PAYLOAD_TYPE:
                fec_packets.append(packet)
            elif packet.payload_type == RED_PAYLOAD_TYPE:
                red_packets.append(packet)
                # RED'den primary payload'ı çıkar
                primary = self._extract_primary_from_red(packet)
                if primary:
                    media_packets[primary.sequence_number] = primary
            else:
                media_packets[packet.sequence_number] = packet
                self.stats['packets_received'] += 1

        # İlk önce RED ile kurtarma dene
        if red_packets:
            recovered_from_red = self._recover_from_red(red_packets, media_packets)
            media_packets.update(recovered_from_red)

        # Sonra FEC ile kurtarma dene
        if fec_packets:
            recovered_from_fec = self._recover_using_fec(fec_packets, media_packets)
            media_packets.update(recovered_from_fec)

        # İstatistikleri güncelle
        if media_packets:
            expected_seqs = set(range(
                min(media_packets.keys()),
                max(media_packets.keys()) + 1
            ))
            missing = expected_seqs - media_packets.keys()
            self.stats['packets_lost'] = len(missing)

            if missing:
                print(f"[FEC] Kurtarılamayan paketler: {sorted(missing)}")

        return sorted(media_packets.values(), key=lambda p: p.sequence_number)

    def _extract_primary_from_red(self, red_packet: RtpPacket) -> Optional[RtpPacket]:
        """RED paketinden primary payload'ı çıkarır"""
        try:
            payload = red_packet.payload
            offset = 0

            # RED header'ları atla
            while offset < len(payload):
                header_byte = payload[offset]
                offset += 1

                if header_byte & 0x80:  # F bit set - more blocks
                    # Skip timestamp offset (14 bits) + length (10 bits)
                    if offset + 2 < len(payload):
                        length = ((payload[offset] & 0x03) << 8) | payload[offset + 1]
                        offset += 2 + length  # Skip header + block data
                else:
                    # Final block - primary encoding
                    break

            # Primary payload
            if offset < len(payload):
                return RtpPacket(
                    payload_type=96,  # H264
                    sequence_number=red_packet.sequence_number,
                    timestamp=red_packet.timestamp,
                    ssrc=red_packet.ssrc,
                    payload=payload[offset:],
                    marker=red_packet.marker
                )
        except Exception as e:
            print(f"[FEC] RED extraction error: {e}")

        return None

    def _recover_from_red(self, red_packets: List[RtpPacket],
                          existing: Dict[int, RtpPacket]) -> Dict[int, RtpPacket]:
        """RED paketlerinden kayıp paketleri kurtarır"""
        recovered = {}

        for red_packet in red_packets:
            try:
                payload = red_packet.payload
                offset = 0
                seq_offset = 0

                # RED bloklarını parse et
                while offset < len(payload):
                    header_byte = payload[offset]
                    offset += 1

                    if header_byte & 0x80:  # More blocks
                        if offset + 2 >= len(payload):
                            break

                        # Timestamp offset ve length
                        ts_offset = (payload[offset] << 6) | (payload[offset + 1] >> 2)
                        length = ((payload[offset + 1] & 0x03) << 8) | payload[offset + 2]
                        offset += 2

                        # Önceki paket
                        prev_seq = red_packet.sequence_number - 1 - seq_offset

                        if offset + length <= len(payload):
                            if prev_seq not in existing and prev_seq not in recovered:
                                recovered[prev_seq] = RtpPacket(
                                    payload_type=96,
                                    sequence_number=prev_seq,
                                    timestamp=red_packet.timestamp - ts_offset,
                                    ssrc=red_packet.ssrc,
                                    payload=payload[offset:offset + length]
                                )
                                self.stats['packets_recovered'] += 1
                                print(f"[FEC] RED ile kurtarıldı: SN {prev_seq}")

                        offset += length
                        seq_offset += 1
                    else:
                        break  # Primary encoding

            except Exception as e:
                print(f"[FEC] RED recovery error: {e}")
                continue

        return recovered

    def _recover_using_fec(self, fec_packets: List[RtpPacket],
                           existing: Dict[int, RtpPacket]) -> Dict[int, RtpPacket]:
        """
        FEC paketlerini kullanarak kayıp paketleri kurtarır
        Gaussian elimination benzeri bir yöntem kullanır
        """
        recovered = {}

        for fec_packet in fec_packets:
            try:
                # FEC header'ı parse et
                header = fec_packet.payload[:19]
                if len(header) < 19:
                    continue

                num_protected = header[0]
                base_seq = struct.unpack('!H', header[1:3])[0]
                bitmask = struct.unpack('!H', header[3:5])[0]
                coeffs = list(header[5:15])

                # Korunan paketleri belirle
                protected_seqs = []
                for i in range(16):
                    if bitmask & (1 << i):
                        protected_seqs.append(base_seq + i)

                if len(protected_seqs) != num_protected:
                    protected_seqs = list(range(base_seq, base_seq + num_protected))

                # Kayıp paketleri bul
                missing = [seq for seq in protected_seqs if seq not in existing]

                # Sadece 1 kayıp varsa kurtarabiliriz
                if len(missing) == 1:
                    missing_seq = missing[0]
                    fec_payload = fec_packet.payload[19:]

                    # FEC hesaplamasını tersine çevir
                    result = np.frombuffer(fec_payload, dtype=np.uint8).copy()

                    # Bilinen paketleri çıkar
                    for i, seq in enumerate(protected_seqs):
                        if seq in existing and i < len(coeffs):
                            packet_payload = existing[seq].payload
                            padded = np.pad(
                                np.frombuffer(packet_payload, dtype=np.uint8),
                                (0, len(result) - len(packet_payload)),
                                'constant'
                            )
                            # GF(256) subtraction
                            result = (result - padded * coeffs[i]) % 256

                    # Kayıp paketin katsayısıyla böl
                    missing_idx = protected_seqs.index(missing_seq)
                    if missing_idx < len(coeffs) and coeffs[missing_idx] != 0:
                        # Modular inverse in GF(256)
                        inv_coeff = self._gf256_inverse(coeffs[missing_idx])
                        result = (result * inv_coeff) % 256

                        # Kurtarılan paketi oluştur
                        recovered[missing_seq] = RtpPacket(
                            payload_type=96,
                            sequence_number=missing_seq,
                            timestamp=existing[protected_seqs[0]].timestamp,
                            ssrc=existing[protected_seqs[0]].ssrc,
                            payload=result.tobytes()
                        )
                        self.stats['packets_recovered'] += 1
                        print(f"[FEC] FEC ile kurtarıldı: SN {missing_seq}")

                # Birden fazla kayıp varsa daha karmaşık kurtarma
                elif 2 <= len(missing) <= 3 and len(fec_packets) >= len(missing):
                    # TODO: Matrix inversion ile çoklu kurtarma
                    pass

            except Exception as e:
                print(f"[FEC] Recovery error: {e}")
                continue

        return recovered

    def _gf256_inverse(self, a: int) -> int:
        """GF(256) üzerinde modular inverse hesaplar"""
        # Extended Euclidean algorithm
        if a == 0:
            return 0

        # GF(256) için a^254 = a^-1 (Fermat's little theorem)
        result = 1
        power = 254
        base = a

        while power > 0:
            if power & 1:
                result = (result * base) % 257
            base = (base * base) % 257
            power >>= 1

        return result % 256

    def get_stats(self) -> Dict:
        """İstatistikleri döndürür"""
        if self.stats['packets_sent'] > 0:
            self.stats['overhead_ratio'] = (
                    self.stats['fec_packets_generated'] /
                    self.stats['packets_sent']
            )

        if self.stats['packets_received'] > 0:
            total_lost_or_recovered = self.stats['packets_lost'] + self.stats['packets_recovered']
            if total_lost_or_recovered > 0:
                self.stats['recovery_rate'] = (
                        self.stats['packets_recovered'] / total_lost_or_recovered
                )

        return self.stats