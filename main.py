# main.py (AĞ İLETİŞİMİ İYİLEŞTİRİLMİŞ SON VERSİYON)
"""
Ana uygulama - UDP üzerinden doğrudan RTP iletimi
WebRTC yerine saf UDP kullanımı
"""
import asyncio
import socket
import struct
import time
import argparse
from typing import Optional, List
import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import threading
from collections import deque

from aiortc.rtp import RtpPacket
from resilience import FecHandler as EnhancedFecHandler
from adaptive_controller import AdaptiveController as AdaptiveBitrateController
from packet_buffer import PacketBuffer

Gst.init(None)


class UdpRtpTransport:
    def __init__(self, local_port: int = 5000):
        self.local_port = local_port
        self.remote_addr = None
        self.loop = asyncio.get_event_loop()

        self.rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rtp_socket.bind(('0.0.0.0', local_port))
        self.rtp_socket.setblocking(False)

        self.rtcp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rtcp_socket.bind(('0.0.0.0', local_port + 1))
        self.rtcp_socket.setblocking(False)

        self.stats = {'packets_sent': 0, 'packets_received': 0, 'bytes_sent': 0, 'bytes_received': 0, 'last_rtt': 0,
                      'last_loss_rate': 0}

    def set_remote(self, host: str, port: int):
        self.remote_addr = (host, port)
        print(f"[Transport] Uzak sunucu ayarlandı: {host}:{port}")

    async def send_rtp(self, data: bytes):
        if self.remote_addr:
            try:
                await self.loop.sock_sendto(self.rtp_socket, data, self.remote_addr)
                self.stats['packets_sent'] += 1
                self.stats['bytes_sent'] += len(data)
            except Exception as e:
                print(f"[Transport] Send RTP Error: {e}")

    async def receive_rtp(self) -> Optional[bytes]:
        try:
            data, addr = await self.loop.sock_recvfrom(self.rtp_socket, 2048)
            if not self.remote_addr:
                self.remote_addr = addr
                print(f"[Transport] Uzak adres öğrenildi: {addr}")
            self.stats['packets_received'] += 1
            self.stats['bytes_received'] += len(data)
            return data
        except (BlockingIOError, ConnectionRefusedError):
            return None
        except Exception as e:
            print(f"[Transport] Receive RTP Error: {e}")
            return None

    async def send_rtcp(self, data: bytes):
        if self.remote_addr:
            rtcp_addr = (self.remote_addr[0], self.remote_addr[1] + 1)
            try:
                await self.loop.sock_sendto(self.rtcp_socket, data, rtcp_addr)
            except Exception as e:
                print(f"[Transport] Send RTCP Error: {e}")

    async def receive_rtcp(self) -> Optional[bytes]:
        try:
            data, _ = await self.loop.sock_recvfrom(self.rtcp_socket, 2048)
            self._parse_rtcp(data)
            return data
        except (BlockingIOError, ConnectionRefusedError):
            return None
        except Exception as e:
            print(f"[Transport] Receive RTCP Error: {e}")
            return None

    def _parse_rtcp(self, data: bytes):
        if len(data) < 8: return
        pt = data[1]
        if pt == 201 and len(data) >= 24:
            self.stats['last_loss_rate'] = data[12] / 256.0

    def close(self):
        self.rtp_socket.close()
        self.rtcp_socket.close()


