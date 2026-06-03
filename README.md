# Flashing the firmward onto the SleepyPi

1. Install the programming board onto the SleepyPi like pictured [here](https://spellfoundry.com/product/sleepy-pi-external-programming-adapter-console/) (not attached to a Pi though)
2. Plug in USB-C into the SleepyPi for power
3. Plug in Micro-USB into the programming board for the serial console and into the computer you're flashing from
4. Open up [sleepypi.ino](sleepypi.ino) in the [Arduino IDE](https://www.arduino.cc/en/software)
5. In the IDE, go under Tools -> Board, select "Arduino Fio"
6. In the IDE, go under Sketch -> Include Library -> Manage Libraries, and install all of the libraries specifically named in the comments at the top of the `sleepypi.ino` file you opened
7. In the IDE, under Tools, open "Serial Monitor"
8. In the IDE, on the opened `sleepypi.ino` file, first "Verify" (check mark icon), and assuming that succeeds, proceed to "Upload" (right arrow icon)
9. You should see JSON info coming in on the serial monitor and the board should start blinking
10. Pull USB cables, remove programming board, and deploy your freshly flashed SleepyPi

# sleepypid daemon

`sleepypid/sleepypid.py` polls the SleepyPi hat over serial, manages sleep/wake
duty cycling, and logs telemetry.

## Seasonal full-voltage scaling

For a solar-charged node the battery's "considered full" threshold can be tuned
to the time of year and site latitude. Set `--winter-fullvoltage` (the darkest-day
threshold) and `--latitude`; the threshold is then interpolated by today's
clear-sky solar *energy* between `--winter-fullvoltage` and `--fullvoltage` (the
lightest-day value). Energy (not just daylength) is the driver because winter's
low sun angle cuts daily charge more than the shorter day alone implies, so the
winter ramp arrives earlier and deeper. Less energy raises the threshold, so the
battery reads as less full, the state-of-charge drops, and the Pi sleeps more. It
is off (static `--fullvoltage`) unless `--winter-fullvoltage` is set.

## Sleep curve

The Pi's sleep duty cycle is the state-of-charge directly, which only yields long
sleeps once charge is already deep. `--soc-sleep-gamma` (default `1.0`, linear)
bends that curve: a value `>1` lowers the duty across the mid-range so the node
sleeps harder for the same charge (e.g. `2.0` roughly triples the sleep at 50%
SOC) while still keeping 0% and 100% fixed. This amplifies the seasonal and
forecast adjustments — and the raw SOC — without distorting the logged SOC. The
applied duty is exported as `sleepypi_duty`.

## Solar forecast scaling

On top of the seasonal threshold, the daemon can fetch a short-range solar
forecast and sleep *more* when restricted sunlight is coming, so a cloudy spell
in an already-short-day season doesn't flatten the battery. The bias is
deliberately conservative: over-sleeping is fine, draining the battery is not.

Enable it by setting `--forecast-fullvoltage-span` (>0), the maximum extra volts
added to the threshold when no sunlight is forecast. The applied bump is
`(1 - light_factor) * span`, where `light_factor` is the next
`--forecast-days` (default 3) of forecast shortwave radiation divided by a
clear-sky reference (derived from latitude/day-of-year) and averaged. A missing
day counts as zero (err sleepy).

Providers (`--forecast-provider`):

- `open-meteo` (default) — keyless and free, works out of the box. Not an
  NZ-government source, but it serves NZ-region model data.
- `metservice` — the NZ MetService / MetOcean Point Forecast API. A **paid**
  plan (from ~US$30/mo, <https://console.metoceanapi.com/>) with the key passed
  via `--forecast-key`. The response parser is coded against the documented
  schema but is **unverified against a live response** — confirm it before
  relying on it.
- `none` — disable fetching.

Set `--latitude` and `--longitude` for the site. The forecast is fetched at most
once per `--forecast-refresh-hours` (default 6) and cached to
`--forecast-cache` (default `/var/lib/sleepypid/forecast.json`) so a single fetch
covers a whole wake/sleep cycle. On a failed fetch the last forecast is held for
up to `--forecast-max-age-hours` (default 48), after which the bump reverts to
zero (seasonal-only) — never permanently sleepy, or the node could never wake
long enough to fetch a fresh forecast.

Example (keyless default provider):

```
sleepypid.py --winter-fullvoltage 27.0 --fullvoltage 26.0 \
  --latitude -41.1 --longitude 174.8 \
  --forecast-fullvoltage-span 0.4
```

To use the paid MetService source instead, add
`--forecast-provider metservice --forecast-key $METSERVICE_KEY`.

## Prometheus metrics

By default the daemon exposes its current sensor values and derived state as
Prometheus gauges on port `9110` (override with `--prometheus-port`, disable
with `--no-prometheus`). Point a Prometheus scrape at `http://<host>:9110/metrics`
instead of parsing the log file. All metrics are prefixed `sleepypi_`, e.g.
`sleepypi_mean1mSupplyVoltage`, `sleepypi_mean1mRpiCurrent`, `sleepypi_soc`,
`sleepypi_powerState`, and `sleepypi_cputempc`.

When solar forecast scaling is enabled, these are also exported:

- `sleepypi_forecast_factor` — expected clear-sky fraction `[0,1]` (1 = clear).
- `sleepypi_forecast_bump` — volts added to the considered-full threshold.
- `sleepypi_forecast_age_seconds` — age of the forecast in use (`-1` if none).
- `sleepypi_forecast_fetch_ok` / `sleepypi_forecast_fetch_error` — `1/0` for the
  last attempt's outcome.
- `sleepypi_forecast_source_<provider>` — `1` for the active provider, else `0`.
- `sleepypi_forecast_errors_<provider>` — cumulative fetch failures per provider,
  persisted across restarts so a failing source can be alerted on.

## Running the tests

Tests and lint run inside Docker:

```
docker build --target test .
```

