# PCE-323 Schallpegel-Logger mit Webansicht

Zeichnet die Messwerte eines PCE-323 (CEM-DT-8852-kompatibel, USB über CP2102-Adapter) rund um die Uhr auf
und zeigt sie im Browser an (Handy und PC im Heimnetz). Gedacht für einen Raspberry Pi.

| Datei | Zweck |
|---|---|
| `pce323.py` | Logger: liest den seriellen Port, schreibt CSV und eine Statusdatei |
| `pce323_web.py` | Webserver: Live-Wert, Verlauf, CSV-Download, Logger neu starten |
| `web/index.html` | Die Webseite selbst |
| `systemd/*.service.in`, `*.timer` | Vorlagen der Dienste und des Update-Timers |
| `systemd/wait-time-sync.sh` | Lässt den Logger nach dem Booten auf die Internet-Uhrzeit warten |
| `install.sh` | Installiert und startet die Dienste |
| `update.sh` | Holt die neueste Version aus GitHub und startet die Dienste neu (läuft alle 5 Min. automatisch) |
| `data/` | CSV-Dateien, eine pro Tag (`pegel_JJJJ-MM-TT.csv`) |

## Checkliste

- [ ] Pi hat Netzwerk (der Pi 2 hat **kein WLAN**: LAN-Kabel oder USB-WLAN-Stick)
- [ ] Zeitzone und Uhrzeit stimmen (siehe Schritt 1)
- [ ] Repository per `git clone` auf den Pi geholt (Schritt 2)
- [ ] Messgerät per USB am Pi, eingeschaltet, Auto-Power-off aus (Schritt 3)
- [ ] `./install.sh` gelaufen (Schritt 4)
- [ ] Seite im Browser öffnet sich und zeigt „Live" (Schritt 5)
- [ ] Messgerät hat Netzteil oder frische Batterie (siehe „Stromversorgung")
- [ ] Autostart getestet: `sudo reboot`, nach 1–2 Minuten zeigt die Webseite wieder „Live"

## 1. Pi vorbereiten

Per SSH auf dem Pi:

```
python3 --version                 # muss 3.7 oder neuer sein
timedatectl                       # Zeitzone Europe/Berlin und "System clock synchronized: yes"?
sudo timedatectl set-timezone Europe/Berlin     # falls die Zeitzone nicht stimmt
```

Die Zeitzone ist wichtig: Der Wechsel zur nächsten Tagesdatei folgt der Uhr des Pi. Der Pi 2 hat keine
Batterieuhr und kennt die Zeit nach einem Neustart erst wieder, wenn er per Internet synchronisiert hat.

## 2. Code auf den Pi holen

Der Code liegt in GitHub (`github.com/ox7f/pce323`). Der Pi holt ihn per `git clone`, und danach holt er sich neue Versionen selbst
(siehe „Update der Programme“). Die Messdaten (`data/`) sind nicht im Repository und bleiben auf dem Pi.

**Öffentliches Repository:**

```
git clone https://github.com/ox7f/pce323.git ~/pce323
```

**Privates Repository:** Der Pi braucht einen eigenen Schlüssel, der nur lesen darf (Deploy Key):

```
ssh-keygen -t ed25519 -N "" -f ~/.ssh/pce323_deploy -C "pce323-pi"
cat ~/.ssh/pce323_deploy.pub     # Ausgabe kopieren
```

Auf GitHub im Repository: *Settings → Deploy keys → Add deploy key*, Schlüssel einfügen, **Allow write access aus lassen**.
(Oder auf dem PC: `gh repo deploy-key add pce323_deploy.pub -R ox7f/pce323`.) Dann auf dem Pi:

```
printf 'Host github.com\n  IdentityFile ~/.ssh/pce323_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
chmod 600 ~/.ssh/config
git clone git@github.com:ox7f/pce323.git ~/pce323
```

**Pi hat die Dateien schon (früher per rsync kopiert):** im vorhandenen Ordner zu einem Klon machen, `data/` bleibt unberührt:

```
cd ~/pce323
git init -b main
git remote add origin git@github.com:ox7f/pce323.git     # bzw. die https-Adresse
git fetch origin
git reset --hard origin/main
git branch --set-upstream-to=origin/main main
```

## 3. Messgerät vorbereiten

1. USB-Kabel (Datenkabel, nicht nur Ladekabel) vom Messgerät an den Pi.
2. Messgerät einschalten.
3. **SETUP-Taste** drücken, bis das kleine **Uhr-Symbol** im Display verschwindet. Dann ist die Auto-Power-off
   aus und das Gerät sendet Daten. Ist die Uhr sichtbar, schaltet es sich nach einiger Zeit selbst aus.
4. Nach jedem Neustart des Messgeräts (Batteriewechsel, Stromausfall) kurz prüfen, ob die Uhr fehlt.

Ob der Adapter erkannt wird, siehst du auf dem Pi mit `ls /dev/serial/by-id/` (es sollte ein Eintrag mit `CP2102` erscheinen).

## 4. Installieren

Auf dem Pi, als normaler Benutzer (nicht direkt als root):

```
cd ~/pce323
./install.sh
```

Das Skript installiert bei Bedarf `python3-serial` und `avahi-daemon` (damit der Pi als `<name>.local` erreichbar ist),
schaltet die Zeitsynchronisierung aus dem Internet ein, trägt dich in die Gruppe `dialout` ein und richtet die beiden
Dienste `pce323-logger` und `pce323-web` ein. Am Ende nennt es die Adresse der Webseite und die Zeitzone.

Es richtet auch den Timer `pce323-update.timer` ein (siehe „Update der Programme“); mit `--no-autoupdate` bleibt er aus.

Das ist nur einmal nötig. Danach startet bei jedem Einschalten des Pi alles von selbst, und abgestürzte Dienste
werden neu gestartet. Der Logger wartet nach dem Booten, bis der Pi die Uhrzeit aus dem Internet hat (meist wenige
Sekunden, höchstens 5 Minuten). Der Pi 2 hat keine Batterieuhr und würde sonst die ersten Werte mit der Uhrzeit vom
letzten Ausschalten speichern. Ohne Internet startet der Logger nach 5 Minuten trotzdem, dann mit ungenauer Uhrzeit.

Optionen:

```
./install.sh --port 8081                       # anderer Web-Port (Standard 8080)
./install.sh --serial /dev/serial/by-id/...    # anderer serieller Port
./install.sh --no-autoupdate                   # ohne automatisches Update aus GitHub
./install.sh --uninstall                       # Dienste entfernen, Daten bleiben
```

## 5. Prüfen

```
systemctl status pce323-logger pce323-web      # beide "active (running)"
journalctl -u pce323-logger -f                 # Live-Log, Strg+C zum Beenden
curl -s localhost:8080/api/latest              # JSON mit "state": "ok" und einem "leq"-Wert
```

Dann am Handy oder PC `http://<pi-adresse>:8080/` öffnen. Oben rechts sollte **Live** mit grünem Häkchen stehen.

## Im Betrieb

- **Ein- und Ausschalten:** Nach dem Einschalten startet alles von selbst (siehe Schritt 4). Das Messgerät muss dabei
  eingeschaltet sein, nach einem Stromausfall also von Hand wieder einschalten und die Uhr im Display wegdrücken.
  Zum Ausschalten `sudo poweroff`, warten bis die grüne LED nicht mehr blinkt, dann den Strom ziehen. Einfach den
  Strom ziehen geht meistens gut, kann aber die SD-Karte beschädigen.
- **Daten:** `~/pce323/data/pegel_JJJJ-MM-TT.csv`, eine Datei pro Tag, ca. 5 MB pro Tag (eine Zeile pro Sekunde),
  knapp 2 GB im Jahr.
  Die Spalten sind Zeit, Leq, Min, Max, Anzahl Rohwerte, Bewertung, Zeitbewertung, Bereich, Modus.
- **Aufräumen:** Es wird nichts automatisch gelöscht. Auf einer 64-GB-Karte reicht der Platz für viele Jahre,
  den freien Platz zeigt `df -h ~`. Falls doch nötig, Dateien älter als 90 Tage löschen:
  `find ~/pce323/data -name 'pegel_*.csv' -mtime +90 -delete`
- **Sicherung:** Den Ordner `data` regelmäßig kopieren, zum Beispiel
  `scp -r <benutzer>@<pi-adresse>:~/pce323/data ./backup`.
- **Verlauf:** Die Knöpfe 10 Min bis 24 Std zeigen live die letzte Zeit. Ältere Tage über die Tagesauswahl daneben:
  `‹` `›` einen Tag zurück/vor, `«` `»` eine Woche, oder das Datum direkt wählen. Alternativ „Anzeigen“ neben einer
  Datei in der Liste „CSV-Dateien“. Ein Tag wird von 0 bis 24 Uhr gezeigt, Zoom und Ausschlag-Markierung gehen dort genauso.
  Ein Klick auf 10 Min … 24 Std führt zurück zur Live-Ansicht. Der erste Aufruf eines alten Tages kann auf dem Pi einen
  Moment dauern, weil die ganze Tagesdatei eingelesen wird.
- **Zoomen:** Im Diagramm einen Bereich mit der Maus aufziehen, am Handy mit zwei Fingern spreizen (am Laptop auch
  Touchpad-Spreizen oder Strg+Mausrad). Beim Zoomen lädt die Seite den Ausschnitt in feinerer Auflösung nach.
  Reicht der Ausschnitt bis „jetzt“, läuft er live mit. Doppelklick oder „Zoom zurücksetzen“ zeigt wieder den ganzen Zeitraum.
- **Ausschläge:** Unter dem Diagramm die Schwelle in dB einstellen (Standard 60). Was darüber liegt, ist rot, jeder
  Ausschlag hat oben ein rotes Dreieck, daneben stehen Anzahl und höchster Wert. Feld leeren schaltet die Markierung aus.
- **Logger neu starten:** Button „Logger neu starten" oben rechts auf der Webseite, oder
  `sudo systemctl restart pce323-logger`.
- **Update der Programme:** Auf dem PC ändern, `git commit`, `git push`. Der Pi prüft alle 5 Minuten (und 3 Minuten nach dem
  Booten), ob es auf GitHub etwas Neues gibt, holt es und startet Logger und Webseite neu. Dabei entsteht eine Lücke von
  ein paar Sekunden in den Messdaten. Wurden die Dienstvorlagen oder `install.sh` geändert, führt der Pi auch `install.sh`
  mit den früheren Optionen erneut aus. Sofort aktualisieren: `sudo ~/pce323/update.sh`.
  Protokoll: `journalctl -u pce323-update -n 30`, nächster Lauf: `systemctl list-timers pce323-update`.
  Es wird nur fast-forward geholt: Wer auf dem Pi Dateien von Hand ändert, bekommt im Protokoll einen Fehler statt
  überschriebener Änderungen (`git status` im Ordner, dann `git stash` oder `git checkout -- .`).
  Es gilt immer der Stand von `main`. Ein fehlerhafter Push landet also nach spätestens 5 Minuten auf dem Pi.

## Stromversorgung

Eine 9-V-Batterie im Messgerät hält bei Dauerbetrieb nicht lange. Das Messgerät hat laut Handbuch einen Anschluss
für ein Netzteil. Ob das Mini-USB-Kabel das Gerät mit Strom versorgt, ist nicht geprüft.

## Wenn etwas nicht klappt

| Anzeige auf der Webseite / Symptom | Wahrscheinliche Ursache | Was tun |
|---|---|---|
| „Warte auf Uhrzeit aus dem Internet …" | Pi ist gerade gebootet und hat noch keine Internet-Uhrzeit | Normal für einige Sekunden. Dauert es Minuten: Netzwerk prüfen, `timedatectl`. Nach 5 Minuten startet der Logger trotzdem. |
| „Kein Status vom Logger – Dienst gestartet?" | Logger-Dienst läuft nicht | `systemctl status pce323-logger`, `journalctl -u pce323-logger -n 50` |
| „USB-Verbindung getrennt" | Kabel locker, Messgerät aus, Adapter nicht erkannt | Kabel prüfen, `ls /dev/serial/by-id/`. Der Logger verbindet sich von selbst neu. |
| „Keine Messwerte – Gerät an und Auto-Power-off aus?" | Messgerät aus oder die Uhr im Display ist wieder sichtbar | Messgerät einschalten, SETUP drücken bis die Uhr weg ist, ggf. „Logger neu starten" |
| Im Log „Permission denied" | Benutzer nicht in Gruppe `dialout` | `./install.sh` erneut ausführen und neu starten |
| Seite lädt nicht | Falsche Adresse, Port oder Firewall | `systemctl status pce323-web`, Adresse und Port prüfen |
| Im Log „multiple access on port" | Ein zweites Programm hat den Port offen (z. B. von Hand gestarteter Logger) | Das andere Programm beenden: `ps aux \| grep pce323` |
| Zeitstempel falsch oder Tagesdatei zur falschen Zeit | Uhr oder Zeitzone des Pi falsch | `timedatectl` prüfen (Schritt 1) |
| Der Button „Logger neu starten" fehlt | Logger läuft nicht unter systemd (z. B. von Hand gestartet) | Normal beim lokalen Testen auf dem PC |

## Sicherheit

Die Webseite hat **keine Anmeldung**. Jeder im Heimnetz kann Messwerte ansehen, CSV-Dateien laden und den Logger neu starten.
Den Pi nicht per Portfreigabe aus dem Internet erreichbar machen.

## Hinweis

Entwickelt und getestet auf einem PC mit dem echten PCE-323, dort auch über Nacht samt Wechsel der Tagesdatei um
Mitternacht ohne Lücke. Auf einem Raspberry Pi 2 selbst ist es noch nicht gelaufen. Wenn dort etwas anders reagiert, hilft das Log (`journalctl -u pce323-logger`).
