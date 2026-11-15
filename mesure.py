cat > ~/clock.py <<'EOF'
import glob, json, math, os, re, sqlite3, threading, time, urllib.request
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import board, adafruit_dht
try:
    import serial          # pyserial, needed for the SDS011 (sudo apt install python3-serial)
except ImportError:
    serial = None

READ_S = 600           # log one temperature/humidity point every 10 min (same as reference)
DHT_N = 4              # each point = average of 4 readings taken 2.5 min apart (1 = no averaging)
WINDOW_S = 4 * 3600    # graph window (4 hours)
GRID_S = 300           # grid every 5 min
SAVE_S = 300           # save to disk every 5 min (saves the SD card)
FILE = os.path.expanduser("~/history1m.json")

# Reference weather: Open-Meteo (weather service MODEL data, not a measurement*), Aerodrom, Skopje
LAT, LON = 41.98, 21.47
REF_S = 600            # refresh reference every 10 min
REF_URL = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,relative_humidity_2m"
           "&hourly=temperature_2m,relative_humidity_2m"
           "&past_hours=6&forecast_hours=1&timezone=GMT&timeformat=unixtime"
           % (LAT, LON))

C_T, C_H = "#e53935", "#1e88e5"      # sensor: red temp, blue humidity
C_RT, C_RH = "#fb8c00", "#00bcd4"    # reference: orange temp, cyan humidity

# ---------------- air quality: SDS011 (Nova PM sensor) ----------------
PM_LOG_S = 600         # log one PM point every 10 min (same as the reference)
PM_N = 4               # each point = (sum of 4 measurements taken 2.5 min apart) / 4
PM_MIN_OK = 2          # fewer valid measurements than this -> no point is logged
PM_WARM_S = 30         # fan + laser warm-up before every measurement
PM_SAMPLE_S = 10       # one measurement = average of the ~10 frames of 10 s (1 frame/s)
SDS_PORT = os.environ.get("SDS_PORT")        # optional: force a port, e.g. /dev/ttyUSB0
FILE_PM = os.path.expanduser("~/history_pm.json")
INCIDENT_DB = os.path.expanduser("~/incidents.sqlite3")
INCIDENT_LABELS = ("clean", "cleaning", "smoking", "fire")
INCIDENT_DISCARDED = "nc"
INCIDENT_CONTEXT_N = 3
INCIDENT_MIN_RISE_UGM3 = 10.0
INCIDENT_MIN_RISE_RATIO = 1.25
INCIDENT_TRANSIENT_RISE_UGM3 = 30.0
INCIDENT_TRANSIENT_RISE_RATIO = 2.0
INCIDENT_PM25_HIGH_UGM3 = 35.5
INCIDENT_PM10_HIGH_UGM3 = 155.0
INCIDENT_VALIDATION_MIN_N = 8
INCIDENT_DEDUP_S = PM_LOG_S

# Reference pollution, PM2.5 and PM10 each handled on their own:
#   1) OFFICIAL station "Lisice" of the Ministry of Environment (MOEPP), relayed by aqicn.org (WAQI)
#      -> drawn DASHED  (-----)
#   2) wherever the station has nothing (one pollutant, or the newest hours) the Open-Meteo CAMS
#      model fills in -> drawn DOTTED and marked with *  (model data, NOT government-measured)
#   so a line can look like  ----(official)----(official)....(model)
WAQI_IDX = 8106
WAQI_TOKEN_FILE = os.path.expanduser("~/.waqi_token")
REF_PM_S = 600
# model values are shown per clock hour (12:00-12:59) until the station publishes that hour, then replaced
REF_STALE_S = 3 * 3600 # a reference value older than this is shown as N/A
OM_AQ_URL = ("https://air-quality-api.open-meteo.com/v1/air-quality?latitude=%s&longitude=%s"
             "&current=pm10,pm2_5&hourly=pm10,pm2_5"
             "&past_hours=6&forecast_hours=1&timezone=GMT&timeformat=unixtime" % (LAT, LON))

# aqicn gives US-EPA AQI numbers; these tables turn them back into ug/m3
# (AQI lo, AQI hi, ug/m3 lo, ug/m3 hi). PM2.5 = EPA table valid since May 2024.
# If aqicn still used the old table, change the first PM2.5 rows to (0,50,0,12.0),(51,100,12.1,35.4)
PM25_BP = [(0, 50, 0.0, 9.0), (51, 100, 9.1, 35.4), (101, 150, 35.5, 55.4),
           (151, 200, 55.5, 125.4), (201, 300, 125.5, 225.4), (301, 500, 225.5, 325.4)]
PM10_BP = [(0, 50, 0, 54), (51, 100, 55, 154), (101, 150, 155, 254), (151, 200, 255, 354),
           (201, 300, 355, 424), (301, 400, 425, 504), (401, 500, 505, 604)]

C_P25, C_P10 = "#9c27b0", "#43a047"      # sensor: purple PM2.5, green PM10
C_RP25, C_RP10 = "#ec407a", "#afb42b"    # reference: pink PM2.5, olive PM10

lock = threading.Lock()
live = {"t": None, "h": None}
hist = []              # [ts, temp, humidity]
ref = {"pts": [], "t": None, "h": None, "ts": None, "dt": None}
last_save = [0.0]

live_pm = {"p25": None, "p10": None, "ts": None}
hist_pm = []           # [ts, pm2.5, pm10]  (your SDS011)
off_pm = []            # [ts, pm2.5, pm10]  official station points, either value may be None
mod_pm = []            # [ts, pm2.5, pm10]  model points, either value may be None
ref_pm = {"ts": None, "note": ""}
sds_state = {"msg": "starting..."}
last_save_pm = [0.0]

def load():
    now = time.time()
    try:
        with open(FILE) as f:
            d = json.load(f)
        hist.extend(p for p in d if now - p[0] <= WINDOW_S)
    except Exception:
        pass
    try:
        with open(FILE_PM) as f:
            d = json.load(f)
        hist_pm.extend(p for p in d.get("pm", []) if now - p[0] <= WINDOW_S)
        off_pm.extend(p for p in d.get("off", []) if now - p[0] <= WINDOW_S + 3600)
    except Exception:
        pass

def save():
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(hist, f)
    os.replace(tmp, FILE)

def save_pm():
    tmp = FILE_PM + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"pm": hist_pm, "off": off_pm}, f)
    os.replace(tmp, FILE_PM)

def maybe_save_pm(now):          # call with the lock held
    if now - last_save_pm[0] >= SAVE_S:
        try:
            save_pm()
            last_save_pm[0] = now
        except Exception:
            pass

def add(t, h):
    now = time.time()
    with lock:
        live["t"], live["h"] = t, h
        hist.append([now, t, h])
        hist[:] = [p for p in hist if now - p[0] <= WINDOW_S]
        if now - last_save[0] >= SAVE_S:
            try:
                save()
                last_save[0] = now
            except Exception:
                pass

def add_pm(p25, p10):
    now = time.time()
    with lock:
        live_pm["p25"], live_pm["p10"], live_pm["ts"] = p25, p10, now
        hist_pm.append([now, p25, p10])
        hist_pm[:] = [p for p in hist_pm if now - p[0] <= WINDOW_S]
        maybe_save_pm(now)
    detect_logged_incidents()

def init_incidents():
    with sqlite3.connect(INCIDENT_DB) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY,
            event_ts REAL NOT NULL,
            samples TEXT NOT NULL,
            label TEXT,
            created_ts REAL NOT NULL,
            detection_reason TEXT
        )""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(incidents)")}
        if "detection_reason" not in columns:
            db.execute("ALTER TABLE incidents ADD COLUMN detection_reason TEXT")

