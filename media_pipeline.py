# media_pipeline.py
import gi
import threading
import asyncio

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GObject

from config import GST_SENDER_PIPELINE, GST_RECEIVER_PIPELINE


class GStreamerPipeline:
    def __init__(self, loop: asyncio.AbstractEventLoop, output_queue: asyncio.Queue):
        Gst.init(None)
        self.loop = loop
        self.output_queue = output_queue  # Python'a (webrtc_handler'a) paket göndermek için
        self.pipeline = None
        self.appsrc = None

        # GStreamer'ın kendi ana döngüsü için ayrı bir thread
        self.glib_loop = GObject.MainLoop()
        self.thread = threading.Thread(target=self.glib_loop.run)
        self.thread.daemon = True

    def start_sender(self):
        print("Starting GStreamer sender pipeline...")
        self.pipeline = Gst.parse_launch(GST_SENDER_PIPELINE)
        appsink = self.pipeline.get_by_name('appsink')
        appsink.set_property('emit-signals', True)
        appsink.connect('new-sample', self._on_new_sample, None)
        self.pipeline.set_state(Gst.State.PLAYING)
        if not self.thread.is_alive():
            self.thread.start()

    def start_receiver(self):
        print("Starting GStreamer receiver pipeline...")
        self.pipeline = Gst.parse_launch(GST_RECEIVER_PIPELINE)
        self.appsrc = self.pipeline.get_by_name('appsrc')
        self.pipeline.set_state(Gst.State.PLAYING)
        if not self.thread.is_alive():
            self.thread.start()

    def push_packet(self, data: bytes):
        """Ağdan gelen RTP paketini alıcı pipeline'ına besler."""
        if self.appsrc:
            buf = Gst.Buffer.new_wrapped(data)
            self.appsrc.emit('push-buffer', buf)

    def _on_new_sample(self, appsink, user_data):
        """appsink'ten gelen her yeni RTP paketi için çağrılır."""
        sample = appsink.emit('pull-sample')
        if sample:
            buf = sample.get_buffer()
            data = buf.extract_dup(0, buf.get_size())
            # asyncio thread'ine güvenli bir şekilde veri göndermek için:
            self.loop.call_soon_threadsafe(self.output_queue.put_nowait, data)
        return Gst.FlowReturn.OK

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        self.glib_loop.quit()