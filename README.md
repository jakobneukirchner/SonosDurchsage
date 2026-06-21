# 🔊 SONOS Durchsagesystem

Ein lokales Durchsagesystem für SONOS Lautsprecher mit moderner Web-Oberfläche.

## Features

- 🔍 **Automatische SONOS-Erkennung** – alle Lautsprecher im Netzwerk werden erkannt
- 📁 **Schnelldurchsagen** – Audiodateien in Ordnerstruktur
- 🎙️ **Aufnahme & Abspielen** – Mikrofon aufnehmen, direkt auf SONOS abspielen
- 🔔 **Gong-Auswahl** – wählbarer Gong vor jeder Durchsage
- 🎚️ **Lautstärke-Kontrolle** – pro Lautsprecher einstellbar
- 🌐 **Lokale Web-App** – läuft komplett im Browser

## Installation

```bash
# 1. Repository klonen
git clone https://github.com/jakobneukirchner/SonosDurchsage.git
cd SonosDurchsage

# 2. Abhängigkeiten installieren
pip install -r requirements.txt

# 3. Starten
python app.py
```

Dann im Browser öffnen: **http://localhost:5000**

## Projektstruktur

```
SonosDurchsage/
├── app.py                  # Flask Backend
├── requirements.txt        # Python Abhängigkeiten
├── templates/
│   └── index.html          # Web-Oberfläche
├── assets/
│   ├── gongs/              # Gong-Dateien (.mp3)
│   │   ├── gong_classic.mp3
│   │   ├── gong_soft.mp3
│   │   └── gong_double.mp3
│   └── recordings/         # Aufnahmen (wird automatisch erstellt)
└── schnelldurchsagen/      # Eigene Audiodateien
    ├── Allgemein/
    ├── Notfall/
    └── Information/
```

## Gong-Dateien hinzufügen

Kopiere `.mp3`-Dateien in den Ordner `assets/gongs/`. Sie erscheinen automatisch in der Auswahl.

## Schnelldurchsagen hinzufügen

Erstelle Unterordner in `schnelldurchsagen/` und lege dort `.mp3`-Dateien ab.

## Systemvoraussetzungen

- Python 3.8+
- SONOS Lautsprecher im gleichen Netzwerk
- Mikrofon (für Aufnahme-Funktion)
- Moderner Browser
