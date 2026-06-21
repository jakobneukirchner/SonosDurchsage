import os
import time
import wave
import threading
import socket
import urllib.parse
import mimetypes
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
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
    print("[WARN] pyaudio nicht installiert. Aufnahme deaktiviert.")

try:
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    print("[WARN] pydub nicht installiert. WAV-Konvertierung deaktiviert.")

# ─── Pfade ───────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
GONGS_DIR      = BASE_DIR / "assets" / "gongs"
RECORDINGS_DIR = BASE_DIR / "assets" / "recordings"
SCHNELL_DIR    = BASE_DIR / "schnelldurchsagen"

for d in [GONGS_DIR, RECORDINGS_DIR,
          SCHNELL_DIR / "Allgemein",
          SCHNELL_DIR / "Notfall",
          SCHNELL_DIR / "Information"]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Lokale IP ermitteln ─────────────────────────────────────────────────────
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
FILE_PORT  = 8080   # Dedizierter Datei-Server für SONOS
FLASK_PORT = 5000

# ─── Dedizierter HTTP-Fileserver für SONOS ───────────────────────────────────
# SONOS braucht einen simplen HTTP-Server ohne Redirects, mit korrektem
# Content-Type und ohne Transfer-Encoding: chunked.
# Flask's send_from_directory gibt manchmal 301-Redirects → SONOS bricht ab.

class SonosFileHandler(BaseHTTPRequestHandler):
    """Minimaler HTTP-Handler: gibt Audiodateien direkt aus BASE_DIR aus."""

    def log_message(self, format, *args):
        # Nur Fehler loggen
        if args and str(args[1]) not in ("200", "206"):
            print(f"[FILESERVER] {format % args}")

    def do_GET(self):
        # URL-Pfad dekodieren und in Dateisystempfad umwandeln
        url_path = urllib.parse.unquote(self.path.lstrip("/"))
        file_path = BASE_DIR / url_path

        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, f"Datei nicht gefunden: {url_path}")
            return

        # Sicherheitscheck: nur innerhalb BASE_DIR
        try:
            file_path.relative_to(BASE_DIR)
        except ValueError:
            self.send_error(403, "Zugriff verweigert")
            return

        # MIME-Type bestimmen
        mime, _ = mimetypes.guess_type(str(file_path))
        if not mime:
            ext = file_path.suffix.lower()
            mime = {
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
                ".ogg": "audio/ogg",
                ".aac": "audio/aac",
                ".m4a": "audio/mp4",
            }.get(ext, "application/octet-stream")

        file_size = file_path.stat().st_size

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "close")
        self.end_headers()

        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    break

    def do_HEAD(self):
        url_path = urllib.parse.unquote(self.path.lstrip("/"))
        file_path = BASE_DIR / url_path
        if not file_path.exists():
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(str(file_path))
        if not mime:
            mime = "audio/mpeg"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()


def start_file_server():
    """Startet den Datei-Server in einem Daemon-Thread."""
    server = HTTPServer(("0.0.0.0", FILE_PORT), SonosFileHandler)
    print(f"[FILESERVER] Läuft auf http://{LOCAL_IP}:{FILE_PORT}/")
    server.serve_forever()


# ─── Aufnahme-State ──────────────────────────────────────────────────────────
recording_state = {
    "active": False,
    "frames": [],
    "stream": None,
    "audio": None,
    "last_file": None,
}

# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

def safe_get(device, attr, default=""):
    try:
        return getattr(device, attr)
    except Exception:
        return default


def file_url(abs_path: Path) -> str:
    """Erstellt eine SONOS-kompatible HTTP-URL für eine lokale Datei."""
    rel = abs_path.relative_to(BASE_DIR)
    # Unter Windows Backslashes ersetzen, dann URL-encoden
    encoded = urllib.parse.quote(rel.as_posix())
    return f"http://{LOCAL_IP}:{FILE_PORT}/{encoded}"


