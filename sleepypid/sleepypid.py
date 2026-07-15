#!/usr/bin/python3

"""SleepyPi hat manager."""

import argparse
import bisect
import copy
import datetime
import json
import math
import platform
import random
import statistics
import subprocess
import sys
import time
import os
import urllib.parse
import urllib.request
from collections import defaultdict
import serial
from prometheus_client import Gauge, start_http_server

MIN_SLEEP_MINS = 15
MAX_SLEEP_MINS = (6 * 60) - MIN_SLEEP_MINS
MEAN_V = 'mean1mSupplyVoltage'
MEAN_C = 'mean1mRpiCurrent'
SHUTDOWN_TIMEOUT = 60
PROMETHEUS_PREFIX = 'sleepypi_'
prometheus_prefix = PROMETHEUS_PREFIX
prometheus_gauges = {}

# Published resting open-circuit volts per cell -> SOC%, ascending (see interp_soc).
OCV_CURVES = {
    'lifepo4': (
        (2.500, 0), (3.000, 10), (3.175, 20), (3.200, 30), (3.225, 40),
        (3.250, 50), (3.275, 60), (3.300, 70), (3.325, 80), (3.350, 90),
        (3.400, 100),
    ),
    'lead-acid': (
        (1.893, 0), (1.918, 10), (1.943, 20), (1.968, 30), (1.993, 40),
        (2.017, 50), (2.040, 60), (2.062, 70), (2.083, 80), (2.103, 90),
        (2.122, 100),
    ),
}


class SerialException(Exception):
    """Serial port exception."""


def get_temp():
    """Return CPU temperature."""
    return float(open('/sys/class/thermal/thermal_zone0/temp', encoding='utf-8').read()) / 1e3


def get_uptime():
    """Return uptime in seconds."""
    with open('/proc/uptime', encoding='utf-8') as uptime:
        return float(uptime.read().split()[0])


def mean_diff(stats):
    """Return mean, of the consecutive difference of list of numbers."""
    return statistics.mean([y - x for x, y in zip(stats, stats[1:])])


def sleep_duty_seconds(duty_cycle, sleep_interval_mins, max_sleep_mins):
    """Calculate sleep period if any based on duty cycle."""
    if duty_cycle >= 100:
        return 0
    if duty_cycle <= 0:
        return max_sleep_mins
    i = 0
    while random.random() * 100 >= duty_cycle:
        i += 1
    return i * sleep_interval_mins


def soc_sleep_duty(soc, gamma):
    """Bend the SOC->sleep duty curve so low charge sleeps harder.

    sleep_duty_seconds treats the duty cycle as the SOC directly, which only
    yields long sleeps once SOC is already deep (E[sleep] = interval*(100-d)/d).
    Raising the normalised SOC to gamma>1 lowers the duty across the mid-range
    (e.g. gamma=2 takes 50% SOC down to a 25% duty -> ~3x the sleep) while
    pinning the 0 and 100 endpoints. gamma=1.0 is the original linear behaviour.
    """
    if gamma == 1.0:
        return soc
    return (max(0.0, soc) / 100.0) ** gamma * 100.0


def prune_voltage_history(history, now, max_age_seconds):
    """Drop samples older than max_age_seconds, preserving ascending order."""
    return [sample for sample in history if now - sample[0] <= max_age_seconds]


def voltage_trend(history, min_span_seconds):
    """Volts/hour from the newest sample at least min_span old, else None.

    Sleep gaps are an asset: hours of baseline lift dV/dt far above the hat's
    ADC quantisation, which a short in-wake window cannot clear. Both endpoints
    are sampled awake, so the pack's IR drop is common and cancels.
    """
    if len(history) < 2:
        return None
    last_ts, last_v = history[-1]
    baseline = None
    for sample in history:
        if last_ts - sample[0] >= min_span_seconds:
            baseline = sample
    if baseline is None:
        return None
    return (last_v - baseline[1]) * 3600.0 / (last_ts - baseline[0])


def charging_state(trend, threshold, previous):
    """True while the pack is measurably gaining charge.

    Flat is not charging: an idle pack overnight barely moves, so holding a
    previous 'charging' through the flat would keep the node awake till dawn.
    The previous state stands only while no trend is measurable at all.
    """
    if trend is None:
        return previous
    return trend > threshold


