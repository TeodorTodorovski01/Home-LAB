cat > ~/clock.py <<'EOF'
import glob, json, math, os, threading, time, urllib.request
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import board, adafruit_dht
try:
    import serial          # pyserial, needed for the SDS011 (sudo apt install python3-serial)
except ImportError:
    serial = None

READ_S = 600           # sensor read + log every 10 min (same as reference)
WINDOW_S = 4 * 3600    # graph window (4 hours)
GRID_S = 300           # grid every 5 min
SAVE_S = 300           # save to disk every 5 min (saves the SD card)
FILE = os.path.expanduser("~/history1m.json")

# Reference weather: Open-Meteo (national weather service models), Aerodrom, Skopje
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
PM_S = 300             # PM read + log every 5 min
PM_WARM_S = 30         # fan + laser warm-up before reading
PM_SAMPLE_S = 10       # then average ~10 frames (1 frame/s)
SDS_PORT = os.environ.get("SDS_PORT")        # optional: force a port, e.g. /dev/ttyUSB0
FILE_PM = os.path.expanduser("~/history_pm.json")

# Reference pollution: OFFICIAL station "Lisice" of the Ministry of Environment (MOEPP) of North
# Macedonia, 41deg58'42"N 21deg28'12"E = the same spot as LAT/LON above. Fetched through the World
# Air Quality Index project (aqicn.org), which relays the MOEPP feed (free token, see WAQI_TOKEN_FILE).
# No token / feed down -> automatic fallback to the Open-Meteo CAMS model (not a station!).
WAQI_IDX = 8106
WAQI_TOKEN_FILE = os.path.expanduser("~/.waqi_token")
REF_PM_S = 600
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
hist_pm = []           # [ts, pm2.5, pm10]
ref_pm = {"pts": [], "p25": None, "p10": None, "ts": None, "dt": None, "src": None, "note": ""}
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
        ref_pm["pts"] = [p for p in d.get("ref", []) if now - p[0] <= WINDOW_S + 3600]
        ref_pm["src"] = d.get("src")
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
        json.dump({"pm": hist_pm, "ref": ref_pm["pts"], "src": ref_pm["src"]}, f)
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
        ok = False
        try:
            if dht is None:
                dht = adafruit_dht.DHT11(board.D4)
            r = read_once(dht)
            if r:
                add(*r)
                ok = True
        except Exception:
            try:
                dht.exit()
            except Exception:
                pass
            dht = None
        time.sleep(max(1, READ_S - (time.time() - t0)) if ok else 30)   # failed read: retry in 30 s

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

def sds_measure(s):
    """wake the sensor, let it warm up, average a few frames, put it back to sleep (saves the laser)"""
    s.write(sds_cmd(0x06, 1, 1))              # work mode: fan + laser on
    time.sleep(1)
    s.write(sds_cmd(0x02, 1, 0))              # active reporting (1 frame/s), in case it was in query mode
    time.sleep(PM_WARM_S)
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
            if s is not None:
                r = sds_measure(s)
                if r:
                    add_pm(*r)
                    ok = True
                    sds_state["msg"] = "SDS011 ok"
                else:
                    sds_state["msg"] = "SDS011: port open but no valid data"
        except Exception as e:
            sds_state["msg"] = "SDS011 error: %s" % e
        if not ok and s is not None:
            try:
                s.close()
            except Exception:
                pass
            s = None
        time.sleep(max(1, PM_S - (time.time() - t0)) if ok else 15)

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
    return t.strip() if t and t.strip() else None

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

def latest(pts, i):
    for p in reversed(pts):
        if p[i] is not None:
            return p[i]
    return None

