# config.py - GÜNCELLENMİŞ KONFİGÜRASYON

# Temel port ayarları
DEFAULT_PORT = 5000
SIGNALING_HOST = "0.0.0.0"
SIGNALING_PORT = 8080

# RTP Payload Types
RTP_PAYLOAD_TYPE = 96      # H264 video
FEC_PAYLOAD_TYPE = 127     # ULPFEC
RED_PAYLOAD_TYPE = 100     # Redundancy Encoding

# FEC Parametreleri
FEC_GROUP_SIZE = 10        # Bir FEC grubundaki paket sayısı
FEC_PROTECTION_LEVEL = 0.3 # %30 FEC (10 paket için 3 FEC paketi)
FEC_ENABLE_RED = True      # RED encoding aktif

# Adaptive Bitrate Parametreleri
INITIAL_BITRATE = 2500000  # 2.5 Mbps başlangıç
MIN_BITRATE = 500000       # 500 Kbps minimum
MAX_BITRATE = 8000000      # 8 Mbps maksimum

# Buffer Parametreleri
JITTER_BUFFER_MS = 100     # 100ms jitter buffer
MAX_BUFFER_MS = 500        # 500ms maksimum buffer

# GStreamer Pipeline'ları - UDP TRANSPORT İÇİN
GST_SENDER_PIPELINE_UDP = """
    v4l2src device={device} ! 
    videoconvert ! 
    video/x-raw,format=I420,width=640,height=480,framerate=30/1 ! 
    x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate} 
            key-int-max=30 threads=2 ! 
    rtph264pay config-interval=1 mtu=1400 pt=96 ! 
    udpsink host={host} port={port}
"""

GST_RECEIVER_PIPELINE_UDP = """
    udpsrc port={port} caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96" ! 
    rtpjitterbuffer latency={jitter_buffer} ! 
    rtph264depay ! 
    avdec_h264 ! 
    videoconvert ! 
    autovideosink sync=false
"""

# WebRTC Pipeline'ları (mevcut kod için)
GST_SENDER_PIPELINE = (
    "rtpbin name=rtpbin "
    "v4l2src device=/dev/video0 ! "
    "videoconvert ! "
    "video/x-raw,format=I420,width=640,height=480,framerate=30/1 ! "
    "x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=30 ! "
    "rtph264pay pt=96 config-interval=1 ! "
    "tee name=t ! queue ! rtpbin.send_rtp_sink_0 "
    "t. ! queue ! "
    "rtpulpfecenc pt=127 percentage=30 ! "  # %30 FEC
    "rtpbin.send_rtp_sink_1 "
    "rtpbin.send_rtp_src_0 ! udpsink host={host} port={rtp_port} "
    "rtpbin.send_rtp_src_1 ! udpsink host={host} port={fec_port} "
    "rtpbin.send_rtcp_src_0 ! udpsink host={host} port={rtcp_port} sync=false async=false"
)

GST_RECEIVER_PIPELINE = (
    "rtpbin name=rtpbin "
    "udpsrc port={rtp_port} caps=\"application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)H264,payload=(int)96\" ! "
    "queue ! rtpbin.recv_rtp_sink_0 "
    "udpsrc port={fec_port} caps=\"application/x-rtp,media=(string)application,clock-rate=(int)90000,encoding-name=(string)ulpfec,payload=(int)127\" ! "
    "queue ! rtpbin.recv_rtp_sink_1 "
    "udpsrc port={rtcp_port} ! rtpbin.recv_rtcp_sink_0 "
    "rtpbin. ! "
    "rtpulpfecdec ! "
    "rtpjitterbuffer latency=100 ! "
    "rtph264depay ! "
    "avdec_h264 ! "
    "videoconvert ! "
    "autovideosink sync=false"
)

# İstatistik Parametreleri
STATS_INTERVAL = 5.0       # İstatistik yazdırma aralığı (saniye)
RTCP_INTERVAL = 1.0        # RTCP rapor gönderme aralığı (saniye)

# Network Simulation Parametreleri (test için)
TEST_PACKET_LOSS = 0.0     # Test için paket kaybı oranı (0.0-1.0)
TEST_DELAY_MS = 0           # Test için gecikme (ms)
TEST_JITTER_MS = 0          # Test için jitter (ms)