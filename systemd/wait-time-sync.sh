#!/bin/sh
# Wird vom Logger-Dienst vor dem Start aufgerufen (ExecStartPre).
#
# Der Raspberry Pi 2 hat keine Batterieuhr. Nach dem Einschalten laeuft er zunaechst mit der
# Uhrzeit vom letzten Ausschalten, bis er sie aus dem Internet (NTP) holt. Ohne Warten wuerde
# der Logger bis dahin Messwerte mit falschem Zeitstempel schreiben, oft in die Datei vom Vortag.
#
# Wartet deshalb direkt nach dem Booten hoechstens MAX Sekunden auf die Synchronisierung.
# Ohne Internet startet der Logger danach trotzdem (dann mit ungenauer Uhrzeit).
#
#   wait-time-sync.sh [MAX_SEKUNDEN] [STATUSDATEI]

MAX="${1:-300}"
STATUS="${2:-}"

command -v timedatectl >/dev/null 2>&1 || exit 0
# Nur kurz nach dem Booten warten, nicht bei jedem Neustart des Loggers
UP="$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)"
[ "$UP" -lt 900 ] || exit 0
# Keine Zeitsynchronisierung eingerichtet: Warten waere sinnlos
[ "$(timedatectl show -p NTP --value 2>/dev/null)" = "yes" ] || exit 0

synced() { [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; }
synced && exit 0

echo "Warte auf Uhrzeit aus dem Internet (hoechstens $MAX s) ..."
if [ -n "$STATUS" ]; then   # Webansicht zeigt solange "Warte auf Uhrzeit"
  printf '{"state": "waiting_time", "updated": %s}\n' "$(date +%s)" > "$STATUS.tmp" && mv -f "$STATUS.tmp" "$STATUS"
fi
i=0
while [ "$i" -lt "$MAX" ]; do
  sleep 2
  i=$((i + 2))
  if synced; then
    echo "Uhrzeit synchronisiert nach $i s: $(date '+%d.%m.%Y %H:%M:%S')"
    exit 0
  fi
done
echo "Uhrzeit nach $MAX s nicht synchronisiert (kein Internet?). Starte trotzdem, Zeitstempel koennen falsch sein."
exit 0