def ref_pm_loop():
    while True:
        wait = 60
        token = waqi_token()
        src, official, point, pts, note = None, False, None, None, ""
        if token:
            try:
                point = fetch_pm_official(token)
                src, official = "MOEPP station Lisice (official)", True
            except Exception as e:
                note = "official feed failed (%s), showing model instead" % e
        else:
            note = "no token in ~/.waqi_token, showing model instead of the official station"
        if src is None:
            try:
                pts = fetch_pm_model()
                src = "Open-Meteo CAMS model"
            except Exception as e:
                note = (note + "; " if note else "") + "model failed: %s" % e
        now = time.time()
        with lock:
            ref_pm["note"] = note
            if src:
                if src != ref_pm["src"]:                 # source changed: never mix the two
                    ref_pm["pts"] = []
                    ref_pm["src"] = src
                if official:
                    if not ref_pm["pts"] or ref_pm["pts"][-1][0] != point[0]:   # station updates hourly
                        ref_pm["pts"].append(point)
                else:
                    ref_pm["pts"] = pts
                ref_pm["pts"] = [p for p in ref_pm["pts"] if now - p[0] <= WINDOW_S + 3600]
                if ref_pm["pts"]:
                    ref_pm["p25"], ref_pm["p10"] = latest(ref_pm["pts"], 1), latest(ref_pm["pts"], 2)
                    ref_pm["dt"] = ref_pm["pts"][-1][0]
                ref_pm["ts"] = now
                maybe_save_pm(now)
                wait = REF_PM_S
        time.sleep(wait)

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

def draw(now, W, Ht, axl, axr, layers, cid, unit=""):
    """axl/axr = (lo, hi, label colour, label format) or None;
    layers = (data, point index, 'L'/'R' axis, colour, dotted, max gap s, hold-to-now s)"""
    L, R, T, B = 56, 56, 14, 34
    pw, ph = W - L - R, Ht - T - B
    x0 = now - WINDOW_S
    X = lambda ts: L + (ts - x0) / WINDOW_S * pw
    Y = {}
    for k, ax in (("L", axl), ("R", axr)):
        if ax:
            Y[k] = (lambda lo, hi: lambda v: T + (hi - v) / (hi - lo) * ph)(ax[0], ax[1])

    s = [f'<svg viewBox="0 0 {W} {Ht}" style="width:100%;height:auto;display:block" font-size="14">',
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
        s.append(f'<text x="{W-R+6}" y="{T+5}" fill="currentColor" opacity=".7">{unit}</text>')
    first = math.ceil(x0 / GRID_S) * GRID_S
    for ts in range(int(first), int(now) + 1, GRID_S):
        x = X(ts)
        hour = ts % 3600 == 0
        s.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T}" y2="{T+ph}" stroke="currentColor" opacity="{.35 if hour else .12}"/>')
        if ts % 1800 == 0:
            s.append(f'<text x="{x:.1f}" y="{Ht-10}" text-anchor="middle" fill="currentColor">{datetime.fromtimestamp(ts):%H:%M}</text>')

    s.append(f'<g clip-path="url(#{cid})">')
    # reference first (dotted, underneath), then the sensor on top (solid); each is held flat to now
    for data, idx, axis, col, dotted, gap, hold in layers:
        data = [p for p in data if p[idx] is not None]
        if not data:
            continue
        Yv = Y[axis]
        style = ('stroke-width="3.5" stroke-dasharray="0.1 9" stroke-linecap="round"'
                 if dotted else 'stroke-width="2.5" stroke-linejoin="round"')
        for sg in segments(data, gap):
            c = " ".join(f"{X(p[0]):.1f},{Yv(p[idx]):.1f}" for p in sg)
            s.append(f'<polyline points="{c}" fill="none" stroke="{col}" {style}/>')
        p = data[-1]
        if 0 < now - p[0] <= hold:      # zero-order hold: keep the last value flat up to "now"
            s.append(f'<polyline points="{X(p[0]):.1f},{Yv(p[idx]):.1f} {X(now):.1f},{Yv(p[idx]):.1f}" '
                     f'fill="none" stroke="{col}" stroke-opacity=".55" {style}/>')
        if dotted:
            s.append(f'<circle cx="{X(p[0]):.1f}" cy="{Yv(p[idx]):.1f}" r="5" fill="none" stroke="{col}" stroke-width="2.5"/>')
        else:
            s.append(f'<circle cx="{X(p[0]):.1f}" cy="{Yv(p[idx]):.1f}" r="4.5" fill="{col}"/>')
    s.append("</g></svg>")
    return "".join(s)