def charging_duty_scale(args, charging):
    """Duty scale applied while the pack is not measurably gaining charge.

    Night, or solar failing to carry the load: the node buys the deficit back
    with sleep now, rather than predicting it.
    """
    if charging:
        return 1.0
    return max(0.0, min(1.0, getattr(args, 'not_charging_duty_scale', 1.0)))


def policy_duty(soc, args, scales):
    """Duty from measured SOC, reduced by each independent power policy.

    scales multiply because they answer different questions: how dark is the
    season, how cloudy the forecast, is the pack gaining charge right now.
    """
    duty = soc_sleep_duty(soc, args.soc_sleep_gamma)
    for scale in scales:
        duty *= scale
    return duty


def send_command(command, args):
    """Send a JSON command to the SleepyPi hat and parse response."""

    command_error = None

    try:
        pserial = serial.Serial(
            port=args.port, baudrate=args.speed,
            timeout=args.timeout, write_timeout=args.timeout)
        command_bytes = ('%s\r' % json.dumps(command)).encode()
        pserial.write(command_bytes)
        response_bytes = b''
        while True:
            serial_byte = pserial.read()
            if len(serial_byte) == 0 or serial_byte in (b'\r', 'b\n'):
                break
            response_bytes += serial_byte
    except serial.serialutil.SerialException as err:
        raise SerialException from err
    summary = {
        'command': json.loads(command_bytes.decode()),
        'response': {},
    }
    if response_bytes:
        summary['response'] = json.loads(response_bytes.decode())
        command_error = summary['response'].get('error', None)

    log_json(args.log, summary, args.prometheus)
    return (summary, command_error)


def configure_sleepypi(args):
    """Set SleepyPi's firmware defaults."""
    summary, command_error  = send_command({'command': 'getconfig'}, args)
    response = summary.get('response', '')
    if command_error or command_error is None:
        print('getconfig failed')
        sys.exit(-1)

    pid_config = {
        'shutdownVoltage': args.deepsleepvoltage,
        'startupVoltage': args.shutdownvoltage,
        'snoozeTimeout': SHUTDOWN_TIMEOUT * 2,
        'overrideEnabled': args.overrideenabled,
        'shutdownRpiCurrent': args.shutdowncurrent,
    }
    pi_config = {
        'shutdownVoltage': response['shutdownVoltage'],
        'startupVoltage': response['startupVoltage'],
        'snoozeTimeout': response['snoozeTimeout'],
        'overrideEnabled': response['overrideEnabled'],
        'shutdownRpiCurrent': response['shutdownRpiCurrent'],
    }

    if pid_config != pi_config:
        for k, v in pid_config.items():
            single_command = {'command': 'setconfig', k: v}
            response, command_error = send_command(single_command, args)
            if command_error or command_error is None:
                print('setconfig failed')
                sys.exit(-1)


def flatten_telemetry(obj):
    """Flatten a nested telemetry object into scalar key/value pairs."""
    flat = copy.copy(obj)
    if "loadavg" in flat:
        m1, m5, m15 = flat.pop("loadavg")
        flat["loadavg1m"] = m1
        flat["loadavg5m"] = m5
        flat["loadavg15m"] = m15
    response = flat.get("response")
    if isinstance(response, dict) and response.get("command") == "sensors":
        for key, value in response.items():
            flat[key] = value
        del flat["response"]
    if "window_diffs" in flat:
        for key, value in flat.pop("window_diffs").items():
            flat[key + "_window_diffs"] = value
    return flat


def log_prometheus(prometheus, obj):
    """Update Prometheus gauges from a telemetry object."""
    if not prometheus:
        return
    for key, value in flatten_telemetry(obj).items():
        if isinstance(value, bool):
            value = int(value)
        elif not isinstance(value, (int, float)):
            continue
        gauge = prometheus_gauges.get(key)
        if gauge is None:
            gauge = Gauge(
                "%s%s" % (prometheus_prefix, key),
                "sleepypi telemetry %s" % key)
            prometheus_gauges[key] = gauge
        gauge.set(value)


