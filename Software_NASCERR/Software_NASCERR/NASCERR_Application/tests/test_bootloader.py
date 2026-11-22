"""
Bootloader fault-injection and firmware update tests.

Faults are injected from the master side (corrupted CRC, incomplete image,
wrong SHA, out-of-order packets, power loss) and recorded as coverage metric C;
the full update records each firmware packet as a request (metric T).
"""

import itertools
import os
import random
from math import ceil
from time import sleep

import metrics
from nascerr_modules.util.crc import calculate_stm32_compatible_sha256

# Delays in seconds
SLEEP_BETWEEN_OPERATIONS = 0.2
SLEEP_TIMEOUT = 6.0
SLEEP_BOOT = 40.0


class FaultInjector:
    """Injects master-side faults by sending invalid or out-of-order packets to the slave."""

    def __init__(self, operation):
        self.op = operation

    def send_corrupted_crc(self, packet_id: int, data: bytes):
        """Send a data packet with a corrupted CRC. Requires boot_init to have been done."""
        pkt = self.op.communication.boot_data_transfer(packet_id, data)
        pkt_bytes = bytearray.fromhex(pkt["command_bytes"])
        sleep(SLEEP_BETWEEN_OPERATIONS)
        pkt_bytes[-1] ^= 0xFF

        return self.op.communication.set_register("BOOT_DATA", bytes(pkt_bytes))

    def send_out_of_order(self, fw: bytes, packet_order: list[int], pkt_size=256):
        for idx in packet_order:
            chunk = fw[(idx - 1) * pkt_size: idx * pkt_size]
            sleep(SLEEP_BETWEEN_OPERATIONS)
            self.op.communication.boot_data_transfer(idx, chunk)

    def send_incomplete(self, fw: bytes, stop_at: int, pkt_size=256):
        for idx in range(1, stop_at + 1):
            chunk = fw[(idx - 1) * pkt_size: idx * pkt_size]
            sleep(SLEEP_BETWEEN_OPERATIONS)
            self.op.communication.boot_data_transfer(idx, chunk)

    def send_wrong_sha(self, version: int):
        fake_sha = bytes([0xAA] * 32)
        return self.op.communication.boot_donwload_finish(version, fake_sha)

    def send_command_storm(self, n: int = 20, pkt_size=256):
        fw = bytes([0xAB] * (pkt_size * 3))  # dummy 3-packet firmware

        for i in range(n):
            choice = random.choice(["INIT", "DATA", "FINISH"])
            sleep(SLEEP_BETWEEN_OPERATIONS)
            if choice == "INIT":
                fw_size = random.randint(0, len(fw) * 2)
                num_pkts = random.randint(1, 10)
                self.op.communication.boot_init(
                    firmware_size=fw_size,
                    version=random.randint(1, 0xFFFFFF),
                    num_packets=num_pkts
                )
            elif choice == "DATA":
                pkt_id = random.randint(1, 5)
                chunk = fw[0:pkt_size]
                self.op.communication.boot_data_transfer(pkt_id, chunk)
            elif choice == "FINISH":
                fake_sha = bytes([random.randint(0, 255) for _ in range(32)])
                version = random.randint(1, 0xFFFFFF)
                self.op.communication.boot_donwload_finish(version, fake_sha)


def _boot_status(operation):
    """BOOT_STAT after the injection, or None if the system did not respond (hung)."""
    try:
        st = operation.communication.boot_get_status()
    except Exception:
        return None
    if not st or st.get("error") or not st.get("data"):
        return None
    return st


def _record_injection(test, operation, is_detected, detail=""):
    """
    Read the bootloader status, record the injected fault in metric C and
    return True if it was detected. is_detected(boot_stat_data) -> bool.
    """
    st = _boot_status(operation)
    hung = st is None
    detected = (not hung) and bool(is_detected(st["data"]))
    metrics.fault(test, detected, hung, detail)
    return detected


def _error_detected(data):
    return "ERROR_DETECTED" in data["flags"]


def _image_rejected(data):
    return "IMAGE_VALID" not in data["flags"]