def chart(pts, rpts, now):
    x0 = now - WINDOW_S
    pts = [p for p in pts if p[0] >= x0]
    rpts = [p for p in rpts if p[0] >= x0 - 3600]   # one point before the window for continuity
    allp = pts + rpts
    if not allp:
        return '<p style="opacity:.6">Collecting data...</p>'
    tl, th = scale([p[1] for p in allp])
    hl, hh = scale([p[2] for p in allp], (0, 100))
    layers = ((rpts, 1, "L", C_RT, True, 7200, REF_S * 3), (rpts, 2, "R", C_RH, True, 7200, REF_S * 3),
              (pts, 1, "L", C_T, False, READ_S * 3, READ_S * 3), (pts, 2, "R", C_H, False, READ_S * 3, READ_S * 3))
    return draw(now, 1200, 600, (tl, th, C_T, "%.0f°"), (hl, hh, C_H, "%.0f%%"), layers, "cp")

def chart_pm(pts, rpts, now):
    x0 = now - WINDOW_S
    pts = [p for p in pts if p[0] >= x0]
    rpts = [p for p in rpts if p[0] >= x0 - 3600]
    vals = [v for p in pts + rpts for v in (p[1], p[2]) if v is not None]
    if not vals:
        return '<p style="opacity:.6">Collecting PM data...</p>'
    top = nice_top(max(vals))
    # one shared ug/m3 axis (0..top); PM10 drawn first so PM2.5 stays visible on top
    layers = ((rpts, 2, "L", C_RP10, True, 10800, 7200), (rpts, 1, "L", C_RP25, True, 10800, 7200),
              (pts, 2, "L", C_P10, False, PM_S * 3, PM_S * 3), (pts, 1, "L", C_P25, False, PM_S * 3, PM_S * 3))
    return draw(now, 1200, 450, (0, top, "currentColor", "%.0f"), None, layers, "cp2", "µg/m³")

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="refresh" content="30"><title>Pi</title></head>
<body style="font-family:sans-serif;text-align:center;margin:8px 1vw">
<div style="font-size:1.1rem;display:flex;justify-content:center;gap:.4rem 1.2rem;flex-wrap:wrap;margin-bottom:4px">
<b id="c">--:--:--</b><span>CPU: @CPU@</span>
<span style="color:#e53935">Temperatura: @T@</span>
<span style="color:#1e88e5">Vlaznost: @H@</span>
<span style="color:#fb8c00">ref Temperatura: @RT@</span>
<span style="color:#00bcd4">ref Vlaznost: @RH@</span></div>
<div style="font-size:.85rem;opacity:.8;margin-bottom:6px">solid = Pi sensor (DHT11) &nbsp;|&nbsp; dotted = ref, source: <a href="https://open-meteo.com/" style="color:inherit"><b>Open-Meteo.com</b></a> (api.open-meteo.com), Aerodrom, Skopje (@LL@), value time @REFDT@, checked @REFTIME@, polled every @REFEVERY@ min</div>
@CHART@
<div style="font-size:.75rem;opacity:.6;margin-top:6px">ref data: Open-Meteo.com (CC BY 4.0), best-match national weather service models (DWD, ECMWF, NOAA...) interpolated to the grid point. Model values, not a ground station.</div>

