# adaptive_controller.py
import time
from resilience import FecHandler


class AdaptiveController:
    def __init__(self, fec_handler: FecHandler):
        self.fec_handler = fec_handler
        self._last_check_time = time.time()
        self._stats = {}

    def process_stats(self, stats):
        """aiortc'den gelen istatistikleri işler."""
        # Bu örnekte, basitlik adına istatistikleri doğrudan işlemek yerine
        # sahte bir paket kaybı senaryosu yaratacağız.
        # Gerçek uygulamada 'packetsLost' değerini parse etmelisiniz.
        pass

    def adapt(self):
        """Periyodik olarak çağrılarak adaptasyon mantığını çalıştırır."""
        if time.time() - self._last_check_time > 5.0:  # Her 5 saniyede bir kontrol et
            # TODO: Gerçek paket kaybı oranını 'self._stats'den al.
            # Şimdilik sahte bir değer kullanalım.
            simulated_packet_loss = 0.0  # Normalde 0.0 olmalı

            if simulated_packet_loss > 0.05:  # %5'ten fazla kayıp
                new_level = min(self.fec_handler.protection_level + 0.1, 0.8)
                if new_level > self.fec_handler.protection_level:
                    print(f"[ADAPT] High packet loss! Increasing FEC to {new_level:.2f}")
                    self.fec_handler.protection_level = new_level

            elif simulated_packet_loss < 0.01:  # %1'den az kayıp
                new_level = max(self.fec_handler.protection_level - 0.05, 0.1)
                if new_level < self.fec_handler.protection_level:
                    print(f"[ADAPT] Network is stable. Decreasing FEC to {new_level:.2f}")
                    self.fec_handler.protection_level = new_level

            self._last_check_time = time.time()