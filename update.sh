#!/usr/bin/env bash
# Holt den neuesten Stand aus GitHub und startet die Dienste neu, wenn es etwas Neues gibt.
# Wird alle paar Minuten vom Timer pce323-update.timer aufgerufen, geht aber auch von Hand:
#
#   sudo ./update.sh
#
# Es wird nur "fast-forward" geholt. Lokale Aenderungen auf dem Pi werden nie ueberschrieben:
# gibt es einen Konflikt, bricht das Skript mit Fehlermeldung ab (journalctl -u pce323-update).
# Kein Internet ist kein Fehler, dann passiert einfach nichts bis zum naechsten Mal.

# Alles in einer Funktion, damit das Skript sich selbst aktualisieren darf, waehrend es laeuft.
main() {
  set -euo pipefail
  local DIR OWNER OWNER_HOME before after changed
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  OWNER="$(stat -c %U "$DIR")"
  OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"

  local SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"

  # git immer als Besitzer des Ordners ausfuehren (Schluessel in ~/.ssh, kein "dubious ownership")
  g() {
    if [ "$(id -u)" -eq 0 ]; then
      timeout 120 runuser -u "$OWNER" -- env HOME="$OWNER_HOME" GIT_TERMINAL_PROMPT=0 \
        GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15" git -C "$DIR" "$@"
    else
      GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15" timeout 120 git -C "$DIR" "$@"
    fi
  }

  exec 9>"/tmp/pce323-update.lock"
  flock -n 9 || { echo "Update laeuft bereits."; return 0; }

  before="$(g rev-parse HEAD)"
  if ! g fetch --quiet; then
    echo "Kein Zugriff auf GitHub (Internet? Schluessel?). Versuche es spaeter wieder."
    return 0
  fi

  if [ "$(g rev-parse '@{u}')" = "$before" ]; then
    return 0   # nichts Neues
  fi

  echo "Neuer Stand auf GitHub, aktualisiere ..."
  if ! g merge --ff-only --quiet '@{u}'; then
    echo "FEHLER: 'git merge --ff-only' geht nicht (lokale Aenderungen auf dem Pi?). Bitte von Hand pruefen: cd $DIR && git status" >&2
    return 1
  fi
  after="$(g rev-parse HEAD)"
  changed="$(g diff --name-only "$before" "$after")"
  echo "Jetzt auf $(g log -1 --format='%h %s' "$after")"

  # Dienstvorlagen oder Installer geaendert: Units neu erzeugen (mit den damals gewaehlten Optionen)
  if echo "$changed" | grep -qE '^(systemd/|install\.sh$)'; then
    echo "Dienstvorlagen geaendert, fuehre install.sh erneut aus ..."
    local args=()
    if [ -r /etc/pce323.conf ]; then
      # shellcheck disable=SC1091
      . /etc/pce323.conf
      [ -n "${WEBPORT:-}" ] && args+=(--port "$WEBPORT")
      [ -n "${SERIAL:-}" ] && args+=(--serial "$SERIAL")
    fi
    SUDO_USER="$OWNER" "$DIR/install.sh" "${args[@]}"
  fi

  echo "Starte Dienste neu ..."
  $SUDO systemctl restart pce323-logger.service pce323-web.service
  echo "Fertig."
}
main "$@"
exit $?
