from __future__ import annotations

import threading
import time
from typing import List

from nascerr_modules.nascerr_operation import Operation

from app.models import LogEntry, LogPage, LogStatus

from nascerr_logging.mylogger import get_logger

log_logger = get_logger("logs")
grpc_logger = get_logger("grpc")


class LogSync:
    """
    Responsável por varrer as páginas de log do hardware,
    coletar as que estão NOT_READ e retorná-las como lista de LogPage.
    """

    # Status numérico → LogStatus (conforme nascerr_modules)
    _STATUS_MAP: dict[int, str] = {
        0: "NONE",
        1: "SUCCESS",
        2: "WARN",
        3: "ERROR",
    }

    def __init__(self, op: Operation, hw_lock: threading.Lock) -> None:
        self._op = op
        self._hw_lock = hw_lock
        self._last_sync_time = 0.0
        self._sync_interval = 60 * 5

    def collect(self) -> List[LogPage]:
        """
        Varre todas as páginas de log.
        Retorna apenas as que têm status NOT_READ, com seus LogEntry.
        Páginas vazias (timestamp=0 ou type ausente) são filtradas.
        """
        pages: List[LogPage] = []

        # 1. Lê o sumário sem selecionar página para obter total_pages
        if time.time() - self._last_sync_time < self._sync_interval:
            log_logger.debug("Coleta de logs ignorada: intervalo mínimo não atingido.")
            return pages

        with self._hw_lock:
            initial = self._op.get_log_summary()

        if initial.get("error"):
            log_logger.error(
                f"Erro ao ler sumário inicial: {initial.get('log_error')}")
            return pages

        total_pages = initial["data"].get("total_pages", 0)
        if total_pages == 0:
            return pages

        # 2. Itera somente verificando o header de cada página
        for page_idx in range(total_pages):
            with self._hw_lock:
                self._op.set_log_page(page_idx)
                time.sleep(0.2)
                summary = self._op.get_log_summary()

            if summary.get("error"):
                log_logger.error(
                    f"Erro no header da pág {page_idx}")
                continue

            status = summary["data"].get(
                "page_header", {}).get("status", "UNKNOWN")
            if status != "NOT_READ":
                continue

            # 3. Lê conteúdo completo apenas para páginas NOT_READ
            with self._hw_lock:
                content = self._op.get_log_page_content()

            if content.get("error"):
                log_logger.error(
                    f"Erro ao ler conteúdo da pág {page_idx}")
                continue

            entries = self._parse_entries(content.get("data", []))
            if entries:
                pages.append(LogPage(page_idx=page_idx, entries=entries))
                log_logger.info(
                    f"Página {page_idx} coletada ({len(entries)} entradas)")

        return pages

    def _parse_entries(self, raw_data: list) -> List[LogEntry]:
        entries: List[LogEntry] = []
        for item in raw_data:
            if not item.get("timestamp") or item.get("type", {}).get("name") == "LOG_VOID":
                continue

            raw_status = item.get("status", 0)
            status_str = self._STATUS_MAP.get(raw_status, str(raw_status))

            entries.append(LogEntry(
                timestamp=int(item["timestamp"]),
                type=str(item["type"]["name"]),
                status=status_str,
                error_code=str(item["error_code"]["name"]),
            ))
        return entries
