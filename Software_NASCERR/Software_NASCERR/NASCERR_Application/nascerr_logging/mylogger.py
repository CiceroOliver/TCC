import datetime as dt
import json
import logging
import logging.config
import logging.handlers
import queue
import atexit
from pathlib import Path


# Conjunto de atributos nativos do LogRecord do Python.
# Usado pelo MyJSONFormatter para identificar campos customizados:
# qualquer chave em record.__dict__ que NÃO esteja aqui é incluída
# automaticamente no JSON de saída (ex: hardware_layer, page, x).
LOG_RECORD_BUILTIN_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


class MyJSONFormatter(logging.Formatter):
    """
    Formatter customizado que serializa cada LogRecord como uma linha JSON.

    Instanciado pelo dictConfig via a chave '()' no config.json:
        "json": { "()": "mylogger.MyJSONFormatter", "fmt_keys": { ... } }

    Os campos que aparecem no JSON e seus nomes são controlados por fmt_keys,
    definido no config.json. Campos extras passados via extra={} são incluídos
    automaticamente desde que não sejam atributos nativos do LogRecord.
    """

    def __init__(self, *, fmt_keys: dict[str, str] | None = None):
        super().__init__()
        self.fmt_keys = fmt_keys if fmt_keys is not None else {}

    def format(self, record: logging.LogRecord) -> str:
        """Serializa o LogRecord como string JSON."""
        message = self._prepare_log_dict(record)
        return json.dumps(message, default=str, ensure_ascii=False)

    def _prepare_log_dict(self, record: logging.LogRecord):
        """
        Monta o dicionário que será serializado em JSON. Executa em 3 passos:

        1. Campos obrigatórios: 'message' e 'timestamp' (UTC ISO 8601).
           Se o record tiver exc_info ou stack_info, também os inclui.

        2. Campos mapeados via fmt_keys (config.json): para cada par
           (chave_json, atributo_record), busca o valor em always_fields
           primeiro e, se não encontrar, lê direto do record via getattr().

        3. Campos extras: qualquer atributo do record que não esteja em
           LOG_RECORD_BUILTIN_ATTRS é adicionado ao JSON (ex: hardware_layer).
        """
        always_fields = {
            "message": record.getMessage(),
            "timestamp": dt.datetime.fromtimestamp(
                record.created, tz=dt.timezone.utc
            ).isoformat(),
        }
        if record.exc_info is not None:
            always_fields["exc_info"] = self.formatException(record.exc_info)

        if record.stack_info is not None:
            always_fields["stack_info"] = self.formatStack(record.stack_info)

        message = {
            key: msg_val
            if (msg_val := always_fields.pop(val, None)) is not None
            else getattr(record, val)
            for key, val in self.fmt_keys.items()
        }
        message.update(always_fields)

        for key, val in record.__dict__.items():
            if key not in LOG_RECORD_BUILTIN_ATTRS:
                message[key] = val

        return message


class NonErrorFilter(logging.Filter):
    """
    Filtro que aprova apenas registros com nível INFO ou abaixo (DEBUG, INFO).

    Referenciado no config.json como 'info_and_below' e aplicado exclusivamente
    ao handler stdout. Impede que WARNING, ERROR e CRITICAL apareçam no stdout,
    evitando duplicação com o handler stderr que já os captura.
    """

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        return record.levelno <= logging.INFO


class NascerrFilter(logging.Filter):
    """
    Filtro que aprova apenas registros emitidos pelo logger 'nascerr'
    ou qualquer um de seus filhos (ex: nascerr.hardware, nascerr.experiment).
    Aplicado ao handler file_json para manter o arquivo limpo de logs
    de bibliotecas externas (grpc, protobuf, etc).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "nascerr" or record.name.startswith("nascerr.")


def setup_logging():
    """
    Inicializa o sistema de logging. Deve ser chamada uma única vez no
    ponto de entrada do programa (__main__), antes de qualquer uso de logger.

    Fluxo:
      1. Guard: retorna imediatamente se o root logger já tiver handlers.
      2. Cria o diretório logs/ se não existir.
      3. Carrega logging_configs/config.json via dictConfig, que instancia
         os formatters (simple, json), o filtro (info_and_below) e os
         handlers (stdout, stderr, file_json) e os anexa ao root logger.
      4. Remove os handlers do root e os move para dentro de um QueueListener,
         que os consome em thread dedicada (I/O assíncrono e thread-safe).
      5. Adiciona apenas o QueueHandler ao root logger.
      6. Registra listener.stop() no atexit para drenar a fila ao encerrar.
    """
    if logging.getLogger().handlers:
        return

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    config_file = Path(__file__).parent / "logging_configs" / "config.json"
    with open(config_file) as f_in:
        config = json.load(f_in)

    logging.config.dictConfig(config)

    root = logging.getLogger()
    handlers = {h.name: h for h in root.handlers if h.name}

    stdout_handler = handlers.get("stdout")
    stderr_handler = handlers.get("stderr")
    file_handler = handlers.get("file_json")

    for h in [stdout_handler, stderr_handler, file_handler]:
        if h:
            root.removeHandler(h)

    q = queue.Queue()
    queue_handler = logging.handlers.QueueHandler(q)

    listener = logging.handlers.QueueListener(
        q,
        *[h for h in [stdout_handler, stderr_handler, file_handler] if h],
        respect_handler_level=True
    )
    listener.start()
    atexit.register(listener.stop)

    root.setLevel(logging.DEBUG)
    root.addHandler(queue_handler)


def get_logger(abstraction_layer: str) -> logging.LoggerAdapter:
    """
    Retorna um LoggerAdapter configurado para o subsistema informado.

    O adapter injeta automaticamente o campo 'abstraction_layer' em todo
    registro emitido, sem necessidade de passar extra={} em cada chamada.
    Não cria novos loggers — todos os adapters compartilham o mesmo
    logger 'nascerr' e o mesmo pipeline de handlers.

    O campo 'abstraction_layer' aparece apenas no arquivo .jsonl (MyJSONFormatter).
    O formatter 'simple' do terminal não o exibe.

    Uso:
        hardware_logger   = get_logger("hardware")
        experiment_logger = get_logger("experiment")
        telemetry_logger  = get_logger("telemetry")

        hardware_logger.debug("lendo sensores I2C")
        experiment_logger.info("pagina lida", extra={"page": 42})
    """
    return logging.LoggerAdapter(
        logging.getLogger("nascerr"),
        {"abstraction_layer": abstraction_layer}
    )
