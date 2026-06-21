#!/usr/bin/env python3
"""
SonosDurchsage v6
"""
import os, sys, time, socket, threading, mimetypes, logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, abort
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

try:
    import soco
    SOCO_AVAILABLE = True
except ImportError:
    SOCO_AVAILABLE = False
    log.warning("soco fehlt -> pip install soco")

try:
    import sounddevice as sd
    import soundfile as sf
    import numpy as np
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False

app = Flask(__name__)
CORS(app)

BASE_DIR       = Path(__file__).parent.resolve()
ASSETS_DIR     = BASE_DIR / "assets"
GONGS_DIR      = ASSETS_DIR / "gongs"
RECORDINGS_DIR = ASSETS_DIR / "recordings"
SCHNELL_DIR    = BASE_DIR / "schnelldurchsagen"

for d in [GONGS_DIR, RECORDINGS_DIR, SCHNELL_DIR]:
    d.mkdir(parents=True, exist_ok=True)

_recording_chunks = []
_recording_active = False
_recording_stream = None
_discovered = []
_flask_port = int(os.environ.get("SONOS_PORT", "5000"))

# Letzter Spielstatus fuer Frontend-Rueckmeldung
_last_play_result = {"success": None, "error": "", "url": "", "ts": 0}

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
MIME_MAP = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".ogg": "audio/ogg",  ".flac": "audio/flac",
    ".m4a": "audio/mp4",  ".aac": "audio/aac",
}

def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def media_url(rel_path: str) -> str:
    clean = rel_path.lstrip("/").replace("\\", "/")
    return f"http://{local_ip()}:{_flask_port}/media/{clean}"

@app.route("/media/<path:filename>")
def serve_media(filename):
    target = (BASE_DIR / filename).resolve()
    try:
        target.relative_to(BASE_DIR)
    except ValueError:
        abort(403)
    if not target.exists():
        log.warning(f"404 fuer SONOS: {filename}")
        abort(404)
    ext = target.suffix.lower()
    mime = MIME_MAP.get(ext, mimetypes.guess_type(str(target))[0] or "application/octet-stream")
    log.info(f"SONOS streamt: {filename} [{mime}]")
    return send_from_directory(str(target.parent), target.name, mimetype=mime, conditional=True)

def discover_sonos(timeout=8):
    global _discovered
    if not SOCO_AVAILABLE:
        return []
    try:
        devices = list(soco.discover(timeout=timeout) or [])
        _discovered = []
        for d in devices:
            try:
                _discovered.append({
                    "ip": d.ip_address, "name": d.player_name,
                    "volume": d.volume, "model": getattr(d, 'model_name', 'SONOS'),
                    "group": d.group.coordinator.player_name if d.group else "Solo"
                })
                log.info(f"  Gefunden: {d.player_name} ({d.ip_address})")
            except Exception as e:
                log.warning(f"  {d.ip_address}: {e}")
        return _discovered
    except Exception as e:
        log.error(f"Discovery-Fehler: {e}")
        return []

def _wait_until_done(dev, timeout=25):
    deadline = time.time() + timeout
    time.sleep(0.6)
    while time.time() < deadline:
        try:
            state = dev.get_current_transport_info()['current_transport_state']
            if state not in ('PLAYING', 'TRANSITIONING'):
                return
        except Exception:
            return
        time.sleep(0.4)

def play_on_sonos(speaker_ips, audio_path, gong_path=None, volume=None):
    global _last_play_result
    if not SOCO_AVAILABLE:
        _last_play_result = {"success": False, "error": "soco nicht installiert", "url": "", "ts": time.time()}
        return _last_play_result
    errors = []
    a_url = media_url(audio_path)
    g_url = media_url(gong_path) if gong_path else None
    log.info(f"=== PLAY START ==='")
    log.info(f"  Audio-URL : {a_url}")
    if g_url: log.info(f"  Gong-URL  : {g_url}")
    log.info(f"  Lautsprecher: {speaker_ips}")
    for ip in speaker_ips:
        try:
            dev = soco.SoCo(ip)
            if volume is not None:
                try: dev.volume = max(0, min(100, int(volume)))
                except Exception as ve: log.warning(f"Vol {ip}: {ve}")
            if g_url:
                try:
                    log.info(f"[{ip}] play_uri Gong...")
                    dev.play_uri(g_url, title="Gong")
                    _wait_until_done(dev, timeout=25)
                    log.info(f"[{ip}] Gong fertig")
                except Exception as ge:
                    log.warning(f"[{ip}] Gong-Fehler (weiter): {ge}")
            log.info(f"[{ip}] play_uri Durchsage: {a_url}")
            dev.play_uri(a_url, title="Durchsage")
            log.info(f"[{ip}] play_uri OK")
        except Exception as e:
            msg = f"{ip}: {type(e).__name__}: {e}"
            log.error(f"FEHLER: {msg}")
            errors.append(msg)
    result = {"success": len(errors) == 0, "errors": errors, "url": a_url, "ts": time.time()}
    _last_play_result = result
    return result

