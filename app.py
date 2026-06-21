import os
import time
import wave
import threading
import socket
import urllib.parse
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import TCPServer
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS

# ─── Optionale Imports ───────────────────────────────────────────────────────
try:
    import soco
    SOCO_AVAILABLE = True
except ImportError:
    SOCO_AVAILABLE = False
    print("[WARN] soco nicht installiert. Demo-Modus aktiv.")

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False

try:
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False

# ─── Pfade ───────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent.resolve()
GONGS_DIR      = BASE_DIR / "assets" / "gongs"
RECORDINGS_DIR = BASE_DIR / "assets" / "recordings"
SCHNELL_DIR    = BASE_DIR / "schnelldurchsagen"

for d in [GONGS_DIR, RECORDINGS_DIR,
          SCHNELL_DIR / "Allgemein",
          SCHNELL_DIR / "Notfall",
          SCHNELL_DIR / "Information"]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Lokale IP ───────────────────────────────────────────────────────────────
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

LOCAL_IP   = get_local_ip()
FILE_PORT  = 8080
FLASK_PORT = 5000

# Globales Status-Log fuer die UI
play_log = []

def log(msg, level="info"):
    entry = {"time": time.strftime("%H:%M:%S"), "msg": msg, "level": level}
    play_log.append(entry)
    if len(play_log) > 50:
        play_log.pop(0)
    print(f"[{entry['time']}] {msg}")

# ─── HTTP-Fileserver fuer SONOS ──────────────────────────────────────────────
# Exakt wie das offizielle SoCo-Beispiel:
# https://github.com/SoCo/SoCo/blob/master/examples/play_local_files/play_local_files.py
#
# Der SimpleHTTPRequestHandler serviert Dateien relativ zum aktuellen
# Arbeitsverzeichnis – daher wechseln wir ins BASE_DIR.
# SONOS holt die Datei selbst per HTTP ab, play_uri/add_uri_to_queue
# bekommt nur die URL mitgeteilt.

class QuietHTTPHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler mit reduziertem Logging."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, fmt, *args):
        code = args[1] if len(args) > 1 else "?"
        log(f"[FILESERVER] {self.address_string()} → {fmt % args}",
            level="error" if str(code) not in ("200", "206") else "info")


class ReuseAddrTCPServer(TCPServer):
    allow_reuse_address = True


_file_server = None

def start_file_server():
    global _file_server
    try:
        _file_server = ReuseAddrTCPServer(("0.0.0.0", FILE_PORT), QuietHTTPHandler)
        log(f"Datei-Server laeuft: http://{LOCAL_IP}:{FILE_PORT}/", "info")
        _file_server.serve_forever()
    except Exception as e:
        log(f"Datei-Server FEHLER: {e}", "error")


# ─── URL fuer SONOS bauen ────────────────────────────────────────────────────
def file_url(abs_path: Path) -> str:
    """Baut eine HTTP-URL relativ zu BASE_DIR (fuer SONOS)."""
    rel = abs_path.resolve().relative_to(BASE_DIR)
    # Jeden Pfadteil einzeln URL-encoden, Slashes behalten
    parts = [urllib.parse.quote(p, safe="") for p in rel.parts]
    return f"http://{LOCAL_IP}:{FILE_PORT}/" + "/".join(parts)


# ─── Audio-Dauer ────────────────────────────────────────────────────────────
def audio_duration(p: Path) -> float:
    try:
        if p.suffix.lower() == ".wav":
            with wave.open(str(p), "rb") as wf:
                return wf.getnframes() / float(wf.getframerate())
        return p.stat().st_size / 16000  # MP3 ~128kbps Schaetzung
    except Exception:
        return 3.0


# ─── SONOS Discovery ─────────────────────────────────────────────────────────
def safe_get(device, attr, default=""):
    try:
        return getattr(device, attr)
    except Exception:
        return default


