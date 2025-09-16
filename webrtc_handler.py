# webrtc_handler.py (TAM VE EKSİKSİZ KOD)

import asyncio
import json
import logging
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtp import RtpPacket
from aiohttp import web, ClientSession

from media_pipeline import GStreamerPipeline
from resilience import FecHandler
from adaptive_controller import AdaptiveController
from config import FEC_GROUP_SIZE, FEC_PAYLOAD_TYPE


class WebRTCHandler:
    def __init__(self):
        self.pc = RTCPeerConnection()
        self.loop = asyncio.get_event_loop()

        self.fec_handler = FecHandler()
        self.adaptive_controller = AdaptiveController(self.fec_handler)
        self.gstreamer_output_queue = asyncio.Queue()
        self.media_pipeline = GStreamerPipeline(self.loop, self.gstreamer_output_queue)

        self.data_channel = None
        self.receiver_packet_buffer = []

        @self.pc.on("datachannel")
        def on_datachannel(channel):
            print(f"Veri kanalı '{channel.label}' uzaktan oluşturuldu.")
            self.data_channel = channel

            @channel.on("message")
            def on_message(message):
                if isinstance(message, bytes):
                    self.receiver_packet_buffer.append(message)

    async def handle_offer(self, request):
        params = await request.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        await self.pc.setRemoteDescription(offer)

        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        print("SDP Teklifi alındı, cevap oluşturuldu.")
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type}
            ),
        )

    async def run_listener(self, port):
        self.media_pipeline.start_receiver()
        self.loop.create_task(self.process_receiver_buffer())

        app = web.Application()
        app.router.add_post("/offer", self.handle_offer)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        print(f"Dinleyici başlatıldı. {port} portunda SDP teklifi bekleniyor...")
        await site.start()

        await self.wait_for_connection()
        print("WebRTC bağlantısı kuruldu! Medya akışı başlıyor...")

    async def run_connector(self, host, port):
        self.data_channel = self.pc.createDataChannel("rtp-data", ordered=False, maxRetransmits=0)

        @self.data_channel.on("open")
        def on_open():
            print("Veri kanalı açık. Medya pipeline'ı başlatılıyor.")
            self.media_pipeline.start_sender()
            self.loop.create_task(self.process_gstreamer_output())
            self.loop.create_task(self.run_adaptation())

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        url = f"http://{host}:{port}/offer"
        print(f"{url} adresine SDP teklifi gönderiliyor...")

        body = {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type}

        try:
            async with ClientSession() as session:
                async with session.post(url, json=body) as response:
                    if response.status == 200:
                        data = await response.json()
                        answer = RTCSessionDescription(**data)
                        print("SDP Cevabı alındı.")
                        await self.pc.setRemoteDescription(answer)
                    else:
                        print(f"Hata: Sunucudan {response.status} kodu alındı. Dinleyici çalışıyor mu?")
                        return
        except Exception as e:
            print(f"Bağlantı hatası: {e}. Dinleyici IP ({host}) ve port ({port}) doğru mu?")
            return

        await self.wait_for_connection()
        print("WebRTC bağlantısı kuruldu! Medya akışı başlıyor...")

    async def wait_for_connection(self):
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            print(f"ICE bağlantı durumu: {self.pc.iceConnectionState}")

        while self.pc.iceConnectionState not in ["connected", "completed"]:
            await asyncio.sleep(0.1)
            if self.pc.iceConnectionState in ["failed", "closed", "disconnected"]:
                raise Exception("ICE bağlantısı kurulamadı.")

    async def process_gstreamer_output(self):
        while True:
            raw_packet = await self.gstreamer_output_queue.get()
            rtp_packet = RtpPacket.parse(raw_packet)
            packets_to_send = self.fec_handler.protect(rtp_packet)
            if packets_to_send:
                for pkt in packets_to_send:
                    if self.data_channel and self.data_channel.readyState == "open":
                        self.data_channel.send(pkt.serialize())

    async def process_receiver_buffer(self):
        """
        Gecikme testi için basitleştirilmiş buffer mantığı.
        GStreamer'daki rtpjitterbuffer'a güveniyoruz.
        """
        while True:
            if not self.receiver_packet_buffer:
                await asyncio.sleep(0.005)
                continue

            # Tampondaki tüm paketleri işle
            packets_to_process_raw = self.receiver_packet_buffer
            self.receiver_packet_buffer = []

            # --- BASİTLEŞTİRİLMİŞ TEST ---
            # Gecikmenin tamamen gittiğinden emin olmak için FEC'siz deniyoruz.
            for raw_packet in packets_to_process_raw:
                packet = RtpPacket.parse(raw_packet)
                # Sadece medya paketlerini yolla, FEC'i atla
                if packet.payload_type != FEC_PAYLOAD_TYPE:
                    self.media_pipeline.push_packet(packet.serialize())

    async def run_adaptation(self):
        while True:
            await asyncio.sleep(5)
            self.adaptive_controller.adapt()

    async def close(self):
        print("Her şey kapatılıyor.")
        if self.pc:
            await self.pc.close()
        self.media_pipeline.stop()