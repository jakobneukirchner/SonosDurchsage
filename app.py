#!/usr/bin/env python3
"""
SonosDurchsage - Lokales Durchsagesystem fuer SONOS Lautsprecher
"""

import os
import time
import socket
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS

try:
    import soco
    SOCO_AVAILABLE = True
except ImportError:
    SOCO_AVAILABLE = False
    print("WARNING: soco nicht installiert. SONOS-Funktionen deaktiviert.")

try:
    import sounddevice as sd
    import soundfile as sf
    import numpy as np
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False
    print("WARNING: sounddevice/soundfile nicht installiert. Audio-Funktionen deaktiviert.")

app = Flask(__name__)
CORS(app)

BASE_DIR = Path(__file__).parent
ASSETS_DIR = BASE_DIR / "assets"
GONGS_DIR = ASSETS_DIR / "gongs"
RECORDINGS_DIR = ASSETS_DIR / "recordings"
SCHNELL_DIR = BASE_DIR / "schnelldurchsagen"

for d in [GONGS_DIR, RECORDINGS_DIR, SCHNELL_DIR]:
    d.mkdir(parents=True, exist_ok=True)

recording_data = []
recording_active = False
stream = None
discovered_speakers = []


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def discover_sonos():
    global discovered_speakers
    if not SOCO_AVAILABLE:
        return []
    try:
        devices = list(soco.discover() or [])
        discovered_speakers = [
            {
                "ip": d.ip_address,
                "name": d.player_name,
                "volume": d.volume,
                "group": d.group.coordinator.player_name if d.group else "Solo"
            }
            for d in devices
        ]
        return discovered_speakers
    except Exception as e:
        print(f"Fehler bei SONOS-Suche: {e}")
        return []


def get_folder_tree(root: Path, base: Path = None) -> dict:
    if base is None:
        base = root.parent
    result = {
        "name": root.name,
        "path": str(root.relative_to(base)),
        "files": [],
        "folders": []
    }
    if root.is_dir():
        for item in sorted(root.iterdir()):
            if item.name.startswith('.'):
                continue
            if item.is_dir():
                result["folders"].append(get_folder_tree(item, base))
            elif item.suffix.lower() in [".mp3", ".wav", ".ogg", ".flac", ".m4a"]:
                result["files"].append({
                    "name": item.name,
                    "path": str(item.relative_to(BASE_DIR)),
                    "size": item.stat().st_size
                })
    return result


