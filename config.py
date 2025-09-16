# config.py (H264 YAZIM HATASI DÜZELTİLDİ)

# Kullanıcı tarafından belirtilen ana port
DEFAULT_PORT = 5000

# --- GÖNDERİCİ PİPELİNE ---
GST_SENDER_PIPELINE = (
    "rtpbin name=rtpbin "
    "v4l2src device=/dev/video0 ! "
    "videoconvert ! "
    "video/x-raw,format=I420,width=640,height=480,framerate=30/1 ! "
    "x264enc tune=zerolatency speed-preset=veryfast bitrate=2500 key-int-max=60 ! "
    "rtph264pay pt=96 ! "
    "tee name=t ! queue ! rtpbin.send_rtp_sink_0 "
    "t. ! queue ! "
    "rtpulpfecenc pt=127 percentage=10 ! "
    "rtpbin.send_rtp_sink_1 "
    "rtpbin.send_rtp_src_0 ! udpsink host={host} port={rtp_port} "
    "rtpbin.send_rtp_src_1 ! udpsink host={host} port={fec_port} "
    "rtpbin.send_rtcp_src_0 ! udpsink host={host} port={rtcp_port} sync=false async=false"
)

# --- ALICI PİPELİNE ---
GST_RECEIVER_PIPELINE = (
    "rtpbin name=rtpbin "
    # DÜZELTME: rtpbin'e girmeden önce bir 'queue' elemanı ekliyoruz.
    "udpsrc port={rtp_port} caps=\"application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)H264,payload=(int)96\" ! queue ! rtpbin.recv_rtp_sink_0 "
    # DÜZELTME: rtpbin'e girmeden önce bir 'queue' elemanı ekliyoruz.
    "udpsrc port={fec_port} caps=\"application/x-rtp,media=(string)application,clock-rate=(int)90000,encoding-name=(string)ulpfec,payload=(int)127\" ! queue ! rtpbin.recv_rtp_sink_1 "
    "udpsrc port={rtcp_port} ! rtpbin.recv_rtcp_sink_0 "
    "rtpbin. ! "
    "rtpulpfecdec ! "
    "rtpjitterbuffer latency=80 ! "
    "rtph264depay ! "
    "avdec_h264 ! "
    "videoconvert ! "
    "autovideosink sync=false"
)