def log_json(log, obj, prometheus=True):
    """Log JSON object."""

    if os.path.isdir(log):
        ns_time = int(time.time_ns() / 1e6)
        log_dir = os.path.join(log, '%s-%u' % (platform.node(), ns_time))
        if not os.path.exists(log_dir):
            os.mkdir(log_dir)
        log_path = os.path.join(log_dir, 'sleepypi.%u' % ns_time)
    else:
        log_path = log

    obj.update({
        'timestamp': time.time(),
        'utctimestamp': str(datetime.datetime.utcnow()),
        'loadavg': os.getloadavg(),
        'uptime': get_uptime(),
        'cputempc': get_temp(),
    })
    with open(log_path, 'a', encoding='utf-8') as logfile:
        logfile.write(json.dumps(obj) + '\n')

    log_prometheus(prometheus, obj)


def daylength_hours(day_of_year, latitude):
    """Daylight hours for a day-of-year and latitude (Forsythe et al. 1995)."""
    lat = math.radians(latitude)
    theta = 0.2163108 + 2 * math.atan(
        0.9671396 * math.tan(0.00860 * (day_of_year - 186)))
    phi = math.asin(0.39795 * math.cos(theta))
    p = 0.8333  # sun's apparent radius + refraction at sunrise/sunset
    arg = ((math.sin(math.radians(p)) + math.sin(lat) * math.sin(phi)) /
           (math.cos(lat) * math.cos(phi)))
    arg = max(-1.0, min(1.0, arg))
    return 24.0 - (24.0 / math.pi) * math.acos(arg)


def seasonal_light(args, when=None):
    """Today's clear-sky solar energy as a fraction of the year's range [0,1].

    Energy, not daylength, drives this: winter's low sun angle cuts daily charge
    far more than the shorter day implies (clear-sky energy bottoms out near
    0.27x its summer peak at mid-latitudes, versus ~0.6x for daylength).
    """
    latitude = getattr(args, 'latitude', 0)
    when = when or datetime.date.today()
    energies = [clearsky_radiation(day, latitude) for day in range(1, 366)]
    emin, emax = min(energies), max(energies)
    today = clearsky_radiation(when.timetuple().tm_yday, latitude)
    light = (today - emin) / (emax - emin) if emax > emin else 1.0
    return max(0.0, min(1.0, light))


def ramp_scale(floor, fraction):
    """Interpolate a duty scale between floor (fraction 0) and 1.0 (fraction 1)."""
    return floor + max(0.0, min(1.0, fraction)) * (1.0 - floor)


def seasonal_duty_scale(args, when=None):
    """Duty scale that banks energy into the pack as the season darkens.

    Winter offers less energy than the pack can be relied on to replace, so the
    same SOC must buy a lower duty. This is a policy about reserve, kept
    separate from SOC, which stays a measurement.
    """
    floor = getattr(args, 'winter_duty_scale', 1.0)
    if floor >= 1.0:
        return 1.0
    return ramp_scale(floor, seasonal_light(args, when))


def forecast_duty_scale(args, factor):
    """Duty scale that banks energy ahead of a forecast cloudy spell.

    factor is the expected clear-sky fraction [0,1]; less light -> lower duty,
    so the node sheds load BEFORE the pack sags rather than after.
    """
    floor = getattr(args, 'forecast_duty_scale', 1.0)
    if floor >= 1.0 or factor is None:
        return 1.0
    return ramp_scale(floor, factor)


def interp_soc(volts_per_cell, curve):
    """Interpolate a resting-OCV curve -> SOC%, clamped to the curve's ends.

    Charge is not linear in voltage, so a chemistry curve reads a pack far more
    faithfully than a ramp between two policy voltages: on LiFePO4's plateau a
    linear ramp calls a half-empty pack nearly full.
    """
    volts = [point[0] for point in curve]
    socs = [point[1] for point in curve]
    i = bisect.bisect_left(volts, volts_per_cell)
    if i == 0:
        return float(socs[0])
    if i == len(volts):
        return float(socs[-1])
    span = volts[i] - volts[i - 1]
    frac = (volts_per_cell - volts[i - 1]) / span if span else 0.0
    return socs[i - 1] + frac * (socs[i] - socs[i - 1])


