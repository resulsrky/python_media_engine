# resilience.py (ÇOKLU PAKET KAYBI İÇİN YENİDEN YAZILDI)

import numpy as np
from aiortc.rtp import RtpPacket
from config import FEC_PAYLOAD_TYPE
from typing import List, Dict, Set


class FecHandler:
    def __init__(self, group_size=8, protection_level=1.0):
        self.group_size = group_size
        self.protection_level = protection_level
        self._media_packet_buffer: List[RtpPacket] = []

    def protect(self, packet: RtpPacket) -> List[RtpPacket]:
        packets_to_send = [packet]
        self._media_packet_buffer.append(packet)

        if len(self._media_packet_buffer) >= self.group_size:
            fec_packets = self._generate_fec(self._media_packet_buffer)
            packets_to_send.extend(fec_packets)
            self._media_packet_buffer = []

        return packets_to_send

    def _generate_fec(self, media_packets: List[RtpPacket]) -> List[RtpPacket]:
        num_fec_packets = int(self.group_size * self.protection_level)
        if num_fec_packets == 0 or not media_packets:
            return []

        print(f"-> {self.group_size} medya paketine karşılık {num_fec_packets} FEC paketi oluşturuluyor.")

        first_packet = media_packets[0]
        max_len = max(len(p.payload) for p in media_packets)

        # Medya paketlerini payload'ları ile birlikte bir dictionary'de sakla
        media_payloads = {
            p.sequence_number: np.pad(np.frombuffer(p.payload, dtype=np.uint8), (0, max_len - len(p.payload)))
            for p in media_packets
        }

        fec_packets = []
        # Her FEC paketi için farklı bir XOR kombinasyonu oluştur
        for i in range(num_fec_packets):
            # Basit bir kombinasyon: i'ninci FEC paketi, i'ninci paketten başlayarak tüm paketleri XOR'lar
            # Bu, her FEC paketinin farklı olmasını sağlar.
            xor_result = np.zeros(max_len, dtype=np.uint8)
            protected_sns = []

            for j in range(self.group_size):
                # Döngüsel bir şekilde paketleri seç
                packet_to_xor = media_packets[(i + j) % self.group_size]
                xor_result = np.bitwise_xor(xor_result, media_payloads[packet_to_xor.sequence_number])
                protected_sns.append(packet_to_xor.sequence_number)

            # FEC Başlığı: [korunan_sn_1, korunan_sn_2, ...]
            fec_header = b''.join([sn.to_bytes(2, 'big') for sn in protected_sns])
            fec_payload = fec_header + xor_result.tobytes()

            fec_packet = RtpPacket(
                payload_type=FEC_PAYLOAD_TYPE,
                sequence_number=first_packet.sequence_number + self.group_size + i,
                timestamp=first_packet.timestamp,
                ssrc=first_packet.ssrc,
                payload=fec_payload
            )
            fec_packets.append(fec_packet)

        return fec_packets

    def recover(self, received_packets: List[RtpPacket]) -> List[RtpPacket]:
        media_packets = {p.sequence_number: p for p in received_packets if p.payload_type != FEC_PAYLOAD_TYPE}
        fec_packets = [p for p in received_packets if p.payload_type == FEC_PAYLOAD_TYPE]

        if not fec_packets:
            return sorted(media_packets.values(), key=lambda p: p.sequence_number)

        # Kurtarma işlemini denemek için bir döngü başlat
        # Bazen bir paketi kurtarmak, başka bir paketin de kurtarılmasını tetikleyebilir.
        for _ in range(len(fec_packets)):  # En fazla FEC paketi sayısı kadar deneme yap
            made_recovery = False
            for fec_packet in fec_packets:
                header_len = self.group_size * 2
                if len(fec_packet.payload) < header_len: continue

                protected_sns = {int.from_bytes(fec_packet.payload[i:i + 2], 'big') for i in range(0, header_len, 2)}

                known_sns = protected_sns.intersection(media_packets.keys())
                missing_sns = protected_sns.difference(media_packets.keys())

                # Eğer bu FEC denklemiyle sadece 1 bilinmeyen (kayıp paket) varsa, onu çözebiliriz
                if len(missing_sns) == 1 and len(known_sns) == self.group_size - 1:
                    missing_sn = missing_sns.pop()
                    print(f"Kayıp tespit edildi: SN {missing_sn}. Kurtarma başlıyor...")

                    xor_payload = fec_packet.payload[header_len:]
                    max_len = len(xor_payload)
                    xor_result = np.frombuffer(xor_payload, dtype=np.uint8)

                    for sn in known_sns:
                        payload_array = np.frombuffer(media_packets[sn].payload, dtype=np.uint8)
                        payload_padded = np.pad(payload_array, (0, max_len - len(payload_array)), 'constant')
                        xor_result = np.bitwise_xor(xor_result, payload_padded)

                    first_packet = next(iter(media_packets.values()))
                    recovered_packet = RtpPacket(
                        payload_type=first_packet.payload_type,
                        sequence_number=missing_sn,
                        timestamp=first_packet.timestamp,
                        ssrc=first_packet.ssrc,
                        payload=xor_result.tobytes()
                    )
                    print(f"PAKET KURTARILDI: SN {missing_sn}")
                    media_packets[missing_sn] = recovered_packet
                    made_recovery = True

            # Eğer bu turda hiçbir paket kurtaramadıysak, daha fazla denemenin anlamı yok.
            if not made_recovery:
                break

        return sorted(media_packets.values(), key=lambda p: p.sequence_number)