<div style="font-size:1.1rem;display:flex;justify-content:center;gap:.4rem 1.2rem;flex-wrap:wrap;margin:22px 0 4px">
<span style="color:#9c27b0">PM2.5: @P25@</span>
<span style="color:#43a047">PM10: @P10@</span>
<span style="color:#ec407a">ref PM2.5: @RP25@</span>
<span style="color:#afb42b">ref PM10: @RP10@</span></div>
<div style="font-size:.85rem;opacity:.8;margin-bottom:6px">solid = Pi sensor (SDS011, @SDSMSG@) &nbsp;|&nbsp; dotted = ref, source: <b>@PMSRC@</b>, Lisice, Skopje, value time @RPDT@, checked @RPTIME@, polled every @RPEVERY@ min</div>
@CHART2@
<div style="font-size:.75rem;opacity:.6;margin-top:6px">@PMNOTE@</div>
<script>
var off=@OFF@-Date.now();
function tick(){document.getElementById('c').textContent=new Date(Date.now()+off).toISOString().substr(11,8)}
tick();setInterval(tick,1000);
</script></body></html>"""

PM_NOTE_OFFICIAL = ("ref PM: Ministry of Environment and Physical Planning of North Macedonia (air.moepp.gov.mk), "
                    "automatic station Lisice, relayed by the World Air Quality Index project (aqicn.org). "
                    "The feed gives US-EPA AQI, converted here back to µg/m³. Unvalidated near-real-time data, "
                    "updated hourly, the line builds up while this script runs. ")
PM_NOTE_MODEL = ("ref PM: Open-Meteo.com (CC BY 4.0), CAMS European air-quality model (Copernicus) at the grid point. "
                 "Model values, not a ground station. ")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        now = time.time()
        with lock:
            pts = list(hist)
            rp = list(ref["pts"])
            t, h = live["t"], live["h"]
            rt, rh, rts, rdt = ref["t"], ref["h"], ref["ts"], ref["dt"]
            ppts = list(hist_pm)
            rpp = list(ref_pm["pts"])
            pm25, pm10, pmts = live_pm["p25"], live_pm["p10"], live_pm["ts"]
            r25, r10, rpts, rpdt = ref_pm["p25"], ref_pm["p10"], ref_pm["ts"], ref_pm["dt"]
            rpsrc, rpnote = ref_pm["src"], ref_pm["note"]
            sdsmsg = sds_state["msg"]
        off = datetime.now().astimezone().utcoffset().total_seconds()
        pm_ok = pmts is not None and now - pmts <= PM_S * 3 + 120      # unplugged sensor -> numbers disappear
        official = rpsrc is not None and "official" in rpsrc
        subs = {
            "@CPU@": cpu_temp(),
            "@T@": f"{t} °C" if t is not None else "...",
            "@H@": f"{h} %" if h is not None else "...",
            "@RT@": f"{rt:.1f} °C" if rt is not None else "N/A",
            "@RH@": f"{rh:.0f} %" if rh is not None else "N/A",
            "@REFTIME@": f"{datetime.fromtimestamp(rts):%H:%M}" if rts else "never (no internet?)",
            "@REFDT@": f"{datetime.fromtimestamp(rdt):%H:%M}" if rdt else "N/A",
            "@REFEVERY@": str(REF_S // 60),
            "@LL@": f"{LAT}N {LON}E",
            "@CHART@": chart(pts, rp, now),
            "@P25@": f"{pm25:.1f} µg/m³" if pm_ok and pm25 is not None else "no data",
            "@P10@": f"{pm10:.1f} µg/m³" if pm_ok and pm10 is not None else "no data",
            "@RP25@": f"{r25:.1f} µg/m³" if r25 is not None else "N/A",
            "@RP10@": f"{r10:.1f} µg/m³" if r10 is not None else "N/A",
            "@SDSMSG@": escape(sdsmsg),
            "@PMSRC@": escape(rpsrc) if rpsrc else "N/A (no internet?)",
            "@RPTIME@": f"{datetime.fromtimestamp(rpts):%H:%M}" if rpts else "never",
            "@RPDT@": f"{datetime.fromtimestamp(rpdt):%H:%M}" if rpdt else "N/A",
            "@RPEVERY@": str(REF_PM_S // 60),
            "@CHART2@": chart_pm(ppts, rpp, now),
            "@PMNOTE@": (PM_NOTE_OFFICIAL if official else PM_NOTE_MODEL if rpsrc else "")
                        + (("<br>" + escape(rpnote)) if rpnote else ""),
            "@OFF@": str(int((now + off) * 1000)),
        }
        html = PAGE
        for k, v in subs.items():
            html = html.replace(k, v)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

if __name__ == "__main__":
    load()
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=ref_loop, daemon=True).start()
    threading.Thread(target=pm_reader, daemon=True).start()
    threading.Thread(target=ref_pm_loop, daemon=True).start()
    mirror = ThreadingHTTPServer(("0.0.0.0", 8081), H)     # LAN mirror, no VPN needed
    threading.Thread(target=mirror.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
EOF
