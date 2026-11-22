import asyncio
import logging
import os
import sys
 
# --- Caminhos ---
current_dir  = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)
 
from dotenv import load_dotenv
 
load_dotenv()
 
SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "192.168.1.209:50051")
DEVICE_NAME    = os.getenv("DEVICE_NAME", "NASCERR_CargaUtil")
I2C_ADDRESS    = int(os.getenv("I2C_ADDRESS", "0x55"), 16)
 
from gRPC.cliente_grpc import NascerrClient
# Importado aqui para evitar dependência circular entre grpc/ e roboc_payload/
from roboc_payload.handler import RoboCHandler

from nascerr_logging.mylogger import setup_logging

PAYLOAD_REGISTRY = {
    "roboc": lambda: RoboCHandler(DEVICE_NAME, I2C_ADDRESS),
    # "outro_exp": lambda: OutroExpHandler(DEVICE_NAME, 0x56),
}

if __name__ == "__main__": 
    setup_logging()

    payload_type = os.getenv("PAYLOAD_TYPE", "roboc")
    payload = PAYLOAD_REGISTRY[payload_type]()

    client = NascerrClient(SERVER_ADDRESS, DEVICE_NAME, payload)
    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        print("\n[INFO] Encerrado.")