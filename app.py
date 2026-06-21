#!/usr/bin/env python3
"""
SonosDurchsage v3
Abspielen: Flask-eigener /media/-Server, kein separater Thread-Webserver.
SONOS muss den Flask-Host per HTTP erreichen koennen.
"""

import os
import time
import socket
import threading
import logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, abort
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

try:
    import soco
    SOCO_AVAILABLE = True
except ImportError:
    SOCO_AVAILABLE = False
    log.warning("soco nicht installiert - pip install soco")

try:
    import sounddevice as sd
    import soundfile as sf
    import numpy as np
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False
    log.warning("sounddevice/soundfile/numpy fehlt - pip install sounddevice soundfile numpy")

app = Flask(__name__)
CORS(app)

BASE_DIR       = Path(__file__).parent.resolve()
ASSETS_DIR     = BASE_DIR / "assets"
GONGS_DIR      = ASSETS_DIR / "gongs"
RECORDINGS_DIR = ASSETS_DIR / "recordings"
SCHNELL_DIR    = BASE_DIR / "schnelldurchsagen"

for d in [GONGS_DIR, RECORDINGS_DIR, SCHNELL_DIR]:
    d.mkdir(parents=True, exist_ok=True)

_recording_chunks: list = []
_recording_active: bool = False
_recording_stream        = None
_discovered: list        = []
_flask_port: int         = 5000

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}


# -----------------------------------------------------------------------
# Netzwerk
# -----------------------------------------------------------------------

def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def file_url(rel_path: str) -> str:
    """HTTP-URL fuer SONOS, erreichbar vom Netzwerk."""
    ip    = local_ip()
    clean = rel_path.lstrip("/").replace("\\", "/")
    return f"http://{ip}:{_flask_port}/media/{clean}"


# -----------------------------------------------------------------------
# SONOS Discovery
# -----------------------------------------------------------------------

def discover_sonos() -> list:
    global _discovered
    if not SOCO_AVAILABLE:
        return []
    try:
        devices = list(soco.discover(timeout=5) or [])
        _discovered = []
        for d in devices:
            try:
                _discovered.append({
                    "ip":    d.ip_address,
                    "name":  d.player_name,
                    "volume": d.volume,
                    "group": d.group.coordinator.player_name if d.group else "Solo"
                })
            except Exception as e:
                log.warning(f"Speaker-Info {d.ip_address}: {e}")
        log.info(f"Discovery: {len(_discovered)} Geraet(e)")
        return _discovered
    except Exception as e:
        log.error(f"Discovery-Fehler: {e}")
        return []


# -----------------------------------------------------------------------
# Abspielen (KERN-FIX)
# Flask laeuft threaded=True, der /media/-Endpunkt bedient SONOS
# direkt aus demselben Prozess.
# -----------------------------------------------------------------------

def play_on_speakers(speaker_ips: list, audio_path: str,
                     gong_path: str = None, volume: int = None) -> dict:
    if not SOCO_AVAILABLE:
        return {"success": False, "error": "soco nicht installiert"}
    errors = []
    for ip in speaker_ips:
        try:
            dev = soco.SoCo(ip)
            if volume is not None:
                dev.volume = max(0, min(100, int(volume)))

            if gong_path:
                gong_url = file_url(gong_path)
                log.info(f"Gong  -> {ip}  {gong_url}")
                dev.play_uri(gong_url, title="Gong")
                # Warten bis Gong beendet (max 10 s)
                for _ in range(50):
                    time.sleep(0.2)
                    try:
                        state = dev.get_current_transport_info()[
                            'current_transport_state']
                        if state != 'PLAYING':
                            break
                    except Exception:
                        break

            audio_url = file_url(audio_path)
            log.info(f"Audio -> {ip}  {audio_url}")
            dev.play_uri(audio_url, title="Durchsage")

        except Exception as e:
            log.error(f"Abspielfehler {ip}: {e}")
            errors.append(f"{ip}: {e}")

    if errors:
        return {"success": False, "error": ", ".join(errors)}
    return {"success": True}


# -----------------------------------------------------------------------
# Dateibaumfunktion
# -----------------------------------------------------------------------

def folder_tree(root: Path, base: Path) -> dict:
    node = {"name": root.name, "files": [], "folders": []}
    if not root.is_dir():
        return node
    for item in sorted(root.iterdir()):
        if item.name.startswith('.'):
            continue
        if item.is_dir():
            node["folders"].append(folder_tree(item, base))
        elif item.suffix.lower() in AUDIO_EXTS:
            node["files"].append({
                "name": item.name,
                "path": "schnelldurchsagen/" + str(
                    item.relative_to(SCHNELL_DIR)).replace("\\", "/"),
                "size": item.stat().st_size
            })
    return node


# -----------------------------------------------------------------------
# Media-Route (Flask bedient Audiodateien fuer SONOS)
# -----------------------------------------------------------------------

@app.route("/media/<path:filename>")
def serve_media(filename):
    target = (BASE_DIR / filename).resolve()
    try:
        target.relative_to(BASE_DIR)
    except ValueError:
        abort(403)
    if not target.exists():
        log.warning(f"Nicht gefunden: {filename}")
        abort(404)
    log.info(f"SONOS ruft ab: {filename}")
    return send_from_directory(str(target.parent), target.name)


# -----------------------------------------------------------------------
# API
# -----------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "soco":  SOCO_AVAILABLE,
        "audio": AUDIO_AVAILABLE,
        "ip":    local_ip(),
        "port":  _flask_port
    })


@app.route("/api/speakers/discover", methods=["POST"])
def api_discover():
    return jsonify({"speakers": discover_sonos()})


@app.route("/api/speakers")
def api_speakers():
    return jsonify({"speakers": _discovered})


