#!/usr/bin/env python3
"""Live-Leser und Logger fuer das PCE-323 (CEM-DT-8852-kompatibel) ueber den CP2102-USB-Adapter.

Protokoll: 9600 8N1, das Geraet streamt fortlaufend Pakete: 0xA5, Token, Daten.
Voraussetzung am Geraet: eingeschaltet und Auto-Power-off deaktiviert (SETUP-Taste,
Uhr-Symbol im Display muss weg sein).

Pro Intervall (Standard 1 s) werden Leq (energetischer Mittelwert), Minimum und Maximum
der ca. 14 Messwerte/s berechnet. Bei USB-Abbruch oder Datenstillstand verbindet sich das
Programm selbststaendig neu. Zeitluecken erkennt man in der CSV an fehlenden Zeitstempeln.

Beispiele:
    pce323.py                                  # 1-s-Werte anzeigen
    pce323.py --csv messung.csv                # zusaetzlich in CSV (wird angehaengt)
    pce323.py --csv 'pegel_%Y-%m-%d.csv'       # eine Datei pro Tag
    pce323.py --interval 60 --csv tag.csv      # Minutenwerte (Leq/Min/Max)
    pce323.py --interval 0                     # jeden Rohwert einzeln (ca. 14/s)
    pce323.py --status /run/pce323/latest.json # Statusdatei fuer die Webansicht
"""
import argparse
import csv
import json
import math
import os
import signal
import sys
import time

import serial

# Token -> Anzahl Datenbytes (Quelle: sigrok-Wiki zum CEM DT-8852, am PCE-323 bestaetigt)
TOKEN_LEN = {
    0x02: 0, 0x03: 0, 0x04: 0, 0x05: 0, 0x06: 3, 0x07: 0, 0x08: 0, 0x09: 0,
    0x0A: 0, 0x0B: 1, 0x0C: 0, 0x0D: 2, 0x0E: 0, 0x0F: 0, 0x11: 0, 0x19: 0,
    0x1A: 0, 0x1B: 1, 0x1C: 1, 0x1F: 0, 0x30: 0, 0x40: 0, 0x4B: 0, 0x4C: 0,
}
RANGES = {0x30: "30-80 dB", 0x40: "30-130 dB", 0x4B: "50-100 dB", 0x4C: "80-130 dB"}
CSV_HEADER = ["zeit_iso", "leq_db", "min_db", "max_db", "n", "gewichtung",
              "zeitbewertung", "bereich", "modus"]


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def bcd(b):
    """Zwei BCD-Ziffern in einem Byte -> int, None bei ungueltigem BCD."""
    hi, lo = b >> 4, b & 0x0F
    return hi * 10 + lo if hi < 10 and lo < 10 else None


class Parser:
    """Zerlegt den Bytestrom in Pakete. Synchronisiert sich auf 0xA5 neu."""

    def __init__(self):
        self.buf = bytearray()
        self.state = {"weighting": "?", "speed": "?", "range": "?", "mode": "live"}

    def reset(self):
        self.buf.clear()

    def feed(self, data):
        """Gibt eine Liste von dB-Messwerten (float) zurueck, die in `data` fertig wurden."""
        self.buf += data
        values = []
        while True:
            i = self.buf.find(0xA5)
            if i < 0:
                self.buf.clear()
                break
            del self.buf[:i]
            if len(self.buf) < 2:
                break
            tok = self.buf[1]
            n = TOKEN_LEN.get(tok)
            if n is None:                    # unbekannt -> Startbyte verwerfen, neu syncen
                del self.buf[0]
                continue
            if len(self.buf) < 2 + n:
                break                        # Paket unvollstaendig, auf mehr Daten warten
            d = bytes(self.buf[2:2 + n])
            del self.buf[:2 + n]
            self._apply(tok, d, values)
        return values

    def _apply(self, tok, d, values):
        st = self.state
        if tok == 0x0D:
            hi, lo = bcd(d[0]), bcd(d[1])
            if hi is not None and lo is not None:
                values.append((hi * 100 + lo) / 10.0)
        elif tok == 0x1B:
            st["weighting"] = "A"
        elif tok == 0x1C:
            st["weighting"] = "C"
        elif tok == 0x02:
            st["speed"] = "Fast"
        elif tok == 0x03:
            st["speed"] = "Slow"
        elif tok in RANGES:
            st["range"] = RANGES[tok]
        elif tok == 0x04:
            st["mode"] = "max-hold"
        elif tok == 0x05:
            st["mode"] = "min-hold"
        elif tok == 0x0E:
            st["mode"] = "live"


class Bin:
    """Sammelt Messwerte eines Zeitintervalls. Leq = 10*log10(mittel(10^(L/10)))."""

    def __init__(self, start):
        self.start = start
        self.n = 0
        self.energy = 0.0
        self.mn = float("inf")
        self.mx = float("-inf")

    def add(self, db):
        self.n += 1
        self.energy += 10 ** (db / 10)
        self.mn = min(self.mn, db)
        self.mx = max(self.mx, db)

    @property
    def leq(self):
        return 10 * math.log10(self.energy / self.n)


