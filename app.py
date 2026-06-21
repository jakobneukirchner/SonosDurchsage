#!/usr/bin/env python3
"""
SonosDurchsage v7
- Automatische Firewall-Freischaltung (Windows/macOS/Linux)
- Port-Bug gefixt: FLASK_PORT als echte Modul-Variable
- Medienserver auf separatem Port (5001) fuer sauberere Trennung
"""
import os, sys, time, socket, threading, mimetypes, logging, subprocess, platform
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
_discovered       = []
_last_play_result = {"success": None, "error": "", "url": "", "ts": 0}

# Ports als echte Modul-Variablen (kein Shadowing-Problem)
FLASK_PORT = int(os.environ.get("SONOS_PORT",  "5000"))
MEDIA_PORT = int(os.environ.get("SONOS_MEDIA", "5001"))  # separater Medienport

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
MIME_MAP   = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".ogg": "audio/ogg",  ".flac": "audio/flac",
    ".m4a": "audio/mp4",  ".aac": "audio/aac",
}

# ─────────────────────────────────────────────
# FIREWALL-HILFSFUNKTIONEN
# ─────────────────────────────────────────────
def _fw_windows(port: int):
    """Schaltet Port in der Windows-Firewall frei – lautlos, kein Dialog."""
    name = f"SonosDurchsage-{port}"
    # Erst pruefen ob Regel schon existiert
    check = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
        capture_output=True, text=True
    )
    if "No rules match" in check.stdout or check.returncode != 0:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name={name}", "dir=in", "action=allow",
             "protocol=TCP", f"localport={port}"],
            capture_output=True
        )
        log.info(f"  [Firewall] Windows-Regel fuer Port {port} erstellt")
    else:
        log.info(f"  [Firewall] Windows-Regel fuer Port {port} existiert bereits")

def _fw_macos(port: int):
    """Auf macOS reicht es den Socket zu oeffnen; PF-Firewall blockiert
       normalerweise nicht LAN-Traffic. Wir pruefen trotzdem und loggen."""
    result = subprocess.run(
        ["sudo", "-n", "pfctl", "-s", "rules"],
        capture_output=True, text=True
    )
    if str(port) not in (result.stdout or ""):
        log.info(f"  [Firewall] macOS: Port {port} – kein PF-Block erkannt.")
        log.info(f"  [Firewall] Falls SONOS nicht erreicht: Systemeinstellungen -> Firewall -> Python zulassen")
    else:
        log.info(f"  [Firewall] macOS: Port {port} moeglicherweise blockiert, bitte Python in Firewall-Einstellungen erlauben")

def _fw_linux(port: int):
    """UFW-Freischaltung auf Linux."""
    r = subprocess.run(["sudo", "-n", "ufw", "allow", str(port)+"/tcp"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        log.info(f"  [Firewall] Linux UFW: Port {port} freigegeben")
    else:
        # iptables fallback
        subprocess.run(
            ["sudo", "-n", "iptables", "-I", "INPUT", "-p", "tcp",
             "--dport", str(port), "-j", "ACCEPT"],
            capture_output=True
        )
        log.info(f"  [Firewall] Linux iptables: Port {port} freigegeben")

def open_firewall(port: int):
    """Erkennt OS automatisch und schaltet Port frei."""
    os_name = platform.system()
    try:
        if os_name == "Windows":
            _fw_windows(port)
        elif os_name == "Darwin":
            _fw_macos(port)
        elif os_name == "Linux":
            _fw_linux(port)
        else:
            log.info(f"  [Firewall] Unbekanntes OS ({os_name}) – bitte Port {port} manuell freigeben")
    except Exception as e:
        log.warning(f"  [Firewall] Fehler beim Freischalten: {e}")
        log.warning(f"  [Firewall] Bitte manuell Port {port} in der Firewall freischalten")

# ─────────────────────────────────────────────
# NETZWERK
# ─────────────────────────────────────────────
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
    """Baut die URL die SONOS zum Abrufen nutzt – benutzt MEDIA_PORT."""
    clean = rel_path.lstrip("/").replace("\\", "/")
    url = f"http://{local_ip()}:{MEDIA_PORT}/media/{clean}"
    log.info(f"  [media_url] {url}")
    return url

# ─────────────────────────────────────────────
# MEDIEN-ROUTE (wird auch auf MEDIA_PORT gehostet)
# ─────────────────────────────────────────────
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
    ext  = target.suffix.lower()
    mime = MIME_MAP.get(ext, mimetypes.guess_type(str(target))[0] or "application/octet-stream")
    log.info(f"SONOS streamt: {filename} [{mime}]")
    return send_from_directory(str(target.parent), target.name, mimetype=mime, conditional=True)

# ─────────────────────────────────────────────
# SONOS
# ─────────────────────────────────────────────
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
                    "ip":     d.ip_address,
                    "name":   d.player_name,
                    "volume": d.volume,
                    "model":  getattr(d, 'model_name', 'SONOS'),
                    "group":  d.group.coordinator.player_name if d.group else "Solo"
                })
                log.info(f"  Gefunden: {d.player_name} ({d.ip_address})")
            except Exception as e:
                log.warning(f"  {d.ip_address}: {e}")
        return _discovered
    except Exception as e:
        log.error(f"Discovery-Fehler: {e}")
        return []

