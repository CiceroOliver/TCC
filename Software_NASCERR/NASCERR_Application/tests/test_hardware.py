"""
Hardware tests for the NASCERR payload.

Covers temperature, current and power readings (per channel and total),
sleep/wakeup, physical and nominal limits, and reading stability.
Every telecommand sent by the tests is recorded in the T metric.
"""

import statistics
import time

import pytest

from nascerr_modules.nascerr_operation import Operation
from metrics import MeasuredOperation


DEVICE_NAME = "NASCERR_CargaUtil"
I2C_ADDRESS = 0x55

# Wait times (s)
WAIT_TIME = 0.1
MEASUREMENT_WAIT = 0.2
SLEEP_WAIT = 1.0

# Expected limits (adjust to the hardware)
TEMPERATURE_LIMITS = {
    "min": -40.0,   # °C
    "max": 85.0,    # °C
    "nominal_min": 0.0,
    "nominal_max": 60.0
}

CURRENT_LIMITS = {
    "min": 0.0,     # mA
    "max": 5000.0,  # mA (5 A)
    "nominal_min": 0.0,
    "nominal_max": 1000.0  # mA (1 A)
}

POWER_LIMITS = {
    "min": 0.0,        # mW
    "max": 25000.0,    # mW (25 W)
    "nominal_min": 0.0,
    "nominal_max": 5000.0  # mW (5 W)
}

EXPECTED_CURRENT_CHANNELS = 5
EXPECTED_POWER_CHANNELS = 5


def _in_range(limits, n_channels=None):
    """Build a validator: response data is a non-empty list within the physical limits."""
    def validate(resp):
        data = resp["data"]
        if not isinstance(data, list) or len(data) == 0:
            return False
        if n_channels is not None and len(data) != n_channels:
            return False
        return all(limits["min"] <= v <= limits["max"] for v in data)
    return validate


HARDWARE_VALIDATORS = {
    "get_hardware_temperature":   _in_range(TEMPERATURE_LIMITS),
    "get_hardware_current":       _in_range(CURRENT_LIMITS, EXPECTED_CURRENT_CHANNELS),
    "get_hardware_total_current": _in_range(CURRENT_LIMITS),
    "get_hardware_power":         _in_range(POWER_LIMITS, EXPECTED_POWER_CHANNELS),
    "get_hardware_total_power":   _in_range(POWER_LIMITS),
    "get_status":   lambda r: r.get("connection_status") == "conectado",
    "system_sleep":  None,
    "system_wakeup": None,
}


