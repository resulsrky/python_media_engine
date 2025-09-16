# main.py (DOĞRU pipeline.add() KULLANIMI İLE GÜNCELLENDİ)

import sys
import gi
import argparse

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# GStreamer'ı başlat
Gst.init(None)


def on_pad_added(rtpbin, new_pad, fecdec):
    """
    rtpbin elemanı bir medya akışı tespit ettiğinde bu fonksiyon tetiklenir.
    """
    sink_pad = fecdec.get_static_pad("sink")
    if not sink_pad.is_linked():
        print(f"rtpbin'in yeni akış pedi ({new_pad.get_name()}) rtpulpfecdec'e bağlanıyor...")
        new_pad.link(sink_pad)


def create_receiver_pipeline(port):
    """
    Alıcı pipeline'ını programatik olarak oluşturur ve bağlar.
    """
    pipeline = Gst.Pipeline.new("receiver-pipeline")

    # Gerekli tüm elemanları oluştur
    rtpbin = Gst.ElementFactory.make("rtpbin", "rtpbin")
    rtp_udpsrc = Gst.ElementFactory.make("udpsrc", "rtp_udpsrc")
    fec_udpsrc = Gst.ElementFactory.make("udpsrc", "fec_udpsrc")
    rtcp_udpsrc = Gst.ElementFactory.make("udpsrc", "rtcp_udpsrc")
    rtpulpfecdec = Gst.ElementFactory.make("rtpulpfecdec", "rtpulpfecdec")
    rtpjitterbuffer = Gst.ElementFactory.make("rtpjitterbuffer", "rtpjitterbuffer")
    rtph264depay = Gst.ElementFactory.make("rtph264depay", "rtph264depay")
    avdec_h264 = Gst.ElementFactory.make("avdec_h264", "avdec_h264")
    videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")
    autovideosink = Gst.ElementFactory.make("autovideosink", "autovideosink")

    all_elements = [pipeline, rtpbin, rtp_udpsrc, fec_udpsrc, rtcp_udpsrc, rtpulpfecdec, rtpjitterbuffer, rtph264depay,
                    avdec_h264, videoconvert, autovideosink]
    if not all(all_elements):
        print("HATA: GStreamer elemanlarından biri oluşturulamadı.")
        return None

    # Elemanların özelliklerini ayarla
    rtp_udpsrc.set_property("port", port)
    rtp_udpsrc.set_property("caps", Gst.Caps.from_string(
        "application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)H264,payload=(int)96"))

    fec_udpsrc.set_property("port", port + 4)
    fec_udpsrc.set_property("caps", Gst.Caps.from_string(
        "application/x-rtp,media=(string)application,clock-rate=(int)90000,encoding-name=(string)ulpfec,payload=(int)127"))

    rtcp_udpsrc.set_property("port", port + 5)

    rtpjitterbuffer.set_property("latency", 80)
    autovideosink.set_property("sync", False)

    # DÜZELTME: Tüm elemanları pipeline'a TEK TEK ekliyoruz.
    for element in all_elements[1:]:  # pipeline'ı kendine eklememek için [1:]
        pipeline.add(element)

    # Pad'leri birbirine bağlıyoruz
    rtp_sink_pad = rtpbin.get_request_pad("recv_rtp_sink_0")
    rtp_udpsrc.get_static_pad("src").link(rtp_sink_pad)

    fec_sink_pad = rtpbin.get_request_pad("recv_rtp_sink_1")
    fec_udpsrc.get_static_pad("src").link(fec_sink_pad)

    rtcp_sink_pad = rtpbin.get_request_pad("recv_rtcp_sink_0")
    rtcp_udpsrc.get_static_pad("src").link(rtcp_sink_pad)

    rtpulpfecdec.link(rtpjitterbuffer)
    rtpjitterbuffer.link(rtph264depay)
    rtph264depay.link(avdec_h264)
    avdec_h264.link(videoconvert)
    videoconvert.link(autovideosink)

    rtpbin.connect("pad-added", on_pad_added, rtpulpfecdec)

    return pipeline


def on_message(bus: Gst.Bus, message: Gst.Message, loop: GLib.MainLoop):
    mtype = message.type
    if mtype == Gst.MessageType.EOS:
        print("Akış sonlandı.")
        loop.quit()
    elif mtype == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"HATA: {err}. Detaylar: {debug}")
        loop.quit()
    elif mtype == Gst.MessageType.WARNING:
        err, debug = message.parse_warning()
        print(f"UYARI: {err}. Detaylar: {debug}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Resilient RTP GStreamer Streaming Application")
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser_listen = subparsers.add_parser("listen", help="Listen for an incoming RTP stream")
    parser_listen.add_argument("--port", type=int, default=5000, help="Base port to listen on")
    parser_connect = subparsers.add_parser("connect", help="Connect to an RTP listener")
    parser_connect.add_argument("--host", required=True, help="The host IP to connect to")
    parser_connect.add_argument("--port", type=int, default=5000, help="The base host port to connect to")
    args = parser.parse_args()

    pipeline = None
    if args.command == "listen":
        print(f"Alıcı başlatılıyor, RTP akışı {args.port} portunda bekleniyor...")
        pipeline = create_receiver_pipeline(args.port)
    elif args.command == "connect":
        from config import GST_SENDER_PIPELINE
        base_port = args.port
        pipeline_str = GST_SENDER_PIPELINE.format(
            host=args.host, rtp_port=base_port, fec_port=base_port + 4, rtcp_port=base_port + 1
        )
        print(f"Gönderici başlatılıyor, {args.host}:{base_port} adresine yayın yapılıyor...")
        pipeline = Gst.parse_launch(pipeline_str)

    if not pipeline:
        print("HATA: Pipeline oluşturulamadı.")
        sys.exit(1)

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    loop = GLib.MainLoop()
    bus.connect("message", on_message, loop)

    print("Pipeline başlatılıyor...")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except KeyboardInterrupt:
        print("Durduruluyor.")
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()