def calc_soc(mean_v, args):
    """Measure battery SOC, by OCV curve when a chemistry is configured.

    Falls back to the legacy linear ramp between --shutdownvoltage and
    --fullvoltage when --battery-chemistry is 'linear'. SOC is a measurement:
    policy belongs in the duty scales, not in a moved goalpost here.
    """
    # TODO: IR-compensate with MEAN_C; a loaded reading sits below true OCV.
    curve = OCV_CURVES.get(getattr(args, 'battery_chemistry', 'linear'))
    if curve:
        return interp_soc(mean_v / args.battery_cells, curve)
    if mean_v >= args.fullvoltage:
        return 100
    if mean_v <= args.shutdownvoltage:
        return 0
    return (mean_v - args.shutdownvoltage) / (args.fullvoltage - args.shutdownvoltage) * 100


def extraterrestrial_radiation(day_of_year, latitude):
    """Daily top-of-atmosphere radiation Ra in MJ/m^2 (FAO-56 eq. 21).

    The astronomical upper bound on a day's solar energy at this latitude;
    used as the clear-sky reference the forecast is normalised against.
    """
    lat = math.radians(latitude)
    dr = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    decl = 0.409 * math.sin(2 * math.pi * day_of_year / 365 - 1.39)
    arg = max(-1.0, min(1.0, -math.tan(lat) * math.tan(decl)))
    sunset = math.acos(arg)
    gsc = 0.0820  # solar constant, MJ/m^2/min
    ra = (24 * 60 / math.pi) * gsc * dr * (
        sunset * math.sin(lat) * math.sin(decl) +
        math.cos(lat) * math.cos(decl) * math.sin(sunset))
    return max(0.0, ra)


def clearsky_radiation(day_of_year, latitude):
    """Clear-sky surface solar radiation Rso in MJ/m^2 (FAO-56, ~0.75*Ra)."""
    return 0.75 * extraterrestrial_radiation(day_of_year, latitude)


def forecast_light_factor(daily_ghi, when, latitude):
    """Expected fraction of clear-sky sunlight over the forecast days [0,1].

    daily_ghi is the per-day forecast global horizontal irradiation (MJ/m^2),
    the first entry for `when`. Each day is divided by its clear-sky reference
    and capped at 1.0; a missing day (None) counts as 0.0 so we err sleepy.
    Returns 1.0 (neutral, no bump) when there is nothing usable to act on.
    """
    ratios = []
    for offset, ghi in enumerate(daily_ghi):
        day = when + datetime.timedelta(days=offset)
        clearsky = clearsky_radiation(day.timetuple().tm_yday, latitude)
        if clearsky <= 0:
            continue
        if ghi is None:
            ratios.append(0.0)
            continue
        ratios.append(max(0.0, min(1.0, ghi / clearsky)))
    if not ratios:
        return 1.0
    return statistics.mean(ratios)


def parse_open_meteo(payload):
    """Open-Meteo daily payload -> per-day GHI in MJ/m^2 (shortwave_radiation_sum)."""
    daily = payload.get('daily', {})
    values = daily.get('shortwave_radiation_sum', [])
    return [None if v is None else float(v) for v in values]


def parse_metservice(payload):
    """MetService/MetOcean point/time payload -> per-day GHI in MJ/m^2.

    Integrates the hourly radiation.shortwave flux (W/m^2) over each UTC day:
    W/m^2 sustained for one hour is W/m^2 * 3600 s = J/m^2, /1e6 -> MJ/m^2.

    NOTE: coded against the documented CF-JSON shape but UNVERIFIED against a
    live response (MetService is a paid plan). Confirm the field layout before
    relying on it; only this parser and forecast_request should need changing.
    """
    dims = payload.get('dimensions', {})
    times = dims.get('time', {}).get('data', [])
    var = payload.get('variables', {}).get('radiation.shortwave', {})
    values = var.get('data', [])
    nodata = payload.get('noData')
    daily = defaultdict(float)
    for ts, val in zip(times, values):
        if val is None or (nodata is not None and val == nodata):
            continue
        day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()
        daily[day] += float(val) * 3600 / 1e6
    return [daily[day] for day in sorted(daily)]


FORECAST_PARSERS = {
    'open-meteo': parse_open_meteo,
    'metservice': parse_metservice,
}


