from __future__ import annotations
import asyncio
import os
import sys

_grpc_dir = os.path.dirname(os.path.abspath(__file__))  # .../gRPC/
# .../NASCERR_Application/
_projeto_root = os.path.dirname(_grpc_dir)
_proto_dir = os.path.join(_grpc_dir, "proto")
sys.path.insert(0, _grpc_dir)
sys.path.insert(0, _proto_dir)     # para encontrar proto/ dentro de gRPC/
# para encontrar app/, payloads/, roboc_payload/
sys.path.insert(0, _projeto_root)

from datetime import datetime, timezone
from nascerr_logging.mylogger import get_logger
from payloads.base import PayloadHandler
from app.models import ExperimentCommand, EccMode, WriteType
from proto import experiment_agent_pb2_grpc as pb_grpc
from proto import experiment_agent_pb2 as pb
from google.protobuf.timestamp_pb2 import Timestamp
import grpc.aio
import grpc

hardware_logger = get_logger("hardware")
comms_logger = get_logger("communication")
experiment_logger = get_logger("experiment")
log_logger = get_logger("logs")


def _now_ts() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(datetime.now(timezone.utc))
    return ts


_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms",              10_000),  # envia ping a cada 10s
    ("grpc.keepalive_timeout_ms",            5_000),  # aguarda ack por 5s
    ("grpc.keepalive_permit_without_calls",      1),  # keepalive mesmo sem chamadas ativas
    ("grpc.http2.max_pings_without_data",        0),  # sem limite de pings
    ("grpc.http2.min_time_between_pings_ms", 10_000),
]

# Timeout para chamadas unárias (o canal pode estar reconectando)
_CALL_TIMEOUT_S = 30.0


