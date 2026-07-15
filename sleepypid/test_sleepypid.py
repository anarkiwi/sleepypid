#!/usr/bin/python3

import datetime
import json
import os
import tempfile
import unittest
from collections import namedtuple
from types import SimpleNamespace
from prometheus_client import REGISTRY
import sleepypid
from sleepypid import (
    get_uptime, mean_diff, sleep_duty_seconds, soc_sleep_duty, calc_soc,
    flatten_telemetry, log_prometheus, call_script, parse_args, override_args,
    daylength_hours, seasonal_light, seasonal_duty_scale, forecast_duty_scale,
    extraterrestrial_radiation, clearsky_radiation, forecast_light_factor,
    parse_open_meteo, parse_metservice, update_forecast,
    OCV_CURVES, interp_soc, prune_voltage_history, voltage_trend,
    charging_state, charging_duty_scale, policy_duty)


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

    def test_calc_soc_linear_static_fullvoltage(self):
        args = namedtuple('args', ('fullvoltage', 'shutdownvoltage'))
        args.fullvoltage = 26.0
        args.shutdownvoltage = 24.3
        self.assertAlmostEqual(41.18, calc_soc(25.0, args), places=1)

    def test_daylength_hours(self):
        # southern hemisphere: winter solstice (Jun) shorter than summer (Dec)
        june = daylength_hours(172, -41.1)
        december = daylength_hours(355, -41.1)
        self.assertLess(june, december)
        self.assertAlmostEqual(june, 9.2, delta=0.4)
        self.assertAlmostEqual(december, 15.1, delta=0.4)
        # equator is ~12h year round
        self.assertAlmostEqual(daylength_hours(172, 0.0), 12.0, delta=0.2)

    def test_seasonal_duty_scale(self):
        args = SimpleNamespace(winter_duty_scale=0.5, latitude=-41.102223)
        winter = seasonal_duty_scale(args, datetime.date(2026, 6, 21))
        summer = seasonal_duty_scale(args, datetime.date(2026, 12, 21))
        # southern hemisphere: darkest June -> banks hardest, lightest Dec -> free
        self.assertAlmostEqual(0.5, winter, places=2)
        self.assertAlmostEqual(1.0, summer, places=2)
        self.assertLess(winter, summer)

    def test_seasonal_duty_scale_disabled(self):
        off = SimpleNamespace(winter_duty_scale=1.0, latitude=-41.102223)
        self.assertEqual(1.0, seasonal_duty_scale(off, datetime.date(2026, 6, 21)))

    def test_seasonal_duty_scale_energy_ramp(self):
        # energy-based interpolation banks harder than a daylength-linear ramp
        # through the dark half: winter's low sun angle cuts charge more than
        # the shorter day implies.
        args = SimpleNamespace(winter_duty_scale=0.5, latitude=-41.102223)
        when = datetime.date(2026, 5, 1)
        energy = seasonal_duty_scale(args, when)
        daylengths = [daylength_hours(d, args.latitude) for d in range(1, 366)]
        dmin, dmax = min(daylengths), max(daylengths)
        light = (daylength_hours(when.timetuple().tm_yday, args.latitude) - dmin) / (dmax - dmin)
        self.assertLess(energy, 0.5 + light * 0.5)

    def test_seasonal_light_bounds(self):
        args = SimpleNamespace(latitude=-41.102223)
        self.assertAlmostEqual(0.0, seasonal_light(args, datetime.date(2026, 6, 21)), places=2)
        self.assertAlmostEqual(1.0, seasonal_light(args, datetime.date(2026, 12, 21)), places=2)

    def test_extraterrestrial_radiation(self):
        # FAO-56 Example 8: 3 September (day 246) at 20 S -> Ra ~ 32.2 MJ/m^2
        self.assertAlmostEqual(32.2, extraterrestrial_radiation(246, -20.0), delta=0.3)
        # clear-sky surface reference is 0.75 * Ra
        self.assertAlmostEqual(
            0.75 * extraterrestrial_radiation(246, -20.0),
            clearsky_radiation(246, -20.0), places=5)

    def test_forecast_light_factor(self):
        when = datetime.date(2026, 6, 21)
        lat = -41.102223
        clearsky = clearsky_radiation(when.timetuple().tm_yday, lat)
        # a clear forecast (~ clear-sky energy) -> full light, no extra sleep
        self.assertGreater(forecast_light_factor([clearsky] * 3, when, lat), 0.95)
        # heavily clouded forecast -> little light -> sleepier
        self.assertAlmostEqual(
            0.2, forecast_light_factor([clearsky * 0.2] * 3, when, lat), delta=0.05)
        # a missing day counts as zero (err sleepy); empty -> neutral 1.0
        self.assertAlmostEqual(
            0.5, forecast_light_factor([None, clearsky], when, lat), delta=0.05)
        self.assertEqual(1.0, forecast_light_factor([], when, lat))

    def test_forecast_duty_scale(self):
        args = SimpleNamespace(forecast_duty_scale=0.5)
        # clear forecast -> no banking; overcast -> full banking
        self.assertAlmostEqual(1.0, forecast_duty_scale(args, 1.0), places=5)
        self.assertAlmostEqual(0.5, forecast_duty_scale(args, 0.0), places=5)
        self.assertAlmostEqual(0.75, forecast_duty_scale(args, 0.5), places=5)

    def test_forecast_duty_scale_disabled(self):
        args = SimpleNamespace(forecast_duty_scale=1.0)
        self.assertEqual(1.0, forecast_duty_scale(args, 0.0))
        args.forecast_duty_scale = 0.5
        self.assertEqual(1.0, forecast_duty_scale(args, None))

    def test_parse_open_meteo(self):
        payload = {"daily": {"time": ["2026-06-02", "2026-06-03", "2026-06-04"],
                             "shortwave_radiation_sum": [6.0, 4.5, None]}}
        self.assertEqual([6.0, 4.5, None], parse_open_meteo(payload))

    def test_parse_metservice(self):
        # hourly W/m^2 integrated to MJ/m^2 per UTC day; noData filtered out
        payload = {
            "noData": -9999.0,
            "dimensions": {"time": {"data": [0, 3600, 7200]}},
            "variables": {"radiation.shortwave": {"data": [100.0, 200.0, -9999.0]}},
        }
        # (100 + 200) W/m^2 * 3600 s / 1e6 = 1.08 MJ/m^2 for 1970-01-01
        self.assertEqual(1, len(parse_metservice(payload)))
        self.assertAlmostEqual(1.08, parse_metservice(payload)[0], places=5)

    def _forecast_args(self, cache_path):
        return SimpleNamespace(
            forecast_cache=cache_path, forecast_refresh_hours=6.0,
            forecast_max_age_hours=48.0, forecast_provider='open-meteo',
            latitude=-41.102223, longitude=174.8, forecast_days=3)

    def test_update_forecast_live(self):
        now = 1700000000
        payload = {"daily": {"time": ["a", "b", "c"],
                             "shortwave_radiation_sum": [1.0, 1.0, 1.0]}}
        with tempfile.TemporaryDirectory() as test_dir:
            args = self._forecast_args(os.path.join(test_dir, 'forecast.json'))
            factor, status = update_forecast(
                args, now=now, fetcher=lambda a: payload)
            when = datetime.datetime.fromtimestamp(
                now, datetime.timezone.utc).date()
            self.assertAlmostEqual(
                forecast_light_factor([1.0, 1.0, 1.0], when, args.latitude), factor)
            self.assertEqual(1, status['forecast_fetch_ok'])
            self.assertEqual(0, status['forecast_fetch_error'])
            self.assertEqual(1, status['forecast_source_open_meteo'])
            self.assertEqual(0, status['forecast_errors_open_meteo'])
            self.assertTrue(os.path.exists(args.forecast_cache))

    def _write_cache(self, path, **kwargs):
        with open(path, 'w', encoding='utf-8') as cache:
            cache.write(json.dumps(kwargs))

    def _boom(self, _args):
        raise OSError('forecast unreachable')

    def test_update_forecast_fresh_cache(self):
        now = 1700000000
        with tempfile.TemporaryDirectory() as test_dir:
            args = self._forecast_args(os.path.join(test_dir, 'forecast.json'))
            self._write_cache(args.forecast_cache, ts=now, factor=0.3, errors={})
            # fresh cache -> reused without calling the (raising) fetcher
            factor, status = update_forecast(args, now=now, fetcher=self._boom)
            self.assertEqual(0.3, factor)
            self.assertEqual(0, status['forecast_fetch_error'])

    def test_update_forecast_stale_cache_held(self):
        now = 1700000000
        with tempfile.TemporaryDirectory() as test_dir:
            args = self._forecast_args(os.path.join(test_dir, 'forecast.json'))
            # older than refresh (6h) but within max-age (48h) -> hold last value
            self._write_cache(
                args.forecast_cache, ts=now - 10 * 3600, factor=0.4, errors={})
            factor, status = update_forecast(args, now=now, fetcher=self._boom)
            self.assertEqual(0.4, factor)
            self.assertEqual(1, status['forecast_fetch_error'])
            # the failure is counted and persisted for Prometheus
            self.assertEqual(1, status['forecast_errors_open_meteo'])
            with open(args.forecast_cache, encoding='utf-8') as cache:
                self.assertEqual(1, json.loads(cache.read())['errors']['open-meteo'])

    def test_update_forecast_expired_cache_neutral(self):
        now = 1700000000
        with tempfile.TemporaryDirectory() as test_dir:
            args = self._forecast_args(os.path.join(test_dir, 'forecast.json'))
            # older than max-age -> revert to seasonal-neutral (factor 1.0)
            self._write_cache(
                args.forecast_cache, ts=now - 100 * 3600, factor=0.4, errors={})
            factor, status = update_forecast(args, now=now, fetcher=self._boom)
            self.assertEqual(1.0, factor)
            self.assertEqual(1, status['forecast_fetch_error'])

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

    def test_soc_sleep_duty(self):
        # gamma 1.0 is the identity (original linear behaviour)
        self.assertEqual(50, soc_sleep_duty(50, 1.0))
        # endpoints are pinned regardless of gamma
        self.assertEqual(0, soc_sleep_duty(0, 2.0))
        self.assertEqual(100, soc_sleep_duty(100, 2.0))
        # gamma>1 lowers the mid-range duty -> sleepier for the same SOC
        self.assertAlmostEqual(25, soc_sleep_duty(50, 2.0), places=5)
        self.assertLess(soc_sleep_duty(75, 2.0), 75)
        # a lower duty yields more sleep through sleep_duty_seconds
        bent = sum(sleep_duty_seconds(soc_sleep_duty(50, 2.0), 15, 1440)
                   for _ in range(1000))
        linear = sum(sleep_duty_seconds(50, 15, 1440) for _ in range(1000))
        self.assertGreater(bent, linear)

    def test_soc_sleep_gamma_arg(self):
        self.assertEqual(1.0, parse_args().soc_sleep_gamma)

    def test_ocv_curves_wellformed(self):
        for chemistry, curve in OCV_CURVES.items():
            volts = [v for v, _ in curve]
            socs = [s for _, s in curve]
            self.assertEqual(sorted(volts), volts, chemistry)
            self.assertEqual(sorted(socs), socs, chemistry)
            self.assertEqual((0, 100), (socs[0], socs[-1]), chemistry)

    def test_interp_soc_clamps_and_interpolates(self):
        curve = ((3.0, 0), (3.2, 50), (3.4, 100))
        self.assertEqual(0, interp_soc(2.0, curve))
        self.assertEqual(100, interp_soc(4.0, curve))
        self.assertEqual(0, interp_soc(3.0, curve))
        self.assertEqual(50, interp_soc(3.2, curve))
        self.assertAlmostEqual(25, interp_soc(3.1, curve), places=5)

    def test_interp_soc_is_not_linear(self):
        # the point of the curve: equal voltage steps are unequal charge steps
        curve = OCV_CURVES['lifepo4']
        plateau = interp_soc(3.30, curve) - interp_soc(3.25, curve)
        top = interp_soc(3.40, curve) - interp_soc(3.35, curve)
        self.assertAlmostEqual(20, plateau, places=5)
        self.assertAlmostEqual(10, top, places=5)
        self.assertGreater(plateau, top)

    def test_calc_soc_lifepo4_curve(self):
        args = SimpleNamespace(battery_chemistry='lifepo4', battery_cells=8,
                               fullvoltage=26.0, shutdownvoltage=24.3)
        # real ridge-pi 8S pack: overnight low, then midday peak
        self.assertAlmostEqual(64.5, calc_soc(26.29, args), places=1)
        self.assertAlmostEqual(90.2, calc_soc(26.81, args), places=1)
        linear = SimpleNamespace(battery_chemistry='linear', fullvoltage=27.0,
                                 shutdownvoltage=24.3)
        # the linear ramp calls the same pack fuller, which kept the node awake
        self.assertGreater(calc_soc(26.29, linear), calc_soc(26.29, args))

    def test_calc_soc_curve_ignores_fullvoltage(self):
        args = SimpleNamespace(battery_chemistry='lifepo4', battery_cells=8,
                               fullvoltage=26.0, shutdownvoltage=24.3)
        # an OCV curve is a property of the pack, not of a policy voltage
        before = calc_soc(26.4, args)
        args.fullvoltage = 99.0
        self.assertEqual(before, calc_soc(26.4, args))
        self.assertAlmostEqual(70.0, before, places=1)

    def test_prune_voltage_history(self):
        history = [[100, 26.0], [200, 26.1], [300, 26.2]]
        self.assertEqual(history, prune_voltage_history(history, 300, 1000))
        self.assertEqual([[200, 26.1], [300, 26.2]],
                         prune_voltage_history(history, 300, 150))
        self.assertEqual([], prune_voltage_history(history, 10000, 10))

    def test_voltage_trend_needs_a_baseline(self):
        self.assertIsNone(voltage_trend([], 1800))
        self.assertIsNone(voltage_trend([[0, 26.0]], 1800))
        # 10m of samples cannot clear a 30m minimum span
        self.assertIsNone(voltage_trend([[0, 26.0], [600, 26.1]], 1800))

    def test_voltage_trend_across_a_sleep(self):
        # a 2h sleep gap is the baseline: +0.2V over 2h -> +0.1V/h, charging
        charging = voltage_trend([[0, 26.3], [7200, 26.5]], 1800)
        self.assertAlmostEqual(0.1, charging, places=5)
        discharging = voltage_trend([[0, 26.5], [7200, 26.3]], 1800)
        self.assertAlmostEqual(-0.1, discharging, places=5)

    def test_voltage_trend_uses_newest_valid_baseline(self):
        # an old sample must not average day and night together
        history = [[0, 20.0], [3600, 26.3], [7200, 26.5]]
        self.assertAlmostEqual(0.2, voltage_trend(history, 1800), places=5)

    def test_charging_state(self):
        self.assertTrue(charging_state(0.1, 0.01, False))
        self.assertFalse(charging_state(-0.1, 0.01, True))

    def test_charging_state_flat_is_not_charging(self):
        # holding 'charging' through a flat idle night would keep it awake
        self.assertFalse(charging_state(0.0, 0.01, True))
        self.assertFalse(charging_state(0.001, 0.01, True))

    def test_charging_state_holds_when_unmeasurable(self):
        # no baseline yet -> caller's state stands (fail awake at boot)
        self.assertTrue(charging_state(None, 0.01, True))
        self.assertFalse(charging_state(None, 0.01, False))

    def test_charging_duty_scale(self):
        args = SimpleNamespace(not_charging_duty_scale=0.25)
        self.assertEqual(1.0, charging_duty_scale(args, True))
        self.assertEqual(0.25, charging_duty_scale(args, False))
        neutral = SimpleNamespace(not_charging_duty_scale=1.0)
        self.assertEqual(1.0, charging_duty_scale(neutral, False))

    def test_policy_duty_composes_scales(self):
        args = SimpleNamespace(soc_sleep_gamma=2.0)
        base = policy_duty(64.5, args, ())
        self.assertAlmostEqual(41.6, base, places=1)
        # independent policies multiply: dark season AND not charging
        self.assertAlmostEqual(base * 0.5 * 0.25,
                               policy_duty(64.5, args, (0.5, 0.25)), places=5)
        # neutral scales leave the SOC duty alone
        self.assertAlmostEqual(base, policy_duty(64.5, args, (1.0, 1.0)), places=5)

    def test_charge_args_default_neutral(self):
        args = parse_args()
        self.assertEqual('linear', args.battery_chemistry)
        self.assertEqual(1.0, args.not_charging_duty_scale)
        self.assertEqual(1.0, args.winter_duty_scale)
        self.assertEqual(1.0, args.forecast_duty_scale)

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