def forecast_request(args):
    """Build (url, headers, body) for the configured forecast provider."""
    if args.forecast_provider == 'open-meteo':
        base = args.forecast_url or 'https://api.open-meteo.com/v1/forecast'
        query = urllib.parse.urlencode({
            'latitude': args.latitude,
            'longitude': args.longitude,
            'daily': 'shortwave_radiation_sum',
            'forecast_days': args.forecast_days,
            'timezone': 'UTC',
        })
        return ('%s?%s' % (base, query), {}, None)
    if args.forecast_provider == 'metservice':
        base = args.forecast_url or 'https://forecast-v2.metoceanapi.com/point/time'
        body = json.dumps({
            'points': [{'lon': args.longitude, 'lat': args.latitude}],
            'variables': ['radiation.shortwave'],
            'time': {
                'from': datetime.datetime.now(
                    datetime.timezone.utc).strftime('%Y-%m-%dT%H:00:00Z'),
                'interval': '1h',
                'repeat': args.forecast_days * 24,
            },
        }).encode()
        headers = {'x-api-key': args.forecast_key, 'Content-Type': 'application/json'}
        return (base, headers, body)
    raise ValueError('unknown forecast provider %s' % args.forecast_provider)


def fetch_forecast(args):
    """Fetch and JSON-decode the raw forecast payload (network IO)."""
    url, headers, body = forecast_request(args)
    request = urllib.request.Request(
        url, data=body, headers=headers, method='POST' if body else 'GET')
    with urllib.request.urlopen(request, timeout=args.forecast_timeout) as resp:
        return json.loads(resp.read().decode())


def load_json_cache(path):
    """Return the cached JSON object, or None if absent/unreadable."""
    try:
        with open(path, encoding='utf-8') as cache:
            return json.loads(cache.read())
    except (OSError, ValueError):
        return None


def save_json_cache(path, obj):
    """Persist a JSON object so it survives sleep cycles."""
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as cache:
        cache.write(json.dumps(obj))


def forecast_status(args, factor, age, cache, outcome):
    """Numeric forecast telemetry for Prometheus.

    outcome is 'live' (fresh fetch), 'cache' (reused, no attempt) or 'error'
    (fetch failed). Exposes the applied factor, cache age, the last outcome,
    which provider is active, and a persisted cumulative error count per
    provider so Prometheus can alert on a failing source even though the daemon
    restarts every wake.
    """
    status = {
        'forecast_factor': factor,
        'forecast_age_seconds': -1.0 if age is None else age,
        'forecast_fetch_ok': int(outcome == 'live'),
        'forecast_fetch_error': int(outcome == 'error'),
    }
    errors = (cache or {}).get('errors', {})
    for provider in FORECAST_PARSERS:
        key = provider.replace('-', '_')
        status['forecast_source_' + key] = int(provider == args.forecast_provider)
        status['forecast_errors_' + key] = errors.get(provider, 0)
    return status


def record_forecast_error(args, cache):
    """Increment and persist the per-provider error count, keeping any cached factor."""
    if cache is None:
        cache = {'ts': 0, 'errors': {}}
    errors = cache.setdefault('errors', {})
    errors[args.forecast_provider] = errors.get(args.forecast_provider, 0) + 1
    try:
        save_json_cache(args.forecast_cache, cache)
    except OSError:
        pass
    return cache


def update_forecast(args, now=None, fetcher=fetch_forecast):
    """Return (light_factor, status), honouring cache + fail-sleepy semantics.

    Fetches at most once per --forecast-refresh-hours and persists to
    --forecast-cache, so a single fetch covers a whole wake/sleep cycle. On a
    failed fetch the last cached factor is held until --forecast-max-age-hours,
    after which it reverts to 1.0 (seasonal-only) -- never permanently sleepy,
    or the node could never wake long enough to fetch a fresh forecast. status
    is numeric telemetry (see forecast_status) merged into the Prometheus feed.
    """
    if now is None:
        now = time.time()
    cache = load_json_cache(args.forecast_cache)
    if cache and 'factor' in cache:
        age = now - cache.get('ts', 0)
        if age < args.forecast_refresh_hours * 3600:
            return cache['factor'], forecast_status(args, cache['factor'], age, cache, 'cache')
    try:
        daily = FORECAST_PARSERS[args.forecast_provider](fetcher(args))
        when = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
        factor = forecast_light_factor(daily, when, args.latitude)
        new_cache = {
            'ts': now, 'provider': args.forecast_provider,
            'lat': args.latitude, 'lon': args.longitude,
            'factor': factor, 'daily_ghi': daily,
            'errors': (cache or {}).get('errors', {}),
        }
        save_json_cache(args.forecast_cache, new_cache)
        return factor, forecast_status(args, factor, 0.0, new_cache, 'live')
    except (OSError, ValueError, KeyError):
        cache = record_forecast_error(args, cache)
        if 'factor' in cache:
            age = now - cache.get('ts', 0)
            if age < args.forecast_max_age_hours * 3600:
                return cache['factor'], forecast_status(args, cache['factor'], age, cache, 'error')
        return 1.0, forecast_status(args, 1.0, None, cache, 'error')


