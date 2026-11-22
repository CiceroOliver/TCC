from __future__ import annotations
import os
import time
import threading
from math import ceil
from typing import Dict, Callable, Optional
from nascerr_modules.nascerr_operation import Operation
from app.models import ImageReport


class ImageReportReader:
    def __init__(self, op: Operation, hw_lock: threading.Lock) -> None:
        self._op = op
        self._hw_lock = hw_lock
        self._image_status_content = None

    def read(self) -> ImageReport:
        """Retorna uma leitura de relatório de imagem ."""
        with self._hw_lock:
            # comando para atualizar o status da imagem
            self._image_status_content = self._op.get_image_status_content()

        if self._image_status_content["error"]:
            return ImageReport(
                image_load=False,
                image_size=0,
                image_name=""
            )

        return ImageReport(
            image_load=(self._image_status_content["data"]["valid"] == 0xAA),
            image_size=self._image_status_content["data"]["image_size"],
            image_name="image_test.jpg"
        )