def play_on_sonos(speakers: list, audio_path: str, gong_path: str = None, volume: int = None):
    if not SOCO_AVAILABLE:
        return {"success": False, "error": "soco nicht installiert"}

    local_ip = get_local_ip()
    port = 8765

    import http.server
    handler = http.server.SimpleHTTPRequestHandler
    os.chdir(BASE_DIR)
    httpd = http.server.HTTPServer(("", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    try:
        for ip in speakers:
            try:
                device = soco.SoCo(ip)
                if volume is not None:
                    device.volume = volume
                if gong_path:
                    gong_url = f"http://{local_ip}:{port}/{gong_path}"
                    device.play_uri(gong_url)
                    time.sleep(3)
                audio_url = f"http://{local_ip}:{port}/{audio_path}"
                device.play_uri(audio_url)
            except Exception as e:
                print(f"Fehler bei Lautsprecher {ip}: {e}")
    finally:
        httpd.shutdown()

    return {"success": True}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/speakers/discover", methods=["GET"])
def api_discover():
    speakers = discover_sonos()
    return jsonify({"speakers": speakers, "soco_available": SOCO_AVAILABLE})


@app.route("/api/speakers", methods=["GET"])
def api_speakers():
    return jsonify({"speakers": discovered_speakers, "soco_available": SOCO_AVAILABLE})


@app.route("/api/gongs", methods=["GET"])
def api_gongs():
    gongs = []
    if GONGS_DIR.exists():
        for f in sorted(GONGS_DIR.iterdir()):
            if f.suffix.lower() in [".mp3", ".wav", ".ogg"]:
                gongs.append({"name": f.name, "path": f"assets/gongs/{f.name}"})
    return jsonify({"gongs": gongs})


@app.route("/api/schnelldurchsagen", methods=["GET"])
def api_schnelldurchsagen():
    tree = get_folder_tree(SCHNELL_DIR)
    return jsonify(tree)


@app.route("/api/recordings", methods=["GET"])
def api_recordings():
    recs = []
    if RECORDINGS_DIR.exists():
        for f in sorted(RECORDINGS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() in [".wav", ".mp3"]:
                recs.append({
                    "name": f.name,
                    "path": f"assets/recordings/{f.name}",
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime
                })
    return jsonify({"recordings": recs})


@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    global recording_data, recording_active, stream
    if not AUDIO_AVAILABLE:
        return jsonify({"success": False, "error": "sounddevice nicht installiert"})
    recording_data = []
    recording_active = True

    def callback(indata, frames, time_info, status):
        if recording_active:
            recording_data.append(indata.copy())

    stream = sd.InputStream(samplerate=44100, channels=1, callback=callback)
    stream.start()
    return jsonify({"success": True})


@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    global recording_active, stream
    if not AUDIO_AVAILABLE:
        return jsonify({"success": False, "error": "sounddevice nicht installiert"})
    recording_active = False
    if stream:
        stream.stop()
        stream.close()
        stream = None
    if not recording_data:
        return jsonify({"success": False, "error": "Keine Aufnahmedaten gefunden"})
    audio = np.concatenate(recording_data, axis=0)
    filename = f"aufnahme_{int(time.time())}.wav"
    filepath = RECORDINGS_DIR / filename
    sf.write(str(filepath), audio, 44100)
    return jsonify({"success": True, "filename": filename, "path": f"assets/recordings/{filename}"})


@app.route("/api/play", methods=["POST"])
def api_play():
    data = request.json
    audio_path = data.get("audio_path", "")
    gong_path = data.get("gong_path", "")
    speakers = data.get("speakers", [])
    volume = data.get("volume", None)
    if not speakers:
        return jsonify({"success": False, "error": "Keine Lautsprecher ausgewaehlt"})
    abs_path = BASE_DIR / audio_path
    if not abs_path.exists():
        return jsonify({"success": False, "error": f"Datei nicht gefunden: {audio_path}"})
    result = play_on_sonos(speakers, audio_path, gong_path if gong_path else None, volume)
    return jsonify(result)


@app.route("/api/volume", methods=["POST"])
def api_volume():
    if not SOCO_AVAILABLE:
        return jsonify({"success": False, "error": "soco nicht installiert"})
    data = request.json
    ip = data.get("ip")
    volume = data.get("volume", 30)
    try:
        device = soco.SoCo(ip)
        device.volume = int(volume)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not SOCO_AVAILABLE:
        return jsonify({"success": False, "error": "soco nicht installiert"})
    data = request.json
    speakers = data.get("speakers", [s["ip"] for s in discovered_speakers])
    for ip in speakers:
        try:
            soco.SoCo(ip).stop()
        except Exception as e:
            print(f"Stop-Fehler {ip}: {e}")
    return jsonify({"success": True})


@app.route("/api/folder/create", methods=["POST"])
def api_folder_create():
    data = request.json
    folder_name = data.get("name", "").strip()
    parent = data.get("parent", "")
    if not folder_name:
        return jsonify({"success": False, "error": "Kein Name angegeben"})
    target = SCHNELL_DIR / parent / folder_name
    target.mkdir(parents=True, exist_ok=True)
    return jsonify({"success": True})


@app.route("/assets/<path:filename>")
def serve_assets(filename):
    return send_from_directory(ASSETS_DIR, filename)


@app.route("/schnelldurchsagen/<path:filename>")
def serve_schnell(filename):
    return send_from_directory(SCHNELL_DIR, filename)


if __name__ == "__main__":
    ip = get_local_ip()
    print(f"\nSonosDurchsage laeuft auf http://{ip}:5000")
    print(f"SONOS-Unterstuetzung: {'OK' if SOCO_AVAILABLE else 'FEHLT (pip install soco)'}")
    print(f"Audio-Unterstuetzung: {'OK' if AUDIO_AVAILABLE else 'FEHLT (pip install sounddevice soundfile numpy)'}")
    app.run(host="0.0.0.0", port=5000, debug=False)