class GStreamerMediaPipeline:
    def __init__(self, mode: str):
        self.mode = mode
        self.pipeline = None
        self.appsrc = None
        self.loop = GLib.MainLoop()
        self.thread = threading.Thread(target=self.loop.run, daemon=True)
        self.current_bitrate = 2500000
        self.packet_queue = deque(maxlen=1000)

    def start_sender(self, video_source: str = "/dev/video0"):
        pipeline_str = f"""
            v4l2src device={video_source} !
            videoconvert ! video/x-raw,format=I420,width=640,height=480,framerate=30/1 !
            x264enc name=x264enc tune=zerolatency speed-preset=ultrafast bitrate={self.current_bitrate // 1000} key-int-max=30 !
            rtph264pay config-interval=1 mtu=1400 pt=96 !
            appsink name=appsink emit-signals=true sync=false max-buffers=1 drop=true
        """
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.pipeline.get_by_name('appsink').connect('new-sample', self._on_new_sample)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.thread.start()
        print(f"[GStreamer] Gönderici pipeline'ı başlatıldı (bitrate: {self.current_bitrate})")

    def start_receiver(self):
        pipeline_str = """
            appsrc name=appsrc format=time is-live=true do-timestamp=true caps=application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96 !
            rtpjitterbuffer latency=100 ! rtph264depay ! avdec_h264 ! videoconvert ! autovideosink sync=false
        """
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.appsrc = self.pipeline.get_by_name('appsrc')
        self.pipeline.set_state(Gst.State.PLAYING)
        self.thread.start()
        print("[GStreamer] Alıcı pipeline'ı başlatıldı")

    def push_rtp_packet(self, data: bytes):
        if self.appsrc:
            self.appsrc.emit('push-buffer', Gst.Buffer.new_wrapped(data))

    def _on_new_sample(self, appsink):
        sample = appsink.emit('pull-sample')
        if sample:
            self.packet_queue.append(sample.get_buffer().extract_dup(0, sample.get_buffer().get_size()))
        return Gst.FlowReturn.OK

    def get_packet(self) -> Optional[bytes]:
        return self.packet_queue.popleft() if self.packet_queue else None

    def update_bitrate(self, bitrate: int):
        if self.mode == 'sender' and self.pipeline:
            encoder = self.pipeline.get_by_name('x264enc')
            if encoder:
                GLib.idle_add(encoder.set_property, 'bitrate', bitrate // 1000)
                self.current_bitrate = bitrate
                print(f"[GStreamer] Bitrate güncellendi: {bitrate / 1000000:.2f} Mbps")

    def stop(self):
        if self.pipeline: self.pipeline.set_state(Gst.State.NULL)
        if self.loop.is_running(): self.loop.quit()


class RtpMediaEngine:
    def __init__(self, mode: str, local_port: int = 5000):
        self.mode = mode
        self.running = False
        self.transport = UdpRtpTransport(local_port)
        self.fec_handler = EnhancedFecHandler(group_size=10, protection_level=0.3, enable_red=True)
        self.abr_controller = AdaptiveBitrateController(fec_handler=self.fec_handler)
        self.packet_buffer = PacketBuffer(target_delay_ms=100, max_delay_ms=500)
        self.media_pipeline = GStreamerMediaPipeline(mode)
        self.ssrc = int(time.time()) & 0xFFFFFFFF
        self.send_seq, self.send_timestamp = 0, 0
        self.last_stats_time, self.last_rtcp_time = time.time(), time.time()

    async def start_sender(self, remote_host: str, remote_port: int, video_source: str = "/dev/video0"):
        self.transport.set_remote(remote_host, remote_port)
        self.media_pipeline.start_sender(video_source)
        self.running = True
        print(f"[Engine] Gönderici başlatılıyor -> {remote_host}:{remote_port}")
        await asyncio.gather(self._sender_loop(), self._rtcp_loop(), self._stats_loop())

    async def start_receiver(self):
        self.media_pipeline.start_receiver()
        self.running = True
        print(f"[Engine] Alıcı başlatılıyor, port: {self.transport.local_port}")
        await asyncio.gather(self._receiver_loop(), self._rtcp_loop(), self._playback_loop(), self._stats_loop())

    async def _sender_loop(self):
        while self.running:
            raw_packet = self.media_pipeline.get_packet()
            if raw_packet:
                packet = RtpPacket.parse(raw_packet)
                packet.sequence_number, packet.timestamp, packet.ssrc = self.send_seq, self.send_timestamp, self.ssrc
                protected_packets = self.fec_handler.protect(packet)
                await asyncio.gather(*(self.transport.send_rtp(pkt.serialize()) for pkt in protected_packets))
                self.send_seq = (self.send_seq + 1) & 0xFFFF
                self.send_timestamp += 3000
            await asyncio.sleep(0.005)

    async def _receiver_loop(self):
        receive_buffer_raw: List[bytes] = []
        last_buffer_time = time.time()
        while self.running:
            data = await self.transport.receive_rtp()
            if data: receive_buffer_raw.append(data)
            if (time.time() - last_buffer_time) * 1000 >= 20:
                if receive_buffer_raw:
                    packets = [RtpPacket.parse(p) for p in receive_buffer_raw if len(p) > 12]
                    if packets:
                        for p in self.fec_handler.recover(packets): self.packet_buffer.push(p)
                    receive_buffer_raw.clear()
                last_buffer_time = time.time()
            await asyncio.sleep(0.001)

    async def _playback_loop(self):
        while self.running:
            packet = self.packet_buffer.pop()
            if packet: self.media_pipeline.push_rtp_packet(packet.serialize())
            await asyncio.sleep(0.01)

    async def _rtcp_loop(self):
        while self.running:
            await self.transport.receive_rtcp()
            if time.time() - self.last_rtcp_time >= 2.0:
                rtcp_packet = self._create_receiver_report() if self.mode == 'receiver' else self._create_sender_report()
                if rtcp_packet: await self.transport.send_rtcp(rtcp_packet)
                self.last_rtcp_time = time.time()
            await asyncio.sleep(1)

    def _create_sender_report(self) -> bytes:
        header = struct.pack('!BBH', (2 << 6) | 0, 200, 6)
        payload = struct.pack('!IIIIII', self.ssrc, int(time.time()) + 2208988800, 0,
                              self.send_timestamp, self.transport.stats['packets_sent'],
                              self.transport.stats['bytes_sent'])
        return header + payload

    def _create_receiver_report(self) -> bytes:
        if not self.transport.remote_addr: return b''
        header = struct.pack('!BBH', (2 << 6) | 1, 201, 7)
        reporter_ssrc = struct.pack('!I', self.ssrc)
        fec_stats = self.fec_handler.get_stats()
        total_expected = fec_stats.get('packets_received', 0) + fec_stats.get('packets_recovered', 0) + fec_stats.get(
            'packets_lost', 0)
        lost_count = fec_stats.get('packets_lost', 0)
        fraction_lost = int((lost_count * 256) / total_expected) if total_expected > 0 else 0
        report_block = struct.pack('!I B 3s IIII', 0, fraction_lost, (lost_count & 0xFFFFFF).to_bytes(3, 'big'), 0, 0,
                                   0, 0)
        return header + reporter_ssrc + report_block

    async def _stats_loop(self):
        while self.running:
            await asyncio.sleep(5.0)
            print("\n--- İSTATİSTİKLER ---")
            print(f"Taşıma: {self.transport.stats}")
            print(f"FEC: {self.fec_handler.get_stats()}")
            if self.mode == 'sender': print(f"ABR: {self.abr_controller.get_current_settings()}")
            print(f"Buffer: {self.packet_buffer.get_stats()}")
            print("---------------------\n")

    async def stop(self):
        self.running = False
        await asyncio.sleep(0.1)
        self.media_pipeline.stop()
        self.transport.close()
        print("[Engine] Durduruldu")


async def main():
    parser = argparse.ArgumentParser(description='Paket kaybına dayanıklı RTP Medya Motoru')
    subparsers = parser.add_subparsers(dest='mode', required=True)
    parser_rx = subparsers.add_parser('receive', help='Alıcı olarak başlat')
    parser_rx.add_argument('--port', type=int, default=5000, help='Yerel UDP portu')
    parser_tx = subparsers.add_parser('send', help='Gönderici olarak başlat')
    parser_tx.add_argument('--host', required=True, help='Uzak sunucu IP adresi')
    parser_tx.add_argument('--port', type=int, default=5000, help='Uzak UDP portu')
    parser_tx.add_argument('--video', default='/dev/video0', help='Video kaynağı')
    args = parser.parse_args()

    engine = None
    try:
        engine = RtpMediaEngine(args.mode, args.port) if args.mode == 'receive' else RtpMediaEngine(args.mode)
        if args.mode == 'receive':
            await engine.start_receiver()
        else:
            await engine.start_sender(args.host, args.port, args.video)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nKapatılıyor...")
    finally:
        if engine: await engine.stop()


if __name__ == '__main__':
    asyncio.run(main())