def forecast_enabled(args):
    """True when forecast scaling is opted in (a scale and a real provider)."""
    return (getattr(args, 'forecast_duty_scale', 1.0) < 1.0 and
            getattr(args, 'forecast_provider', 'none') != 'none')


def call_script(script, timeout=SHUTDOWN_TIMEOUT):
    """Call an external script with a timeout."""
    return subprocess.call(['timeout', str(timeout), script])


def loop(args):
    """Event loop."""

    sample_count = 0
    window_stats = defaultdict(list)
    window_diffs = {}
    ticker = 0
    forecast_factor = 1.0
    forecast_telemetry = {}
    next_forecast = 0
    history_max_age = args.voltage_history_max_age_hours * 3600
    voltage_history = prune_voltage_history(
        load_json_cache(args.voltage_history) or [], time.time(), history_max_age)
    # fail awake: an unknown charge state must never strand the node asleep.
    charging = True

    # TODO: sync sleepypi rtc with settime/hwclock -w if out of sync
    while True:
        if forecast_enabled(args) and time.time() >= next_forecast:
            forecast_factor, forecast_telemetry = update_forecast(args)
            next_forecast = time.time() + args.forecast_refresh_hours * 3600
        summary = None
        try:
            summary, command_error = send_command({'command': 'sensors'}, args)
        except SerialException:
            pass
        if summary and not command_error:
            response = summary.get('response', None)
            if response:
                sample_count += 1
                for stat in (MEAN_C, MEAN_V):
                    window_stats[stat].append(response[stat])
                for stat in ('cputempc',):
                    window_stats[stat].append(summary[stat])
                for stat in window_stats:
                    window_stats[stat] = window_stats[stat][-(args.window_samples):]
                    if len(window_stats[stat]) > 1:
                        window_diffs[stat] = mean_diff(window_stats[stat])
                now = time.time()
                voltage_history = prune_voltage_history(
                    voltage_history + [[now, response[MEAN_V]]], now, history_max_age)
                trend = voltage_trend(voltage_history, args.charge_min_span_mins * 60)
                charging = charging_state(
                    trend, args.charge_trend_threshold, charging)
                if window_diffs and sample_count >= args.window_samples:
                    soc = calc_soc(response[MEAN_V], args)
                    seasonal_scale = seasonal_duty_scale(args)
                    cloud_scale = forecast_duty_scale(args, forecast_factor)
                    charge_scale = charging_duty_scale(args, charging)
                    duty = policy_duty(
                        soc, args, (seasonal_scale, cloud_scale, charge_scale))
                    window_summary = {
                        'window_diffs': window_diffs,
                        'soc': soc,
                        'duty': duty,
                        'charging': charging,
                        'seasonal_duty_scale': seasonal_scale,
                        'forecast_duty_scale': cloud_scale,
                        'charging_duty_scale': charge_scale,
                    }
                    if trend is not None:
                        window_summary['voltage_trend'] = trend
                    if forecast_enabled(args):
                        window_summary.update(forecast_telemetry)
                    log_json(args.log, window_summary, args.prometheus)

                    # persisted here so the pre-sleep sample survives the poweroff
                    try:
                        save_json_cache(args.voltage_history, voltage_history)
                    except OSError:
                        pass

                    if args.sleepscript and (sample_count % args.window_samples == 0):
                        duration = sleep_duty_seconds(duty, args.minsleepmins, args.maxsleepmins)
                        if duration:
                            send_command({'command': 'snooze', 'duration': duration}, args)
                            call_script(args.sleepscript)
                            sys.exit(0)

        ticker += 1
        time.sleep(args.polltime)


