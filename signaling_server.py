# signaling_server.py (GÜNCELLENMİŞ KOD)

import asyncio
import websockets
import json
import logging

# Loglamayı ayarla
logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    level=logging.INFO,
)

# Bağlı olan tüm kullanıcıları tutan set
USERS = set()


async def handler(websocket):
    """Her yeni WebSocket bağlantısı için bu fonksiyon çalışır."""
    USERS.add(websocket)
    logging.info(f"Yeni bağlantı kuruldu. Toplam kullanıcı: {len(USERS)}")
    try:
        # Bağlantı açık olduğu sürece gelen mesajları dinle
        async for message in websocket:
            # Gelen mesajı gönderen hariç herkese yayınla
            other_users = [user for user in USERS if user != websocket]
            if other_users:
                await asyncio.wait([user.send(message) for user in other_users])
                logging.info(f"Mesaj {len(other_users)} kullanıcıya iletildi")
    finally:
        # Bağlantı kapandığında kullanıcıyı set'ten çıkar
        USERS.remove(websocket)
        logging.info(f"Bağlantı kapandı. Kalan kullanıcı: {len(USERS)}")


async def main():
    """Sunucuyu başlatan ve sonsuza kadar çalıştıran ana fonksiyon."""
    from config import SIGNALING_HOST, SIGNALING_PORT
    logging.info(f"İşaretleşme sunucusu başlatılıyor: ws://{SIGNALING_HOST}:{SIGNALING_PORT}")

    # "async with" yapısı sunucunun düzgün bir şekilde başlatılmasını ve kapatılmasını sağlar
    async with websockets.serve(handler, SIGNALING_HOST, SIGNALING_PORT):
        await asyncio.Future()  # Sunucuyu sonsuza kadar çalıştır


if __name__ == "__main__":
    try:
        # Modern asyncio uygulamalarını başlatma yöntemi
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Sunucu kapatılıyor.")