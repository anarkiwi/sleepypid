#!/usr/bin/python3

import json
import os
import tempfile
import unittest
from collections import namedtuple
from prometheus_client import REGISTRY
import sleepypid
from sleepypid import (
    get_uptime, mean_diff, sleep_duty_seconds, calc_soc, flatten_telemetry,
    log_prometheus, call_script, parse_args, override_args)


class SleepyidTestCase(unittest.TestCase):
    """Test sleepypid"""

    def test_call_script(self):
        call_script('ls')
        call_script('/bin/notsogood')
        call_script('cat', timeout=2)

    def test_uptime(self):
        self.assertGreaterEqual(get_uptime(), 0)

    def test_soc(self):
        args = namedtuple('args', ('fullvoltage', 'shutdownvoltage'))
        args.fullvoltage=13.3
        args.shutdownvoltage=12.9
        self.assertEqual(100, calc_soc(13.3, args))
        self.assertEqual(100, calc_soc(14, args))
        self.assertEqual(0, calc_soc(12.9, args))
        self.assertEqual(0, calc_soc(12.8, args))
        self.assertAlmostEqual(50, calc_soc(13.1, args), places=2)

    def test_flatten_telemetry(self):
        flat = flatten_telemetry(
            {"command": {"command": "sensors"},
             "response": {"command": "sensors", "error": "", "rpiCurrent": 1,
                          "supplyVoltage": 2, "meanValid": True},
             "loadavg": [1, 2, 3], "window_diffs": {"cputempc": 0.01}})
        # response keys hoisted to the top level
        self.assertEqual(1, flat["rpiCurrent"])
        self.assertEqual(2, flat["supplyVoltage"])
        self.assertNotIn("response", flat)
        # loadavg tuple expanded into per-window keys
        self.assertEqual(1, flat["loadavg1m"])
        self.assertEqual(3, flat["loadavg15m"])
        self.assertNotIn("loadavg", flat)
        # window_diffs flattened with a suffix
        self.assertEqual(0.01, flat["cputempc_window_diffs"])
        self.assertNotIn("window_diffs", flat)

    def test_log_prometheus(self):
        log_prometheus(False, {"soc": 42})
        self.assertIsNone(REGISTRY.get_sample_value("sleepypi_soc"))
        log_prometheus(True,
            {"command": {"command": "sensors"},
             "response": {"command": "sensors", "error": "", "rpiCurrent": 1, "supplyVoltage": 1, "mean1mSupplyVoltage": 1,
                          "mean1mRpiCurrent": 1, "min1mSupplyVoltage": 1, "min1mRpiCurrent": 1, "max1mSupplyVoltage": 1,
                          "max1mRpiCurrent": 1, "meanValid": True, "powerState": True, "powerStateOverride": False, "uptimems": 1},
                          "timestamp": 1, "utctimestamp": "2021-01-01 01:11:11.11",
                          "loadavg": [1, 1, 1], "uptime": 1, "cputempc": 5})
        log_prometheus(True,
            {"window_diffs": {"mean1mRpiCurrent": 0.1, "mean1mSupplyVoltage": -0.01, "cputempc": 0.01}, "soc": 100, "timestamp": 1,
                              "utctimestamp": "2021-01-01 01:11:11.11", "loadavg": [1, 1, 1], "uptime": 1, "cputempc": 5})
        # numeric sensor values are exported as gauges
        self.assertEqual(1, REGISTRY.get_sample_value("sleepypi_mean1mSupplyVoltage"))
        # booleans are coerced to 0/1
        self.assertEqual(1, REGISTRY.get_sample_value("sleepypi_powerState"))
        self.assertEqual(0, REGISTRY.get_sample_value("sleepypi_powerStateOverride"))
        # window diffs and derived values are exported
        self.assertEqual(100, REGISTRY.get_sample_value("sleepypi_soc"))
        self.assertAlmostEqual(0.01, REGISTRY.get_sample_value("sleepypi_cputempc_window_diffs"))
        # non-numeric values (strings/dicts) are not exported
        self.assertIsNone(REGISTRY.get_sample_value("sleepypi_utctimestamp"))

    def test_prometheus_prefix_empty(self):
        # an empty prefix exports bare metric names for drop-in compatibility
        # with the legacy pushgateway series (e.g. ridge-pi deployment).
        original_prefix = sleepypid.prometheus_prefix
        sleepypid.prometheus_prefix = ''
        try:
            log_prometheus(True, {"window_diffs": {}, "legacyBareMetric": 7})
        finally:
            sleepypid.prometheus_prefix = original_prefix
        self.assertEqual(7, REGISTRY.get_sample_value("legacyBareMetric"))
        self.assertIsNone(REGISTRY.get_sample_value("sleepypi_legacyBareMetric"))

    def test_prometheus_prefix_arg(self):
        args = parse_args()
        self.assertEqual('sleepypi_', args.prometheus_prefix)

    def test_mean_diff(self):
        self.assertEqual(0, mean_diff([0, 1, 2, 3, 4, 3, 2, 1, 0]))
        self.assertEqual(0, mean_diff([1, 1]))
        self.assertEqual(-0.25, mean_diff([1, 1.5, 0.5]))
        voltages = [12.8, 12.8, 12.8, 12.9, 12.9, 12.9, 13.0, 13.0, 13.0]
        self.assertAlmostEqual(0.025, mean_diff(voltages), places=2)
        self.assertAlmostEqual(-0.025, mean_diff(list(reversed(voltages))), places=2)

    def test_sleep_duty_seconds(self):
        self.assertEqual(0, sleep_duty_seconds(100, 15, 1440))
        self.assertEqual(1440, sleep_duty_seconds(0, 15, 1440))
        pct75_sleep_time = 0
        for _ in range(1000):
            pct75_sleep_time += sleep_duty_seconds(75, 15, 1440)
        pct50_sleep_time = 0
        for _ in range(1000):
            pct50_sleep_time += sleep_duty_seconds(50, 15, 1440)
        pct10_sleep_time = 0
        for _ in range(1000):
            pct10_sleep_time += sleep_duty_seconds(10, 15, 1440)
        self.assertGreater(pct10_sleep_time, pct50_sleep_time)
        self.assertGreater(pct50_sleep_time, pct75_sleep_time)

    def test_parse_args(self):
        with tempfile.TemporaryDirectory() as test_dir:
            argjson_file = os.path.join(test_dir, 'asgjson.txt')
            with open(argjson_file, 'w', encoding='utf-8') as f:
                argsjson_txt = json.dumps({'shutdowncurrent': 123})
                f.write(argsjson_txt)

            main_args = parse_args()
            self.assertNotEqual(main_args.shutdowncurrent, 123)
            main_args.argjson = argjson_file
            main_args = override_args(main_args)
            self.assertEqual(main_args.shutdowncurrent, 123)


if __name__ == '__main__':
    unittest.main()