def incident_samples(event_ts):
    with lock:
        points = [p for p in hist_pm if p[1] is not None and p[2] is not None
                  and math.isfinite(p[1]) and math.isfinite(p[2])]
    before = [p for p in points if p[0] < event_ts][-INCIDENT_CONTEXT_N:]
    after = [p for p in points if p[0] >= event_ts][:INCIDENT_CONTEXT_N]
    if len(before) != INCIDENT_CONTEXT_N or len(after) != INCIDENT_CONTEXT_N:
        raise ValueError("Need three local PM readings before and three after the incident time. "
                         "The SDS011 logs about every 10 minutes; wait for three readings after the event.")
    return before + after

def get_incidents():
    with sqlite3.connect(INCIDENT_DB) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, event_ts, samples, label, detection_reason "
            "FROM incidents ORDER BY event_ts DESC, id DESC"
        ).fetchall()
    return [{"id": row["id"], "ts": row["event_ts"],
             "samples": json.loads(row["samples"]), "label": row["label"],
             "reason": row["detection_reason"]} for row in rows]

def incident_sample_parts(samples):
    if isinstance(samples, dict):
        return samples["before"], samples["after"]
    context_n = len(samples) // 2
    return samples[:context_n], samples[context_n:]

def incident_features(samples):
    features = []
    before, after = incident_sample_parts(samples)
    for column in (1, 2):
        baseline = sum(point[column] for point in before) / len(before)
        features.append(math.log1p(max(0, baseline)))
        scale = max(baseline, 5.0)
        changes = [(point[column] - baseline) / scale for point in after]
        peak_index = max(range(len(changes)), key=changes.__getitem__)
        features.extend((max(changes), peak_index / max(1, len(changes) - 1), changes[-1]))
    return features

def incident_metrics(samples):
    before, after = incident_sample_parts(samples)
    metrics = []
    for column, name in ((1, "PM2.5"), (2, "PM10")):
        baseline = sum(point[column] for point in before) / len(before)
        peak = max(after, key=lambda point: point[column])
        rise = peak[column] - baseline
        peak_offset = max(0, round((peak[0] - after[0][0]) / 60))
        metrics.append("%s baseline %.1f → peak %.1f (rise %+.1f, +%d min)" % (
            name, baseline, peak[column], rise, peak_offset))
    return "; ".join(metrics)

def incident_rise_detected(samples):
    before, after = incident_sample_parts(samples)
    baseline = [sum(point[column] for point in before) / len(before) for column in (1, 2)]
    pollutant_names = ("PM2.5", "PM10")
    high_levels = (INCIDENT_PM25_HIGH_UGM3, INCIDENT_PM10_HIGH_UGM3)

    for column, (value, name) in enumerate(zip(baseline, pollutant_names), start=1):
        transient_threshold = max(value + INCIDENT_TRANSIENT_RISE_UGM3,
                                  value * INCIDENT_TRANSIENT_RISE_RATIO)
        if after[0][column] >= transient_threshold:
            return "Transient %s spike to %.1f µg/m³" % (name, after[0][column])

    for column, (high_level, name) in enumerate(zip(high_levels, pollutant_names), start=1):
        if (before[-1][column] < high_level <= after[0][column]
                and sum(point[column] >= high_level for point in after) >= 2):
            return "Sustained %s high level (>= %.1f µg/m³)" % (name, high_level)

    for column, (value, name) in enumerate(zip(baseline, pollutant_names), start=1):
        threshold = max(value + INCIDENT_MIN_RISE_UGM3, value * INCIDENT_MIN_RISE_RATIO)
        if len(before) < INCIDENT_CONTEXT_N:
            continue
        if (after[0][column] >= threshold
                and sum(point[column] >= threshold for point in after) >= 2):
            return "Sustained %s rise (>= %.1f µg/m³ and %.2fx baseline)" % (
                name, INCIDENT_MIN_RISE_UGM3, INCIDENT_MIN_RISE_RATIO)
    return None

def detect_logged_incidents():
    with lock:
        points = [p for p in hist_pm if p[1] is not None and p[2] is not None
                  and math.isfinite(p[1]) and math.isfinite(p[2])]
    incidents = get_incidents()
    existing_times = [item["ts"] for item in incidents]
    for index in range(1, len(points) - INCIDENT_CONTEXT_N + 1):
        before = points[max(0, index - INCIDENT_CONTEXT_N):index]
        after = points[index:index + INCIDENT_CONTEXT_N]
        samples = (before + after if len(before) == INCIDENT_CONTEXT_N
                   else {"before": before, "after": after})
        event_ts = points[index][0]
        reason = incident_rise_detected(samples)
        if reason is None:
            continue
        if any(abs(event_ts - existing_ts) <= INCIDENT_DEDUP_S for existing_ts in existing_times):
            continue
        with sqlite3.connect(INCIDENT_DB) as db:
            db.execute(
                "INSERT INTO incidents "
                "(event_ts, samples, label, created_ts, detection_reason) "
                "VALUES (?, ?, NULL, ?, ?)",
                (event_ts, json.dumps(samples), time.time(), reason))
        existing_times.append(event_ts)

def classify_incident(incident, incidents):
    training = [item for item in incidents
                if item["label"] in INCIDENT_LABELS and item["id"] != incident["id"]]
    if len(training) < 4:
        return {"label": None, "votes": 0, "neighbors": [],
                "reason": "Needs at least four labeled examples"}
    if len({item["label"] for item in training}) < 2:
        return {"label": None, "votes": 0, "neighbors": [],
                "reason": "Needs examples from at least two classes"}

    vectors = [(item, incident_features(item["samples"])) for item in training]
    columns = zip(*(vector for _, vector in vectors))
    scales = [math.sqrt(sum((value - sum(col) / len(col)) ** 2 for value in col) / len(col))
              for col in columns]
    if not any(scale > 1e-9 for scale in scales):
        return {"label": None, "votes": 0, "neighbors": [],
                "reason": "No variation in labeled examples"}
    features = incident_features(incident["samples"])
    distances = []
    for item, vector in vectors:
        distance = math.sqrt(sum(((a - b) / scale) ** 2 if scale > 1e-9 else 0
                                 for a, b, scale in zip(features, vector, scales)))
        distances.append((distance, item["label"], item["ts"]))
    nearest = sorted(distances)[:3]
    votes = {}
    for _, label, _ in nearest:
        votes[label] = votes.get(label, 0) + 1
    winner, count = sorted(votes.items(), key=lambda item: (-item[1], item[0]))[0]
    return {
        "label": winner if count >= 2 else None,
        "votes": count,
        "neighbors": [(label, ts) for _, label, ts in nearest],
        "reason": None if count >= 2 else "No neighbor majority",
    }

def predict_incident(incident, incidents):
    result = classify_incident(incident, incidents)
    return result["label"] if result else None

def evaluate_classifier(incidents):
    labeled = [item for item in incidents if item["label"] in INCIDENT_LABELS]
    classes = {item["label"] for item in labeled}
    if len(labeled) < INCIDENT_VALIDATION_MIN_N or len(classes) < 2:
        return ("Leave-one-out check available after at least %d labeled incidents "
                "across two or more classes." % INCIDENT_VALIDATION_MIN_N)

    results = [(item["label"], classify_incident(item, labeled)) for item in labeled]
    confident = [(actual, result["label"]) for actual, result in results
                 if result is not None and result["label"] is not None]
    if not confident:
        return "Leave-one-out check: no confident predictions; abstained on all %d examples." % len(labeled)
    correct = sum(actual == predicted for actual, predicted in confident)
    abstained = len(labeled) - len(confident)
    return ("Leave-one-out check: %d/%d correct among %d confident predictions; "
            "%d/%d abstained." % (
                correct, len(confident), len(confident), abstained, len(labeled)))

