from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from app.models import ExperimentCommand, ExperimentResultBatch, LogPage, TelemetryFrame


class PayloadHandler(ABC):
    """
    Contrato que todo payload deve implementar.
    O NascerrClient nunca conhece detalhes de nenhum payload concreto.
    """

    @abstractmethod
    def initialize(self) -> None:
        """Inicializa hardware, wakeup, calibração inicial, etc."""

    @abstractmethod
    def read_telemetry(self) -> TelemetryFrame:
        """
        Lê uma amostra de telemetria (sensores + GNSS + firmware).
        Chamado periodicamente pelo loop de telemetria do cliente.
        """

    @abstractmethod
    def execute_command(self, cmd: ExperimentCommand) -> None:
        """
        Despacha um comando de experimento para o hardware.
        Lança RuntimeError em caso de falha — o cliente fará o NACK.
        """

    @abstractmethod
    def collect_results(self) -> Optional[ExperimentResultBatch]:
        """
        Coleta resultados de experimentos finalizados.
        Retorna None se não há nada novo a enviar.
        """

    @abstractmethod
    def sync_logs(self) -> list[LogPage]:
        """
        Retorna páginas de log não lidas.
        O cliente envia cada página via gRPC após receber a lista.
        """
    
    @abstractmethod
    def read_firmware_version(self) -> str:
        """Retorna a versão de firmware atual do hardware."""

    def collect_watchdog_failures(self) -> Optional[List[int]]:
        """
        Retorna None se nenhum reset ocorreu.
        Retorna [] se houve reset mas sem CMDs pendentes.
        Retorna [ids] se houve reset com CMDs perdidos.
        """
        return None