class TestNASCERRHardware:
    """Hardware tests for the NASCERR payload."""

    @pytest.fixture(scope="class")
    def operation(self):
        """Operation instance whose telecommands are recorded in the T metric."""
        op = Operation(DEVICE_NAME, I2C_ADDRESS)
        return MeasuredOperation(op, "hardware", HARDWARE_VALIDATORS)

    @pytest.fixture(scope="function")
    def ensure_awake(self, operation):
        """Make sure the device is awake before and after each test."""
        # Test setup uses .raw so it does not count towards the metric
        try:
            operation.raw.system_wakeup()
            time.sleep(WAIT_TIME)
        except:
            pass  # May already be awake

        yield

        try:
            operation.raw.system_wakeup()
            time.sleep(WAIT_TIME)
        except:
            pass

    # --- Temperature ---

    @pytest.mark.hardware
    @pytest.mark.order(1)
    def test_01_read_temperature(self, operation, ensure_awake):
        """Read the hardware temperature."""
        print("\n" + "=" * 70)
        print("TEST 1: Temperature Reading")
        print("=" * 70)

        result = operation.get_hardware_temperature()

        assert not result["error"], f"Error reading temperature: {result.get('log_error')}"
        assert result["connection_status"] == "conectado", "Device not connected"

        temps = result["data"]
        assert isinstance(temps, list), "Temperature data must be a list"
        assert len(temps) > 0, "No temperature reading returned"

        print(f"✓ {len(temps)} temperature reading(s) obtained")

        for i, temp in enumerate(temps):
            print(f"  Sensor {i}: {temp:.2f}°C")

            assert TEMPERATURE_LIMITS["min"] <= temp <= TEMPERATURE_LIMITS["max"], \
                f"Temperature {temp}°C outside physical limits ({TEMPERATURE_LIMITS['min']}-{TEMPERATURE_LIMITS['max']}°C)"

            if not (TEMPERATURE_LIMITS["nominal_min"] <= temp <= TEMPERATURE_LIMITS["nominal_max"]):
                print(f"  ⚠ Temperature {temp}°C outside nominal range ({TEMPERATURE_LIMITS['nominal_min']}-{TEMPERATURE_LIMITS['nominal_max']}°C)")

    @pytest.mark.hardware
    @pytest.mark.order(2)
    def test_02_temperature_stability(self, operation, ensure_awake):
        """Check the stability of temperature readings."""
        print("\n" + "=" * 70)
        print("TEST 2: Temperature Stability")
        print("=" * 70)

        readings = []
        num_samples = 5

        print(f"Collecting {num_samples} temperature samples...")

        for i in range(num_samples):
            result = operation.get_hardware_temperature()
            assert not result["error"], f"Error on sample {i+1}"

            temps = result["data"]
            readings.append(temps[0])

            print(f"  Sample {i+1}: {temps[0]:.2f}°C")
            time.sleep(MEASUREMENT_WAIT)

        mean_temp = statistics.mean(readings)
        std_temp = statistics.stdev(readings) if len(readings) > 1 else 0

        print(f"\n✓ Mean: {mean_temp:.2f}°C")
        print(f"✓ Standard deviation: {std_temp:.2f}°C")

        assert std_temp < 5.0, f"Temperature too unstable (σ={std_temp:.2f}°C)"

        if std_temp < 1.0:
            print("✓ Very stable readings")
        elif std_temp < 2.0:
            print("✓ Stable readings")
        else:
            print("⚠ Readings with moderate variation")

    # --- Current ---

    @pytest.mark.hardware
    @pytest.mark.order(3)
    def test_03_read_current_individual(self, operation, ensure_awake):
        """Read the current of each channel."""
        print("\n" + "=" * 70)
        print("TEST 3: Per-Channel Current Reading")
        print("=" * 70)

        result = operation.get_hardware_current()

        assert not result["error"], f"Error reading current: {result.get('log_error')}"

        currents = result["data"]
        assert isinstance(currents, list), "Current data must be a list"
        assert len(currents) == EXPECTED_CURRENT_CHANNELS, \
            f"Expected {EXPECTED_CURRENT_CHANNELS} channels, got {len(currents)}"

        print(f"✓ {len(currents)} current channel(s) read")

        total_current = 0
        for i, current in enumerate(currents):
            print(f"  Channel {i}: {current:.2f} mA")
            total_current += current

            assert CURRENT_LIMITS["min"] <= current <= CURRENT_LIMITS["max"], \
                f"Current {current}mA on channel {i} outside physical limits"

            if current > CURRENT_LIMITS["nominal_max"]:
                print(f"  ⚠ Channel {i}: high current ({current:.2f}mA)")

        print(f"\n✓ Computed total current: {total_current:.2f} mA")

    @pytest.mark.hardware
    @pytest.mark.order(4)
    def test_04_read_current_total(self, operation, ensure_awake):
        """Read the total current."""
        print("\n" + "=" * 70)
        print("TEST 4: Total Current Reading")
        print("=" * 70)

        result = operation.get_hardware_total_current()

        assert not result["error"], f"Error reading total current: {result.get('log_error')}"

        total_current = result["data"]
        assert isinstance(total_current, list), "Data must be a list"
        assert len(total_current) > 0, "No reading returned"

        total = total_current[0]
        print(f"✓ Total current: {total:.2f} mA")

        assert CURRENT_LIMITS["min"] <= total <= CURRENT_LIMITS["max"], \
            f"Total current {total}mA outside physical limits"

        if total > CURRENT_LIMITS["nominal_max"]:
            print(f"⚠ High total current: {total:.2f}mA")

    @pytest.mark.hardware
    @pytest.mark.order(5)
    def test_05_current_consistency(self, operation, ensure_awake):
        """Compare the reported total current with the sum of the channels."""
        print("\n" + "=" * 70)
        print("TEST 5: Current Consistency")
        print("=" * 70)

        result_individual = operation.get_hardware_current()
        assert not result_individual["error"], "Error reading per-channel currents"

        currents = result_individual["data"]
        sum_individual = sum(currents)

        print(f"Sum of channels: {sum_individual:.2f} mA")

        result_total = operation.get_hardware_total_current()
        assert not result_total["error"], "Error reading total current"

        total = result_total["data"][0]
        print(f"Reported total current: {total:.2f} mA")

        if total > 0:
            diff_percent = abs(sum_individual - total) / total * 100
            print(f"Difference: {diff_percent:.2f}%")

            # Only warns: the total and the channels may use different measurement methods
            if diff_percent < 5:
                print("✓ Consistent values (difference < 5%)")
            elif diff_percent < 10:
                print("⚠ Reasonably consistent values (difference < 10%)")
            else:
                print(f"⚠ WARNING: large difference between sum and total ({diff_percent:.2f}%)")
                print("  This may be normal depending on the measurement method")

    # --- Power ---

    @pytest.mark.hardware
    @pytest.mark.order(6)
    def test_06_read_power_individual(self, operation, ensure_awake):
        """Read the power of each channel."""
        print("\n" + "=" * 70)
        print("TEST 6: Per-Channel Power Reading")
        print("=" * 70)

        result = operation.get_hardware_power()

        assert not result["error"], f"Error reading power: {result.get('log_error')}"

        powers = result["data"]
        assert isinstance(powers, list), "Power data must be a list"
        assert len(powers) == EXPECTED_POWER_CHANNELS, \
            f"Expected {EXPECTED_POWER_CHANNELS} channels, got {len(powers)}"

        print(f"✓ {len(powers)} power channel(s) read")

        total_power = 0
        for i, power in enumerate(powers):
            print(f"  Channel {i}: {power:.2f} mW")
            total_power += power

            assert POWER_LIMITS["min"] <= power <= POWER_LIMITS["max"], \
                f"Power {power}mW on channel {i} outside physical limits"

            if power > POWER_LIMITS["nominal_max"]:
                print(f"  ⚠ Channel {i}: high power ({power:.2f}mW)")

        print(f"\n✓ Computed total power: {total_power:.2f} mW ({total_power/1000:.2f} W)")

    @pytest.mark.hardware
    @pytest.mark.order(7)
    def test_07_read_power_total(self, operation, ensure_awake):
        """Read the total power."""
        print("\n" + "=" * 70)
        print("TEST 7: Total Power Reading")
        print("=" * 70)

        result = operation.get_hardware_total_power()

        assert not result["error"], f"Error reading total power: {result.get('log_error')}"

        total_power = result["data"]
        assert isinstance(total_power, list), "Data must be a list"
        assert len(total_power) > 0, "No reading returned"

        total = total_power[0]
        print(f"✓ Total power: {total:.2f} mW ({total/1000:.2f} W)")

        assert POWER_LIMITS["min"] <= total <= POWER_LIMITS["max"], \
            f"Total power {total}mW outside physical limits"

        if total > POWER_LIMITS["nominal_max"]:
            print(f"⚠ High total power: {total:.2f}mW")

    # --- Power management ---

    @pytest.mark.hardware
    @pytest.mark.order(8)
    def test_08_system_sleep(self, operation):
        """Put the system into sleep mode."""
        print("\n" + "=" * 70)
        print("TEST 8: Sleep Mode")
        print("=" * 70)

        operation.system_wakeup()
        time.sleep(WAIT_TIME)

        print("Putting system to sleep...")
        result = operation.system_sleep()

        assert not result["error"], f"Error entering sleep: {result.get('log_error')}"

        print("✓ Sleep command sent")
        time.sleep(SLEEP_WAIT)

        # Not counted in the metric: no response here is the expected behaviour while asleep
        status_result = operation.raw.get_status()

        if status_result["error"]:
            print("✓ System not responding (probably asleep)")
        else:
            print("⚠ System still responding")
            print(f"  Status: {status_result['connection_status']}")

    @pytest.mark.hardware
    @pytest.mark.order(9)
    def test_09_system_wakeup(self, operation):
        """Wake the system up from sleep mode."""
        print("\n" + "=" * 70)
        print("TEST 9: Wakeup from Sleep")
        print("=" * 70)

        print("Waking system up...")
        # May report an error if already awake; that is not a failure
        operation.system_wakeup()

        print("✓ Wakeup command sent")
        time.sleep(SLEEP_WAIT)

        print("Checking whether the system responds...")
        status_result = operation.get_status()

        assert not status_result["error"], "System did not respond after wakeup"
        assert status_result["connection_status"] == "conectado", \
            "System not connected after wakeup"

        print("✓ System woke up successfully")
        print(f"✓ Status: {status_result['connection_status']}")

    @pytest.mark.hardware
    @pytest.mark.order(10)
    def test_10_sleep_wakeup_cycle(self, operation):
        """Run full sleep -> wakeup cycles."""
        print("\n" + "=" * 70)
        print("TEST 10: Sleep/Wakeup Cycle")
        print("=" * 70)

        num_cycles = 3

        for cycle in range(num_cycles):
            print(f"\n--- Cycle {cycle + 1}/{num_cycles} ---")

            print("  Entering sleep...")
            operation.system_sleep()
            time.sleep(SLEEP_WAIT)

            print("  Waking up...")
            operation.system_wakeup()
            time.sleep(SLEEP_WAIT)

            status = operation.get_status()
            assert not status["error"], f"Error in cycle {cycle + 1}"
            print(f"  ✓ Cycle {cycle + 1} completed")

        print(f"\n✓ {num_cycles} cycles completed successfully")

    # --- Integration ---

    @pytest.mark.hardware
    @pytest.mark.integration
    @pytest.mark.order(11)
    def test_11_all_sensors_reading(self, operation, ensure_awake):
        """Read all sensors in sequence."""
        print("\n" + "=" * 70)
        print("TEST 11: Full Sensor Reading")
        print("=" * 70)

        print("\n1. Reading temperature...")
        temp_result = operation.get_hardware_temperature()
        assert not temp_result["error"], "Error reading temperature"
        temps = temp_result["data"]
        print(f"   ✓ Temperature: {temps[0]:.2f}°C")

        time.sleep(MEASUREMENT_WAIT)

        print("\n2. Reading currents...")
        current_result = operation.get_hardware_current()
        assert not current_result["error"], "Error reading currents"
        currents = current_result["data"]
        print(f"   ✓ Currents: {[f'{c:.2f}mA' for c in currents]}")

        time.sleep(MEASUREMENT_WAIT)

        print("\n3. Reading powers...")
        power_result = operation.get_hardware_power()
        assert not power_result["error"], "Error reading powers"
        powers = power_result["data"]
        print(f"   ✓ Powers: {[f'{p:.2f}mW' for p in powers]}")

        print("\n" + "-" * 70)
        print("FULL READING SUMMARY")
        print("-" * 70)
        print(f"Temperature:     {temps[0]:.2f}°C")
        print(f"Total current:   {sum(currents):.2f} mA")
        print(f"Total power:     {sum(powers):.2f} mW ({sum(powers)/1000:.2f} W)")
        print("-" * 70)

    @pytest.mark.hardware
    @pytest.mark.integration
    @pytest.mark.slow
    @pytest.mark.order(12)
    def test_12_continuous_monitoring(self, operation, ensure_awake):
        """Monitor the sensors continuously for 30 seconds."""
        print("\n" + "=" * 70)
        print("TEST 12: Continuous Monitoring (30 seconds)")
        print("=" * 70)

        duration = 30  # s
        interval = 2   # s between readings

        readings = {
            "temperature": [],
            "current": [],
            "power": []
        }

        num_samples = duration // interval
        print(f"Collecting {num_samples} samples ({interval}s interval)...\n")

        for i in range(num_samples):
            print(f"Sample {i+1}/{num_samples}:")

            temp_result = operation.get_hardware_temperature()
            if not temp_result["error"]:
                temp = temp_result["data"][0]
                readings["temperature"].append(temp)
                print(f"  Temp: {temp:.2f}°C", end="")

            current_result = operation.get_hardware_total_current()
            if not current_result["error"]:
                current = current_result["data"][0]
                readings["current"].append(current)
                print(f" | Current: {current:.2f}mA", end="")

            power_result = operation.get_hardware_total_power()
            if not power_result["error"]:
                power = power_result["data"][0]
                readings["power"].append(power)
                print(f" | Power: {power:.2f}mW")

            time.sleep(interval)

        print("\n" + "-" * 70)
        print("STATISTICAL ANALYSIS")
        print("-" * 70)

        for sensor, values in readings.items():
            if len(values) > 0:
                mean_val = statistics.mean(values)
                std_val = statistics.stdev(values) if len(values) > 1 else 0
                min_val = min(values)
                max_val = max(values)

                print(f"\n{sensor.upper()}:")
                print(f"  Mean:   {mean_val:.2f}")
                print(f"  StdDev: {std_val:.2f}")
                print(f"  Min:    {min_val:.2f}")
                print(f"  Max:    {max_val:.2f}")
                print(f"  Range:  {max_val - min_val:.2f}")

        print("-" * 70)

    # --- Full diagnostic ---

    @pytest.mark.hardware
    @pytest.mark.order(13)
    def test_13_hardware_diagnostic(self, operation, ensure_awake):
        """Run a full hardware diagnostic."""
        print("\n" + "=" * 70)
        print("TEST 13: Full Hardware Diagnostic")
        print("=" * 70)

        diagnostic_report = {
            "temperature": {"status": "UNKNOWN", "value": None, "unit": "°C"},
            "current_channels": {"status": "UNKNOWN", "values": [], "unit": "mA"},
            "current_total": {"status": "UNKNOWN", "value": None, "unit": "mA"},
            "power_channels": {"status": "UNKNOWN", "values": [], "unit": "mW"},
            "power_total": {"status": "UNKNOWN", "value": None, "unit": "mW"},
            "sleep_wakeup": {"status": "UNKNOWN"}
        }

        print("\n1. Testing temperature...")
        try:
            result = operation.get_hardware_temperature()
            if not result["error"]:
                temp = result["data"][0]
                diagnostic_report["temperature"]["value"] = temp
                diagnostic_report["temperature"]["status"] = "OK"
                print(f"   ✓ Temperature: {temp:.2f}°C")
            else:
                diagnostic_report["temperature"]["status"] = "ERROR"
                print(f"   ✗ Error: {result.get('log_error')}")
        except Exception as e:
            diagnostic_report["temperature"]["status"] = "ERROR"
            print(f"   ✗ Exception: {e}")

        print("\n2. Testing currents...")
        try:
            result = operation.get_hardware_current()
            if not result["error"]:
                currents = result["data"]
                diagnostic_report["current_channels"]["values"] = currents
                diagnostic_report["current_channels"]["status"] = "OK"
                print(f"   ✓ {len(currents)} channels OK")

                result_total = operation.get_hardware_total_current()
                if not result_total["error"]:
                    total = result_total["data"][0]
                    diagnostic_report["current_total"]["value"] = total
                    diagnostic_report["current_total"]["status"] = "OK"
                    print(f"   ✓ Total current: {total:.2f}mA")
            else:
                diagnostic_report["current_channels"]["status"] = "ERROR"
                print(f"   ✗ Error: {result.get('log_error')}")
        except Exception as e:
            diagnostic_report["current_channels"]["status"] = "ERROR"
            print(f"   ✗ Exception: {e}")

        print("\n3. Testing powers...")
        try:
            result = operation.get_hardware_power()
            if not result["error"]:
                powers = result["data"]
                diagnostic_report["power_channels"]["values"] = powers
                diagnostic_report["power_channels"]["status"] = "OK"
                print(f"   ✓ {len(powers)} channels OK")

                result_total = operation.get_hardware_total_power()
                if not result_total["error"]:
                    total = result_total["data"][0]
                    diagnostic_report["power_total"]["value"] = total
                    diagnostic_report["power_total"]["status"] = "OK"
                    print(f"   ✓ Total power: {total:.2f}mW")
            else:
                diagnostic_report["power_channels"]["status"] = "ERROR"
                print(f"   ✗ Error: {result.get('log_error')}")
        except Exception as e:
            diagnostic_report["power_channels"]["status"] = "ERROR"
            print(f"   ✗ Exception: {e}")

        print("\n4. Testing sleep/wakeup...")
        try:
            operation.system_sleep()
            time.sleep(1)
            operation.system_wakeup()
            time.sleep(1)
            status = operation.get_status()
            if not status["error"]:
                diagnostic_report["sleep_wakeup"]["status"] = "OK"
                print("   ✓ Sleep/Wakeup working")
            else:
                diagnostic_report["sleep_wakeup"]["status"] = "ERROR"
                print("   ✗ Sleep/wakeup error")
        except Exception as e:
            diagnostic_report["sleep_wakeup"]["status"] = "ERROR"
            print(f"   ✗ Exception: {e}")

        print("\n" + "=" * 70)
        print("DIAGNOSTIC REPORT")
        print("=" * 70)

        all_ok = True
        for component, data in diagnostic_report.items():
            status = data.get("status", "UNKNOWN")
            icon = "✓" if status == "OK" else "✗"
            print(f"{icon} {component.replace('_', ' ').title()}: {status}")
            if status != "OK":
                all_ok = False

        print("=" * 70)

        if all_ok:
            print("\nALL HARDWARE COMPONENTS WORKING NORMALLY")
        else:
            print("\n⚠ SOME COMPONENTS REPORTED PROBLEMS")

        assert all_ok, "Some hardware components failed the diagnostic"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])
