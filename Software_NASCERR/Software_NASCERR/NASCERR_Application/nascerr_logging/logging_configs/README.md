# NASCERR Logging System

Sistema de logging baseado em arquivo para o projeto CubeSat NASCERR.  
Logs são escritos no terminal (INFO+) e em arquivo JSON Lines (DEBUG+) de forma assíncrona e thread-safe.

---

## Estrutura de arquivos

```
grpc/
├── mylogger.py                     # formatters, filtros, setup e fábrica de loggers
└── logging_configs/
    └── config.json                 # declaração dos handlers, formatters e filtros
logs/
└── my_app.log.jsonl                # saída persistente (criado automaticamente)
```

---

## Uso rápido

```python
from mylogger import setup_logging, get_logger

setup_logging()  # uma vez, no __main__

hardware_logger   = get_logger("hardware")
experiment_logger = get_logger("experiment")
telemetry_logger  = get_logger("telemetry")
gnss_logger       = get_logger("gnss")

hardware_logger.debug("lendo sensores")       # só no arquivo
hardware_logger.info("hardware inicializado") # terminal + arquivo
hardware_logger.warning("tensão baixa")       # terminal (stderr) + arquivo
```

---

## Níveis e destinos

| Nível      | Terminal stdout | Terminal stderr | Arquivo `.jsonl` |
|------------|:---------------:|:---------------:|:----------------:|
| `DEBUG`    |                 |                 | ✓                |
| `INFO`     | ✓               |                 | ✓                |
| `WARNING`  |                 | ✓               | ✓                |
| `ERROR`    |                 | ✓               | ✓                |
| `CRITICAL` |                 | ✓               | ✓                |

---

## config.json — referência detalhada

### `formatters`

#### `simple`
Usado pelos handlers `stdout` e `stderr`. Formata a saída do terminal.

```json
"simple": {
  "format":  "[%(levelname)s|%(module)s|L%(lineno)d] %(asctime)s: %(message)s",
  "datefmt": "%d/%m/%Y %H:%M:%S %z"
}
```

| Placeholder     | Valor exibido                                  |
|-----------------|------------------------------------------------|
| `%(levelname)s` | Nível: DEBUG, INFO, WARNING, ERROR, CRITICAL   |
| `%(module)s`    | Nome do módulo sem extensão (ex: cliente_grpc) |
| `%(lineno)d`    | Linha onde o log foi chamado                   |
| `%(asctime)s`   | Timestamp no fuso do sistema operacional       |
| `%(message)s`   | Texto da mensagem                              |

> **Fuso horário:** o `%(asctime)s` usa o fuso configurado no SO (`timedatectl`).  
> Para Fortaleza: `sudo timedatectl set-timezone America/Fortaleza`

#### `json`
Usado pelo handler `file_json`. Instancia `MyJSONFormatter` com os campos mapeados.

```json
"json": {
  "()": "mylogger.MyJSONFormatter",
  "fmt_keys": {
    "level":       "levelname",
    "message":     "message",
    "timestamp":   "timestamp",
    "logger":      "name",
    "module":      "module",
    "function":    "funcName",
    "line":        "lineno",
    "thread_name": "threadName"
  }
}
```

A chave `"()"` instrui o `dictConfig` a instanciar uma classe customizada.  
Cada par em `fmt_keys` define `chave_no_json → atributo_do_LogRecord`.  
Campos passados via `extra={}` (como `hardware_layer`) são adicionados automaticamente ao JSON.

Exemplo de linha no `.jsonl`:
```json
{"level": "INFO", "message": "hardware inicializado", "timestamp": "2026-03-22T14:48:54+00:00", "logger": "my_app", "module": "cliente_grpc", "function": "_read_hardware_sensors", "line": 142, "thread_name": "Thread-2", "hardware_layer": "hardware"}
```

---

### `filters`

#### `info_and_below`
Instancia `NonErrorFilter`. Aprovado apenas em registros com nível `INFO` ou abaixo.

```json
"filters": {
  "info_and_below": {
    "()": "mylogger.NonErrorFilter"
  }
}
```

Aplicado exclusivamente ao handler `stdout` para evitar que `WARNING+` apareça duas vezes no terminal (o `stderr` já os captura).

---

### `handlers`

#### `stdout`
```json
"stdout": {
  "class":     "logging.StreamHandler",
  "level":     "INFO",
  "formatter": "simple",
  "stream":    "ext://sys.stdout",
  "filters":   ["info_and_below"]
}
```
Exibe `INFO` no terminal. O filtro `info_and_below` bloqueia `WARNING+`.

#### `stderr`
```json
"stderr": {
  "class":     "logging.StreamHandler",
  "level":     "WARNING",
  "formatter": "simple",
  "stream":    "ext://sys.stderr"
}
```
Exibe `WARNING`, `ERROR` e `CRITICAL` no terminal. Sem filtro.

#### `file_json`
```json
"file_json": {
  "class":       "logging.handlers.RotatingFileHandler",
  "level":       "DEBUG",
  "formatter":   "json",
  "filename":    "logs/my_app.log.jsonl",
  "maxBytes":    100000,
  "backupCount": 3
}
```
Registra tudo (`DEBUG+`) em formato JSON Lines com rotação automática:
- `maxBytes: 100000` → rotaciona ao atingir ~100KB
- `backupCount: 3` → mantém até 3 arquivos de backup (`.jsonl.1`, `.jsonl.2`, `.jsonl.3`)

---

### `root`
```json
"root": {
  "level":    "DEBUG",
  "handlers": ["stdout", "stderr", "file_json"]
}
```

Os handlers são listados aqui para que o `dictConfig` os instancie e nomeie.  
O `setup_logging()` os remove do root em seguida e os move para dentro do `QueueListener`.  
Se a lista estivesse vazia, os objetos não seriam criados e a busca por nome falharia.

---

## Arquitetura assíncrona

O sistema usa `QueueHandler` + `QueueListener` para garantir que o I/O de arquivo e terminal não bloqueie as threads do sistema (sensores, telemetria, experimentos).

```
Thread sensor/experimento/telemetria
    └── logger.info()
            └── QueueHandler → queue.Queue()  (não bloqueia)
                                    └── QueueListener (thread dedicada)
                                            ├── stdout handler
                                            ├── stderr handler
                                            └── file_json handler
```