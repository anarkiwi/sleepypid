#!/usr/bin/python3

"""SleepyPi hat manager."""

import argparse
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


def seasonal_fullvoltage(args, when=None):
    """Full-charge voltage scaled by photoperiod.

    With args.winter_fullvoltage set, args.fullvoltage is the lightest-day
    (summer) value and winter_fullvoltage the darkest-day value. The threshold
    is interpolated by today's daylength between the local solstices: less light
    -> higher threshold -> the battery reads as less full -> the Pi sleeps more.
    Returns the static args.fullvoltage when winter_fullvoltage is unset.
    """
    winter = getattr(args, 'winter_fullvoltage', 0)
    if not winter:
        return args.fullvoltage
    latitude = getattr(args, 'latitude', 0)
    when = when or datetime.date.today()
    daylengths = [daylength_hours(d, latitude) for d in range(1, 366)]
    dmin, dmax = min(daylengths), max(daylengths)
    today = daylength_hours(when.timetuple().tm_yday, latitude)
    light = (today - dmin) / (dmax - dmin) if dmax > dmin else 1.0
    light = max(0.0, min(1.0, light))
    return winter + light * (args.fullvoltage - winter)


def calc_soc(mean_v, args, fullvoltage=None):
    """Calculate battery SOC."""
    # TODO: consider discharge current.
    if fullvoltage is None:
        fullvoltage = args.fullvoltage
    if mean_v >= fullvoltage:
        return 100
    if mean_v <= args.shutdownvoltage:
        return 0
    return (mean_v - args.shutdownvoltage) / (fullvoltage - args.shutdownvoltage) * 100


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


def load_forecast_cache(path):
    """Return the cached forecast dict, or None if absent/unreadable."""
    try:
        with open(path, encoding='utf-8') as cache:
            return json.loads(cache.read())
    except (OSError, ValueError):
        return None


def save_forecast_cache(path, obj):
    """Persist the forecast dict so the factor survives sleep cycles."""
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
        save_forecast_cache(args.forecast_cache, cache)
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
    cache = load_forecast_cache(args.forecast_cache)
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
        save_forecast_cache(args.forecast_cache, new_cache)
        return factor, forecast_status(args, factor, 0.0, new_cache, 'live')
    except (OSError, ValueError, KeyError):
        cache = record_forecast_error(args, cache)
        if 'factor' in cache:
            age = now - cache.get('ts', 0)
            if age < args.forecast_max_age_hours * 3600:
                return cache['factor'], forecast_status(args, cache['factor'], age, cache, 'error')
        return 1.0, forecast_status(args, 1.0, None, cache, 'error')


def forecast_enabled(args):
    """True when forecast scaling is opted in (a span and a real provider)."""
    return (getattr(args, 'forecast_fullvoltage_span', 0.0) > 0 and
            getattr(args, 'forecast_provider', 'none') != 'none')


def effective_fullvoltage(args, factor, when=None):
    """Seasonal full voltage plus a forecast bump when sunlight is restricted.

    factor is the expected clear-sky fraction [0,1]; less light -> larger bump
    -> battery reads less full -> the Pi sleeps more.
    """
    full = seasonal_fullvoltage(args, when)
    span = getattr(args, 'forecast_fullvoltage_span', 0.0)
    if not span or factor is None:
        return full
    return full + (1.0 - max(0.0, min(1.0, factor))) * span


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
                if window_diffs and sample_count >= args.window_samples:
                    fullvoltage = effective_fullvoltage(args, forecast_factor)
                    soc = calc_soc(response[MEAN_V], args, fullvoltage)
                    window_summary = {
                        'window_diffs': window_diffs,
                        'soc': soc,
                        'fullvoltage': fullvoltage,
                    }
                    if forecast_enabled(args):
                        window_summary.update(forecast_telemetry)
                        window_summary['forecast_bump'] = (
                            fullvoltage - seasonal_fullvoltage(args))
                    log_json(args.log, window_summary, args.prometheus)

                    if args.sleepscript and (sample_count % args.window_samples == 0):
                        duration = sleep_duty_seconds(soc, args.minsleepmins, args.maxsleepmins)
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
        '--winter-fullvoltage', default=0.0, type=float,
        help='full voltage at the darkest day of the year; if set (>0), the '
             'considered-full threshold is scaled by photoperiod between this '
             'and --fullvoltage so the Pi sleeps more in the dark season')
    parser.add_argument(
        '--latitude', default=0.0, type=float,
        help='site latitude in degrees (negative south) for --winter-fullvoltage')
    parser.add_argument(
        '--longitude', default=0.0, type=float,
        help='site longitude in degrees (negative west) for the solar forecast')
    parser.add_argument(
        '--forecast-fullvoltage-span', default=0.0, type=float,
        help='max extra volts added to the considered-full threshold when the '
             'solar forecast shows no sunlight; if set (>0), enables forecast '
             'scaling on top of the seasonal threshold (the Pi sleeps more when '
             'restricted sunlight is forecast)')
    parser.add_argument(
        '--forecast-provider', default='metservice',
        choices=sorted(FORECAST_PARSERS) + ['none'],
        help='solar forecast source (metservice needs --forecast-key)')
    parser.add_argument(
        '--forecast-key', default='',
        help='API key for the forecast provider (free for metservice from '
             'console.metoceanapi.com)')
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
    if main_args.winter_fullvoltage:
        assert main_args.winter_fullvoltage > main_args.fullvoltage
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