class NascerrClient:
    """
    Cliente gRPC genérico para o NASCERR.
    Não conhece Operation, I2C, experimentos ECC nem logs — apenas PayloadHandler.
    """

    def __init__(self, server_address: str, device_name: str, payload: PayloadHandler) -> None:
        self._device_name = device_name
        self._payload = payload
        self._server_address = server_address

        # Inicializados em start() pois asyncio.Lock() precisa do event loop ativo
        self._channel = None
        self._stub = None
        self._latest_frame = None
        self._latest_lock = None

    # ------------------------------------------------------------------
    # Canal gRPC
    # ------------------------------------------------------------------

    def _create_channel(self) -> grpc.aio.Channel:
        return grpc.aio.insecure_channel(self._server_address, options=_CHANNEL_OPTIONS)

    # ------------------------------------------------------------------
    # Corrotinas de cada loop
    # ------------------------------------------------------------------

    async def run_sensor_sampling_loop(self, sample_period_s: float = 0.2) -> None:
        """Amostra telemetria via payload e mantém a última leitura em cache."""
        hardware_logger.info(f"Iniciado ({sample_period_s}s)")
        loop = asyncio.get_running_loop()
        while True:
            try:
                frame = await loop.run_in_executor(None, self._payload.read_telemetry)
                async with self._latest_lock:
                    self._latest_frame = frame
            except Exception as e:
                hardware_logger.error(f"Erro: {e}")
            await asyncio.sleep(sample_period_s)

    async def run_telemetry_loop(self) -> None:
        """Envia o último frame de telemetria ao servidor via gRPC."""
        comms_logger.info("Telemetry loop Iniciado")
        while True:
            try:
                async with self._latest_lock:
                    frame = self._latest_frame

                if frame and frame.hw:
                    loop = asyncio.get_running_loop()
                    fw_version = await loop.run_in_executor(
                        None, self._payload.read_firmware_version
                    ) if hasattr(self._payload, "read_firmware_version") else "N/A"

                    packet = pb.TelemetryPacket(
                        device_name=self._device_name,
                        hw_info=pb.HardwareInfo(
                            temperature_c=frame.hw.temperature_c,
                            total_power=frame.hw.total_power,
                            currents=frame.hw.currents,
                        ),
                        mdca=pb.MDCAData(
                            latitude=frame.gnss.latitude if frame.gnss else 0.0,
                            longitude=frame.gnss.longitude if frame.gnss else 0.0,
                            altitude_msl=frame.gnss.altitude_msl if frame.gnss else 0.0,
                            gnss_tow=frame.gnss.gnss_tow if frame.gnss else 0,
                            gnss_week=frame.gnss.gnss_week if frame.gnss else 0,
                            ecef_x=frame.gnss.ecef_x if frame.gnss else 0.0,
                            ecef_y=frame.gnss.ecef_y if frame.gnss else 0.0,
                            ecef_z=frame.gnss.ecef_z if frame.gnss else 0.0,
                        ),
                        firmware=pb.FirmwareVersion(
                            firmware_version=fw_version,
                            ts=_now_ts(),
                        ),
                        image_report=pb.ImageReport(
                            image_load=frame.image.image_load,
                            image_size=frame.image.image_size,
                            image_name=frame.image.image_name,
                        ) if frame.image else None,

                        ts=_now_ts(),
                    )
                    await self._stub.SendTelemetry(iter([packet]), wait_for_ready=True, timeout=_CALL_TIMEOUT_S)

            except grpc.RpcError as e:
                comms_logger.error(f"gRPC telemetria: {e.code()}")
                await asyncio.sleep(5)
            except Exception as e:
                comms_logger.error(f"Erro telemetria: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(2)

    async def run_command_loop(self) -> None:
        """Recebe comandos do servidor, delega ao payload e reporta o ACK de execução."""
        comms_logger.info("Esperando telecomando...")
        loop = asyncio.get_running_loop()

        while True:
            try:
                stream = self._stub.PollCommands(
                    pb.DeviceRequest(device_name=self._device_name),
                    wait_for_ready=True,
                )

                async for proto_cmd in stream:
                    comms_logger.info(f"CMD Recebido ID={proto_cmd.id}")
                    
                    # 1. Mapeia para o modelo interno de domínio
                    cmd = ExperimentCommand(
                        id=proto_cmd.id,
                        block=proto_cmd.block,
                        qtd_block=proto_cmd.qtd_block,
                        ecc=int(proto_cmd.ecc),
                        write_type=int(proto_cmd.write_type),
                        scheduler_time=proto_cmd.scheduler_time,
                        repeat=proto_cmd.repeat,
                        delay=proto_cmd.delay,
                    )
                    
                    try:
                        # 2. DISPACHO: O executor roda o RoboCHandler (Phase 1 e 2)
                        # Se o hardware falhar 3x, ele lançará um RuntimeError aqui.
                        await loop.run_in_executor(
                            None, self._payload.execute_command, cmd
                        )
                        
                        # 3. REPORTA SUCESSO: Hardware confirmou Phase 1 e Phase 2
                        await self._stub.ReportCommandAck(pb.CommandAckReport(
                            device_name=self._device_name,
                            command_id=proto_cmd.id,
                            status=pb.CMD_ACK_SUCCESS
                        ), wait_for_ready=True, timeout=_CALL_TIMEOUT_S)
                        comms_logger.info(f"CMD ACK ENVIADO ID={proto_cmd.id}")

                    except Exception as e:
                        # 4. REPORTA FALHA: O hardware esgotou os retries ou deu timeout
                        comms_logger.error(f"CMD NACK ID={proto_cmd.id}: {e}")
                        
                        try:
                            await self._stub.ReportCommandAck(pb.CommandAckReport(
                                device_name=self._device_name,
                                command_id=proto_cmd.id,
                                status=pb.CMD_ACK_FAIL,
                                error=str(e)
                            ), wait_for_ready=True, timeout=_CALL_TIMEOUT_S)
                        except grpc.RpcError as rpc_err:
                            comms_logger.error(f"Erro ao reportar NACK via gRPC: {rpc_err.code()}")

            except grpc.RpcError as e:
                comms_logger.warning(f"PollCommands perdido: {e.code()}")
                await asyncio.sleep(5)
            except Exception as e:
                comms_logger.error(f"Erro inesperado no loop de comandos: {e}")
                await asyncio.sleep(5)

    async def run_experiment_collector(self) -> None:
        """Coleta resultados do payload e envia ao servidor."""
        comms_logger.info("Coletor iniciado")
        loop = asyncio.get_running_loop()
        while True:
            try:
                # Verifica reset de hardware antes de coletar resultados
                failures = await loop.run_in_executor(
                    None, self._payload.collect_watchdog_failures
                )
                if failures is not None:
                    # Reset ocorreu — reporta CMDs perdidos (se houver)
                    for exp_id in failures:
                        comms_logger.warning(
                            f"[WATCHDOG] Reportando CMD id={exp_id} como FAIL ao servidor."
                        )
                        await self._stub.ReportCommandAck(pb.CommandAckReport(
                            device_name=self._device_name,
                            command_id=exp_id,
                            status=pb.CMD_ACK_FAIL,
                            error="watchdog_reset: resultado perdido por reset do hardware",
                        ), wait_for_ready=True, timeout=_CALL_TIMEOUT_S)

                    # Notifica o servidor via TelemetryPacket mesmo sem CMDs pendentes
                    comms_logger.warning("[WATCHDOG] Notificando servidor sobre reset de hardware.")
                    await self._stub.SendTelemetry(iter([pb.TelemetryPacket(
                        device_name=self._device_name,
                        logs=[pb.LogEntry(
                            timestamp=int(datetime.now(timezone.utc).timestamp()),
                            type="HW_WATCHDOG_RESET",
                            status="WARN",
                            error_code="WARM_IWDG_RESET",
                        )],
                        ts=_now_ts(),
                    )]), wait_for_ready=True, timeout=_CALL_TIMEOUT_S)

                batch = await loop.run_in_executor(None, self._payload.collect_results)
                if batch and batch.results:
                    proto_results = [self._result_to_proto(
                        r) for r in batch.results]

                    await self._stub.SendResultBatch(pb.ExperimentResultBatch(
                        device_name=self._device_name,
                        results=proto_results,
                    ), wait_for_ready=True, timeout=_CALL_TIMEOUT_S)
                    comms_logger.info(
                        f"{len(proto_results)} resultado(s) enviados")
                else:
                    await asyncio.sleep(2)
            except grpc.RpcError as e:
                comms_logger.error(f"gRPC coletor: {e.code()}")
                await asyncio.sleep(5)
            except Exception as e:
                comms_logger.error(f"Erro coletor: {e}")
                await asyncio.sleep(2)

    async def run_log_loop(self) -> None:
        """Sincroniza logs do payload com o servidor a cada 60s."""
        await asyncio.sleep(10)  # aguarda estabilização inicial
        loop = asyncio.get_running_loop()
        while True:
            try:
                pages = await loop.run_in_executor(None, self._payload.sync_logs)
                for page in pages:
                    grpc_logs = [
                        pb.LogEntry(
                            timestamp=e.timestamp,
                            type=e.type,
                            status=e.status,
                            error_code=e.error_code,
                        )
                        for e in page.entries
                    ]
                    await self._stub.SendTelemetry(iter([pb.TelemetryPacket(
                        device_name=self._device_name,
                        logs=grpc_logs,
                    )]), wait_for_ready=True, timeout=_CALL_TIMEOUT_S)
                    log_logger.info(
                        f"Página {page.page_idx} enviada ({len(grpc_logs)} logs)")
            except grpc.RpcError as e:
                log_logger.error(f"gRPC logs: {e.code()}")
            except Exception as e:
                log_logger.error(f"Erro logs: {e}")
            await asyncio.sleep(60)

    async def run_firmware_update_loop(self) -> None:
        """Monitora se há atualizações de firmware disponíveis."""
        comms_logger.info("Monitor de Firmware iniciado")
        loop = asyncio.get_running_loop()
        
        while True:
            try:
                # PollFirmware retorna um stream de PollFirmwareResponse
                stream = self._stub.PollFirmware(
                    pb.DeviceRequest(device_name=self._device_name),
                    wait_for_ready=True,
                )
                
                async for response in stream:
                    if response.has_update:
                        update_info = response.update
                        comms_logger.info(f"NOVO FIRMWARE DETECTADO: v{update_info.version}")
                        
                        # 1. Preparar os dados
                        # O update_info.file_data contém os bytes brutos do .bin
                        try:
                            raw_version = str(update_info.version).strip()
    
                            if '.' not in raw_version and len(raw_version) >= 5:
                                v = raw_version.zfill(6)
                                major = int(v[0:2])
                                minor = int(v[2:4])
                                patch = int(v[4:6])
                            else:
                                parts = raw_version.split('.')
                                major = int(parts[0]) if len(parts) > 0 else 0
                                minor = int(parts[1]) if len(parts) > 1 else 0
                                patch = int(parts[2]) if len(parts) > 2 else 0

                            # Cálculo de deslocamento de bits (Shift)
                            version_int = (major << 16) | (minor << 8) | patch
                            
                            print(f"Versão processada: {major}.{minor}.{patch} -> Int: {version_int}")

                            success = await loop.run_in_executor(
                                None, 
                                self._payload.apply_firmware_update, 
                                update_info.file_data,
                                version_int
                            )
                            
                            if success:
                                comms_logger.info(f"Firmware v{update_info.version} aplicado com sucesso!")
                            else:
                                comms_logger.error("Payload reportou falha ao aplicar firmware.")
                                
                        except Exception as e:
                            comms_logger.error(f"Erro crítico ao aplicar firmware: {e}")
                    
            except grpc.RpcError as e:
                comms_logger.warning(f"PollFirmware perdido: {e.code()}")
                await asyncio.sleep(10)
            except Exception as e:
                comms_logger.error(f"Erro no loop de firmware: {e}")
                await asyncio.sleep(10)
                
            await asyncio.sleep(30) # Verifica a cada 30 segundos

    # ------------------------------------------------------------------
    # Mapper: ExperimentResult → protobuf
    # ------------------------------------------------------------------

    def _result_to_proto(self, r) -> pb.ExperimentResult:
        ecc = r.ecc_telemetry or {}  # agora é dict
        mdca = r.mdca_data or {}     # agora é dict, nome correto

        return pb.ExperimentResult(
            experiment_id=r.experiment_id,
            block=r.block,
            data_length=r.data_length,
            time_delta=r.time_delta,
            number_errors=r.number_errors,
            corrected_data=r.corrected_data,
            percent_error=r.percent_error,
            ecc_telemetry=pb.ECCTelemetry(
                single_bit_errors=ecc.get("single_bit_errors", 0),
                double_bit_errors=ecc.get("double_bit_errors", 0),
                triple_bit_errors=ecc.get("triple_bit_errors", 0),
                beg_region_errors=ecc.get("beg_region_errors", 0),
                mid_region_errors=ecc.get("mid_region_errors", 0),
                end_region_errors=ecc.get("end_region_errors", 0),
                uncorrectable_errors=ecc.get("uncorrectable_errors", 0),
                ecc=pb.ECCMode.Value(ecc["ecc"]) if ecc.get("ecc") else pb.ECCMode.ECC_UNKNOWN,
                ecc_status=ecc.get("ecc_status", 0),
            ),
            mdca_data=pb.MDCAData(
                gnss_week=mdca.get("gnss_week", 0),
                gnss_tow=mdca.get("gnss_tow", 0),
                latitude=mdca.get("latitude", 0.0),
                longitude=mdca.get("longitude", 0.0),
                altitude_msl=mdca.get("altitude_msl", 0.0),
            ),
        )

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._latest_lock = asyncio.Lock()

        # initialize() faz I2C (bloqueante) — roda em executor
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._payload.initialize)

        # Canal gRPC assíncrono com keepalive configurado
        self._channel = self._create_channel()
        self._stub = pb_grpc.ExperimentAgentServiceStub(self._channel)

        await asyncio.gather(
            self.run_sensor_sampling_loop(),
            self.run_telemetry_loop(),
            self.run_command_loop(),
            self.run_experiment_collector(),
            self.run_log_loop(),
            self.run_firmware_update_loop(),
        )
