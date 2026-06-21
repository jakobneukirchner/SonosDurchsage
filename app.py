#!/usr/bin/env python3
"""
SonosDurchsage v4
- _flask_port wird beim Import gesetzt (nicht erst in __main__)
- MIME-Typen korrekt gesetzt (SONOS braucht Content-Type)
- Robustes SONOS-Playback mit Fehlerdiagnose
- /api/test-url zum Debuggen der Erreichbarkeit
"""

import os, sys, time, socket, threading, mimetypes, logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, abort, Response
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

try:
    import soco
    from soco.exceptions import SoCoException
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
    log.warning("sounddevice/soundfile/numpy fehlt")

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
_discovered       = []

# Port wird SOFORT beim Modulimport gesetzt - nicht erst in main()
_flask_port = int(os.environ.get("SONOS_PORT", "5000"))

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}

MIME_MAP = {
    ".mp3":  "audio/mpeg",
    ".wav":  "audio/wav",
    ".ogg":  "audio/ogg",
    ".flac": "audio/flac",
    ".m4a":  "audio/mp4",
    ".aac":  "audio/aac",
}


# ---------------------------------------------------------------------------
# Netzwerk-Hilfsfunktionen
# ---------------------------------------------------------------------------

def local_ip() -> str:
    """Eigene LAN-IP ermitteln (kein Loopback)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def media_url(rel_path: str) -> str:
    """Vollstaendige HTTP-URL, die SONOS per GET abrufen kann."""
    clean = rel_path.lstrip("/").replace("\\", "/")
    url = f"http://{local_ip()}:{_flask_port}/media/{clean}"
    log.info(f"media_url -> {url}")
    return url


# ---------------------------------------------------------------------------
# Media-Route  (mit korrektem Content-Type fuer SONOS)
# ---------------------------------------------------------------------------

@app.route("/media/<path:filename>")
def serve_media(filename):
    """Audiodateien an SONOS ausliefern - mit korrektem MIME-Type."""
    target = (BASE_DIR / filename).resolve()
    # Pfad-Traversal verhindern
    try:
        target.relative_to(BASE_DIR)
    except ValueError:
        abort(403)
    if not target.exists():
        log.warning(f"/media/ 404: {filename}")
        abort(404)

    ext      = target.suffix.lower()
    mimetype = MIME_MAP.get(ext, mimetypes.guess_type(str(target))[0] or "application/octet-stream")
    log.info(f"SONOS abruft: {filename}  [{mimetype}]")
    return send_from_directory(
        str(target.parent),
        target.name,
        mimetype=mimetype,
        conditional=True      # Range-Requests unterstuetzen (SONOS nutzt sie)
    )


# ---------------------------------------------------------------------------
# SONOS Discovery
# ---------------------------------------------------------------------------

def discover_sonos(timeout: int = 8) -> list:
    global _discovered
    if not SOCO_AVAILABLE:
        return []
    try:
        devices = list(soco.discover(timeout=timeout) or [])
        _discovered = []
        for d in devices:
            try:
                info = {
                    "ip":     d.ip_address,
                    "name":   d.player_name,
                    "volume": d.volume,
                    "model":  getattr(d, 'model_name', 'SONOS'),
                    "group":  d.group.coordinator.player_name if d.group else "Solo"
                }
                _discovered.append(info)
                log.info(f"  Gefunden: {info['name']} ({info['ip']})")
            except Exception as e:
                log.warning(f"  Geraet {d.ip_address} Info-Fehler: {e}")
        return _discovered
    except Exception as e:
        log.error(f"Discovery: {e}")
        return []


# ---------------------------------------------------------------------------
# SONOS Abspielen  (robuste Version)
# ---------------------------------------------------------------------------

def play_on_sonos(speaker_ips: list, audio_path: str,
                  gong_path: str | None = None,
                  volume: int | None    = None) -> dict:
    """
    Spielt eine Audiodatei auf den angegebenen SONOS-Geraeten ab.
    Gibt dict mit success/errors zurueck.
    """
    if not SOCO_AVAILABLE:
        return {"success": False, "error": "soco nicht installiert"}

    errors = []
    for ip in speaker_ips:
        try:
            dev = soco.SoCo(ip)

            # --- Lautstaerke setzen ---
            if volume is not None:
                try:
                    dev.volume = max(0, min(100, int(volume)))
                except Exception as ve:
                    log.warning(f"Lautstaerke {ip}: {ve}")

            # --- Gong ---
            if gong_path:
                g_url = media_url(gong_path)
                log.info(f"[{ip}] Gong: {g_url}")
                try:
                    dev.play_uri(g_url, title="Gong")
                    _wait_until_done(dev, timeout=15)
                except Exception as ge:
                    log.warning(f"[{ip}] Gong fehlgeschlagen: {ge}")
                    # Weitermachen auch wenn Gong nicht klappt

            # --- Durchsage ---
            a_url = media_url(audio_path)
            log.info(f"[{ip}] Durchsage: {a_url}")
            dev.play_uri(a_url, title="Durchsage")
            log.info(f"[{ip}] play_uri erfolgreich aufgerufen")

        except Exception as e:
            msg = f"{ip}: {e}"
            log.error(f"Abspielfehler: {msg}")
            errors.append(msg)

    return {"success": len(errors) == 0, "errors": errors}


def _wait_until_done(dev, timeout: int = 15) -> None:
    """Blockiert bis SONOS aufhoert zu spielen oder Timeout."""
    deadline = time.time() + timeout
    time.sleep(0.4)   # kurz warten, bis SONOS den Zustand wechselt
    while time.time() < deadline:
        try:
            state = dev.get_current_transport_info()['current_transport_state']
            if state not in ('PLAYING', 'TRANSITIONING'):
                return
        except Exception:
            return
        time.sleep(0.3)


# ---------------------------------------------------------------------------
# Dateibaum
# ---------------------------------------------------------------------------

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
                "path": "schnelldurchsagen/" + str(
                    item.relative_to(SCHNELL_DIR)).replace("\\", "/"),
                "size": item.stat().st_size
            })
    return node


# ---------------------------------------------------------------------------
# REST-API
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "soco":    SOCO_AVAILABLE,
        "audio":   AUDIO_AVAILABLE,
        "ip":      local_ip(),
        "port":    _flask_port,
        "version": "4.0"
    })


@app.route("/api/test-url")
def api_test_url():
    """Gibt zurueck, welche URL SONOS benutzen wuerde."""
    return jsonify({"base_url": f"http://{local_ip()}:{_flask_port}/media/"})


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
    return jsonify({"success": True, "filename": fname,
                    "path": f"assets/recordings/{fname}"})


@app.route("/api/play", methods=["POST"])
def api_play():
    data       = request.get_json(force=True) or {}
    audio_path = (data.get("audio_path") or "").strip()
    gong_path  = (data.get("gong_path")  or "").strip() or None
    speakers   = data.get("speakers", [])
    volume     = data.get("volume")

    if not audio_path:
        return jsonify({"success": False, "error": "Kein Audiodatei-Pfad angegeben"})
    if not speakers:
        return jsonify({"success": False, "error": "Keine Lautsprecher ausgewaehlt"})

    abs_audio = (BASE_DIR / audio_path).resolve()
    try:
        abs_audio.relative_to(BASE_DIR)
    except ValueError:
        return jsonify({"success": False, "error": "Ungueltiger Pfad"})
    if not abs_audio.exists():
        return jsonify({"success": False,
                        "error": f"Datei nicht gefunden: {audio_path}"})

    if gong_path and not (BASE_DIR / gong_path).resolve().exists():
        log.warning(f"Gong nicht gefunden: {gong_path} - wird ignoriert")
        gong_path = None

    audio_url = media_url(audio_path)

    def _bg():
        result = play_on_sonos(speakers, audio_path, gong_path,
                               int(volume) if volume is not None else None)
        if result["errors"]:
            log.error(f"Abspielfehler: {result['errors']}")

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "audio_url": audio_url})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not SOCO_AVAILABLE:
        return jsonify({"success": False, "error": "soco fehlt"})
    data    = request.get_json(force=True) or {}
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
    data = request.get_json(force=True) or {}
    try:
        soco.SoCo(data["ip"]).volume = max(0, min(100, int(data.get("volume", 30))))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


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


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    # Setzt den globalen Port BEVOR der erste Request kommt
    import app as self_module
    self_module._flask_port = port
    globals()['_flask_port'] = port

    ip = local_ip()
    log.info("=" * 60)
    log.info("  SonosDurchsage v4")
    log.info(f"  Browser : http://{ip}:{port}")
    log.info(f"  Media   : http://{ip}:{port}/media/<pfad>")
    log.info(f"  Test-URL: http://{ip}:{port}/api/test-url")
    log.info(f"  SOCO    : {'OK' if SOCO_AVAILABLE else 'FEHLT  -> pip install soco'}")
    log.info(f"  AUDIO   : {'OK' if AUDIO_AVAILABLE else 'FEHLT  -> pip install sounddevice soundfile numpy'}")
    log.info("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