def get_sonos_speakers():
    if not SOCO_AVAILABLE:
        return [
            {"uid": "DEMO-001", "name": "Wohnzimmer (Demo)", "ip": "192.168.1.100", "volume": 30, "model": "SONOS Play:1", "state": "DEMO"},
            {"uid": "DEMO-002", "name": "Kueche (Demo)",     "ip": "192.168.1.101", "volume": 25, "model": "SONOS One",    "state": "DEMO"},
        ]
    speakers = []
    try:
        devices = soco.discover(timeout=5) or []
        for d in devices:
            try:
                model = "SONOS"
                try:
                    info  = d.get_speaker_info(refresh=False)
                    model = info.get("model_name") or info.get("model_number") or "SONOS"
                except Exception:
                    pass

                state = "UNBEKANNT"
                try:
                    ti    = d.get_current_transport_info()
                    state = ti.get("current_transport_state", "UNBEKANNT")
                except Exception:
                    pass

                speakers.append({
                    "uid":    safe_get(d, "uid", d.ip_address),
                    "name":   safe_get(d, "player_name", d.ip_address),
                    "ip":     safe_get(d, "ip_address", ""),
                    "volume": safe_get(d, "volume", 30),
                    "model":  model,
                    "state":  state,
                })
            except Exception as e:
                log(f"Lautsprecher uebersprungen: {e}", "warn")
    except Exception as e:
        log(f"SONOS Discovery FEHLER: {e}", "error")
    return speakers


# ─── Abspielen auf SONOS ─────────────────────────────────────────────────────
def play_on_sonos(speaker_uids, audio_file_path, volume=None, gong_file=None):
    if not SOCO_AVAILABLE:
        log(f"[DEMO] Wuerde abspielen: {audio_file_path}", "info")
        return {"success": True, "message": "Demo-Modus", "log": play_log[-5:]}

    audio_path = Path(audio_file_path).resolve()
    results    = []

    # Test: Ist die Datei vom Fileserver abrufbar?
    audio_url = file_url(audio_path)
    log(f"Audio-URL fuer SONOS: {audio_url}", "info")

    try:
        devices  = soco.discover(timeout=5) or []
        selected = [d for d in devices if safe_get(d, "uid") in speaker_uids]

        if not selected:
            msg = "Keine passenden Lautsprecher gefunden – SONOS Discovery-Timeout?"
            log(msg, "error")
            return {"success": False, "error": msg}

        for device in selected:
            name = safe_get(device, "player_name", device.ip_address)
            try:
                if volume is not None:
                    device.volume = int(volume)
                    log(f"Lautstaerke auf {name}: {volume}", "info")

                # ── Gong ────────────────────────────────────────────────────
                if gong_file:
                    gong_path = GONGS_DIR / gong_file
                    if gong_path.exists():
                        gong_url = file_url(gong_path)
                        log(f"Gong URL: {gong_url}", "info")
                        # Methode: add_uri_to_queue + play_from_queue
                        # (exakt wie offizielles SoCo-Beispiel)
                        idx = device.add_uri_to_queue(gong_url)
                        device.play_from_queue(idx - 1)
                        dur = audio_duration(gong_path)
                        log(f"Gong spielt {dur:.1f}s auf {name}", "info")
                        time.sleep(dur + 1.0)

                # ── Hauptdurchsage ───────────────────────────────────────────
                log(f"Spiele auf {name}: {audio_url}", "info")
                idx = device.add_uri_to_queue(audio_url)
                device.play_from_queue(idx - 1)
                log(f"✓ Befehl gesendet an {name}", "info")

                results.append({"speaker": name, "success": True, "url": audio_url})

            except Exception as e:
                log(f"FEHLER auf {name}: {e}", "error")
                results.append({"speaker": name, "success": False, "error": str(e)})

        return {"success": True, "results": results, "log": play_log[-10:]}

    except Exception as e:
        log(f"Globaler Fehler: {e}", "error")
        return {"success": False, "error": str(e)}


# ─── Ordnerstruktur ──────────────────────────────────────────────────────────
def get_folder_structure(base_path):
    result = []
    base   = Path(base_path)
    if not base.exists():
        return result
    for item in sorted(base.iterdir()):
        if item.is_dir():
            files = [
                {"name": f.stem, "filename": f.name,
                 "path": item.name + "/" + f.name,
                 "size": f.stat().st_size}
                for f in sorted(item.iterdir())
                if f.suffix.lower() in (".mp3", ".wav", ".ogg", ".aac")
            ]
            result.append({"folder": item.name, "files": files})
        elif item.suffix.lower() in (".mp3", ".wav", ".ogg", ".aac"):
            result.append({"folder": "/", "files": [
                {"name": item.stem, "filename": item.name,
                 "path": item.name, "size": item.stat().st_size}
            ]})
    return result