def _wait_until_done(dev, timeout=30):
    deadline = time.time() + timeout
    time.sleep(1.0)
    while time.time() < deadline:
        try:
            state = dev.get_current_transport_info()['current_transport_state']
            if state not in ('PLAYING', 'TRANSITIONING'):
                return
        except Exception:
            return
        time.sleep(0.5)

def play_on_sonos(speaker_ips, audio_path, gong_path=None, volume=None):
    global _last_play_result
    if not SOCO_AVAILABLE:
        _last_play_result = {"success": False, "error": "soco nicht installiert", "url": "", "ts": time.time()}
        return _last_play_result

    # Datei-Existenz pruefen
    abs_audio = (BASE_DIR / audio_path).resolve()
    if not abs_audio.exists():
        msg = f"Datei nicht gefunden: {audio_path}"
        log.error(msg)
        _last_play_result = {"success": False, "error": msg, "url": "", "ts": time.time()}
        return _last_play_result

    errors = []
    a_url  = media_url(audio_path)
    g_url  = media_url(gong_path) if gong_path else None

    log.info(f"=== PLAY START ===")
    log.info(f"  Audio-URL : {a_url}")
    if g_url: log.info(f"  Gong-URL  : {g_url}")
    log.info(f"  Sprecher  : {speaker_ips}")

    # Eigene URL erreichbarkeit kurz pruefen
    try:
        import urllib.request
        req = urllib.request.Request(a_url, method='HEAD')
        urllib.request.urlopen(req, timeout=3)
        log.info("  [self-check] Medienserver erreichbar ✓")
    except Exception as se:
        log.warning(f"  [self-check] Medienserver-Selbsttest fehlgeschlagen: {se}")
        log.warning(f"  [self-check] SONOS wird es trotzdem versuchen...")

    for ip in speaker_ips:
        try:
            dev = soco.SoCo(ip)
            if volume is not None:
                try:
                    dev.volume = max(0, min(100, int(volume)))
                except Exception as ve:
                    log.warning(f"Vol {ip}: {ve}")

            if g_url:
                try:
                    log.info(f"[{ip}] Gong...")
                    dev.play_uri(g_url, title="Gong")
                    _wait_until_done(dev, timeout=30)
                    log.info(f"[{ip}] Gong fertig")
                except Exception as ge:
                    log.warning(f"[{ip}] Gong-Fehler (ignoriert): {ge}")

            log.info(f"[{ip}] Durchsage: {a_url}")
            dev.play_uri(a_url, title="Durchsage")
            log.info(f"[{ip}] play_uri OK")
        except Exception as e:
            msg = f"{ip}: {type(e).__name__}: {e}"
            log.error(f"FEHLER: {msg}")
            errors.append(msg)

    result = {"success": len(errors) == 0, "errors": errors, "url": a_url, "ts": time.time()}
    _last_play_result = result
    return result

