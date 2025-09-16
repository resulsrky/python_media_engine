# enhanced_main.py
"""
Ana uygulama - UDP üzerinden doğrudan RTP iletimi
WebRTC yerine saf UDP kullanımı
"""

import asyncio
import socket
import struct
import time
import argparse
from typing import Optional, Tuple
import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import threading
from collections import deque

from enhanced_resilience import (
    RtpPacket,
    EnhancedFecHandler,
    AdaptiveBitrateController,
    PacketBuffer
)

# Initialize GStreamer
Gst.init(None)


class UdpRtpTransport:
    """UDP-based RTP transport with RTCP support"""

    def __init__(self, local_port: int = 5000):
        self.local_port = local_port
        self.remote_addr = None

        # Create UDP sockets
        self.rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rtp_socket.bind(('0.0.0.0', local_port))
        self.rtp_socket.setblocking(False)

        self.rtcp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rtcp_socket.bind(('0.0.0.0', local_port + 1))
        self.rtcp_socket.setblocking(False)

        # Statistics
        self.stats = {
            'packets_sent': 0,
            'packets_received': 0,
            'bytes_sent': 0,
            'bytes_received': 0,
            'last_rtt': 0,
            'last_loss_rate': 0
        }

    def set_remote(self, host: str, port: int):
        """Set remote endpoint"""
        self.remote_addr = (host, port)
        print(f"[Transport] Remote endpoint set to {host}:{port}")

    async def send_rtp(self, data: bytes):
        """Send RTP packet"""
        if self.remote_addr:
            try:
                self.rtp_socket.sendto(data, self.remote_addr)
                self.stats['packets_sent'] += 1
                self.stats['bytes_sent'] += len(data)
            except Exception as e:
                print(f"[Transport] Send error: {e}")

    async def receive_rtp(self) -> Optional[bytes]:
        """Receive RTP packet"""
        try:
            data, addr = self.rtp_socket.recvfrom(65535)
            if not self.remote_addr:
                # Auto-learn remote address
                self.remote_addr = addr
                print(f"[Transport] Learned remote: {addr}")

            self.stats['packets_received'] += 1
            self.stats['bytes_received'] += len(data)
            return data
        except BlockingIOError:
            return None
        except Exception as e:
            print(f"[Transport] Receive error: {e}")
            return None

    async def send_rtcp(self, data: bytes):
        """Send RTCP packet"""
        if self.remote_addr:
            rtcp_addr = (self.remote_addr[0], self.remote_addr[1] + 1)
            try:
                self.rtcp_socket.sendto(data, rtcp_addr)
            except:
                pass

    async def receive_rtcp(self) -> Optional[bytes]:
        """Receive RTCP packet"""
        try:
            data, _ = self.rtcp_socket.recvfrom(65535)
            self._parse_rtcp(data)
            return data
        except BlockingIOError:
            return None
        except:
            return None

    def _parse_rtcp(self, data: bytes):
        """Parse RTCP packet for statistics"""
        if len(data) < 8:
            return

        # Simple SR/RR parsing
        pt = data[1] & 0x7F
        if pt == 200:  # SR
            if len(data) >= 28:
                # Extract NTP timestamp
                ntp_sec = struct.unpack('!I', data[8:12])[0]
                ntp_frac = struct.unpack('!I', data[12:16])[0]
                rtp_ts = struct.unpack('!I', data[16:20])[0]

                # Calculate RTT if we have sent an RR
                # (simplified - would need to track RR timestamps)
                self.stats['last_rtt'] = 50  # Mock RTT

        elif pt == 201:  # RR
            if len(data) >= 32:
                # Extract loss info
                lost = struct.unpack('!I', data[12:16])[0]
                fraction_lost = (lost >> 24) & 0xFF
                self.stats['last_loss_rate'] = fraction_lost / 256.0

    def close(self):
        """Close sockets"""
        self.rtp_socket.close()
        self.rtcp_socket.close()