# ─── Aufnahme-State ──────────────────────────────────────────────────────────
recording_state = {"active": False, "frames": [], "stream": None, "audio": None}

# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Echtzeit-Status: Fileserver-URL, Log, Lautsprecher-Zustand."""
    speakers = []
    if SOCO_AVAILABLE:
        try:
            devices = soco.discover(timeout=3) or []
            for d in devices:
                state = "?"
                track = ""
                try:
                    ti    = d.get_current_transport_info()
                    state = ti.get("current_transport_state", "?")
                    tk    = d.get_current_track_info()
                    track = tk.get("title", "")
                except Exception:
                    pass
                speakers.append({
                    "name":  safe_get(d, "player_name", d.ip_address),
                    "state": state,
                    "track": track,
                    "volume": safe_get(d, "volume", 0),
                })
        except Exception as e:
            pass

    return jsonify({
        "fileserver": f"http://{LOCAL_IP}:{FILE_PORT}/",
        "local_ip":   LOCAL_IP,
        "file_port":  FILE_PORT,
        "soco":       SOCO_AVAILABLE,
        "pyaudio":    PYAUDIO_AVAILABLE,
        "speakers":   speakers,
        "log":        play_log[-20:],
    })


@app.route("/api/diagnose")
def api_diagnose():
    """Diagnosetool: prueft ob Fileserver erreichbar und SONOS korrekt"""
    import urllib.request
    results = []

    # Test 1: Fileserver selbst erreichbar?
    test_url = f"http://{LOCAL_IP}:{FILE_PORT}/"
    try:
        resp = urllib.request.urlopen(test_url, timeout=2)
        results.append({"test": "Fileserver erreichbar", "ok": True, "detail": str(resp.status)})
    except Exception as e:
        results.append({"test": "Fileserver erreichbar", "ok": False, "detail": str(e)})

    # Test 2: Erste Audiodatei erreichbar?
    audio_files = list(SCHNELL_DIR.rglob("*.mp3")) + list(SCHNELL_DIR.rglob("*.wav"))
    if audio_files:
        url = file_url(audio_files[0])
        try:
            req  = urllib.request.Request(url, method="HEAD")
            resp = urllib.request.urlopen(req, timeout=2)
            results.append({"test": f"Audiodatei abrufbar: {audio_files[0].name}",
                            "ok": True, "detail": f"HTTP {resp.status}, URL: {url}"})
        except Exception as e:
            results.append({"test": f"Audiodatei abrufbar: {audio_files[0].name}",
                            "ok": False, "detail": f"{e} | URL: {url}"})
    else:
        results.append({"test": "Audiodatei vorhanden", "ok": False,
                        "detail": "Keine MP3/WAV in schnelldurchsagen/ gefunden"})

    # Test 3: SONOS Discovery
    if SOCO_AVAILABLE:
        try:
            devices = soco.discover(timeout=4) or []
            names   = [safe_get(d, "player_name", d.ip_address) for d in devices]
            results.append({"test": "SONOS Discovery",
                            "ok": len(devices) > 0,
                            "detail": f"{len(devices)} Geraete: {names}"})
        except Exception as e:
            results.append({"test": "SONOS Discovery", "ok": False, "detail": str(e)})
    else:
        results.append({"test": "SONOS Discovery", "ok": False, "detail": "soco nicht installiert"})

    return jsonify({"results": results, "fileserver_url": f"http://{LOCAL_IP}:{FILE_PORT}/",
                    "local_ip": LOCAL_IP})


@app.route("/api/speakers")
def api_speakers():
    return jsonify({"speakers": get_sonos_speakers()})