@app.route("/api/gongs")
def api_gongs():
    gongs = []
    for f in sorted(GONGS_DIR.iterdir()) if GONGS_DIR.exists() else []:
        if f.suffix.lower() in AUDIO_EXTS:
            gongs.append({"name": f.name, "path": f"assets/gongs/{f.name}"})
    return jsonify({"gongs": gongs})


@app.route("/api/schnelldurchsagen")
def api_schnell():
    return jsonify(folder_tree(SCHNELL_DIR, SCHNELL_DIR.parent))


@app.route("/api/recordings")
def api_recordings():
    recs = []
    if RECORDINGS_DIR.exists():
        for f in sorted(RECORDINGS_DIR.iterdir(),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() in AUDIO_EXTS:
                recs.append({
                    "name":     f.name,
                    "path":     f"assets/recordings/{f.name}",
                    "size":     f.stat().st_size,
                    "modified": int(f.stat().st_mtime)
                })
    return jsonify({"recordings": recs})


@app.route("/api/record/start", methods=["POST"])
def api_rec_start():
    global _recording_chunks, _recording_active, _recording_stream
    if not AUDIO_AVAILABLE:
        return jsonify({"success": False, "error": "sounddevice fehlt"})
    _recording_chunks = []
    _recording_active = True

    def callback(indata, frames, t, status):
        if _recording_active:
            _recording_chunks.append(indata.copy())

    _recording_stream = sd.InputStream(
        samplerate=44100, channels=1, dtype='float32', callback=callback)
    _recording_stream.start()
    log.info("Aufnahme gestartet")
    return jsonify({"success": True})


@app.route("/api/record/stop", methods=["POST"])
def api_rec_stop():
    global _recording_active, _recording_stream
    if not AUDIO_AVAILABLE:
        return jsonify({"success": False, "error": "sounddevice fehlt"})
    _recording_active = False
    if _recording_stream:
        _recording_stream.stop()
        _recording_stream.close()
        _recording_stream = None
    if not _recording_chunks:
        return jsonify({"success": False, "error": "Keine Audiodaten"})
    audio = np.concatenate(_recording_chunks, axis=0)
    fname = f"aufnahme_{int(time.time())}.wav"
    fpath = RECORDINGS_DIR / fname
    sf.write(str(fpath), audio, 44100)
    log.info(f"Gespeichert: {fname}")
    return jsonify({"success": True, "filename": fname,
                    "path": f"assets/recordings/{fname}"})


@app.route("/api/play", methods=["POST"])
def api_play():
    data       = request.get_json(force=True)
    audio_path = (data.get("audio_path") or "").strip()
    gong_path  = (data.get("gong_path")  or "").strip()
    speakers   = data.get("speakers", [])
    volume     = data.get("volume")

    if not audio_path:
        return jsonify({"success": False, "error": "Kein Pfad"})
    if not speakers:
        return jsonify({"success": False, "error": "Keine Lautsprecher"})

    abs_audio = (BASE_DIR / audio_path).resolve()
    try:
        abs_audio.relative_to(BASE_DIR)
    except ValueError:
        return jsonify({"success": False, "error": "Ungueltiger Pfad"})
    if not abs_audio.exists():
        return jsonify({"success": False,
                        "error": f"Datei nicht gefunden: {audio_path}"})

    if gong_path:
        if not (BASE_DIR / gong_path).resolve().exists():
            gong_path = ""

    def _bg():
        play_on_speakers(speakers, audio_path,
                         gong_path or None, volume)
    threading.Thread(target=_bg, daemon=True).start()
    log.info(f"Durchsage: {audio_path} -> {speakers}")
    return jsonify({"success": True, "audio_url": file_url(audio_path)})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not SOCO_AVAILABLE:
        return jsonify({"success": False, "error": "soco fehlt"})
    data    = request.get_json(force=True)
    targets = data.get("speakers") or [s["ip"] for s in _discovered]
    errors  = []
    for ip in targets:
        try:
            soco.SoCo(ip).stop()
        except Exception as e:
            errors.append(str(e))
    return jsonify({"success": not errors, "errors": errors})


@app.route("/api/volume", methods=["POST"])
def api_volume():
    if not SOCO_AVAILABLE:
        return jsonify({"success": False, "error": "soco fehlt"})
    data = request.get_json(force=True)
    try:
        soco.SoCo(data["ip"]).volume = max(0, min(100, int(data.get("volume", 30))))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/folder/create", methods=["POST"])
def api_folder_create():
    data   = request.get_json(force=True)
    name   = (data.get("name")   or "").strip()
    parent = (data.get("parent") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "Kein Name"})
    target = (SCHNELL_DIR / parent / name).resolve()
    try:
        target.relative_to(SCHNELL_DIR)
    except ValueError:
        return jsonify({"success": False, "error": "Ungueltiger Pfad"})
    target.mkdir(parents=True, exist_ok=True)
    return jsonify({"success": True})


# -----------------------------------------------------------------------
# Start
# -----------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    port        = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    _flask_port = port
    ip          = local_ip()
    log.info("=" * 60)
    log.info("  SonosDurchsage v3")
    log.info(f"  URL    : http://{ip}:{port}")
    log.info(f"  Media  : http://{ip}:{port}/media/...")
    log.info(f"  SOCO   : {'OK' if SOCO_AVAILABLE else 'FEHLT -> pip install soco'}")
    log.info(f"  AUDIO  : {'OK' if AUDIO_AVAILABLE else 'FEHLT -> pip install sounddevice soundfile numpy'}")
    log.info("=" * 60)
    # threaded=True ist entscheidend: SONOS-Anfragen an /media/ koennen
    # parallel zu laufenden play_uri()-Aufrufen bedient werden.
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
