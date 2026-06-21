#!/usr/bin/env python3
"""
SonosDurchsage v2 - Produktionsreifes SONOS-Durchsagesystem
Abspielen via Flask-eigenem Static-File-Server (kein separater HTTP-Thread)
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
    log.warning("soco nicht installiert – pip install soco")

try:
    import sounddevice as sd
    import soundfile as sf
    import numpy as np
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False
    log.warning("sounddevice/soundfile nicht installiert – pip install sounddevice soundfile numpy")

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
_recording_stream = None
_discovered: list = []
_flask_port: int = 5000

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

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
    """Baut eine von SONOS erreichbare HTTP-URL fuer eine Datei im Projektverzeichnis."""
    ip = local_ip()
    clean = rel_path.lstrip("/").replace("\\", "/")
    return f"http://{ip}:{_flask_port}/media/{clean}"


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
                    "ip":     d.ip_address,
                    "name":   d.player_name,
                    "volume": d.volume,
                    "model":  getattr(d, 'speaker_info', {}).get('model_name', ''),
                    "group":  d.group.coordinator.player_name if d.group else "Solo"
                })
            except Exception as e:
                log.warning(f"Speaker-Info-Fehler {d.ip_address}: {e}")
        log.info(f"SONOS-Discovery: {len(_discovered)} Geraet(e) gefunden")
        return _discovered
    except Exception as e:
        log.error(f"Discovery-Fehler: {e}")
        return []


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
                url = file_url(gong_path)
                log.info(f"Gong -> {ip}: {url}")
                dev.play_uri(url, title="Gong")
                # Warte bis Gong fertig (max. 8 Sekunden)
                for _ in range(40):
                    time.sleep(0.2)
                    try:
                        if dev.get_current_transport_info()['current_transport_state'] != 'PLAYING':
                            break
                    except Exception:
                        break
            url = file_url(audio_path)
            log.info(f"Durchsage -> {ip}: {url}")
            dev.play_uri(url, title="Durchsage")
        except Exception as e:
            log.error(f"Abspielfehler {ip}: {e}")
            errors.append(f"{ip}: {e}")
    if errors:
        return {"success": False, "error": ", ".join(errors)}
    return {"success": True}


def folder_tree(root: Path, rel_root: Path) -> dict:
    node = {"name": root.name, "rel": str(root.relative_to(rel_root)), "files": [], "folders": []}
    if root.is_dir():
        for item in sorted(root.iterdir()):
            if item.name.startswith('.'):
                continue
            if item.is_dir():
                node["folders"].append(folder_tree(item, rel_root))
            elif item.suffix.lower() in AUDIO_EXTS:
                node["files"].append({
                    "name": item.name,
                    "path": "schnelldurchsagen/" + str(item.relative_to(SCHNELL_DIR)).replace("\\", "/"),
                    "size": item.stat().st_size
                })
    return node


# ---------------------------------------------------------------------------
# Media-Serving (alle Audiodateien ueber einen einzigen Route-Handler)
# ---------------------------------------------------------------------------

@app.route("/media/<path:filename>")
def serve_media(filename):
    """Liefert Dateien aus dem gesamten Projektverzeichnis an SONOS."""
    target = (BASE_DIR / filename).resolve()
    try:
        target.relative_to(BASE_DIR)
    except ValueError:
        abort(403)
    if not target.exists():
        log.warning(f"Media nicht gefunden: {filename}")
        abort(404)
    return send_from_directory(target.parent, target.name)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

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
    speakers = discover_sonos()
    return jsonify({"speakers": speakers})


@app.route("/api/speakers")
def api_speakers():
    return jsonify({"speakers": _discovered})


@app.route("/api/gongs")
def api_gongs():
    gongs = []
    if GONGS_DIR.exists():
        for f in sorted(GONGS_DIR.iterdir()):
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

    _recording_stream = sd.InputStream(samplerate=44100, channels=1, dtype='float32',
                                        callback=callback)
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
        return jsonify({"success": False, "error": "Keine Audiodaten aufgezeichnet"})
    audio = np.concatenate(_recording_chunks, axis=0)
    fname = f"aufnahme_{int(time.time())}.wav"
    fpath = RECORDINGS_DIR / fname
    sf.write(str(fpath), audio, 44100)
    log.info(f"Aufnahme gespeichert: {fname} ({fpath.stat().st_size} Bytes)")
    return jsonify({"success": True, "filename": fname,
                    "path": f"assets/recordings/{fname}"})


@app.route("/api/play", methods=["POST"])
def api_play():
    data = request.get_json(force=True)
    audio_path = (data.get("audio_path") or "").strip()
    gong_path  = (data.get("gong_path")  or "").strip()
    speakers   = data.get("speakers", [])
    volume     = data.get("volume")

    if not audio_path:
        return jsonify({"success": False, "error": "Kein Audiodateipfad angegeben"})
    if not speakers:
        return jsonify({"success": False, "error": "Keine Lautsprecher ausgewaehlt"})

    abs_path = (BASE_DIR / audio_path).resolve()
    try:
        abs_path.relative_to(BASE_DIR)
    except ValueError:
        return jsonify({"success": False, "error": "Ungültiger Pfad"})
    if not abs_path.exists():
        return jsonify({"success": False, "error": f"Datei nicht gefunden: {audio_path}"})

    if gong_path:
        gong_abs = (BASE_DIR / gong_path).resolve()
        if not gong_abs.exists():
            gong_path = ""

    def _play():
        play_on_speakers(speakers, audio_path,
                         gong_path if gong_path else None, volume)

    threading.Thread(target=_play, daemon=True).start()
    log.info(f"Durchsage gestartet: {audio_path} -> {speakers}")
    return jsonify({"success": True,
                    "audio_url": file_url(audio_path),
                    "speakers": speakers})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not SOCO_AVAILABLE:
        return jsonify({"success": False, "error": "soco fehlt"})
    data     = request.get_json(force=True)
    targets  = data.get("speakers") or [s["ip"] for s in _discovered]
    errors   = []
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
    ip   = data.get("ip")
    vol  = data.get("volume", 30)
    try:
        soco.SoCo(ip).volume = max(0, min(100, int(vol)))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/folder/create", methods=["POST"])
def api_folder_create():
    data   = request.get_json(force=True)
    name   = (data.get("name") or "").strip()
    parent = (data.get("parent") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "Kein Name angegeben"})
    target = (SCHNELL_DIR / parent / name).resolve()
    try:
        target.relative_to(SCHNELL_DIR)
    except ValueError:
        return jsonify({"success": False, "error": "Ungültiger Pfad"})
    target.mkdir(parents=True, exist_ok=True)
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    _flask_port = port
    ip = local_ip()
    log.info("="*60)
    log.info(f"  SonosDurchsage v2 gestartet")
    log.info(f"  URL        : http://{ip}:{port}")
    log.info(f"  SONOS      : {'OK  (soco verfuegbar)' if SOCO_AVAILABLE else 'FEHLT -> pip install soco'}")
    log.info(f"  Audio      : {'OK  (sounddevice verfuegbar)' if AUDIO_AVAILABLE else 'FEHLT -> pip install sounddevice soundfile numpy'}")
    log.info(f"  Media-Base : {BASE_DIR}")
    log.info("="*60)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