# ─────────────────────────────────────────────
# HILFSFUNKTIONEN
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify({
        "soco":       SOCO_AVAILABLE,
        "audio":      AUDIO_AVAILABLE,
        "ip":         local_ip(),
        "port":       FLASK_PORT,
        "media_port": MEDIA_PORT,
        "version":    "7.0",
        "media_base": f"http://{local_ip()}:{MEDIA_PORT}/media/",
        "os":         platform.system()
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

@app.route("/api/speakers/<ip>/volume", methods=["POST"])
def api_volume(ip):
    if not SOCO_AVAILABLE:
        return jsonify({"success": False, "error": "soco fehlt"})
    data = request.get_json(force=True) or {}
    vol  = int(data.get("volume", 50))
    try:
        soco.SoCo(ip).volume = max(0, min(100, vol))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

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
        for f in sorted(RECORDINGS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
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
    data       = request.get_json(force=True) or {}
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
        return jsonify({"success": False, "error": "Ungueltiger Pfad"})
    if not abs_audio.exists():
        return jsonify({"success": False, "error": f"Datei nicht gefunden: {audio_path}"})

    if gong_path:
        abs_gong = (BASE_DIR / gong_path).resolve()
        if not abs_gong.exists():
            log.warning(f"Gong nicht gefunden: {gong_path} – ignoriert")
            gong_path = None

    _last_play_result = {"success": None, "error": "laeuft...", "url": media_url(audio_path), "ts": time.time()}

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
    data    = request.get_json(force=True) or {}
    targets = data.get("speakers") or [s["ip"] for s in _discovered]
    errors  = []
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

@app.route("/api/upload", methods=["POST"])
def api_upload():
    folder = request.form.get("folder", "").strip()
    if "file" not in request.files:
        return jsonify({"success": False, "error": "Keine Datei"})
    f = request.files["file"]
    if not f.filename:
        return jsonify({"success": False, "error": "Kein Dateiname"})
    ext = Path(f.filename).suffix.lower()
    if ext not in AUDIO_EXTS:
        return jsonify({"success": False, "error": "Nur Audiodateien erlaubt"})
    dest_dir = (SCHNELL_DIR / folder).resolve() if folder else SCHNELL_DIR
    try:
        dest_dir.relative_to(SCHNELL_DIR)
    except ValueError:
        return jsonify({"success": False, "error": "Ungueltiger Ordner"})
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f.filename
    f.save(str(dest))
    rel = "schnelldurchsagen/" + str(dest.relative_to(SCHNELL_DIR)).replace("\\", "/")
    return jsonify({"success": True, "path": rel})

@app.route("/api/firewall-test")
def api_firewall_test():
    """Prueft ob der Medienport von aussen erreichbar scheint."""
    ip = local_ip()
    return jsonify({
        "media_url_example": f"http://{ip}:{MEDIA_PORT}/media/",
        "tip": f"Teste im Browser eines anderen Geraets: http://{ip}:{MEDIA_PORT}/api/status",
        "os": platform.system(),
        "media_port": MEDIA_PORT,
        "flask_port": FLASK_PORT
    })

# ─────────────────────────────────────────────
# START
# ─────────────────────────────────────────────
if __name__ == "__main__":
    global FLASK_PORT, MEDIA_PORT
    if len(sys.argv) > 1:
        FLASK_PORT = int(sys.argv[1])
    if len(sys.argv) > 2:
        MEDIA_PORT = int(sys.argv[2])

    ip = local_ip()

    log.info("=" * 60)
    log.info(f"  SonosDurchsage v7  |  OS: {platform.system()}")
    log.info(f"  Browser  : http://{ip}:{FLASK_PORT}")
    log.info(f"  Media    : http://{ip}:{MEDIA_PORT}/media/")
    log.info(f"  SOCO     : {'OK' if SOCO_AVAILABLE else 'FEHLT -> pip install soco'}")
    log.info(f"  AUDIO    : {'OK' if AUDIO_AVAILABLE else 'FEHLT -> pip install sounddevice soundfile numpy'}")
    log.info("=" * 60)

    # Firewall automatisch freischalten fuer BEIDE Ports
    log.info("[Firewall] Oeffne Ports automatisch...")
    open_firewall(FLASK_PORT)
    open_firewall(MEDIA_PORT)
    log.info("[Firewall] Fertig.")
    log.info("=" * 60)

    # Flask auf beiden Ports starten:
    # FLASK_PORT fuer Browser-UI
    # MEDIA_PORT fuer SONOS-Medienstreaming (separater Thread)
    def run_media_server():
        """Identische Flask-App aber auf MEDIA_PORT – nur fuer Medien-Requests."""
        from werkzeug.serving import make_server
        srv = make_server("0.0.0.0", MEDIA_PORT, app)
        log.info(f"[Media] Server gestartet auf Port {MEDIA_PORT}")
        srv.serve_forever()

    if MEDIA_PORT != FLASK_PORT:
        t = threading.Thread(target=run_media_server, daemon=True)
        t.start()
        log.info(f"[Media] Separater Medienserver laeuft auf Port {MEDIA_PORT}")

    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, threaded=True)
