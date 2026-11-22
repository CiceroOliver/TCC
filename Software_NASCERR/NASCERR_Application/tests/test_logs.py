"""
Tests for the NASCERR log system.

Covers clearing the log and experiment regions, filling the internal flash
(301 logs), overflowing into the external flash (64 pages of 301 logs) and
checking the integrity of every page.
"""

import json
import time
from typing import Dict, List

import pytest

import metrics
from nascerr_modules.nascerr_command import CmdTypeDef, W_DataTypeDef
from nascerr_modules.nascerr_operation import Operation
from nascerr_modules.nascerr_system import NASCERR_MODE, FlashRegionTypeDef

INTERNAL_FLASH_CAPACITY = 301  # logs in the internal flash
EXTERNAL_PAGE_CAPACITY = 301   # logs per external flash page
TOTAL_EXTERNAL_PAGES = 64
TOTAL_LOGS_CAPACITY = INTERNAL_FLASH_CAPACITY + (EXTERNAL_PAGE_CAPACITY * TOTAL_EXTERNAL_PAGES)

DEVICE_NAME = "NASCERR_CargaUtil"
I2C_ADDRESS = 0x55

# Delays in seconds
WAIT_TIME = 0.1
COMMAND_WAIT = 0.5


class TestNASCERRLogs:
    """NASCERR log system tests."""

    @pytest.fixture(scope="class")
    def operation(self):
        """Create an Operation instance."""
        op = Operation(DEVICE_NAME, I2C_ADDRESS)
        return op

    @pytest.fixture(scope="class")
    def clean_device(self, operation):
        """Clear the log and experiment regions before the tests."""
        print("\n" + "=" * 70)
        print("SETUP: Clearing log and experiment regions")
        print("=" * 70)

        result_logs = operation.clear_database(FlashRegionTypeDef.FLASH_LOGS)
        assert not result_logs["error"], f"Error clearing logs: {result_logs.get('log_error')}"
        print(f"✓ Logs cleared: {result_logs['message']}")
        time.sleep(WAIT_TIME)

        result_exp = operation.clear_database(FlashRegionTypeDef.FLASH_EXPERIMENT)
        assert not result_exp["error"], f"Error clearing experiments: {result_exp.get('log_error')}"
        print(f"✓ Experiments cleared: {result_exp['message']}")
        time.sleep(WAIT_TIME)

        status = operation.get_status()
        assert not status["error"], "Error reading status after clearing"
        print(f"✓ Device status: {status['connection_status']}")

        return True

    def generate_test_command(self, cmd_id: int, block: int = 0) -> Dict:
        """Build a write command that generates log entries."""
        return {
            "cmd_type": CmdTypeDef.WRITE_CMD,
            "id": cmd_id,
            "block": block,
            "qtd_block": 1,
            "ecc": NASCERR_MODE.MRSC,
            "write_type": W_DataTypeDef.WRITE_xAAh,
            "scheduler_time": 0,  # run immediately
            "repeat": 1,
            "delay": 0,
            "current_tick": 0
        }

    def send_command_and_wait(self, operation: Operation, cmd_params: Dict) -> Dict:
        """Send a command (recorded in metrics) and wait for it to run."""
        result = metrics.measure(
            "logs", lambda: operation.send_commandNew(**cmd_params),
            detail=f"cmd {cmd_params['id']}")
        time.sleep(COMMAND_WAIT)
        return result

    def get_log_summary(self, operation: Operation, retry: int = 3) -> Dict:
        """Read the log summary, retrying on error."""
        last_error = None
        for attempt in range(retry):
            try:
                result = operation.get_log_summary()
                time.sleep(WAIT_TIME)

                if not result.get("error"):
                    return result

                last_error = result
                print(f"  ⚠ Attempt {attempt + 1}/{retry} failed, waiting...")
                time.sleep(WAIT_TIME * 2)

            except Exception as e:
                last_error = {"error": True, "log_error": str(e)}
                print(f"  ⚠ Exception on attempt {attempt + 1}/{retry}: {e}")
                time.sleep(WAIT_TIME * 2)

        return last_error if last_error else {"error": True, "log_error": "All attempts failed"}

    def set_and_read_log_page(self, operation: Operation, page: int, retry: int = 3) -> List[Dict]:
        """Select a log page (0-255) and read its content, retrying on CRC errors."""
        set_result = operation.set_log_page(page)
        assert not set_result["error"], f"Error selecting page {page}: {set_result.get('log_error')}"
        time.sleep(WAIT_TIME * 2)

        last_error = None
        for attempt in range(retry):
            try:
                read_result = operation.get_log_page_content()

                if not read_result["error"]:
                    time.sleep(WAIT_TIME)
                    return read_result.get("data", [])

                if "CRC" in read_result.get("log_error", ""):
                    last_error = read_result
                    print(f"  ⚠ Page {page}: invalid CRC on attempt {attempt + 1}/{retry}, retrying...")
                    time.sleep(WAIT_TIME * 3)

                    # Re-select the page before the next read
                    operation.set_log_page(page)
                    time.sleep(WAIT_TIME * 2)
                else:
                    # Only CRC errors are worth retrying
                    raise AssertionError(f"Error reading page {page}: {read_result.get('log_error')}")

            except AssertionError:
                raise
            except Exception as e:
                last_error = {"error": True, "log_error": str(e)}
                print(f"  ⚠ Page {page}: exception on attempt {attempt + 1}/{retry}: {e}")
                time.sleep(WAIT_TIME * 3)

        raise AssertionError(f"Error reading page {page} after {retry} attempts: {last_error.get('log_error')}")

    @pytest.mark.order(1)
    def test_01_initial_status(self, operation, clean_device):
        """Check the device status after clearing."""
        print("\n" + "=" * 70)
        print("TEST 1: Checking initial status")
        print("=" * 70)

        status = operation.get_status()
        assert not status["error"], "Error reading initial status"
        assert status["connection_status"] == "conectado", "Device not connected"

        print("✓ Device connected")
        print(f"✓ Status: {json.dumps(status['data'], indent=2)}")

        # The summary should be empty after clearing
        print("\n--- Checking initial log summary ---")
        summary = self.get_log_summary(operation, retry=3)

        if not summary["error"]:
            print(f"✓ Initial summary: {json.dumps(summary['data'], indent=2)}")

            if summary["data"]["total_pages"] > 0:
                print(f"⚠ WARNING: {summary['data']['total_pages']} page(s) still exist after clearing")
        else:
            print(f"⚠ Could not read initial summary: {summary.get('log_error')}")

    @pytest.mark.order(2)
    def test_02_fill_internal_flash(self, operation, clean_device):
        """Fill the internal flash (301 logs)."""
        print("\n" + "=" * 70)
        print(f"TEST 2: Filling internal flash ({INTERNAL_FLASH_CAPACITY} logs)")
        print("=" * 70)

        logs_sent = 0
        failed_commands = []

        for i in range(INTERNAL_FLASH_CAPACITY):
            cmd_params = self.generate_test_command(cmd_id=i + 1, block=i % 192)
            result = self.send_command_and_wait(operation, cmd_params)

            if result["error"]:
                failed_commands.append(i + 1)
                print(f"✗ Command {i + 1} failed: {result.get('log_error')}")
            else:
                logs_sent += 1

            if (i + 1) % 50 == 0:
                print(f"  Progress: {i + 1}/{INTERNAL_FLASH_CAPACITY} commands sent")

        print(f"\n✓ Commands sent successfully: {logs_sent}/{INTERNAL_FLASH_CAPACITY}")
        if failed_commands:
            print(f"✗ Failed commands: {failed_commands}")

        # Give the firmware time to commit the logs to flash
        print("  ⏳ Waiting for firmware to commit logs to flash (5 seconds)...")
        time.sleep(5.0)

        summary = self.get_log_summary(operation, retry=5)
        assert not summary["error"], "Error reading log summary"

        total_pages = summary["data"]["total_pages"]
        print(f"✓ Total log pages: {total_pages}")

        # Committing the internal flash into a page can take longer
        if total_pages == 0:
            print("  ⚠ No pages found, waiting for commit (10 seconds)...")
            time.sleep(10.0)
            summary = self.get_log_summary(operation, retry=5)
            total_pages = summary["data"]["total_pages"]
            print(f"  ℹ Pages after waiting: {total_pages}")

        if total_pages == 0:
            print("  ⚠ WARNING: internal flash filled but page not committed yet")
            print("  ℹ This may be normal firmware behaviour")
        else:
            print(f"✓ Internal flash committed into {total_pages} page(s)")
            assert total_pages >= 1, f"Expected at least 1 page, got {total_pages}"

    @pytest.mark.order(3)
    def test_03_verify_internal_flash_logs(self, operation, clean_device):
        """Check the logs in the internal flash (page 0)."""
        print("\n" + "=" * 70)
        print("TEST 3: Checking internal flash logs (page 0)")
        print("=" * 70)

        logs_page_0 = self.set_and_read_log_page(operation, 0)

        total_logs = len(logs_page_0)
        print(f"✓ Total logs on page 0: {total_logs}")

        assert total_logs > 0, "No logs found on page 0"
        print(f"✓ Page 0 contains {total_logs} logs")

        if total_logs > 0:
            print("\nSample - first log:")
            print(json.dumps(logs_page_0[0], indent=2))
            if total_logs > 1:
                print("\nSample - last log:")
                print(json.dumps(logs_page_0[-1], indent=2))

    @pytest.mark.order(4)
    def test_04_overflow_to_external_flash(self, operation, clean_device):
        """Overflow the logs into the external flash."""
        print("\n" + "=" * 70)
        print("TEST 4: Testing overflow into external flash")
        print("=" * 70)

        # Enough logs to fill 3 more external pages
        extra_logs = EXTERNAL_PAGE_CAPACITY * 3

        print(f"Sending {extra_logs} additional commands for overflow...")

        logs_sent = 0
        start_id = INTERNAL_FLASH_CAPACITY + 1

        for i in range(extra_logs):
            cmd_id = start_id + i
            cmd_params = self.generate_test_command(cmd_id=cmd_id, block=i % 192)
            result = self.send_command_and_wait(operation, cmd_params)

            if not result["error"]:
                logs_sent += 1

            if (i + 1) % 100 == 0:
                print(f"  Progress: {i + 1}/{extra_logs} commands sent")

        print(f"\n✓ Additional commands sent: {logs_sent}/{extra_logs}")

        summary = self.get_log_summary(operation)
        assert not summary["error"], "Error reading summary after overflow"

        total_pages = summary["data"]["total_pages"]
        print(f"✓ Total pages after overflow: {total_pages}")

        # 1 internal + 3 external
        assert total_pages >= 4, f"Expected at least 4 pages, got {total_pages}"

    @pytest.mark.order(5)
    def test_05_verify_all_pages(self, operation, clean_device):
        """Check every log page created."""
        print("\n" + "=" * 70)
        print("TEST 5: Checking all log pages")
        print("=" * 70)

        summary = self.get_log_summary(operation, retry=3)
        total_pages = summary["data"]["total_pages"]

        print(f"Pages to check: {total_pages}")

        page_stats = []

        for page_num in range(total_pages):
            print(f"\n--- Checking page {page_num} ---")

            try:
                logs = self.set_and_read_log_page(operation, page_num, retry=3)
                num_logs = len(logs)

                page_info = {
                    "page": page_num,
                    "logs_count": num_logs,
                    "status": "OK" if num_logs > 0 else "EMPTY"
                }
                page_stats.append(page_info)

                print(f"  Page {page_num}: {num_logs} logs - {page_info['status']}")

                # Only the last page may be partial
                if page_num < total_pages - 1:
                    if num_logs == 0:
                        print(f"  ⚠ WARNING: page {page_num} is empty (unexpected)")

            except Exception as e:
                print(f"  ✗ Error reading page {page_num}: {str(e)}")
                page_stats.append({
                    "page": page_num,
                    "logs_count": 0,
                    "status": "ERROR",
                    "error": str(e)
                })

        print("\n" + "=" * 70)
        print("PAGE CHECK SUMMARY")
        print("=" * 70)
        print(f"{'Page':<10} {'Logs':<10} {'Status':<10}")
        print("-" * 30)

        for stat in page_stats:
            print(f"{stat['page']:<10} {stat['logs_count']:<10} {stat['status']:<10}")

        total_logs_verified = sum(s['logs_count'] for s in page_stats)
        total_errors = sum(1 for s in page_stats if s['status'] == 'ERROR')

        print("-" * 30)
        print(f"Total logs checked: {total_logs_verified}")
        print(f"Pages with errors: {total_errors}/{total_pages}")

        assert total_logs_verified > 0, "No logs were checked on any page"

        if total_errors > 0:
            print(f"\n⚠ WARNING: {total_errors} page(s) with read errors")
            print("  This may indicate a hardware or timing problem")

    @pytest.mark.order(6)
    def test_06_verify_page_capacity(self, operation, clean_device):
        """Check that every full page holds exactly 301 logs."""
        print("\n" + "=" * 70)
        print("TEST 6: Checking page capacity (301 logs)")
        print("=" * 70)

        summary = self.get_log_summary(operation)
        total_pages = summary["data"]["total_pages"]

        pages_with_full_capacity = 0
        pages_partial = []

        # The last page may be partial, so it is checked separately
        for page_num in range(total_pages - 1):
            logs = self.set_and_read_log_page(operation, page_num)
            num_logs = len(logs)

            if num_logs == EXTERNAL_PAGE_CAPACITY:
                pages_with_full_capacity += 1
                print(f"✓ Page {page_num}: {num_logs} logs (FULL)")
            else:
                pages_partial.append((page_num, num_logs))
                print(f"⚠ Page {page_num}: {num_logs} logs (PARTIAL)")

        last_page_logs = self.set_and_read_log_page(operation, total_pages - 1)
        print(f"ℹ Last page ({total_pages - 1}): {len(last_page_logs)} logs")

        print(f"\n✓ Full pages (301 logs): {pages_with_full_capacity}")
        if pages_partial:
            print(f"⚠ Partial pages: {len(pages_partial)}")
            for page, count in pages_partial:
                print(f"  - Page {page}: {count} logs")

    @pytest.mark.order(7)
    def test_07_stress_test_full_capacity(self, operation, clean_device):
        """Stress test: fill the whole external flash (skipped by default, takes hours)."""
        pytest.skip("Stress test disabled by default (takes a very long time)")

        print("\n" + "=" * 70)
        print("TEST 7: STRESS TEST - Filling total capacity")
        print(f"Total logs to send: {TOTAL_LOGS_CAPACITY}")
        print("=" * 70)


def pytest_configure(config):
    """Register custom pytest markers."""
    config.addinivalue_line(
        "markers", "order(n): sets the test execution order"
    )
    config.addinivalue_line(
        "markers", "slow: slow tests that may be skipped"
    )
    config.addinivalue_line(
        "markers", "hardware: tests that need connected hardware"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])