def folder_tree(root: Path) -> dict:
    node = {"name": root.name, "files": [], "folders": []}
    if not root.is_dir():
        return node
    for item in sorted(root.iterdir()):
        if item.name.startswith('.'):
            continue
        if item.is_dir():
            node["folders"].append(folder_tree(item))
        elif item.suffix.lower() in AUDIO_EXTS:
            node["files"].append({
                "name": item.name,
                "path": "schnelldurchsagen/" + str(item.relative_to(SCHNELL_DIR)).replace("\\", "/"),
                "size": item.stat().st_size
            })
    return node

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify({
        "soco": SOCO_AVAILABLE, "audio": AUDIO_AVAILABLE,
        "ip": local_ip(), "port": _flask_port, "version": "6.0",
        "media_base": f"http://{local_ip()}:{_flask_port}/media/"
    })

@app.route("/api/play-result")
def api_play_result():
    return jsonify(_last_play_result)

@app.route("/api/speakers/discover", methods=["POST"])
def api_discover():
    speakers = discover_sonos(timeout=8)
    return jsonify({"speakers": speakers, "count": len(speakers)})

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
    return jsonify(folder_tree(SCHNELL_DIR))

@app.route("/api/recordings")
def api_recordings():
    recs = []
    if RECORDINGS_DIR.exists():
        for f in sorted(RECORDINGS_DIR.iterdir(),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() in AUDIO_EXTS:
                recs.append({
                    "name": f.name, "path": f"assets/recordings/{f.name}",
                    "size": f.stat().st_size, "modified": int(f.stat().st_mtime)
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
    _recording_stream = sd.InputStream(samplerate=44100, channels=1, dtype='float32', callback=callback)
    _recording_stream.start()
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
    sf.write(str(RECORDINGS_DIR / fname), audio, 44100)
    return jsonify({"success": True, "filename": fname, "path": f"assets/recordings/{fname}"})

@app.route("/api/play", methods=["POST"])
def api_play():
    global _last_play_result
    data = request.get_json(force=True) or {}
    audio_path = (data.get("audio_path") or "").strip()
    gong_path  = (data.get("gong_path")  or "").strip() or None
    speakers   = data.get("speakers", [])
    volume     = data.get("volume")

    if not audio_path:
        return jsonify({"success": False, "error": "Kein audio_path angegeben"})
    if not speakers:
        return jsonify({"success": False, "error": "Keine Lautsprecher ausgewaehlt"})

    abs_audio = (BASE_DIR / audio_path).resolve()
    try:
        abs_audio.relative_to(BASE_DIR)
    except ValueError:
        return jsonify({"success": False, "error": "Ungueltiger Pfad (Traversal)"})
    if not abs_audio.exists():
        return jsonify({"success": False, "error": f"Datei nicht gefunden: {audio_path}"})

    if gong_path:
        abs_gong = (BASE_DIR / gong_path).resolve()
        if not abs_gong.exists():
            log.warning(f"Gong nicht gefunden: {gong_path} - ignoriert")
            gong_path = None

    _last_play_result = {"success": None, "error": "laueft...", "url": media_url(audio_path), "ts": time.time()}

    def _bg():
        play_on_sonos(speakers, audio_path, gong_path,
                      int(volume) if volume is not None else None)

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "audio_url": media_url(audio_path),
                    "message": "Durchsage wird abgespielt"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not SOCO_AVAILABLE:
        return jsonify({"success": False, "error": "soco fehlt"})
    data = request.get_json(force=True) or {}
    targets = data.get("speakers") or [s["ip"] for s in _discovered]
    errors = []
    for ip in targets:
        try:
            soco.SoCo(ip).stop()
        except Exception as e:
            errors.append(str(e))
    return jsonify({"success": not errors, "errors": errors})

@app.route("/api/folder/create", methods=["POST"])
def api_folder_create():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "Kein Name"})
    target = (SCHNELL_DIR / name).resolve()
    try:
        target.relative_to(SCHNELL_DIR)
    except ValueError:
        return jsonify({"success": False, "error": "Ungueltiger Pfad"})
    target.mkdir(parents=True, exist_ok=True)
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    _flask_port = port
    # Modul-Variable direkt setzen
    import __main__ as _m
    _m._flask_port = port
    # Auch im aktuellen Modul-Namespace
    import importlib, sys as _sys
    _mod = _sys.modules[__name__]
    _mod._flask_port = port

    ip = local_ip()
    log.info("=" * 60)
    log.info(f"  SonosDurchsage v6")
    log.info(f"  Browser : http://{ip}:{port}")
    log.info(f"  Media   : http://{ip}:{port}/media/")
    log.info(f"  SOCO    : {'OK' if SOCO_AVAILABLE else 'FEHLT -> pip install soco'}")
    log.info(f"  AUDIO   : {'OK' if AUDIO_AVAILABLE else 'FEHLT -> pip install sounddevice soundfile numpy'}")
    log.info("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
