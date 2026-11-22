#!/usr/bin/env python3
"""Webansicht fuer den PCE-323-Logger (nur Python-Standardbibliothek).

Zeigt aktuellen Pegel, Verlauf (Leq mit Min/Max-Band), Tageskennwerte und
CSV-Download. Liest die vom Logger (pce323.py) geschriebene Statusdatei und
die CSV-Tagesdateien. Greift NICHT auf den seriellen Port zu.

    pce323_web.py --csv 'data/pegel_%Y-%m-%d.csv' --status /run/pce323/latest.json --port 8080

Es gibt keine Anmeldung: nur im vertrauenswuerdigen Heimnetz betreiben.
"""
import argparse
import bisect
from array import array
from collections import OrderedDict
import datetime
import json
import math
import os
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.csv$")
MAX_MINUTES = 48 * 60
RESTART_COOLDOWN = 20          # Sekunden zwischen zwei Neustarts
CACHED_DAYS = 4                # so viele Tagesdateien bleiben eingelesen (zuletzt benutzte)
LAST_RESTART = [0.0]


class DayFile:
    """Eine CSV-Tagesdatei, inkrementell eingelesen (nur neue Bytes bei jedem Zugriff)."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        # kompakte Arrays statt Listen: ca. 3,5 statt 12 MB pro Tag im Speicher
        self.ts, self.leq, self.mn, self.mx, self.n = array("d"), array("d"), array("d"), array("d"), array("i")
        self.partial = b""

    def refresh(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:                       # Datei wurde ersetzt/gekuerzt -> neu lesen
            self.__init__(self.path)
        if size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        self.offset += len(chunk)
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()                   # unvollstaendige letzte Zeile aufheben
        for raw in lines:
            row = raw.decode("utf-8", "replace").strip().split(",")
            if len(row) < 5 or row[0] == "zeit_iso":
                continue
            try:
                t = datetime.datetime.fromisoformat(row[0]).timestamp()
                leq, mn, mx, n = float(row[1]), float(row[2]), float(row[3]), int(row[4])
            except ValueError:
                continue
            if self.ts and t < self.ts[-1]:          # Uhr zurueckgesprungen: Zeile ueberspringen
                continue
            self.ts.append(t)
            self.leq.append(leq)
            self.mn.append(mn)
            self.mx.append(mx)
            self.n.append(n)


class Store:
    def __init__(self, pattern):
        self.pattern = pattern
        self.files = OrderedDict()
        self.lock = threading.Lock()
        self.cache = {}
        self.today_cache = (0.0, None)

    def _day(self, d):
        path = d.strftime(self.pattern)
        f = self.files.get(path)
        if f is None:
            f = self.files[path] = DayFile(path)
        self.files.move_to_end(path)
        while len(self.files) > CACHED_DAYS:         # Speicher begrenzen: am laengsten nicht benutzte Tage raus
            self.files.popitem(last=False)
        f.refresh()
        return f

    def window(self, t0, t1):
        """Alle Zeilen mit t0 <= ts <= t1 (ueber bis zu drei Tagesdateien)."""
        out = []
        day = datetime.date.fromtimestamp(t0)
        last = datetime.date.fromtimestamp(t1)
        while day <= last:
            f = self._day(day)
            i = bisect.bisect_left(f.ts, t0)
            j = bisect.bisect_right(f.ts, t1)
            if j > i:
                out.append((f, i, j))
            day += datetime.timedelta(days=1)
        return out

    def history(self, minutes, points, t0=None, t1=None):
        """Verlauf der letzten `minutes` Minuten, oder mit t0/t1 eines festen Ausschnitts (Zoom)."""
        now = time.time()
        if t0 is None:
            key = (minutes, points)
            t0, t1 = now - minutes * 60, now
        else:
            t1 = min(t1, now)
            key = (round(t0, 1), round(t1, 1), points)
        with self.lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < 4:
                return hit[1]
            parts = self.window(t0, t1)
            total = sum(j - i for _, i, j in parts)
            width = max((t1 - t0) / points, 1.0)
            buckets = {}
            e_sum = n_sum = 0.0
            lo, hi = float("inf"), float("-inf")
            for f, i, j in parts:
                for k in range(i, j):
                    b = int(f.ts[k] // width)
                    w = f.n[k] or 1
                    e = w * 10 ** (f.leq[k] / 10)
                    cur = buckets.get(b)
                    if cur is None:
                        buckets[b] = [e, w, f.mn[k], f.mx[k]]
                    else:
                        cur[0] += e
                        cur[1] += w
                        cur[2] = min(cur[2], f.mn[k])
                        cur[3] = max(cur[3], f.mx[k])
            series = []
            for b in sorted(buckets):
                e, w, mn, mx = buckets[b]
                series.append([round((b + 0.5) * width, 1), round(10 * math.log10(e / w), 1), mn, mx])
                e_sum += e
                n_sum += w
                lo, hi = min(lo, mn), max(hi, mx)
            res = {
                "from": t0, "to": t1, "now": now, "bucket": width, "rows": total, "series": series,
                "stats": ({"leq": round(10 * math.log10(e_sum / n_sum), 1), "min": lo, "max": hi,
                           "samples": int(n_sum)} if n_sum else None),
            }
            self.cache[key] = (now, res)
            if len(self.cache) > 20:
                self.cache.clear()
            return res

    def today_stats(self):
        """Kennwerte seit 00:00 Uhr (15 s gecacht, der ganze Tag ist teuer zu summieren)."""
        now = time.time()
        if now - self.today_cache[0] < 15:
            return self.today_cache[1]
        midnight = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
        stats = self.history((now - midnight) / 60.0 + 0.01, 1)["stats"]
        self.today_cache = (now, stats)
        return stats

    def list_files(self, directory):
        try:
            names = [n for n in os.listdir(directory) if SAFE_NAME.match(n)]
        except OSError:
            return []
        res = []
        for n in sorted(names, reverse=True):
            try:
                st = os.stat(os.path.join(directory, n))
            except OSError:
                continue
            res.append({"name": n, "bytes": st.st_size, "modified": st.st_mtime})
        return res


def restart_logger(status_path):
    """Schickt dem Logger SIGTERM. systemd (Restart=always) startet ihn danach neu.

    Rueckgabe (HTTP-Code, Meldung). Der Prozess wird nur beendet, wenn die PID aus der
    Statusdatei wirklich zu pce323.py gehoert und der Logger von systemd ueberwacht wird.
    """
    try:
        with open(status_path) as f:
            st = json.load(f)
        pid = int(st["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return 409, "Kein laufender Logger gefunden (keine Statusdatei). systemd startet ihn ggf. selbst."
    if not st.get("supervised"):
        return 409, "Logger laeuft nicht unter systemd, ein Neustart wuerde ihn nur beenden."
    now = time.time()
    if now - LAST_RESTART[0] < RESTART_COOLDOWN:
        return 429, "Gerade erst neu gestartet, bitte kurz warten."
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmd = f.read().split(b"\0")
    except OSError:
        return 409, "Logger-Prozess laeuft nicht. systemd startet ihn automatisch neu."
    if not any(a.endswith(b"pce323.py") for a in cmd):
        return 409, "PID aus der Statusdatei gehoert nicht zum Logger."
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return 500, "Konnte den Logger nicht beenden: %s" % e
    LAST_RESTART[0] = now
    return 200, "Logger wird neu gestartet."


def make_handler(store, status_path, data_dir, index_path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pce323-web"

        def log_message(self, *a):               # keine Zugriffslogs im Journal
            pass

        def _send(self, code, body, ctype, extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    with open(index_path, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                elif u.path == "/api/latest":
                    self._json(self._latest())
                elif u.path == "/api/history":
                    points = min(max(int(q.get("points", ["600"])[0]), 10), 2000)
                    if "from" in q:                   # fester Ausschnitt (Zoom), Unix-Sekunden
                        t0 = float(q["from"][0])
                        t1 = min(float(q.get("to", [str(time.time())])[0]), time.time())
                        if not (math.isfinite(t0) and math.isfinite(t1)) or t0 < 0 or t1 - t0 < 1:
                            raise ValueError("ungueltiger Ausschnitt")
                        t0 = max(t0, t1 - MAX_MINUTES * 60)
                        self._json(store.history(None, points, t0, t1))
                    else:
                        minutes = min(max(float(q.get("minutes", ["60"])[0]), 1), MAX_MINUTES)
                        self._json(store.history(minutes, points))
                elif u.path == "/api/files":
                    self._json(store.list_files(data_dir))
                elif u.path.startswith("/files/"):
                    self._file(u.path[len("/files/"):])
                else:
                    self._send(404, "nicht gefunden", "text/plain; charset=utf-8")
            except (ValueError, KeyError):
                self._send(400, "ungueltige Anfrage", "text/plain; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:                # Server soll nie wegen einer Anfrage sterben
                self._send(500, f"Fehler: {e}", "text/plain; charset=utf-8")

        def do_POST(self):
            u = urlparse(self.path)
            try:
                if u.path != "/api/restart-logger":
                    return self._send(404, "nicht gefunden", "text/plain; charset=utf-8")
                # Eigener Header erzwingt bei fremden Webseiten einen CORS-Preflight, den wir nicht erlauben
                if self.headers.get("X-Requested-With") != "pce323":
                    return self._json({"ok": False, "error": "Header fehlt"}, 403)
                code, msg = restart_logger(status_path)
                self._json({"ok": code == 200, "message": msg}, code)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)

        def _latest(self):
            try:
                with open(status_path) as f:
                    st = json.load(f)
            except (OSError, ValueError):
                st = {"state": "unknown"}
            st["now"] = time.time()
            st["today"] = store.today_stats()
            return st

        def _file(self, name):
            if not SAFE_NAME.match(name):
                return self._send(404, "nicht gefunden", "text/plain; charset=utf-8")
            path = os.path.join(data_dir, name)
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._send(404, "nicht gefunden", "text/plain; charset=utf-8")
            self._send(200, body, "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{name}"'})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=os.path.join(BASE, "data", "pegel_%Y-%m-%d.csv"),
                    help="CSV-Muster wie beim Logger")
    ap.add_argument("--status", default="/run/pce323/latest.json", help="Statusdatei des Loggers")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    csv_pattern = os.path.abspath(args.csv)
    data_dir = os.path.dirname(csv_pattern)
    store = Store(csv_pattern)
    handler = make_handler(store, args.status, data_dir, os.path.join(BASE, "web", "index.html"))
    srv = ThreadingHTTPServer((args.host, args.port), handler)
    srv.daemon_threads = True
    print(f"Webansicht auf http://{args.host}:{args.port}/  (CSV: {csv_pattern})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
