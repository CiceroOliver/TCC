# app/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional
from nascerr_modules.nascerr_command import W_DataTypeDef as WriteType
from nascerr_modules.nascerr_system import NASCERR_MODE as EccMode
from nascerr_modules.nascerr_system import ERROS_LOGS as LogStatus

# ============================================================
# Telemetria / Hardware
# ============================================================

@dataclass
class HardwareTelemetry:
    temperature_c: float = 0.0
    total_power: float = 0.0
    currents: Dict[str, float] = field(default_factory=dict)
    status: str = "OK"  # "OK" / "ERROR" (simples e direto)


@dataclass
class GnssFix:
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_msl: float = 0.0
    gnss_tow: int = 0
    gnss_week: int = 0
    ecef_x: float = 0.0
    ecef_y: float = 0.0
    ecef_z: float = 0.0


@dataclass
class ImageReport:
    image_load: bool
    image_size: int
    image_name: str

    
@dataclass
class TelemetryFrame:
    """
    Um "frame" de telemetria que o app pode produzir periodicamente.
    O mapper gRPC decide como isso vira protobuf (TelemetryPacket).
    """
    device_name: str
    hw: Optional[HardwareTelemetry] = None
    gnss: Optional[GnssFix] = None
    logs: List["LogEntry"] = field(default_factory=list)
    image: Optional[ImageReport] = None
    ts_unix: Optional[int] = None  # opcional: timestamp local


# ============================================================
# 3) Comandos (do servidor -> cliente -> hardware)
# ============================================================

@dataclass
class ExperimentCommand:
    """
    Representa o comando de experimento vindo do servidor (protobuf),
    mas aqui em forma neutra.
    """
    id: int
    block: int
    qtd_block: int
    ecc: int
    write_type: int
    scheduler_time: int = 0
    repeat: int = 0
    delay: int = 0

# ============================================================
# 4) Experimentos (do hardware -> cliente -> servidor)
# ============================================================

@dataclass
class EccTelemetry:
    single_bit_errors: int = 0
    double_bit_errors: int = 0
    triple_bit_errors: int = 0
    beg_region_errors: int = 0
    mid_region_errors: int = 0
    end_region_errors: int = 0
    uncorrectable_errors: int = 0
    ecc: str = ""
    ecc_status: int = 0


@dataclass
class ExperimentResult:
    """
    Um resultado "unitário" (uma entrada) retornado por read_experiment().
    """
    experiment_id: int
    block: int = 0
    data_length: int = 0
    time_delta: int = 0
    number_errors: int = 0
    corrected_data: int = 0
    percent_error: float = 0.0

    ecc_telemetry: dict = field(default_factory=dict)
    mdca_data: dict = field(default_factory=dict) 

    # Opcional: salvar o bruto do hw pra debug/auditoria
    raw: Optional[dict] = None


@dataclass
class ExperimentResultBatch:
    device_name: str
    results: List[ExperimentResult] = field(default_factory=list)


# ============================================================
# 5) Logs (do hardware -> cliente -> servidor)
# ============================================================

@dataclass
class LogEntry:
    timestamp: int
    type: str
    status: LogStatus
    error_code: str


@dataclass
class LogPage:
    """
    Se você quiser manter a noção de página (útil pra ack/futuro).
    """
    page_idx: int
    entries: List[LogEntry] = field(default_factory=list)