@app.route("/api/speakers/<uid>/volume", methods=["POST"])
def api_set_volume(uid):
    volume = (request.get_json() or {}).get("volume", 30)
    if not SOCO_AVAILABLE:
        return jsonify({"success": True})
    try:
        for d in (soco.discover(timeout=3) or []):
            if safe_get(d, "uid") == uid:
                d.volume = int(volume)
                return jsonify({"success": True})
        return jsonify({"success": False, "error": "Nicht gefunden"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/gongs")
def api_gongs():
    if not GONGS_DIR.exists():
        return jsonify({"gongs": []})
    return jsonify({"gongs": [
        {"name": f.stem, "filename": f.name}
        for f in sorted(GONGS_DIR.iterdir())
        if f.suffix.lower() in (".mp3", ".wav", ".ogg")
    ]})


@app.route("/api/schnelldurchsagen")
def api_schnelldurchsagen():
    return jsonify({"folders": get_folder_structure(SCHNELL_DIR)})


@app.route("/api/recordings")
def api_recordings():
    recs = []
    if RECORDINGS_DIR.exists():
        for f in sorted(RECORDINGS_DIR.iterdir(), reverse=True):
            if f.suffix.lower() in (".mp3", ".wav"):
                recs.append({"name": f.stem, "filename": f.name,
                             "path": f"assets/recordings/{f.name}",
                             "size": f.stat().st_size, "mtime": f.stat().st_mtime})
    return jsonify({"recordings": recs})


@app.route("/api/play", methods=["POST"])
def api_play():
    data         = request.get_json() or {}
    speaker_uids = data.get("speakers", [])
    audio_path   = data.get("path", "")
    volume       = data.get("volume")
    gong_file    = data.get("gong")

    if not speaker_uids:
        return jsonify({"success": False, "error": "Kein Lautsprecher ausgewaehlt"})
    if not audio_path:
        return jsonify({"success": False, "error": "Keine Audiodatei angegeben"})

    full_path = SCHNELL_DIR / audio_path
    if not full_path.exists():
        full_path = BASE_DIR / audio_path
    if not full_path.exists():
        return jsonify({"success": False, "error": f"Datei nicht gefunden: {audio_path}"})

    return jsonify(play_on_sonos(speaker_uids, str(full_path), volume, gong_file))


# ─── Aufnahme ────────────────────────────────────────────────────────────────
@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    if not PYAUDIO_AVAILABLE:
        return jsonify({"success": False, "error": "pyaudio fehlt – pip install pyaudio"})
    if recording_state["active"]:
        return jsonify({"success": False, "error": "Laeuft bereits"})
    try:
        pa     = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=44100,
                         input=True, frames_per_buffer=1024)
        recording_state.update({"active": True, "frames": [], "stream": stream, "audio": pa})
        def _rec():
            while recording_state["active"]:
                try:
                    recording_state["frames"].append(stream.read(1024, exception_on_overflow=False))
                except Exception:
                    break
        threading.Thread(target=_rec, daemon=True).start()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    if not recording_state["active"]:
        return jsonify({"success": False, "error": "Keine aktive Aufnahme"})
    recording_state["active"] = False
    time.sleep(0.2)
    try:
        s = recording_state["stream"]
        p = recording_state["audio"]
        if s: s.stop_stream(); s.close()
        if p: p.terminate()
        ts  = time.strftime("%Y%m%d_%H%M%S")
        wav = RECORDINGS_DIR / f"aufnahme_{ts}.wav"
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"".join(recording_state["frames"]))
        final = wav
        if PYDUB_AVAILABLE:
            try:
                mp3 = RECORDINGS_DIR / f"aufnahme_{ts}.mp3"
                AudioSegment.from_wav(str(wav)).export(str(mp3), format="mp3")
                wav.unlink(); final = mp3
            except Exception:
                pass
        rel = f"assets/recordings/{final.name}"
        recording_state["last_file"] = rel
        return jsonify({"success": True, "filename": final.name, "path": rel,
                        "duration": len(recording_state["frames"]) * 1024 / 44100})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/audio/gongs/<filename>")
def serve_gong(filename):
    return send_from_directory(str(GONGS_DIR), filename)

@app.route("/audio/recordings/<filename>")
def serve_recording(filename):
    return send_from_directory(str(RECORDINGS_DIR), filename)


# ─── Start ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=start_file_server, daemon=True).start()
    time.sleep(0.5)
    print("\n🔊 SONOS Durchsagesystem")
    print(f"   Web-UI:       http://localhost:{FLASK_PORT}")
    print(f"   Datei-Server: http://{LOCAL_IP}:{FILE_PORT}/  ← SONOS laedt hier")
    print(f"   Diagnose:     http://localhost:{FLASK_PORT}/api/diagnose")
    print(f"   Status:       http://localhost:{FLASK_PORT}/api/status")
    print(f"   SONOS:        {'OK' if SOCO_AVAILABLE else 'FEHLT – pip install soco'}")
    print(f"   Aufnahme:     {'OK' if PYAUDIO_AVAILABLE else 'FEHLT – pip install pyaudio'}\n")
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
