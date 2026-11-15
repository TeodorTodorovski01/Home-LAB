# Home-LAB air-quality monitor

A Raspberry Pi monitor for local DHT11 temperature/humidity and SDS011
PM2.5/PM10 readings. It serves a small web dashboard, compares local readings
with optional weather and air-quality reference feeds, and keeps an incident
log with a local, example-based classifier.

## What it does

- Reads a DHT11 on the Pi's `board.D4` pin and an SDS011 connected over USB
  serial.
- Samples both sensors about every 10 minutes. A logged DHT point averages
  four readings; an SDS011 point averages the valid measurements in its cycle.
- Serves a dashboard on port **8080** and a second LAN mirror on port **8081**.
- Displays Open-Meteo weather and CAMS model references, plus the MOEPP Lisice
  station feed where available. These references are separate from the Pi's
  local sensor measurements.
- Detects local PM events and stores them on the Pi. Detection and
  classification do not send sensor data to a cloud model.

## Requirements

- Raspberry Pi with Raspberry Pi OS and Python 3 (Python 3.9 or newer
  recommended).
- DHT11 temperature/humidity sensor wired to the pin represented by
  `board.D4` (BCM GPIO 4).
- SDS011 particulate sensor connected to a USB serial adapter.
- Internet access for the external reference feeds. The local sensor dashboard
  and incident classifier run on the Pi.

Install the Python dependencies in a virtual environment:

```sh
sudo apt update
sudo apt install -y python3-venv python3-pip libgpiod2
python3 -m venv ~/venvs/home-lab
source ~/venvs/home-lab/bin/activate
python -m pip install --upgrade pip
python -m pip install adafruit-blinka adafruit-circuitpython-dht pyserial
```

`pyserial` is needed for the SDS011. Without it, the application can start, but
the particulate sensor will not be read. If Raspberry Pi OS reports a GPIO or
`gpiod` dependency error, install the matching `libgpiod` package for that OS
release.

The Linux user running the app needs permission to access the serial device.
On Raspberry Pi OS, this is commonly provided by the `dialout` group:

```sh
sudo usermod -aG dialout "$USER"
```

Log out and back in after changing the group. If the SDS011 is not found
automatically, identify its device (often `/dev/ttyUSB0`) and set `SDS_PORT`
when starting the app.

## Install and run

Clone or copy this repository to the Pi. From the repository directory:

```sh
cd ~/Home-LAB
source ~/venvs/home-lab/bin/activate
bash mesure.py
python ~/clock.py
```

`mesure.py` is a shell script: running it with `bash` writes the application to
`~/clock.py`. Keep the Python process running; press **Ctrl+C** in its terminal
to stop it.

If needed, force the serial port:

```sh
SDS_PORT=/dev/ttyUSB0 python ~/clock.py
```

Find the Pi's address with `hostname -I`, then open these pages from a device
on the same network:

- Dashboard: `http://<pi-address>:8080/`
- Incident log: `http://<pi-address>:8080/incidents`
- LAN mirror: `http://<pi-address>:8081/`

The web server has no login or HTTPS. Keep it on a trusted LAN or VPN; do not
forward these ports directly to the public internet.

## Dashboard and reference data

The solid lines are the Pi's own sensors. Dotted and dashed lines are
references, not local measurements:

- Temperature and humidity reference: Open-Meteo weather models for Aerodrom,
  Skopje (`LAT` and `LON` near the top of `mesure.py`).
- Official pollution reference: MOEPP's Lisice station feed, relayed by WAQI.
- Model pollution reference: Open-Meteo CAMS data fills gaps in the station
  feed. Station and model values are combined separately for PM2.5 and PM10.

Reference feeds need internet access; sensor readings and incident
classification do not. The reference models and feeds can be delayed or
unavailable, and are identified separately in the charts.

For WAQI, the app checks `WAQI_TOKEN` in the environment, then `~/.waqi_token`.
If neither is configured, the official station reference is disabled; the
local sensors and other references continue to work. Keep the token out of
the repository. Save it in `~/.waqi_token` on the Pi or export `WAQI_TOKEN`
before launching.

## Incidents and classification

### Automatic detection

On startup and after each new SDS011 reading, the app scans the available
five-day local PM history. It deliberately uses sensitive candidate rules to
collect examples for you to review:

1. **Transient spike:** a PM2.5 or PM10 reading rises by at least
   **15 µg/m³** and reaches **1.5×** its baseline. It is logged as soon as
   the elevated reading arrives; its five-before/five-after context is updated
   as more readings arrive.
2. **Sustained relative rise:** at least **5 µg/m³** and **1.15×** baseline
   in two of three readings, with all five baseline readings available.
3. **Sustained high-level crossing:** PM2.5 reaches **35.5 µg/m³** or PM10
   reaches **155 µg/m³** after a lower reading, and at least two of the next
   three readings remain at or above that level. This catches a gradual
   medium-to-high increase even without a large relative spike.