def incident_page(message=""):
    incidents = get_incidents()
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M")
    rows = []
    for item in incidents:
        classification = classify_incident(item, incidents)
        before_samples, after_samples = incident_sample_parts(item["samples"])
        before = "".join(
            "<li>%s — %.1f / %.1f</li>" % (
                datetime.fromtimestamp(point[0]).strftime("%H:%M"), point[1], point[2])
            for point in before_samples)
        after = "".join(
            "<li>%s — %.1f / %.1f</li>" % (
                datetime.fromtimestamp(point[0]).strftime("%H:%M"), point[1], point[2])
            for point in after_samples)
        options = ['<option value="">Unlabelled</option>']
        options.extend('<option value="%s"%s>%s</option>' % (
            label, " selected" if item["label"] == label else "", label.title())
            for label in INCIDENT_LABELS)
        options.append('<option value="%s"%s>NC — discard from training</option>' % (
            INCIDENT_DISCARDED,
            " selected" if item["label"] == INCIDENT_DISCARDED else ""))
        if item["label"] == INCIDENT_DISCARDED:
            predicted = "Marked NC"
        elif classification and classification["neighbors"]:
            neighbors = ", ".join(
                "%s %s" % (label.title(), datetime.fromtimestamp(ts).strftime("%H:%M"))
                for label, ts in classification["neighbors"])
            if classification["label"]:
                predicted = "%s (%d/3 neighbor votes; nearest: %s)" % (
                    classification["label"].title(), classification["votes"], neighbors)
            else:
                predicted = "Undetermined (%d/3; nearest: %s)" % (
                    classification["votes"], neighbors)
        elif classification:
            predicted = "Undetermined (%s)" % escape(classification["reason"])
        else:
            predicted = "Undetermined"
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td><ol>%s</ol></td><td><ol>%s</ol></td>"
            '<td><form method="post" action="/incidents/label">'
            '<input type="hidden" name="id" value="%d">'
            '<select name="label">%s</select> <button type="submit">Save label</button></form></td>'
            "<td>%s</td></tr>" % (
                datetime.fromtimestamp(item["ts"]).strftime("%Y-%m-%d %H:%M"),
                escape(item["reason"] or "Manual / earlier log"),
                escape(incident_metrics(item["samples"])),
                before, after, item["id"], "".join(options), predicted))
    message_html = '<p class="message">%s</p>' % escape(message) if message else ""
    table_rows = "".join(rows) or '<tr><td colspan="7">No incidents recorded yet.</td></tr>'
    validation = escape(evaluate_classifier(incidents))
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Air incidents</title>
<style>
body{font:16px sans-serif;margin:1.5rem;line-height:1.4}main{max-width:1200px;margin:auto}
form.add{display:flex;gap:.7rem;align-items:end;flex-wrap:wrap;margin:1.5rem 0}
label{display:grid;gap:.3rem}table{border-collapse:collapse;width:100%%;text-align:left}
th,td{border:1px solid #8888;padding:.5rem;vertical-align:top}ol{margin:0;padding-left:1.5rem}
.message{padding:.7rem;background:#b54b3322;border:1px solid #b54b33}
.note{opacity:.8}.scroll{overflow-x:auto}
</style></head><body><main>
<p><a href="/">← Dashboard</a></p><h1>Local air-quality incidents</h1>
<p class="note">Incidents normally store the three PM2.5/PM10 readings before and after their time.
For a very large transient, the detector can use fewer available readings before the spike. The sensor logs
about every 10 minutes, so add an incident manually after the three following readings arrive (about 20 minutes).
Readings come only from this Pi's SDS011. Records and labels are stored locally
in ~/incidents.sqlite3 and are not automatically deleted. Select an incident from the last four hours
of sensor history.</p>
%s
<form class="add" method="post" action="/incidents/add">
<label>Incident date and time (Pi local time)
<input type="datetime-local" name="event_time" value="%s" required></label>
<button type="submit">Save incident readings</button></form>
<p class="note">The local 3-nearest-neighbour classifier needs at least four labelled examples across
at least two classes. It predicts only when at least two neighbours agree; early results are experimental
and are not a fire or safety alarm. Neighbor votes are agreement counts, not calibrated probabilities.
The app scans its saved four-hour sensor history on startup and checks
new readings for a rise in PM2.5 or PM10: a single transient must be at least 30 µg/m³ and 2× baseline;
for this transient, it uses the one to three preceding readings available. Smaller rises must be at least
10 µg/m³ and 1.25× baseline in two of three readings and require all three baseline readings. A sustained
crossing to PM2.5 >=35.5 or PM10 >=155 µg/m³ also records a high-level event when two of three readings
remain high after a lower reading. These short-term thresholds are screening rules, not official AQI
measurements. All detected events are saved, including undetermined ones. Choose a class or mark a false
positive NC to exclude it from classifier training; nearby duplicate detections are ignored.</p>
<p class="note">%s</p>
<div class="scroll"><table><thead><tr><th>Incident time</th><th>Detection reason</th><th>Peak summary</th>
<th>Before<br>(PM2.5 / PM10 µg/m³)</th><th>After<br>(PM2.5 / PM10 µg/m³)</th>
<th>Your classification</th><th>Prediction / nearest examples</th></tr></thead>
<tbody>%s</tbody></table></div></main></body></html>""" % (
        message_html, now, validation, table_rows)

def set_live(t, h):
    with lock:
        live["t"], live["h"] = t, h

def set_live_pm(p25, p10):
    with lock:
        live_pm["p25"], live_pm["p10"], live_pm["ts"] = p25, p10, time.time()

def mean2(vals):
    """[(a, b), ...] -> (sum of a / count, sum of b / count), rounded to 0.1"""
    n = len(vals)
    return round(sum(v[0] for v in vals) / n, 1), round(sum(v[1] for v in vals) / n, 1)

def read_once(dht):
    for _ in range(6):
        try:
            t, h = dht.temperature, dht.humidity
            if t is not None and h is not None:
                return t, h
        except RuntimeError:
            pass            # DHT11 often fails a read, normal
        time.sleep(2)
    return None

def reader():
    dht = None
    while True:
        t0 = time.time()
        vals = []
        try:
            for i in range(DHT_N):                  # DHT_N readings spread evenly over READ_S
                wait = t0 + i * READ_S / DHT_N - time.time()
                if wait > 0:
                    time.sleep(wait)
                if dht is None:
                    dht = adafruit_dht.DHT11(board.D4)
                r = read_once(dht)
                if r:
                    vals.append(r)
                    set_live(*mean2(vals))          # number on the page right after the first reading
        except Exception:
            try:
                dht.exit()
            except Exception:
                pass
            dht = None
        if vals:
            add(*mean2(vals))                       # logged point = sum of the readings / their count
            time.sleep(max(1, READ_S - (time.time() - t0)))
        else:
            time.sleep(30)                          # nothing read: retry in 30 s

# ---------------- SDS011 reader (plug and play) ----------------
def sds_cmd(d1, d2=0, d3=0):
    """19-byte SDS011 command: AA B4 <15 data bytes incl. device id FF FF> <checksum> AB"""
    data = [d1, d2, d3] + [0] * 10 + [0xFF, 0xFF]
    return bytes([0xAA, 0xB4] + data + [sum(data) % 256, 0xAB])

def sds_frame(s):
    """read until one valid data frame (AA C0 ... AB) -> (pm2.5, pm10) in ug/m3, or None"""
    for _ in range(300):
        b = s.read(1)
        if not b:
            return None                       # timeout, nothing coming in
        if b != b"\xaa":
            continue
        f = b + s.read(9)
        if len(f) == 10 and f[1] == 0xC0 and f[9] == 0xAB and sum(f[2:8]) % 256 == f[8]:
            return (f[2] | f[3] << 8) / 10, (f[4] | f[5] << 8) / 10
    return None

def sds_measure(s, warm=PM_WARM_S):
    """wake the sensor, let it warm up, average a few frames, put it back to sleep (saves the laser)"""
    s.write(sds_cmd(0x06, 1, 1))              # work mode: fan + laser on
    time.sleep(1)
    s.write(sds_cmd(0x02, 1, 0))              # active reporting (1 frame/s), in case it was in query mode
    time.sleep(warm)
    s.reset_input_buffer()
    vals = []
    end = time.time() + PM_SAMPLE_S
    while time.time() < end:
        r = sds_frame(s)
        if r:
            vals.append(r)
    try:
        s.write(sds_cmd(0x06, 1, 0))          # sleep mode
    except Exception:
        pass
    if not vals:
        return None
    return (round(sum(v[0] for v in vals) / len(vals), 1),
            round(sum(v[1] for v in vals) / len(vals), 1))

def pm_cycle(s, fresh):
    """PM_N measurements spread evenly over PM_LOG_S, each one warmed up and averaged by sds_measure().
    -> (pm2.5, pm10, count): sum of the valid measurements / their count (4 of 4 normally), or None"""
    t0 = time.time()
    vals, bad = [], 0
    for i in range(PM_N):
        wait = t0 + i * PM_LOG_S / PM_N - time.time()
        if wait > 0:
            time.sleep(wait)
        r = sds_measure(s, PM_WARM_S * 2 if fresh and i == 0 else PM_WARM_S)
        if r:
            vals.append(r)
            set_live_pm(*mean2(vals))               # number on the page right after the first measurement
            sds_state["msg"] = "SDS011 collecting %d/%d" % (len(vals), PM_N)
        else:
            bad += 1
            if not vals and bad >= 2:               # nothing valid twice in a row: sensor gone, stop early
                break
    if len(vals) < PM_MIN_OK:
        return None
    return mean2(vals) + (len(vals),)

def find_port():
    if SDS_PORT:
        return SDS_PORT if os.path.exists(SDS_PORT) else None
    for pat in ("/dev/serial/by-id/*1a86*", "/dev/ttyUSB*", "/dev/ttyACM*"):   # 1a86 = CH340 chip of the SDS011 adapter
        found = sorted(glob.glob(pat))
        if found:
            return found[0]
    return None

def pm_reader():
    if serial is None:
        sds_state["msg"] = "pyserial missing: sudo apt install python3-serial"
        return
    s = None
    fresh = True                              # first reading after (re)connect: warm up twice as long
    while True:
        t0 = time.time()
        ok = False
        try:
            if s is None:
                port = find_port()
                if port is None:
                    sds_state["msg"] = "SDS011 not plugged in (checking every 15 s)"
                else:
                    s = serial.Serial(port, 9600, timeout=2)
                    sds_state["msg"] = "SDS011 found on " + port
                    fresh = True
            if s is not None:
                r = pm_cycle(s, fresh)
                fresh = False
                if r:
                    add_pm(r[0], r[1])
                    ok = True
                    sds_state["msg"] = "SDS011 ok, avg of %d" % r[2]
                else:
                    sds_state["msg"] = "SDS011: port open but too few valid measurements"
        except Exception as e:
            sds_state["msg"] = "SDS011 error: %s" % e
        if not ok and s is not None:
            try:
                s.close()
            except Exception:
                pass
            s = None
        time.sleep(max(1, PM_LOG_S - (time.time() - t0)) if ok else 15)

# ---------------- reference weather ----------------
def fetch_ref():
    req = urllib.request.Request(REF_URL, headers={"User-Agent": "pi-weather-page"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    hr, c = d["hourly"], d["current"]
    cur = [int(c["time"]), float(c["temperature_2m"]), float(c["relative_humidity_2m"])]
    pts = [[int(a), float(b), float(e)]
           for a, b, e in zip(hr["time"], hr["temperature_2m"], hr["relative_humidity_2m"])
           if b is not None and e is not None and int(a) < cur[0]]
    pts.append(cur)
    return pts

def ref_loop():
    while True:
        wait = 60           # retry sooner if it fails
        try:
            pts = fetch_ref()
            with lock:
                ref["pts"] = pts
                ref["t"], ref["h"] = pts[-1][1], pts[-1][2]
                ref["ts"] = time.time()      # when we checked
                ref["dt"] = pts[-1][0]       # time of the value itself (from the API)
            wait = REF_S
        except Exception:
            pass
        time.sleep(wait)

# ---------------- reference pollution ----------------
def aqi_to_ugm3(aqi, bp):
    for ilo, ihi, clo, chi in bp:
        if aqi <= ihi:
            return max(0.0, clo + (aqi - ilo) * (chi - clo) / (ihi - ilo))
    return float(bp[-1][3])

def waqi_token():
    t = os.environ.get("WAQI_TOKEN")
    if not t:
        try:
            with open(WAQI_TOKEN_FILE) as f:
                t = f.read()
        except Exception:
            t = None
    t = t.strip() if t and t.strip() else None
    return t or None

def fetch_pm_official(token):
    """MOEPP station Lisice via aqicn -> one point [ts, pm2.5, pm10] (either may be None)"""
    url = "https://api.waqi.info/feed/@%d/?token=%s" % (WAQI_IDX, token)
    req = urllib.request.Request(url, headers={"User-Agent": "pi-weather-page"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    if d.get("status") != "ok":
        raise RuntimeError("aqicn said: %s" % d.get("data"))
    d = d["data"]
    iaqi, tm = d.get("iaqi", {}), d.get("time", {})
    try:
        ts = datetime.fromisoformat(tm["iso"]).timestamp()    # has the UTC offset, unambiguous
    except Exception:
        ts = float(tm["v"])
    def conc(key, bp):
        try:
            return round(aqi_to_ugm3(float(iaqi[key]["v"]), bp), 1)
        except Exception:
            return None
    p25, p10 = conc("pm25", PM25_BP), conc("pm10", PM10_BP)
    if p25 is None and p10 is None:
        raise RuntimeError("station has no PM values right now")
    return [int(min(ts, time.time())), p25, p10]

def fetch_pm_model():
    """Open-Meteo CAMS air-quality model for LAT/LON -> list of [ts, pm2.5, pm10]"""
    req = urllib.request.Request(OM_AQ_URL, headers={"User-Agent": "pi-weather-page"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    f = lambda v: None if v is None else float(v)
    hr, c = d["hourly"], d["current"]
    cur = [int(c["time"]), f(c["pm2_5"]), f(c["pm10"])]
    pts = [[int(a), f(b), f(e)] for a, b, e in zip(hr["time"], hr["pm2_5"], hr["pm10"])
           if int(a) < cur[0] and (b is not None or e is not None)]
    if cur[1] is not None or cur[2] is not None:
        pts.append(cur)
    if not pts:
        raise RuntimeError("no model data")
    return pts

def ref_pm_loop():
    while True:
        notes, point, pts = [], None, None
        token = waqi_token()
        if token:
            try:
                point = fetch_pm_official(token)
            except Exception as e:
                notes.append("official station failed: %s" % e)
        else:
            notes.append("no WAQI token, official station not used")
        try:                                  # always fetched, it fills whatever the station does not give
            pts = fetch_pm_model()
        except Exception as e:
            notes.append("model failed: %s" % e)
        now = time.time()
        with lock:
            ref_pm["note"] = "; ".join(notes)
            if point:
                b = point[0] // 3600          # clock hour of the station value
                for i, old in enumerate(off_pm):
                    if old[0] // 3600 == b:   # this hour is already stored: rewrite it
                        off_pm[i] = [point[0], point[1] if point[1] is not None else old[1],
                                     point[2] if point[2] is not None else old[2]]
                        break
                else:
                    off_pm.append(point)
                    off_pm.sort(key=lambda p: p[0])
            off_pm[:] = [p for p in off_pm if now - p[0] <= WINDOW_S + 3600]
            if pts is not None:
                mod_pm[:] = [p for p in pts if now - p[0] <= WINDOW_S + 3600]
            if point or pts is not None:
                ref_pm["ts"] = now
            maybe_save_pm(now)
        ok = (point is not None or not token) and pts is not None
        time.sleep(REF_PM_S if ok else 60)

def merged(k):
    """pollutant k (1 = PM2.5, 2 = PM10), handled on its own:
    official station values first, the model only fills the hours the station does not cover
    (that includes the newest hours, until the station publishes them).
    -> [[ts, value, 'o' (official) | 'm' (model)], ...] sorted by time. Call with the lock held."""
    off = [(p[0], p[k]) for p in off_pm if p[k] is not None]
    mod = [(p[0], p[k]) for p in mod_pm if p[k] is not None]
    out = [[t, v, "o"] for t, v in off]
    have = {t // 3600 for t, _ in off}        # clock hours the station already published
    for t, v in mod:
        if t // 3600 in have:
            continue                          # station value for this hour exists: it replaces the model
        out.append([t, v, "m"])
    out.sort(key=lambda p: p[0])
    return out

def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read()) / 1000:.1f} °C"
    except Exception:
        return "N/A"

# ---------------- charts ----------------
def segments(pp, gap):
    segs, cur = [], []
    for p in pp:
        if cur and p[0] - cur[-1][0] > gap:
            segs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        segs.append(cur)
    return segs

def scale(vals, clamp=None):
    lo, hi = min(vals), max(vals)
    pad = max(2.0, (hi - lo) * 0.25)
    lo, hi = math.floor(lo - pad), math.ceil(hi + pad)
    if clamp:
        lo, hi = max(clamp[0], lo), min(clamp[1], hi)
    return lo, hi

def nice_top(v):
    for t in (10, 20, 30, 50, 80, 100, 150, 200, 300, 500, 800, 1000):
        if v * 1.1 <= t:
            return t
    return int(math.ceil(v * 1.1 / 100.0) * 100)

# line styles: sensor solid, official station dashed, model / weather reference dotted
STY = {"solid": 'stroke-width="2.5" stroke-linejoin="round"',
       "dash": 'stroke-width="3" stroke-dasharray="14 7"',
       "dot": 'stroke-width="3.5" stroke-dasharray="0.1 9" stroke-linecap="round"'}

KEYS = ("t", "h", "rt", "rh", "p25", "p10", "rp25", "rp10")    # ids of the 8 lines (click a value to hide it)
INFO_DHT = "Pi sensor DHT11, average of %d readings" % DHT_N
INFO_WX = "Open-Meteo weather model* (estimate, not measured)"
INFO_SDS = "Pi sensor SDS011, average of %d measurements" % PM_N
INFO_OFF = "MOEPP station Lisice (official, measured)"
INFO_MOD = "Open-Meteo CAMS model* (estimate, not measured)"
LAYER_KEYS = ("id", "data", "idx", "axis", "col", "mode", "gap", "hold", "name", "fmt", "info")
LABEL_S = {3600: 600, 7200: 900, 14400: 1800}      # seconds between time labels for the 1 / 2 / 4 h windows
HIDDEN_MSG = '<p style="opacity:.6">All lines are hidden - click a coloured value above to show one.</p>'

def draw(now, win, W, Ht, axl, axr, layers, cid, unit=""):
    """axl/axr = (lo, hi, label colour, label format) or None; win = seconds shown;
    layers = list of dicts (see LAYER_KEYS): data points, index of the value in a point, 'L'/'R' axis,
    colour, mode, max gap s, hold-to-now s, name / format / info for the hover tooltip.
    mode: 'solid' (sensor), 'dot' (weather model), or 'mix' = points are [ts, value, 'o'|'m'], a piece of
    line is dashed when BOTH its ends are official station values, otherwise dotted (model involved).
    Every point is also drawn as a small dot (filled = measured, hollow = model) that the page script
    can hover / tap."""
    L, R, T, B = 56, 56, 14, 34
    pw, ph = W - L - R, Ht - T - B
    x0 = now - win
    X = lambda ts: L + (ts - x0) / win * pw
    Y = {}
    for k, ax in (("L", axl), ("R", axr)):
        if ax:
            Y[k] = (lambda lo, hi: lambda v: T + (hi - v) / (hi - lo) * ph)(ax[0], ax[1])
    lab = LABEL_S.get(win, 1800)

    s = [f'<svg class="chart" viewBox="0 0 {W} {Ht}" style="width:100%;height:auto;display:block;touch-action:pan-y" font-size="14">',
         f'<defs><clipPath id="{cid}"><rect x="{L}" y="{T}" width="{pw}" height="{ph}"/></clipPath></defs>']
    for i in range(6):
        f = i / 5
        y = T + f * ph
        s.append(f'<line x1="{L}" x2="{W-R}" y1="{y:.1f}" y2="{y:.1f}" stroke="currentColor" opacity=".2"/>')
        for ax, x, anchor in ((axl, L - 6, "end"), (axr, W - R + 6, "start")):
            if ax:
                lo, hi, col, fmt = ax
                s.append(f'<text x="{x}" y="{y+5:.1f}" text-anchor="{anchor}" fill="{col}">{fmt % (hi-(hi-lo)*f)}</text>')
    if unit:
        s.append(f'<text x="{W-2}" y="{T+5}" text-anchor="end" fill="currentColor" opacity=".7">{unit}</text>')
    first = math.ceil(x0 / GRID_S) * GRID_S
    for ts in range(int(first), int(now) + 1, GRID_S):
        x = X(ts)
        hour = ts % 3600 == 0
        s.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T}" y2="{T+ph}" stroke="currentColor" opacity="{.35 if hour else .12}"/>')
        if ts % lab == 0:
            s.append(f'<text x="{x:.1f}" y="{Ht-10}" text-anchor="middle" fill="currentColor">{datetime.fromtimestamp(ts):%H:%M}</text>')

    s.append(f'<g clip-path="url(#{cid})">')
    # reference first (underneath), then the sensor on top (solid); each is held flat to now
    for ly in layers:
        idx, col, mode = ly["idx"], ly["col"], ly["mode"]
        data = [p for p in ly["data"] if p[idx] is not None]
        if not data:
            continue
        Yv = Y[ly["axis"]]

        def pl(pp, st, extra=""):
            c = " ".join(f"{X(p[0]):.1f},{Yv(p[idx]):.1f}" for p in pp)
            return f'<polyline points="{c}" fill="none" stroke="{col}" {STY[st]}{extra}/>'

        for sg in segments(data, ly["gap"]):
            if mode == "mix":
                runs = []
                for a, b in zip(sg, sg[1:]):
                    st = "dash" if a[2] == "o" and b[2] == "o" else "dot"
                    if runs and runs[-1][0] == st:
                        runs[-1][1].append(b)
                    else:
                        runs.append([st, [a, b]])
                for st, pp in runs:
                    s.append(pl(pp, st))
            else:
                s.append(pl(sg, mode))
        p = data[-1]
        if 0 < now - p[0] <= ly["hold"]:      # zero-order hold: keep the last value flat up to "now"
            st = ("dash" if p[2] == "o" else "dot") if mode == "mix" else mode
            s.append(pl([p, [now, p[idx], p[idx]]], st, ' stroke-opacity=".55"'))
        for p in data:                         # one hoverable dot per measurement
            if p[0] < x0 or p[0] > now + 60:
                continue
            if mode == "mix":
                measured = p[2] == "o"
                info = INFO_OFF if measured else INFO_MOD
            else:
                measured = mode == "solid"
                info = ly["info"]
            rad = 5.5 if p is data[-1] else 3.5
            fill = col if measured else "none"
            a_n = escape(ly["name"] + ("" if measured else "*"), True)
            a_v = escape(ly["fmt"] % p[idx], True)
            a_x = escape(datetime.fromtimestamp(p[0]).strftime("%H:%M"), True)
            a_i = escape(info, True)
            s.append(f'<circle class="pt" cx="{X(p[0]):.1f}" cy="{Yv(p[idx]):.1f}" r="{rad}" fill="{fill}" '
                     f'stroke="{col}" stroke-width="2" data-n="{a_n}" data-v="{a_v}" data-x="{a_x}" '
                     f'data-i="{a_i}" data-c="{col}"/>')
    s.append("</g>")
    # hover helpers, moved around by the page script
    s.append(f'<line class="gl" x1="0" x2="0" y1="{T}" y2="{T+ph}" stroke="currentColor" stroke-opacity=".45" '
             f'stroke-dasharray="4 4" visibility="hidden" pointer-events="none"/>')
    s.append('<circle class="hl" cx="0" cy="0" r="9" fill="none" stroke-width="3" visibility="hidden" pointer-events="none"/>')
    s.append("</svg>")
    return "".join(s)

def chart(pts, rpts, now, win, hide):
    x0 = now - win
    pts = [p for p in pts if p[0] >= x0]
    rpts = [p for p in rpts if p[0] >= x0 - 3600]   # one point before the window for continuity
    if not (pts or rpts):
        return '<p style="opacity:.6">Collecting data...</p>'
    spec = (("rt", rpts, 1, "L", C_RT, "dot", 7200, REF_S * 3, "ref Temperatura", "%.1f °C", INFO_WX),
            ("rh", rpts, 2, "R", C_RH, "dot", 7200, REF_S * 3, "ref Vlaznost", "%.0f %%", INFO_WX),
            ("t", pts, 1, "L", C_T, "solid", READ_S * 3, READ_S * 3, "Temperatura", "%.1f °C", INFO_DHT),
            ("h", pts, 2, "R", C_H, "solid", READ_S * 3, READ_S * 3, "Vlaznost", "%.1f %%", INFO_DHT))
    layers = [dict(zip(LAYER_KEYS, sp)) for sp in spec if sp[0] not in hide]
    vl = [p[ly["idx"]] for ly in layers if ly["axis"] == "L" for p in ly["data"] if p[ly["idx"]] is not None]
    vr = [p[ly["idx"]] for ly in layers if ly["axis"] == "R" for p in ly["data"] if p[ly["idx"]] is not None]
    if not (vl or vr):
        return HIDDEN_MSG
    axl = (*scale(vl), C_T, "%.0f°") if vl else None
    axr = (*scale(vr, (0, 100)), C_H, "%.0f%%") if vr else None
    return draw(now, win, 1200, 600, axl, axr, layers, "cp")

def chart_pm(pts, r25, r10, now, win, hide):
    x0 = now - win
    pts = [p for p in pts if p[0] >= x0]
    r25 = [p for p in r25 if p[0] >= x0 - 3600]
    r10 = [p for p in r10 if p[0] >= x0 - 3600]
    if not (pts or r25 or r10):
        return '<p style="opacity:.6">Collecting PM data...</p>'
    # PM10 drawn first so PM2.5 stays visible on top
    spec = (("rp10", r10, 1, "L", C_RP10, "mix", 10800, 7200, "ref PM10", "%.1f µg/m³", ""),
            ("rp25", r25, 1, "L", C_RP25, "mix", 10800, 7200, "ref PM2.5", "%.1f µg/m³", ""),
            ("p10", pts, 2, "L", C_P10, "solid", PM_LOG_S * 3, PM_LOG_S * 3, "PM10", "%.1f µg/m³", INFO_SDS),
            ("p25", pts, 1, "L", C_P25, "solid", PM_LOG_S * 3, PM_LOG_S * 3, "PM2.5", "%.1f µg/m³", INFO_SDS))
    layers = [dict(zip(LAYER_KEYS, sp)) for sp in spec if sp[0] not in hide]
    vals = [p[ly["idx"]] for ly in layers for p in ly["data"] if p[ly["idx"]] is not None]
    if not vals:
        return HIDDEN_MSG
    # one shared ug/m3 axis (0..top)
    return draw(now, win, 1200, 450, (0, nice_top(max(vals)), "currentColor", "%.0f"), None, layers, "cp2", "µg/m³")

def stat_txt(pts, idx, now, win, name, unit):
    """min / average / max of one local sensor series inside the window"""
    v = [p[idx] for p in pts if p[0] >= now - win and p[idx] is not None]
    if not v:
        return ""
    return "%s: min %.1f · avg %.1f · max %.1f %s (%d points)" % (name, min(v), sum(v) / len(v), max(v), unit, len(v))

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<noscript><meta http-equiv="refresh" content="30"></noscript><title>Pi</title>
<style>
body{font-family:sans-serif;text-align:center;margin:8px 1vw}
.tg{cursor:pointer;user-select:none;-webkit-user-select:none}
.tg.off{opacity:.4;text-decoration:line-through}
.ctl{font-size:.85rem;margin-bottom:8px;display:flex;justify-content:center;gap:.4rem;flex-wrap:wrap;align-items:center}
.ctl button{font:inherit;padding:2px 10px;border:1px solid currentColor;border-radius:12px;background:none;color:inherit;opacity:.55;cursor:pointer}
.ctl button.on{opacity:1;font-weight:bold}
.hint{opacity:.6}
svg.chart{touch-action:pan-y;-webkit-user-select:none;user-select:none}
#tip{display:none;position:fixed;z-index:10;pointer-events:none;max-width:320px;text-align:left;font-size:.85rem;padding:6px 9px;border-radius:6px;border:1px solid #8888;background:#fff;color:#111;box-shadow:0 2px 8px #0004}
#tip .ti + .ti{margin-top:6px;padding-top:6px;border-top:1px solid #8886}
@media (prefers-color-scheme: dark){#tip{background:#262626;color:#eee}}
@media (max-width:700px){svg.chart text{font-size:24px}}
</style></head>
<body>
<div id="main">
<div class="ctl"><span>Window:</span>
<button data-w="1" class="@B1@">1 h</button><button data-w="2" class="@B2@">2 h</button><button data-w="4" class="@B4@">4 h</button>
<a href="/incidents">Incident log</a>
<span class="hint">click a coloured value to hide / show its line &nbsp;·&nbsp; hover or tap any dot for details &nbsp;·&nbsp; ● measured &nbsp; ○ model*</span></div>
<div style="font-size:1.1rem;display:flex;justify-content:center;gap:.4rem 1.2rem;flex-wrap:wrap;margin-bottom:4px">
<b id="c">--:--:--</b><span>CPU: @CPU@</span>
<span class="tg@G_t@" data-k="t" style="color:#e53935">Temperatura: @T@</span>
<span class="tg@G_h@" data-k="h" style="color:#1e88e5">Vlaznost: @H@</span>
<span class="tg@G_rt@" data-k="rt" style="color:#fb8c00">ref Temperatura: @RT@*</span>
<span class="tg@G_rh@" data-k="rh" style="color:#00bcd4">ref Vlaznost: @RH@*</span></div>
<div style="font-size:.85rem;opacity:.8;margin-bottom:6px">solid = Pi sensor (DHT11, avg of @DHTN@ readings per @READMIN@ min) &nbsp;|&nbsp; dotted = ref, source: <a href="https://open-meteo.com/" style="color:inherit"><b>Open-Meteo.com*</b></a> (api.open-meteo.com), Aerodrom, Skopje (@LL@), value time @REFDT@, checked @REFTIME@, polled every @REFEVERY@ min</div>
@CHART@
<div style="font-size:.8rem;opacity:.8;margin-top:4px">@STATS1@</div>
<div style="font-size:.75rem;opacity:.6;margin-top:6px">* ref data: Open-Meteo.com (CC BY 4.0), best-match national weather service models (DWD, ECMWF, NOAA...) interpolated to the grid point. Model values, not government-measured data and not a ground station.</div>

<div style="font-size:1.1rem;display:flex;justify-content:center;gap:.4rem 1.2rem;flex-wrap:wrap;margin:22px 0 4px">
<span class="tg@G_p25@" data-k="p25" style="color:#9c27b0">PM2.5: @P25@</span>
<span class="tg@G_p10@" data-k="p10" style="color:#43a047">PM10: @P10@</span>
<span class="tg@G_rp25@" data-k="rp25" style="color:#ec407a">ref PM2.5: @RP25@</span>
<span class="tg@G_rp10@" data-k="rp10" style="color:#afb42b">ref PM10: @RP10@</span></div>
<div style="font-size:.85rem;opacity:.8;margin-bottom:6px">solid = Pi sensor (@SDSMSG@) &nbsp;|&nbsp; dashed = official station MOEPP Lisice, last station value @RPDT@ &nbsp;|&nbsp; dotted* = model, fills in where the station has nothing &nbsp;|&nbsp; checked @RPTIME@, polled every @RPEVERY@ min</div>
@CHART2@
<div style="font-size:.8rem;opacity:.8;margin-top:4px">@STATS2@</div>
<div style="font-size:.75rem;opacity:.6;margin-top:6px">@PMNOTE@</div>
</div>
<div id="tip"></div>
<script>
var off=@OFF@-Date.now();
function tick(){var e=document.getElementById('c');if(e)e.textContent=new Date(Date.now()+off).toISOString().substr(11,8)}
tick();setInterval(tick,1000);

var Q=@QJSON@,KEY='pi_graph_state_v1',busy=false,again=false,last=0,cur=null,hideT=null;
var main=document.getElementById('main'),tip=document.getElementById('tip'),cache=new WeakMap();

function saveQ(){try{localStorage.setItem(KEY,JSON.stringify(Q))}catch(e){}}
function url(){return '?w='+Q.w+'&hide='+encodeURIComponent(Q.hide.join(','))}

// reload the graphs in place (keeps the chosen window / hidden lines, no page flash)
function refresh(force){
  if(busy){if(force)again=true;return}
  if(!force&&(document.hidden||cur||Date.now()-last<8000))return;   // do not disturb someone who is looking at a dot
  busy=true;
  fetch(url(),{cache:'no-store'})
    .then(function(r){return r.text()})
    .then(function(t){
      var n=new DOMParser().parseFromString(t,'text/html').getElementById('main');
      if(n){hideTip();main.innerHTML=n.innerHTML;tick()}
    })
    .catch(function(){})
    .then(function(){busy=false;if(again){again=false;refresh(true)}});
}
setInterval(function(){refresh(false)},30000);

// closest dot to the pointer, in screen pixels (sx, sy = screen pixels per svg unit)
function nearest(pts,mx,my,sx,sy,lim){
  var best=null,bd=lim*lim,i,dx,dy,d;
  for(i=0;i<pts.length;i++){dx=pts[i].x*sx-mx;dy=pts[i].y*sy-my;d=dx*dx+dy*dy;if(d<=bd){bd=d;best=pts[i]}}
  return best;
}
// all dots that sit on top of each other with the chosen one
function cluster(pts,b,sx,sy,lim){
  return pts.filter(function(p){var dx=(p.x-b.x)*sx,dy=(p.y-b.y)*sy;return dx*dx+dy*dy<=lim*lim});
}

function hideTip(){
  clearTimeout(hideT);cur=null;tip.style.display='none';
  [].forEach.call(document.querySelectorAll('svg.chart .hl, svg.chart .gl'),function(e){e.setAttribute('visibility','hidden')});
}

function showTip(items,cx,cy){
  tip.textContent='';
  items.forEach(function(it){
    var d=document.createElement('div'),h=document.createElement('b'),t=document.createElement('div');
    d.className='ti';
    h.textContent=it.getAttribute('data-n')+'  '+it.getAttribute('data-v');
    h.style.color=it.getAttribute('data-c');
    t.textContent=it.getAttribute('data-x')+' · '+it.getAttribute('data-i');
    d.appendChild(h);d.appendChild(t);tip.appendChild(d);
  });
  tip.style.display='block';
  var w=tip.offsetWidth,h2=tip.offsetHeight,vw=document.documentElement.clientWidth;
  var l=cx+16,t2=cy-h2-14;
  if(l+w>vw-4)l=cx-w-16;
  if(l<4)l=4;
  if(t2<4)t2=cy+18;
  tip.style.left=l+'px';tip.style.top=t2+'px';
}

function onMove(e){
  var t=e.target,svg=(t&&t.closest)?t.closest('svg.chart'):null;
  if(!svg){if(e.pointerType==='mouse')hideTip();return}
  last=Date.now();
  var r=svg.getBoundingClientRect(),vb=svg.viewBox.baseVal,sx=r.width/vb.width,sy=r.height/vb.height;
  var pts=cache.get(svg);
  if(!pts){
    pts=[].map.call(svg.querySelectorAll('.pt'),function(el){return{el:el,x:+el.getAttribute('cx'),y:+el.getAttribute('cy')}});
    cache.set(svg,pts);
  }
  var b=nearest(pts,e.clientX-r.left,e.clientY-r.top,sx,sy,26);
  if(!b){hideTip();return}
  if(cur&&cur!==svg)hideTip();
  cur=svg;
  var hl=svg.querySelector('.hl'),gl=svg.querySelector('.gl');
  hl.setAttribute('cx',b.x);hl.setAttribute('cy',b.y);hl.setAttribute('stroke',b.el.getAttribute('data-c'));hl.setAttribute('visibility','visible');
  gl.setAttribute('x1',b.x);gl.setAttribute('x2',b.x);gl.setAttribute('visibility','visible');
  showTip(cluster(pts,b,sx,sy,7).map(function(p){return p.el}),e.clientX,e.clientY);
  clearTimeout(hideT);
  if(e.pointerType!=='mouse')hideT=setTimeout(hideTip,6000);     // touch: no "leave" event, so close it by timer
}
main.addEventListener('pointermove',onMove);
main.addEventListener('pointerdown',onMove);
main.addEventListener('pointerleave',function(e){if(e.pointerType==='mouse')hideTip()});
document.addEventListener('pointerdown',function(e){var t=e.target;if(!(t&&t.closest&&t.closest('svg.chart')))hideTip()});

// window buttons and click-to-hide on the coloured values
document.addEventListener('click',function(e){
  var t=e.target,el=(t&&t.closest)?t.closest('[data-w],[data-k]'):null;
  if(!el)return;
  if(el.hasAttribute('data-w')){Q.w=parseInt(el.getAttribute('data-w'),10)}
  else{var k=el.getAttribute('data-k'),i=Q.hide.indexOf(k);if(i>=0)Q.hide.splice(i,1);else Q.hide.push(k);Q.hide.sort()}
  saveQ();refresh(true);
});

// remember the choice: an explicit ?w=..&hide=.. in the address wins, otherwise the saved one
(function(){
  var sp=new URLSearchParams(location.search),want=null;
  if(sp.has('w')||sp.has('hide')){saveQ();return}
  try{want=JSON.parse(localStorage.getItem(KEY)||'null')}catch(e){}
  if(want&&[1,2,4].indexOf(want.w)>=0&&Array.isArray(want.hide)){
    want.hide=want.hide.filter(function(k){return typeof k==='string'}).sort();
    if(want.w!==Q.w||want.hide.join(',')!==Q.hide.join(',')){Q=want;refresh(true)}
  }
})();
</script></body></html>"""

PM_NOTE_OFFICIAL = ("MOEPP = Ministry of Environment and Physical Planning of North Macedonia (air.moepp.gov.mk), "
                    "automatic station Lisice, relayed by the World Air Quality Index project (aqicn.org). "
                    "The feed gives US-EPA AQI, converted here back to µg/m³. Unvalidated near-real-time data, "
                    "updated hourly, the official line builds up while this script runs. ")
PM_NOTE_MODEL = ("* Open-Meteo.com (CC BY 4.0), CAMS European air-quality model (Copernicus) at the grid point: "
                 "model values, not government-measured data. It only fills what the station does not give, "
                 "PM2.5 and PM10 are decided separately. ")

def fmt_ref(m, now):
    """latest merged point -> '12.3 µg/m³'; a model value gets * and the last station value next to it"""
    if m and now - m[-1][0] <= REF_STALE_S:
        t, v, src = m[-1]
        txt = "%.1f µg/m³" % v
        if src == "m":
            txt += "*"
            offs = [p for p in m if p[2] == "o"]
            if offs:
                txt += " (station %.1f at %s)" % (offs[-1][1], datetime.fromtimestamp(offs[-1][0]).strftime("%H:%M"))
        return txt
    return "N/A"

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if u.path == "/incidents":
            body = incident_page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        q = parse_qs(u.query)                 # ?w=1|2|4 (hours shown)  &hide=t,h,p25,... (lines switched off)
        try:
            w = int(q.get("w", ["4"])[0])
        except ValueError:
            w = 4
        if w not in (1, 2, 4):
            w = 4
        hide = {k for k in ",".join(q.get("hide", [])).split(",") if k in KEYS}
        win = w * 3600
        now = time.time()
        with lock:
            pts = list(hist)
            rp = list(ref["pts"])
            t, h = live["t"], live["h"]
            rt, rh, rts, rdt = ref["t"], ref["h"], ref["ts"], ref["dt"]
            ppts = list(hist_pm)
            m25, m10 = merged(1), merged(2)
            pm25, pm10, pmts = live_pm["p25"], live_pm["p10"], live_pm["ts"]
            rpts, rpnote = ref_pm["ts"], ref_pm["note"]
            sdsmsg = sds_state["msg"]
        off = datetime.now().astimezone().utcoffset().total_seconds()
        pm_ok = pmts is not None and now - pmts <= PM_LOG_S * 3 + 120      # unplugged sensor -> numbers disappear
        x0 = now - win
        used = {p[2] for m in (m25, m10) for p in m if p[0] >= x0}      # which sources are on the graph
        offt = [p[0] for m in (m25, m10) for p in m if p[2] == "o"]     # times of the station values

        def stats(parts):
            txt = " &nbsp;|&nbsp; ".join(x for x in parts if x)
            return ("Pi sensor, last %d h: %s" % (w, txt)) if txt else ""
        st1 = stats((stat_txt(pts, 1, now, win, "Temperatura", "°C") if "t" not in hide else "",
                     stat_txt(pts, 2, now, win, "Vlaznost", "%") if "h" not in hide else ""))
        st2 = stats((stat_txt(ppts, 1, now, win, "PM2.5", "µg/m³") if "p25" not in hide else "",
                     stat_txt(ppts, 2, now, win, "PM10", "µg/m³") if "p10" not in hide else ""))

        subs = {
            "@CPU@": cpu_temp(),
            "@T@": f"{t:.1f} °C" if t is not None else "...",
            "@H@": f"{h:.1f} %" if h is not None else "...",
            "@RT@": f"{rt:.1f} °C" if rt is not None else "N/A",
            "@RH@": f"{rh:.0f} %" if rh is not None else "N/A",
            "@REFTIME@": f"{datetime.fromtimestamp(rts):%H:%M}" if rts else "never (no internet?)",
            "@REFDT@": f"{datetime.fromtimestamp(rdt):%H:%M}" if rdt else "N/A",
            "@REFEVERY@": str(REF_S // 60),
            "@DHTN@": str(DHT_N),
            "@READMIN@": str(READ_S // 60),
            "@LL@": f"{LAT}N {LON}E",
            "@CHART@": chart(pts, rp, now, win, hide),
            "@STATS1@": st1,
            "@P25@": f"{pm25:.1f} µg/m³" if pm_ok and pm25 is not None else "no data",
            "@P10@": f"{pm10:.1f} µg/m³" if pm_ok and pm10 is not None else "no data",
            "@RP25@": fmt_ref(m25, now),
            "@RP10@": fmt_ref(m10, now),
            "@SDSMSG@": escape(sdsmsg),
            "@RPTIME@": f"{datetime.fromtimestamp(rpts):%H:%M}" if rpts else "never",
            "@RPDT@": f"{datetime.fromtimestamp(max(offt)):%H:%M}" if offt else "none yet",
            "@RPEVERY@": str(REF_PM_S // 60),
            "@CHART2@": chart_pm(ppts, m25, m10, now, win, hide),
            "@STATS2@": st2,
            "@PMNOTE@": (PM_NOTE_OFFICIAL if "o" in used else "")
                        + (PM_NOTE_MODEL if "m" in used else "")
                        + (("<br>" + escape(rpnote)) if rpnote else ""),
            "@OFF@": str(int((now + off) * 1000)),
            "@QJSON@": json.dumps({"w": w, "hide": sorted(hide)}),
            "@B1@": "on" if w == 1 else "",
            "@B2@": "on" if w == 2 else "",
            "@B4@": "on" if w == 4 else "",
        }
        for k in KEYS:
            subs["@G_%s@" % k] = " off" if k in hide else ""
        html = re.sub(r"@[A-Za-z0-9_]+@", lambda m: subs.get(m.group(0), m.group(0)), PAGE)   # one pass over the page
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/incidents/add", "/incidents/label"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 4096:
                raise ValueError("Invalid form size.")
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            if path == "/incidents/add":
                event_time = datetime.fromisoformat(form.get("event_time", [""])[0])
                event_ts = event_time.timestamp()
                samples = incident_samples(event_ts)
                with sqlite3.connect(INCIDENT_DB) as db:
                    db.execute(
                        "INSERT INTO incidents "
                        "(event_ts, samples, created_ts, detection_reason) VALUES (?, ?, ?, ?)",
                        (event_ts, json.dumps(samples), time.time(), "Manual entry"))
            else:
                incident_id = int(form.get("id", [""])[0])
                label = form.get("label", [""])[0] or None
                if label is not None and label not in INCIDENT_LABELS + (INCIDENT_DISCARDED,):
                    raise ValueError("Choose one of the listed classes or NC.")
                with sqlite3.connect(INCIDENT_DB) as db:
                    result = db.execute(
                        "UPDATE incidents SET label = ? WHERE id = ?", (label, incident_id))
                    if result.rowcount == 0:
                        self.send_error(404, "Incident not found.")
                        return
        except (ValueError, UnicodeDecodeError) as exc:
            self.send_error(400, str(exc))
            return
        self.send_response(303)
        self.send_header("Location", "/incidents")
        self.end_headers()

    def log_message(self, *a): pass

if __name__ == "__main__":
    load()
    init_incidents()
    detect_logged_incidents()
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=ref_loop, daemon=True).start()
    threading.Thread(target=pm_reader, daemon=True).start()
    threading.Thread(target=ref_pm_loop, daemon=True).start()
    mirror = ThreadingHTTPServer(("0.0.0.0", 8081), H)     # LAN mirror, no VPN needed
    threading.Thread(target=mirror.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
EOF