def get_audio_duration(filepath: Path) -> float:
    try:
        with wave.open(str(filepath), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 3.0


def get_mp3_duration_approx(filepath: Path) -> float:
    """Grobe Schätzung der MP3-Dauer über Dateigröße (128 kbps)."""
    try:
        return filepath.stat().st_size / 16000  # 128kbps = 16000 bytes/s
    except Exception:
        return 3.0


def audio_duration(filepath: Path) -> float:
    if filepath.suffix.lower() == ".wav":
        return get_audio_duration(filepath)
    return get_mp3_duration_approx(filepath)


def get_sonos_speakers():
    if not SOCO_AVAILABLE:
        return [
            {"uid": "DEMO-001", "name": "Wohnzimmer (Demo)", "ip": "192.168.1.100", "volume": 30, "model": "SONOS Play:1"},
            {"uid": "DEMO-002", "name": "Küche (Demo)",      "ip": "192.168.1.101", "volume": 25, "model": "SONOS One"},
        ]
    speakers = []
    try:
        devices = soco.discover(timeout=5) or []
        for device in devices:
            try:
                model = "SONOS"
                try:
                    info  = device.get_speaker_info(refresh=False)
                    model = info.get("model_name") or info.get("model_number") or "SONOS"
                except Exception:
                    pass
                speakers.append({
                    "uid":    safe_get(device, "uid", device.ip_address),
                    "name":   safe_get(device, "player_name", device.ip_address),
                    "ip":     safe_get(device, "ip_address", ""),
                    "volume": safe_get(device, "volume", 30),
                    "model":  model,
                })
            except Exception as e:
                print(f"[WARN] Lautsprecher übersprungen: {e}")
    except Exception as e:
        print(f"[ERROR] SONOS Discovery: {e}")
    return speakers


def play_on_sonos(speaker_uids, audio_file_path, volume=None, gong_file=None):
    if not SOCO_AVAILABLE:
        print(f"[DEMO] Abspielen: {audio_file_path}")
        return {"success": True, "message": "Demo-Modus"}

    audio_path = Path(audio_file_path)
    results = []

    try:
        devices  = soco.discover(timeout=5) or []
        selected = [d for d in devices if safe_get(d, "uid") in speaker_uids]

        if not selected:
            return {"success": False, "error": "Keine passenden Lautsprecher gefunden (Discovery-Timeout?)"}

        for device in selected:
            try:
                # Lautstärke setzen
                if volume is not None:
                    device.volume = int(volume)

                # ── Gong abspielen ──────────────────────────────────────────
                if gong_file:
                    gong_path = GONGS_DIR / gong_file
                    if gong_path.exists():
                        gong_url = file_url(gong_path)
                        print(f"[PLAY] Gong URL: {gong_url}")
                        device.play_uri(gong_url, title="Gong")
                        dur = audio_duration(gong_path)
                        time.sleep(dur + 0.8)

                # ── Hauptdurchsage ──────────────────────────────────────────
                audio_url = file_url(audio_path)
                print(f"[PLAY] Audio URL: {audio_url}")

                # play_uri ist die zuverlässigste Methode für einzelne Dateien
                device.play_uri(audio_url, title=audio_path.stem)

                results.append({"speaker": safe_get(device, "player_name", device.ip_address), "success": True})

            except Exception as e:
                print(f"[ERROR] Abspielen auf {safe_get(device, 'player_name', '?')}: {e}")
                results.append({"speaker": safe_get(device, "player_name", "?"), "success": False, "error": str(e)})

        return {"success": True, "results": results}

    except Exception as e:
        return {"success": False, "error": str(e)}


def get_folder_structure(base_path):
    result = []
    base = Path(base_path)
    if not base.exists():
        return result
    for item in sorted(base.iterdir()):
        if item.is_dir():
            files = [
                {
                    "name":     f.stem,
                    "filename": f.name,
                    "path":     item.name + "/" + f.name,
                    "size":     f.stat().st_size,
                }
                for f in sorted(item.iterdir())
                if f.suffix.lower() in (".mp3", ".wav", ".ogg", ".aac")
            ]
            result.append({"folder": item.name, "files": files})
        elif item.suffix.lower() in (".mp3", ".wav", ".ogg", ".aac"):
            result.append({
                "folder": "/",
                "files":  [{"name": item.stem, "filename": item.name,
                             "path": item.name, "size": item.stat().st_size}],
            })
    return result


# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)


@app.route("/")
def index():
    return render_template("index.html")


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
    gongs = [
        {"name": f.stem, "filename": f.name}
        for f in sorted(GONGS_DIR.iterdir())
        if f.suffix.lower() in (".mp3", ".wav", ".ogg")
    ] if GONGS_DIR.exists() else []
    return jsonify({"gongs": gongs})


@app.route("/api/schnelldurchsagen")
def api_schnelldurchsagen():
    return jsonify({"folders": get_folder_structure(SCHNELL_DIR)})


@app.route("/api/recordings")
def api_recordings():
    recs = []
    if RECORDINGS_DIR.exists():
        for f in sorted(RECORDINGS_DIR.iterdir(), reverse=True):
            if f.suffix.lower() in (".mp3", ".wav"):
                recs.append({
                    "name":     f.stem,
                    "filename": f.name,
                    "path":     f"assets/recordings/{f.name}",
                    "size":     f.stat().st_size,
                    "mtime":    f.stat().st_mtime,
                })
    return jsonify({"recordings": recs})


@app.route("/api/play", methods=["POST"])
def api_play():
    data         = request.get_json() or {}
    speaker_uids = data.get("speakers", [])
    audio_path   = data.get("path", "")
    volume       = data.get("volume")
    gong_file    = data.get("gong")

    if not speaker_uids:
        return jsonify({"success": False, "error": "Kein Lautsprecher ausgewählt"})
    if not audio_path:
        return jsonify({"success": False, "error": "Keine Audiodatei angegeben"})

    # Pfad auflösen: erst relativ zu schnelldurchsagen/, dann zu BASE_DIR
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
        return jsonify({"success": False, "error": "pyaudio fehlt. Bitte: pip install pyaudio"})
    if recording_state["active"]:
        return jsonify({"success": False, "error": "Läuft bereits"})
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
        stream = recording_state["stream"]
        pa     = recording_state["audio"]
        if stream:
            stream.stop_stream(); stream.close()
        if pa:
            pa.terminate()

        ts       = time.strftime("%Y%m%d_%H%M%S")
        wav_path = RECORDINGS_DIR / f"aufnahme_{ts}.wav"
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"".join(recording_state["frames"]))

        final = wav_path
        if PYDUB_AVAILABLE:
            try:
                mp3 = RECORDINGS_DIR / f"aufnahme_{ts}.mp3"
                AudioSegment.from_wav(str(wav_path)).export(str(mp3), format="mp3")
                wav_path.unlink()
                final = mp3
            except Exception:
                pass

        rel = f"assets/recordings/{final.name}"
        recording_state["last_file"] = rel
        return jsonify({
            "success":  True,
            "filename": final.name,
            "path":     rel,
            "duration": len(recording_state["frames"]) * 1024 / 44100,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ─── Statische Audio-Routen (für Browser-Vorschau) ───────────────────────────
@app.route("/audio/gongs/<filename>")
def serve_gong(filename):
    return send_from_directory(str(GONGS_DIR), filename)

@app.route("/audio/recordings/<filename>")
def serve_recording(filename):
    return send_from_directory(str(RECORDINGS_DIR), filename)


# ─── Start ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Datei-Server für SONOS in separatem Thread starten
    t = threading.Thread(target=start_file_server, daemon=True)
    t.start()
    time.sleep(0.5)  # kurz warten bis Server bereit

    print("🔊 SONOS Durchsagesystem startet...")
    print(f"   Web-UI:        http://localhost:{FLASK_PORT}")
    print(f"   Datei-Server:  http://{LOCAL_IP}:{FILE_PORT}/  (für SONOS)")
    print(f"   Gong-Ordner:   {GONGS_DIR}")
    print(f"   Durchsagen:    {SCHNELL_DIR}")
    print(f"   SONOS:         {'✓ verfügbar' if SOCO_AVAILABLE else '✗ Demo-Modus'}")
    print(f"   Aufnahme:      {'✓ verfügbar' if PYAUDIO_AVAILABLE else '✗ pyaudio fehlt'}")
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
