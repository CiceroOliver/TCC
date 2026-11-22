from __future__ import annotations

import random
import time

from app.models import GnssFix

from nascerr_logging.mylogger import get_logger

class GnssReader:
    """
    Leitor de GNSS. Atualmente mockado com coordenadas de Fortaleza + ruído.
    Substituir pelo driver real quando o hardware GNSS estiver disponível.
    """

    # Coordenadas base — Fortaleza, CE
    _LAT_BASE = -3.717
    _LON_BASE = -38.543
    _ALT_BASE = 25.0
    _GNSS_WEEK = 2200

    def read(self) -> GnssFix:
        """Retorna uma leitura GNSS (mock com pequena variação aleatória)."""
        return GnssFix(
            latitude=self._LAT_BASE  + random.uniform(-0.0001, 0.0001),
            longitude=self._LON_BASE + random.uniform(-0.0001, 0.0001),
            altitude_msl=self._ALT_BASE + random.uniform(-0.5, 0.5),
            gnss_tow=int(time.time() % 604800),
            gnss_week=self._GNSS_WEEK,
            ecef_x=6378137.0 + random.uniform(-10, 10),
            ecef_y=0.0,
            ecef_z=0.0,
        )