def test_command_storm(operation):
    injector = FaultInjector(operation)

    injector.send_command_storm(n=100)

    sleep(SLEEP_TIMEOUT)
    assert _record_injection("boot_command_storm", operation, _error_detected,
                             "100 random commands")


def test_crc_corrupted_packet(operation, dummy_firmware, runs=20):
    fw = open(dummy_firmware, "rb").read()
    total_size = len(fw)
    num_pkts = (total_size // 256) + 1
    undetected = []

    for i in range(runs):
        injector = FaultInjector(operation)

        operation.communication.boot_init(
            firmware_size=total_size,
            version=0xAABB00 + i,
            num_packets=num_pkts
        )

        sleep(SLEEP_BETWEEN_OPERATIONS)
        operation.communication.boot_data_transfer(1, fw[0:256])

        corrupted_pkt = random.randint(2, num_pkts)
        injector.send_corrupted_crc(corrupted_pkt,
                                    fw[(corrupted_pkt - 1) * 256:corrupted_pkt * 256])

        sleep(SLEEP_TIMEOUT)
        if not _record_injection("boot_crc", operation, _error_detected,
                                 f"packet {corrupted_pkt}"):
            undetected.append(i)

    assert not undetected, f"Undetected faults in runs {undetected}"


def test_incomplete_firmware(operation, dummy_firmware, runs=50):
    fw = open(dummy_firmware, "rb").read()
    total_pkts = (len(fw) // 256) + 1
    undetected = []

    for i in range(runs):
        stop_at = random.randint(1, total_pkts - 1)
        injector = FaultInjector(operation)

        operation.communication.boot_init(
            firmware_size=len(fw),
            version=0x110000 + i,
            num_packets=total_pkts
        )

        injector.send_incomplete(fw, stop_at)
        sleep(SLEEP_TIMEOUT)

        if not _record_injection("boot_incomplete", operation, _image_rejected,
                                 f"stopped at packet {stop_at}/{total_pkts}"):
            undetected.append(i)

    assert not undetected, f"Undetected faults in runs {undetected}"


def test_wrong_sha_scaled(operation, dummy_firmware, runs=20):
    fw = open(dummy_firmware, "rb").read()
    num_pkts = (len(fw) // 256) + 1
    undetected = []

    for i in range(runs):
        injector = FaultInjector(operation)

        operation.communication.boot_init(
            firmware_size=len(fw),
            version=0x550000 + i,
            num_packets=num_pkts
        )

        for k in range(1, num_pkts + 1):
            operation.communication.boot_data_transfer(
                k, fw[(k - 1) * 256:k * 256]
            )

        injector.send_wrong_sha(random.randint(1, 0xFFFFFF))
        sleep(SLEEP_TIMEOUT)

        if not _record_injection("boot_wrong_sha", operation, _image_rejected,
                                 f"run {i}"):
            undetected.append(i)

    assert not undetected, f"Undetected faults in runs {undetected}"


def test_out_of_order(operation, dummy_firmware):
    fw = open(dummy_firmware, "rb").read()
    orders = list(itertools.permutations([1, 2, 3, 4, 5, 6]))
    undetected = []

    for order in orders:
        if order == (1, 2, 3):
            continue  # valid order

        injector = FaultInjector(operation)
        operation.communication.boot_init(
            firmware_size=len(fw),
            version=random.randint(1, 0xFFFFFF),
            num_packets=3
        )

        injector.send_out_of_order(fw, list(order))
        sleep(SLEEP_TIMEOUT)

        if not _record_injection("boot_out_of_order", operation, _error_detected,
                                 f"order {order}"):
            undetected.append(order)

    assert not undetected, f"Undetected faults in orders {undetected}"


def test_power_loss_during_reception(operation, dummy_firmware):
    injector = FaultInjector(operation)
    fw = open(dummy_firmware, "rb").read()

    total_size = len(fw)
    num_pkts = (total_size // 256) + 1

    operation.communication.boot_init(
        firmware_size=total_size,
        version=0x770001,
        num_packets=num_pkts
    )

    for i in range(0, num_pkts // 2):
        chunk = fw[(i - 1) * 256:i * 256]
        operation.communication.boot_data_transfer(i, chunk)
        sleep(SLEEP_BETWEEN_OPERATIONS)

    print(">>> CUT POWER NOW <<<")
    sleep(10)  # time to cut power

    print(">>> RESTORE POWER <<<")
    sleep(SLEEP_BOOT)  # time for a full reboot

    # The system must come back in a consistent state
    assert _record_injection(
        "boot_power_loss_reception", operation,
        lambda d: d["version_raw"] != 0x770001 and _image_rejected(d),
        "power loss during reception")


def test_power_loss_during_verification(operation, dummy_firmware):
    fw = open(dummy_firmware, "rb").read()
    total_size = len(fw)
    num_pkts = (total_size // 256) + 1

    operation.communication.boot_init(
        firmware_size=total_size,
        version=0x880001,
        num_packets=num_pkts
    )

    for i in range(0, num_pkts):
        chunk = fw[(i - 1) * 256:i * 256]
        operation.communication.boot_data_transfer(i, chunk)
        sleep(SLEEP_BETWEEN_OPERATIONS)

    # Finishing the download starts the verification
    operation.communication.boot_donwload_finish(
        version=0x880001,
        sha=calculate_stm32_compatible_sha256(fw)
    )

    print(">>> CUT POWER DURING VERIFICATION <<<")
    sleep(10)

    print(">>> RESTORE POWER <<<")
    sleep(SLEEP_BOOT)

    # After the reboot a rollback must have occurred
    assert _record_injection(
        "boot_power_loss_verification", operation,
        lambda d: d["version_raw"] != 0x880001 and _image_rejected(d),
        "power loss during verification")


def _record_update_packets(result, fw_path, packet_size=256):
    """
    Metric T: each firmware packet is one request, correct when update_firmware
    confirmed it (last_packet_id == idx and DOWNLOAD_SUCCESS). Packets not
    confirmed or never sent (aborted update) count as incorrect.
    """
    total = ceil(os.path.getsize(fw_path) / packet_size)
    summary = result.get("summary") or (result if "total_packets" in result else None)
    if summary is not None:
        total = summary["total_packets"]
        confirmed = summary["successful_packets"]
    else:
        steps = result.get("report", result).get("steps", [])
        packets = next((s["packets"] for s in steps if s.get("action") == "data_transfer"), [])
        confirmed = sum(1 for p in packets
                        if "status_error" not in p and not p["tx_result"].get("error"))

    for i in range(total):
        metrics.request("boot_packets", i < confirmed,
                        f"packet {i + 1}/{total}" + ("" if i < confirmed else " | not confirmed"))
    return confirmed, total


def test_full_update_process(operation):
    """Full firmware update. Only works when the flashed firmware is older than 2.0.2."""
    fw_path = "binario_firmware/dev_nascerr_firmware.bin"
    fw_version = 0x0020002

    sleep(SLEEP_TIMEOUT)
    try:
        result = operation.update_firmware(fw_path, new_version=fw_version, force=True)
    except Exception:
        _record_update_packets({}, fw_path)
        raise

    confirmed, total = _record_update_packets(result, fw_path)
    print(f"Confirmed packets: {confirmed}/{total}")

    if "summary" in result:
        summary = result["summary"]
    else:
        summary = result

    assert "total_packets" in summary, \
        f"Update aborted before completion: {summary.get('message') or ('error at packet ' + str(summary.get('error_at')))}"
    assert summary["total_packets"] > 0, "No packets were transmitted"
    assert summary["successful_packets"] == summary["total_packets"], "Not all packets were sent successfully"
    assert summary["left_bootloader_confirmed"], "The device did not leave the bootloader"
    assert summary["version_match"], "The device did not report the new version"
    assert summary["success"], "The update did not complete successfully"

    print(f"Update completed in {summary['total_time_seconds']:.2f}s (version {summary['version']:#x})")
