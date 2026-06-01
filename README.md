# SonosDurchsage

Lokales Durchsagesystem fuer SONOS Lautsprecher im Netzwerk – mit moderner Weboberflaeche, komplett offline nutzbar.

## Features

- Automatische Erkennung aller SONOS Lautsprecher im Netzwerk (Name, IP, Gruppe)
- Schnelldurchsagen ueber Ordnerstruktur (`schnelldurchsagen/`)
- Mikrofonaufnahme und direktes Abspielen auf SONOS
- Waehlbarer Gong vor der Durchsage (`assets/gongs/`)
- Lautstaerke pro Durchsage einstellbar
- Helles und dunkles Design
- Moderne lokale Weboberflaeche (kein Internet noetig)

## Hinweis zur Livewiedergabe

Echte latenzarme Live-Mikrofonwiedergabe auf SONOS ist technisch nicht moeglich, da SONOS ausschliesslich datei- bzw. streambasiert arbeitet. Diese Anwendung bietet als praktikable Loesung: **Aufnehmen und sofort auf SONOS abspielen** (minimale Verzoegerung).

## Installation

```bash
git clone https://github.com/jakobneukirchner/SonosDurchsage.git
cd SonosDurchsage
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# oder: .venv\Scripts\activate   (Windows)
pip install -r requirements.txt
python app.py
```

Dann im Browser oeffnen:

```
http://localhost:5000
```

## Projektstruktur

```
SonosDurchsage/
├── app.py                   <- Flask-Backend
├── requirements.txt
├── README.md
├── templates/
│   └── index.html           <- Weboberflaeche
├── assets/
│   ├── gongs/               <- Gong-Dateien (.mp3/.wav) hier ablegen
│   └── recordings/          <- Mikrofon-Aufnahmen (automatisch)
└── schnelldurchsagen/       <- Durchsage-Audiodateien in Ordnern
    ├── Allgemein/
    ├── Bahnhof/
    └── Notfall/
```

## Nutzung

1. `python app.py` starten
2. Browser: `http://localhost:5000`
3. **Lautsprecher suchen** klicken
4. Gewuenschte Lautsprecher per Checkbox auswaehlen
5. Gong auswaehlen (optional)
6. Lautstaerke einstellen
7. Datei aus Schnelldurchsagen oder Aufnahmen waehlen
8. **Abspielen** klicken

## Eigene Dateien hinzufuegen

| Ordner | Inhalt |
|---|---|
| `assets/gongs/` | Gong-Sounddateien (.mp3, .wav, .ogg) |
| `schnelldurchsagen/Ordnername/` | Audiodateien fuer Schnelldurchsagen |

Neue Ordner koennen auch direkt ueber die Weboberflaeche angelegt werden.

## Abhaengigkeiten

| Paket | Zweck |
|---|---|
| Flask | Webserver |
| flask-cors | CORS-Header |
| soco | SONOS-Steuerung |
| sounddevice | Mikrofonaufnahme |
| soundfile | WAV-Schreiben |
| numpy | Audiodatenverarbeitung |

## Lizenz

MIT