def parse_args():
    DEFAULT_POLL_TIME = int(60)
    DEFAULT_WINDOW_SAMPLES = int(15 * DEFAULT_POLL_TIME / 60) # 15m
    parser = argparse.ArgumentParser(description='sleepypi hat manager')
    parser.add_argument(
        '--port', default='/dev/ttyAMA1',
        help='sleepypi serial port')
    parser.add_argument(
        '--speed', default=9600, type=int,
        help='sleepypi baudrate')
    parser.add_argument(
        '--timeout', default=5, type=int,
        help='sleepypi serial timeout')
    parser.add_argument(
        '--polltime', default=DEFAULT_POLL_TIME, type=int,
        help='sleepypi sensor poll period')
    parser.add_argument(
        '--log', default='/var/log/sleepypid.log',
        help='if a file, log to this file, if a directory, log telemetry in a subdirectory')
    parser.add_argument(
        '--window_samples', default=DEFAULT_WINDOW_SAMPLES, type=int,
        help='window size for sample results')
    parser.add_argument(
        '--battery-chemistry', default='linear', choices=['linear'] + sorted(OCV_CURVES),
        help='battery chemistry; a chemistry interpolates its resting OCV curve '
             'for SOC and ignores --fullvoltage (and so the seasonal/forecast '
             'scaling of it). "linear" keeps the legacy ramp between '
             '--shutdownvoltage and --fullvoltage')
    parser.add_argument(
        '--battery-cells', default=0, type=int,
        help='cells in series; required with --battery-chemistry (e.g. 8 for a '
             '24V LiFePO4 pack), as OCV curves are per cell')
    parser.add_argument(
        '--not-charging-duty-scale', default=1.0, type=float,
        help='scale the duty cycle by this while the pack is not charging, so '
             'the node sleeps harder overnight (1.0 disables)')
    parser.add_argument(
        '--charge-trend-threshold', default=0.01, type=float,
        help='volts/hour above which the pack counts as charging; at or below '
             'it (including flat) it does not')
    parser.add_argument(
        '--charge-min-span-mins', default=180, type=int,
        help='minimum baseline for a charge trend. A solar pack drifts slowly, '
             'so short spans are mostly sensor noise: on a 24V LiFePO4 pack a '
             '30m span misread ~45%% of the night as charging, 3h under 5%%')
    parser.add_argument(
        '--voltage-history', default='/var/lib/sleepypid/voltage.json',
        help='where supply voltage samples persist, so a trend can be measured '
             'across a sleep')
    parser.add_argument(
        '--voltage-history-max-age-hours', default=24.0, type=float,
        help='discard persisted voltage samples older than this')
    parser.add_argument(
        '--deepsleepvoltage', default=12.8, type=float,
        help='voltage at which sleepypi will disable power itself')
    parser.add_argument(
        '--shutdownvoltage', default=12.9, type=float,
        help='voltage at which sleepyid will disable power')
    parser.add_argument(
        '--shutdowncurrent', default=250, type=int,
        help='current in mA at which the Pi is considered shutdown')
    parser.add_argument(
        '--fullvoltage', default=13.3, type=float,
        help='voltage at which the battery is considered full (the '
             'lightest-day value when --winter-fullvoltage is set)')
    parser.add_argument(
        '--winter-duty-scale', default=1.0, type=float,
        help='duty scale at the darkest day of the year, ramping to 1.0 at the '
             'lightest by clear-sky solar energy. Winter offers less energy '
             'than the pack can be relied on to replace, so the same SOC buys '
             'a lower duty and banks the difference (1.0 disables)')
    parser.add_argument(
        '--latitude', default=0.0, type=float,
        help='site latitude in degrees (negative south) for --winter-duty-scale')
    parser.add_argument(
        '--longitude', default=0.0, type=float,
        help='site longitude in degrees (negative west) for the solar forecast')
    parser.add_argument(
        '--forecast-duty-scale', default=1.0, type=float,
        help='duty scale when the solar forecast shows no sunlight, ramping to '
             '1.0 at a clear forecast, so the node banks energy BEFORE a cloudy '
             'spell rather than after it (1.0 disables)')
    parser.add_argument(
        '--forecast-provider', default='open-meteo',
        choices=sorted(FORECAST_PARSERS) + ['none'],
        help='solar forecast source; open-meteo is keyless, metservice is the '
             'paid NZ MetService API and needs --forecast-key')
    parser.add_argument(
        '--forecast-key', default='',
        help='API key for the forecast provider (required for metservice, a '
             'paid plan from console.metoceanapi.com)')
    parser.add_argument(
        '--forecast-days', default=3, type=int,
        help='number of forecast days to average available sunlight over')
    parser.add_argument(
        '--forecast-cache', default='/var/lib/sleepypid/forecast.json',
        help='file to persist the last forecast across sleep cycles')
    parser.add_argument(
        '--forecast-refresh-hours', default=6.0, type=float,
        help='re-fetch the forecast no more often than this')
    parser.add_argument(
        '--forecast-max-age-hours', default=48.0, type=float,
        help='hold the last forecast this long on fetch failure before '
             'reverting to the seasonal-only threshold')
    parser.add_argument(
        '--forecast-timeout', default=15, type=int,
        help='solar forecast HTTP timeout in seconds')
    parser.add_argument(
        '--forecast-url', default='',
        help='override the forecast provider base URL (mainly for testing)')
    parser.add_argument(
        '--soc-sleep-gamma', default=1.0, type=float,
        help='exponent bending the SOC->sleep curve; >1 sleeps harder at low '
             'charge (e.g. 2.0 ~triples the sleep at 50%% SOC), 1.0 is linear')
    parser.add_argument(
        '--minsleepmins', default=MIN_SLEEP_MINS, type=float,
        help='minimum time to sleep')
    parser.add_argument(
        '--maxsleepmins', default=MAX_SLEEP_MINS, type=float,
        help='maximum time to sleep')
    parser.add_argument(
        '--overrideenabled', default=1, type=int,
        help='enable the sleepypi power override button')
    parser.add_argument('--sleepscript', default='',
        help='script to run to clean poweroff')
    parser.add_argument('--startscript', default='',
        help='script to run on startup')
    parser.add_argument(
        '--argjson', default='',
        help='file with JSON to override arguments')
    parser.add_argument(
        '--prometheus-port', default=9110, type=int,
        help='port to expose Prometheus metrics on')
    parser.add_argument(
        '--prometheus-prefix', default=PROMETHEUS_PREFIX,
        help='prefix for exported Prometheus metric names (set empty for bare names)')
    parser.add_argument('--prometheus', dest='prometheus', action='store_true')
    parser.add_argument('--no-prometheus', dest='prometheus', action='store_false')
    parser.set_defaults(prometheus=True)
    main_args = parser.parse_args()
    assert main_args.shutdownvoltage > main_args.deepsleepvoltage
    assert main_args.fullvoltage > main_args.shutdownvoltage
    assert main_args.soc_sleep_gamma > 0
    assert main_args.charge_trend_threshold >= 0
    for scale in ('not_charging_duty_scale', 'winter_duty_scale',
                  'forecast_duty_scale'):
        assert 0.0 <= getattr(main_args, scale) <= 1.0, '%s must be 0..1' % scale
    if main_args.battery_chemistry != 'linear':
        assert main_args.battery_cells > 0, \
            '--battery-chemistry requires --battery-cells'
    if main_args.winter_duty_scale < 1.0:
        assert main_args.latitude, '--winter-duty-scale requires --latitude'
    if forecast_enabled(main_args) and main_args.forecast_provider == 'metservice':
        assert main_args.forecast_key, '--forecast-provider metservice requires --forecast-key'
    return main_args


def override_args(main_args):
    if main_args.argjson:
        with open(main_args.argjson, encoding='utf-8') as f:
            argjson = json.loads(f.read())
            for k, v in argjson.items():
                if hasattr(main_args, k):
                    setattr(main_args, k, v)
    return main_args


if __name__ == '__main__':
    main_args = parse_args()
    main_args = override_args(main_args)
    if main_args.prometheus:
        prometheus_prefix = main_args.prometheus_prefix
        start_http_server(main_args.prometheus_port)
    if main_args.startscript:
        call_script(main_args.startscript)
    configure_sleepypi(main_args)
    loop(main_args)
