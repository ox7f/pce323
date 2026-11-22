#!/usr/bin/env bash
# Installiert Logger und Webansicht als systemd-Dienste (Raspberry Pi / Debian / Ubuntu).
#
#   ./install.sh                 # installieren und starten
#   ./install.sh --port 8080     # anderer Web-Port
#   ./install.sh --serial /dev/serial/by-id/...   # anderer serieller Port
#   ./install.sh --no-autoupdate # ohne automatisches Update aus GitHub (siehe update.sh)
#   ./install.sh --uninstall     # Dienste entfernen (Daten bleiben)
#
# Benoetigt sudo. Die Dateien bleiben in diesem Verzeichnis, es wird nichts dorthin kopiert.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
WEBPORT=8080
SERIAL=""
UNINSTALL=0
AUTOUPDATE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --port) WEBPORT="$2"; shift 2 ;;
    --serial) SERIAL="$2"; shift 2 ;;
    --no-autoupdate) AUTOUPDATE=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
  esac
done

if [ "$RUN_USER" = "root" ]; then
  echo "Bitte als normaler Benutzer starten (mit sudo davor ist ok), nicht direkt als root." >&2
  exit 1
fi

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

if [ "$UNINSTALL" -eq 1 ]; then
  $SUDO systemctl disable --now pce323-update.timer pce323-logger.service pce323-web.service 2>/dev/null || true
  $SUDO rm -f /etc/systemd/system/pce323-logger.service /etc/systemd/system/pce323-web.service \
              /etc/systemd/system/pce323-update.service /etc/systemd/system/pce323-update.timer /etc/pce323.conf
  $SUDO systemctl daemon-reload
  echo "Dienste entfernt. Daten in $DIR/data bleiben erhalten."
  exit 0
fi

# 1) Pakete: pyserial fuer den Logger, avahi-daemon damit der Pi im Heimnetz als <name>.local erreichbar ist
PKGS=""
python3 -c 'import serial' 2>/dev/null || PKGS="$PKGS python3-serial"
command -v avahi-daemon >/dev/null 2>&1 || PKGS="$PKGS avahi-daemon"
if [ -n "$PKGS" ]; then
  echo "Installiere:$PKGS ..."
  $SUDO apt-get update -qq
  $SUDO apt-get install -y $PKGS
fi

# 2) Uhrzeit aus dem Internet: der Pi 2 hat keine Batterieuhr, ohne NTP stimmen die Zeitstempel nicht
if command -v timedatectl >/dev/null 2>&1 && [ "$(timedatectl show -p NTP --value 2>/dev/null)" != "yes" ]; then
  echo "Schalte Zeitsynchronisierung (NTP) ein ..."
  $SUDO timedatectl set-ntp true || echo "Hinweis: NTP liess sich nicht einschalten, bitte 'timedatectl' pruefen." >&2
fi

# 3) Serieller Port: stabiler Name, der auch nach Neustart/Umstecken gleich bleibt
if [ -z "$SERIAL" ]; then
  SERIAL="$(ls /dev/serial/by-id/*CP2102* 2>/dev/null | head -n1 || true)"
  if [ -z "$SERIAL" ]; then
    SERIAL=/dev/ttyUSB0
    echo "Hinweis: Kein CP2102-Adapter gefunden, verwende $SERIAL (Messgeraet angesteckt?)."
  fi
fi
echo "Serieller Port: $SERIAL"

mkdir -p "$DIR/data"
if [ "$(id -u)" -eq 0 ]; then chown "$RUN_USER" "$DIR/data"; fi

# 4) Rechte auf den seriellen Port (Dienst bekommt dialout selbst, der Benutzer fuer manuelle Tests)
$SUDO usermod -aG dialout "$RUN_USER"

# 5) Units aus den Vorlagen erzeugen (beide starten danach bei jedem Booten von selbst)
render() {
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@DIR@|$DIR|g" -e "s|@SERIAL@|$SERIAL|g" \
      -e "s|@WEBPORT@|$WEBPORT|g" "$1"
}
render "$DIR/systemd/pce323-logger.service.in" | $SUDO tee /etc/systemd/system/pce323-logger.service >/dev/null
render "$DIR/systemd/pce323-web.service.in"    | $SUDO tee /etc/systemd/system/pce323-web.service    >/dev/null

# Gewaehlte Optionen merken, damit update.sh bei geaenderten Vorlagen dasselbe wieder einsetzen kann
printf 'WEBPORT=%q\nSERIAL=%q\n' "$WEBPORT" "$SERIAL" | $SUDO tee /etc/pce323.conf >/dev/null

# Automatisches Update aus GitHub (nur wenn der Ordner ein git-Repository ist)
if [ "$AUTOUPDATE" -eq 1 ] && [ -d "$DIR/.git" ]; then
  render "$DIR/systemd/pce323-update.service.in" | $SUDO tee /etc/systemd/system/pce323-update.service >/dev/null
  $SUDO install -m 644 "$DIR/systemd/pce323-update.timer" /etc/systemd/system/pce323-update.timer
  UPDATE_ON=1
else
  $SUDO systemctl disable --now pce323-update.timer 2>/dev/null || true
  UPDATE_ON=0
  [ "$AUTOUPDATE" -eq 1 ] && echo "Hinweis: $DIR ist kein git-Repository, automatisches Update bleibt aus (siehe README)."
fi

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now pce323-logger.service pce323-web.service
[ "$UPDATE_ON" -eq 1 ] && $SUDO systemctl enable --now pce323-update.timer

sleep 2
echo
$SUDO systemctl --no-pager --lines=0 status pce323-logger.service pce323-web.service | grep -E 'Active|●' || true
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Webansicht:  http://${IP:-<ip-des-pi>}:${WEBPORT}/   (oder http://$(hostname).local:${WEBPORT}/)"
echo "Logs:        journalctl -u pce323-logger -f"
echo "Daten:       $DIR/data/pegel_JJJJ-MM-TT.csv"
TZNOW="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
echo "Zeitzone:    ${TZNOW:-?}   (aendern: sudo timedatectl set-timezone Europe/Berlin)"
echo
echo "Ab jetzt startet beides bei jedem Einschalten des Pi von selbst."
[ "$UPDATE_ON" -eq 1 ] && echo "Neue Versionen aus GitHub holt der Pi alle 5 Minuten selbst (journalctl -u pce323-update)."
echo "Am Messgeraet: einschalten, SETUP druecken bis die Uhr im Display verschwindet (Auto-Power-off aus)."