These are deliberately sensitive, short-term screening thresholds, not
official AQI or health assessments. Expect extra false positives; label them
or choose **NC**. The SDS011 readings are local, short-cycle sensor values,
not the averaged measurements used for formal regulatory reporting. Baselines
use the median of up to five preceding readings. When all five are available,
the transient and relative-rise thresholds also account for normal variation
using the median absolute deviation (MAD), reducing triggers when recent
readings are unusually noisy. The absolute high-level crossing remains a
separate fixed-threshold rule. Rise rules look for an upward crossing, not
every reading in an already elevated plateau, to avoid logging one episode
repeatedly.

Every detected event is saved, including events the classifier cannot decide.
Those rows show **Undetermined** until you label them. Detections within one
PM reading interval (about 10 minutes) of a saved incident are treated as
duplicates. Incident rows are stored in the database indefinitely.

### Add and label incidents

On the incident page:

1. Use **Incident date and time** to select the Pi's local event time.
2. Click **Save incident readings**. Manual entries require five PM readings
   before and after the selected time. With the current sampling period, allow
   about 40–50 minutes for five readings after the event.
3. In the table, choose **Clean**, **Cleaning**, **Smoking**, or **Fire** from
   the dropdown and save the label.
4. Choose **NC — discard from training** for a false positive. This keeps the
   incident record but excludes it from classifier examples.

Manual entries must be within the five-day sensor history and have the
required readings available. Automatic transient candidates are stored
immediately with the available pre-event readings; their sample context is
expanded as later sensor readings arrive.

### How predictions work

The classifier is a local **3-nearest-neighbours** model, not an LLM or neural
network. It compares each event's median baseline, normalized peak rise, peak
position, and final post-event reading for PM2.5 and PM10 with the examples
you labeled. It starts predicting after it has at least four labeled
incidents across at least two classes, and reports a class only when at least
two of the three closest examples agree.

The table shows the detection reason, baseline-to-peak rise and time to peak,
the predicted class, neighbor vote count, and nearest labeled examples. A
`2/3` vote is neighbor agreement, not a calibrated probability. If there are
too few labeled examples or the neighbors do not agree, the event remains
**Undetermined** for you to label.

After at least eight labeled incidents, the page also reports a leave-one-out
sanity check: how many confidently predicted examples matched their saved
labels, and how many predictions were abstentions. This small, local dataset
check is not a safety validation. A prediction must never be used as a fire
alarm or substitute for a certified detector.

The current classifier labels detected candidate events; it does not scan
ordinary readings to discover events by itself. Replacing the candidate
detector later would require labeled **no-event** windows as well as examples
of each event class, followed by validation on separate readings. The current
**NC** option only excludes a candidate from training; it is not a no-event
training class.

## Local files and retention

All paths below are relative to the home directory of the user running the
app:

| File | Contents | Retention |
| --- | --- | --- |
| `~/clock.py` | Generated Python application | Until replaced |
| `~/history1m.json` | DHT temperature/humidity history | Rolling four hours |
| `~/history_pm.json` | SDS011 and pollution reference history | Local PM readings: rolling five days; references: rolling four hours |
| `~/incidents.sqlite3` | Incident readings, detection reason, and labels | Kept until you remove or back up the file |
| `~/.waqi_token` | Optional private WAQI token | Kept until you remove the file |

The five-day local PM log is scanned after a restart. Incidents already saved
in SQLite remain there; raw PM measurements older than the rolling five-day
history are not archived by this application. The dashboard still displays
its selected one-to-four-hour graph window. The five-day window starts filling
after this version is deployed; readings already removed by the old four-hour
retention cannot be recovered.

## Troubleshooting

- **No PM values:** check that the SDS011 is powered and connected, `pyserial`
  is installed in the active virtual environment, and the user can access its
  serial device. Try setting `SDS_PORT=/dev/ttyUSB0`.
- **No temperature/humidity:** check DHT11 wiring to BCM GPIO 4, the active
  virtual environment, and Blinka/DHT dependencies.
- **Incident not detected:** review the event's timestamped before/after
  readings, not only the chart's maximum. The detector must meet a screening
  rule above. Duplicate events within one reading interval are skipped. The
  scanner can only inspect measurements still in the five-day history.
- **Event says Undetermined:** label examples in the dropdown. Predictions
  require at least four labeled examples across two classes and a two-of-three
  neighbor majority.
- **Reference values missing:** the external feed may be offline, delayed, or
  missing the relevant station/model value. The local sensors do not depend on
  those reference feeds.

## Updating

Stop the running application with **Ctrl+C**, update or copy the repository on
the Pi, then regenerate and run the app again:

```sh
cd ~/Home-LAB
git pull
source ~/venvs/home-lab/bin/activate
bash mesure.py
python ~/clock.py
```

Keep a backup of `~/incidents.sqlite3` if you want to preserve labels and
incident history across SD-card failure or replacement.