class GStreamerMediaPipeline:
    """GStreamer pipeline for media capture and playback"""

    def __init__(self, mode: str, callback=None):
        self.mode = mode
        self.callback = callback
        self.pipeline = None
        self.loop = GLib.MainLoop()
        self.thread = None
        self.current_bitrate = 2500000

        # Packet queue for thread safety
        self.packet_queue = deque(maxlen=1000)

    def start_sender(self, video_source: str = "/dev/video0"):
        """Start sender pipeline"""
        pipeline_str = f"""
            v4l2src device={video_source} !
            videoconvert !
            video/x-raw,format=I420,width=640,height=480,framerate=30/1 !
            x264enc tune=zerolatency speed-preset=ultrafast bitrate={self.current_bitrate // 1000} 
                    key-int-max=30 threads=2 !
            rtph264pay config-interval=1 mtu=1400 pt=96 !
            appsink name=appsink emit-signals=true sync=false
        """

        self.pipeline = Gst.parse_launch(pipeline_str)

        # Connect to appsink
        appsink = self.pipeline.get_by_name('appsink')
        appsink.connect('new-sample', self._on_new_sample)

        # Start pipeline
        self.pipeline.set_state(Gst.State.PLAYING)

        # Start GLib loop in thread
        self.thread = threading.Thread(target=self.loop.run, daemon=True)
        self.thread.start()

        print(f"[GStreamer] Sender pipeline started (bitrate: {self.current_bitrate})")

    def start_receiver(self):
        """Start receiver pipeline"""
        pipeline_str = """
            appsrc name=appsrc caps=application/x-rtp,media=video,clock-rate=90000,encoding-name=H264 !
            rtpjitterbuffer latency=100 !
            rtph264depay !
            avdec_h264 !
            videoconvert !
            autovideosink sync=false
        """

        self.pipeline = Gst.parse_launch(pipeline_str)
        self.appsrc = self.pipeline.get_by_name('appsrc')

        # Start pipeline
        self.pipeline.set_state(Gst.State.PLAYING)

        # Start GLib loop in thread
        self.thread = threading.Thread(target=self.loop.run, daemon=True)
        self.thread.start()

        print("[GStreamer] Receiver pipeline started")

    def push_rtp_packet(self, data: bytes):
        """Push RTP packet to receiver pipeline"""
        if self.appsrc:
            buf = Gst.Buffer.new_wrapped(data)
            self.appsrc.emit('push-buffer', buf)

    def _on_new_sample(self, appsink):
        """Handle new sample from appsink"""
        sample = appsink.emit('pull-sample')
        if sample:
            buf = sample.get_buffer()
            data = buf.extract_dup(0, buf.get_size())

            # Queue packet for async processing
            try:
                self.packet_queue.append(data)
                if self.callback:
                    self.callback(data)
            except:
                pass

        return Gst.FlowReturn.OK

    def get_packet(self) -> Optional[bytes]:
        """Get packet from queue"""
        if self.packet_queue:
            return self.packet_queue.popleft()
        return None

    def update_bitrate(self, bitrate: int):
        """Update encoder bitrate"""
        if self.mode == 'sender' and self.pipeline:
            encoder = self.pipeline.get_by_name('x264enc')
            if encoder:
                encoder.set_property('bitrate', bitrate // 1000)
                self.current_bitrate = bitrate
                print(f"[GStreamer] Bitrate updated to {bitrate}")

    def stop(self):
        """Stop pipeline"""
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self.loop:
            self.loop.quit()


class RtpMediaEngine:
    """
    Main RTP Media Engine
    Combines all components for resilient streaming
    """

    def __init__(self, mode: str, local_port: int = 5000):
        self.mode = mode
        self.running = False

        # Core components
        self.transport = UdpRtpTransport(local_port)
        self.fec_handler = EnhancedFecHandler(
            group_size=10,
            fec_ratio=0.3,  # 30% FEC for 20% loss tolerance
            enable_red=True
        )
        self.abr_controller = AdaptiveBitrateController()
        self.packet_buffer = PacketBuffer(
            target_delay_ms=100,
            max_delay_ms=500
        )

        # GStreamer pipeline
        self.media_pipeline = GStreamerMediaPipeline(mode)

        # Sequence tracking
        self.send_seq = 0
        self.send_timestamp = 0
        self.ssrc = int(time.time()) & 0xFFFFFFFF

        # Statistics
        self.last_stats_time = time.time()
        self.last_rtcp_time = time.time()

    async def start_sender(self, remote_host: str, remote_port: int,
                           video_source: str = "/dev/video0"):
        """Start as sender"""
        self.transport.set_remote(remote_host, remote_port)
        self.media_pipeline.start_sender(video_source)
        self.running = True

        print(f"[Engine] Starting sender to {remote_host}:{remote_port}")

        # Start tasks
        await asyncio.gather(
            self._sender_loop(),
            self._rtcp_sender_loop(),
            self._stats_loop()
        )

    async def start_receiver(self):
        """Start as receiver"""
        self.media_pipeline.start_receiver()
        self.running = True

        print(f"[Engine] Starting receiver on port {self.transport.local_port}")

        # Start tasks
        await asyncio.gather(
            self._receiver_loop(),
            self._rtcp_receiver_loop(),
            self._playback_loop(),
            self._stats_loop()
        )

    async def _sender_loop(self):
        """Main sender loop"""
        receive_buffer = []
        buffer_timeout = 0.001  # 1ms

        while self.running:
            # Get packet from GStreamer
            raw_packet = self.media_pipeline.get_packet()

            if raw_packet:
                # Parse RTP packet
                packet = RtpPacket.parse(raw_packet)

                # Update sequence and timestamp
                packet.sequence_number = self.send_seq
                packet.timestamp = self.send_timestamp
                packet.ssrc = self.ssrc

                # Determine priority (keyframe = high priority)
                if packet.marker:  # Simplified keyframe detection
                    packet.priority = 2
                else:
                    packet.priority = 0

                # Apply FEC protection
                protected_packets = self.fec_handler.protect(packet)

                # Send all packets
                for pkt in protected_packets:
                    await self.transport.send_rtp(pkt.serialize())

                # Update counters
                self.send_seq = (self.send_seq + 1) & 0xFFFF
                self.send_timestamp += 3000  # 30fps @ 90kHz

            # Also check for incoming RTCP
            rtcp_data = await self.transport.receive_rtcp()
            if rtcp_data:
                # Update network metrics
                self.abr_controller.update_metrics(
                    rtt=self.transport.stats['last_rtt'],
                    loss_rate=self.transport.stats['last_loss_rate'],
                    jitter=10  # Mock jitter
                )

                # Update bitrate
                target_bitrate = self.abr_controller.calculate_target_bitrate()
                if target_bitrate != self.media_pipeline.current_bitrate:
                    self.media_pipeline.update_bitrate(target_bitrate)

            await asyncio.sleep(0.001)  # 1ms loop

    async def _receiver_loop(self):
        """Main receiver loop"""
        receive_buffer = []
        buffer_duration_ms = 20  # Collect packets for 20ms
        last_buffer_time = time.time()

        while self.running:
            # Receive RTP packets
            data = await self.transport.receive_rtp()

            if data:
                receive_buffer.append(data)

            # Process buffer periodically
            current_time = time.time()
            if (current_time - last_buffer_time) * 1000 >= buffer_duration_ms:
                if receive_buffer:
                    # Apply FEC recovery
                    recovered_packets = self.fec_handler.recover(receive_buffer)

                    # Add to playback buffer
                    for packet in recovered_packets:
                        self.packet_buffer.push(packet)

                    # Clear receive buffer
                    receive_buffer = []

                last_buffer_time = current_time

            await asyncio.sleep(0.0001)  # 100us loop for low latency

    async def _playback_loop(self):
        """Playback loop for receiver"""
        target_interval_ms = 33  # ~30fps
        last_playback_time = time.time()

        while self.running:
            current_time = time.time()
            elapsed_ms = (current_time - last_playback_time) * 1000

            if elapsed_ms >= target_interval_ms:
                # Get packet from buffer
                packet = self.packet_buffer.pop()

                if packet:
                    # Send to GStreamer for decoding
                    self.media_pipeline.push_rtp_packet(packet.serialize())

                last_playback_time = current_time

            await asyncio.sleep(0.001)

    async def _rtcp_sender_loop(self):
        """Send RTCP reports periodically"""
        while self.running:
            current_time = time.time()

            if current_time - self.last_rtcp_time >= 1.0:  # Every second
                # Create SR (Sender Report) if sender
                if self.mode == 'sender':
                    rtcp = self._create_sender_report()
                else:
                    rtcp = self._create_receiver_report()

                await self.transport.send_rtcp(rtcp)
                self.last_rtcp_time = current_time

            await asyncio.sleep(0.1)

    async def _rtcp_receiver_loop(self):
        """Receive RTCP reports"""
        while self.running:
            data = await self.transport.receive_rtcp()
            await asyncio.sleep(0.01)

    def _create_sender_report(self) -> bytes:
        """Create RTCP Sender Report"""
        # Simplified SR packet
        version = 2
        padding = 0
        rc = 0  # Reception report count
        pt = 200  # SR
        length = 6  # Length in 32-bit words - 1

        byte0 = (version << 6) | (padding << 5) | rc

        # NTP timestamp (simplified)
        ntp_sec = int(time.time()) + 2208988800  # NTP epoch
        ntp_frac = 0

        # RTP timestamp
        rtp_ts = self.send_timestamp

        # Sender's packet count
        packet_count = self.transport.stats['packets_sent']
        octet_count = self.transport.stats['bytes_sent']

        return struct.pack('!BBHIIIII',
                           byte0, pt, length, self.ssrc,
                           ntp_sec, ntp_frac, rtp_ts,
                           packet_count, octet_count)

    def _create_receiver_report(self) -> bytes:
        """Create RTCP Receiver Report"""
        # Simplified RR packet
        version = 2
        padding = 0
        rc = 1  # One reception report
        pt = 201  # RR
        length = 7  # Length in 32-bit words - 1

        byte0 = (version << 6) | (padding << 5) | rc

        # Report block
        reporter_ssrc = self.ssrc
        source_ssrc = 0  # Should be sender's SSRC

        # Loss statistics
        stats = self.fec_handler.get_stats()
        fraction_lost = int(stats.get('packets_lost', 0) * 256 /
                            max(1, stats.get('packets_received', 1)))
        cumulative_lost = stats.get('packets_lost', 0)

        # Extended highest sequence number
        highest_seq = self.send_seq

        # Jitter (simplified)
        jitter = 100

        # LSR and DLSR (simplified)
        lsr = 0
        dlsr = 0

        return struct.pack('!BBHIIBBHIIII',
                           byte0, pt, length, reporter_ssrc,
                           source_ssrc, fraction_lost,
                           (cumulative_lost >> 16) & 0xFF,
                           (cumulative_lost >> 8) & 0xFF,
                           cumulative_lost & 0xFF,
                           highest_seq, jitter, lsr, dlsr)

    async def _stats_loop(self):
        """Print statistics periodically"""
        while self.running:
            current_time = time.time()

            if current_time - self.last_stats_time >= 5.0:  # Every 5 seconds
                print("\n=== Statistics ===")
                print(f"Transport: {self.transport.stats}")
                print(f"FEC: {self.fec_handler.get_stats()}")
                print(f"ABR: bitrate={self.abr_controller.current_bitrate}")
                print(f"Buffer: depth={self.packet_buffer.get_depth_ms()}ms")
                print("==================\n")

                self.last_stats_time = current_time

            await asyncio.sleep(1.0)

    async def stop(self):
        """Stop engine"""
        self.running = False
        self.media_pipeline.stop()
        self.transport.close()
        print("[Engine] Stopped")


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='RTP Media Engine with 20% packet loss resilience')
    subparsers = parser.add_subparsers(dest='mode', required=True)

    # Receiver mode
    parser_rx = subparsers.add_parser('receive', help='Start as receiver')
    parser_rx.add_argument('--port', type=int, default=5000,
                           help='Local UDP port (default: 5000)')

    # Sender mode
    parser_tx = subparsers.add_parser('send', help='Start as sender')
    parser_tx.add_argument('--host', required=True,
                           help='Remote host IP')
    parser_tx.add_argument('--port', type=int, default=5000,
                           help='Remote UDP port (default: 5000)')
    parser_tx.add_argument('--video', default='/dev/video0',
                           help='Video device (default: /dev/video0)')

    args = parser.parse_args()

    if args.mode == 'receive':
        engine = RtpMediaEngine('receiver', args.port)
        await engine.start_receiver()

    elif args.mode == 'send':
        engine = RtpMediaEngine('sender')
        await engine.start_sender(args.host, args.port, args.video)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")