class Output:
    """Schreibt Zeilen auf stdout und optional in eine (ggf. tagesweise) CSV-Datei."""

    def __init__(self, csv_pattern, status_path=None):
        self.pattern = csv_pattern
        self.path = None
        self.fh = None
        self.writer = None
        self.status_path = status_path
        self.status = {}

    def set_status(self, **kw):
        """Statusdatei (JSON) atomar aktualisieren, z.B. fuer die Webansicht."""
        if not self.status_path:
            return
        self.status.update(kw)
        self.status["updated"] = time.time()
        self.status["pid"] = os.getpid()
        # INVOCATION_ID setzt systemd: nur dann startet ein Beenden den Logger automatisch neu
        self.status["supervised"] = bool(os.environ.get("INVOCATION_ID"))
        tmp = self.status_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.status, f)
            os.replace(tmp, self.status_path)
        except OSError as e:
            log(f"Statusdatei nicht schreibbar: {e}")
            self.status_path = None

    def _open(self, path):
        if self.fh:
            self.fh.close()
        self.fh = open(path, "a", newline="")
        self.writer = csv.writer(self.fh)
        self.path = path
        if self.fh.tell() == 0:
            self.writer.writerow(CSV_HEADER)
        else:
            with open(path, newline="") as f:
                first = next(csv.reader(f), [])
            if first != CSV_HEADER:
                sys.exit(f"{path} hat ein anderes Spaltenformat (aelteres Programm?). "
                         "Bitte eine neue Datei verwenden.")

    def write(self, b, state, interval):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(b.start))
        st = state
        print(f"{ts}  Leq {b.leq:5.1f} dB({st['weighting']})  min {b.mn:5.1f}  max {b.mx:5.1f}  "
              f"n={b.n:<3} {st['speed']:<4} {st['range']}  {st['mode']}", flush=True)
        self.set_status(state="ok", interval=interval, start=b.start, leq=round(b.leq, 1),
                        min=round(b.mn, 1), max=round(b.mx, 1), n=b.n,
                        weighting=st["weighting"], speed=st["speed"], range=st["range"],
                        mode=st["mode"])
        if self.pattern:
            path = time.strftime(self.pattern, time.localtime(b.start))
            if path != self.path:
                self._open(path)
            self.writer.writerow([ts, f"{b.leq:.1f}", f"{b.mn:.1f}", f"{b.mx:.1f}", b.n,
                                  st["weighting"], st["speed"], st["range"], st["mode"]])
            self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()


class Stalled(Exception):
    """Lange keine Messwerte: Port neu oeffnen."""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--csv", help="CSV-Datei (wird angehaengt); strftime-Muster wie %%Y-%%m-%%d erlaubt")
    ap.add_argument("--status", help="JSON-Statusdatei fuer die Webansicht (z.B. /run/pce323/latest.json)")
    ap.add_argument("--duration", type=float, default=0, help="Sekunden, 0 = bis Strg+C")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="Auswerteintervall in Sekunden (Leq/Min/Max), 0 = jeder Rohwert. Standard 1")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="Sekunden ohne Messwert bis zur Warnung und zum Neuoeffnen des Ports. Standard 10")
    ap.add_argument("--retry", type=float, default=3.0, help="Sekunden zwischen Verbindungsversuchen")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    out = Output(args.csv, args.status)
    parser = Parser()
    start = time.time()
    cur = None            # aktuelles Intervall
    stalled = False       # Warnung wegen Datenstillstand ist aktiv
    first_connect = True
    iv = args.interval

    def flush_bin():
        nonlocal cur
        if cur and cur.n:
            out.write(cur, parser.state, iv)
        cur = None

    def finished():
        return args.duration and time.time() - start >= args.duration

    def add_value(db, now):
        nonlocal cur
        if iv == 0:
            b = Bin(now)
            b.add(db)
            out.write(b, parser.state, 0)
            return
        slot = math.floor(now / iv) * iv
        if cur is None or slot != cur.start:
            flush_bin()
            cur = Bin(slot)
        cur.add(db)

    log("Starte. Strg+C zum Beenden.")
    out.set_status(state="starting", interval=iv)
    try:
        while not finished():
            # --- Verbindung herstellen ---
            flush_bin()   # angefangenes Intervall sofort schreiben, auch wenn die Verbindung weg ist
            try:
                ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N",
                                    stopbits=1, timeout=0.2)
            except (serial.SerialException, OSError) as e:
                if isinstance(e, PermissionError) or "Permission denied" in str(e):
                    sys.exit(f"Keine Rechte fuer {args.port}: {e}\n"
                             "User muss in Gruppe 'dialout' sein (nach neuem Login) "
                             "oder: sg dialout -c '...'")
                log(f"Port {args.port} nicht verfuegbar ({e}). Neuer Versuch in {args.retry:g} s ...")
                out.set_status(state="disconnected", interval=iv)
                time.sleep(args.retry)
                continue
            if not first_connect:
                log("Verbindung wiederhergestellt.")
            first_connect = False
            parser.reset()
            last_value = time.time()
            last_warn = last_value

            # --- Lesen ---
            try:
                while not finished():
                    now = time.time()
                    if cur and iv and now >= cur.start + iv:
                        flush_bin()
                    if now - last_value > args.timeout:
                        if not stalled:
                            log(f"Keine Messwerte seit {args.timeout:g} s. Geraet an und Auto-Power-off "
                                "aus (Uhr-Symbol weg)? Versuche weiter ...")
                            stalled = True
                            out.set_status(state="stalled", interval=iv)
                        if now - last_warn > args.timeout:
                            last_warn = now
                            raise Stalled()
                    data = ser.read(512)
                    if not data:
                        continue
                    for db in parser.feed(data):
                        now = time.time()
                        if stalled:
                            log("Messwerte laufen wieder.")
                            stalled = False
                        last_value = now
                        add_value(db, now)
            except Stalled:
                pass
            except (serial.SerialException, OSError) as e:
                log(f"Verbindung verloren ({e}). Neuer Versuch in {args.retry:g} s ...")
                out.set_status(state="disconnected", interval=iv)
                time.sleep(args.retry)
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        pass
    finally:
        flush_bin()
        out.set_status(state="stopped", interval=iv)
        out.close()
        log("Beendet.")


if __name__ == "__main__":
    main()
