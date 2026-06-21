# SonosDurchsage v2

Professionelles, lokales Durchsagesystem für SONOS-Lautsprecher im Netzwerk.  
Web-basiertes Operator-Interface im HMI-Stil (SCADA-Design).

## Features

- Automatische Erkennung aller SONOS-Lautsprecher im Netzwerk
- Schnelldurchsagen aus Ordnerstruktur (`schnelldurchsagen/`)
- Mikrofonaufnahme → direkt auf SONOS abspielen
- Wählbarer Gong vor jeder Durchsage (`assets/gongs/`)
- Lautstärke pro Durchsage einstellbar
- Robustes Abspielen: Audiodateien werden über Flask's eigenen HTTP-Server an SONOS geliefert (kein separater Thread-Server mehr)
- Professionelles HMI-Interface (dunkel, monospace, Industriedesign)

## Installation

```bash
git clone https://github.com/jakobneukirchner/SonosDurchsage.git
cd SonosDurchsage
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows
pip install -r requirements.txt
python app.py
```

Browser öffnen: **http://localhost:5000**

> SONOS muss sich im selben Netzwerk befinden und die IP des Servers erreichen können.

## Projektstruktur

```
SonosDurchsage/
├── app.py
├── requirements.txt
├── README.md
├── templates/index.html
├── assets/
│   ├── gongs/          ← Gong-Dateien hier ablegen (.mp3 / .wav)
│   └── recordings/     ← Mikrofon-Aufnahmen (automatisch)
└── schnelldurchsagen/
    ├── Allgemein/
    ├── Bahnhof/
    └── Notfall/
```

## Technische Details (Abspielen)

SONOS benötigt eine per HTTP erreichbare Audiodatei.  
Die App dient Dateien über die Route `/media/<pfad>` – derselbe Flask-Prozess, kein externer Server.  
SONOS ruft dann z. B. `http://192.168.1.100:5000/media/assets/gongs/gong.mp3` ab.

## Lizenz

MIT
