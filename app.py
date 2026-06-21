import os
import json
import time
import wave
import struct
import threading
import tempfile
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS

# Optionale Imports – fehlende Module werden abgefangen
try:
    import soco
    SOCO_AVAILABLE = True
except ImportError:
    SOCO_AVAILABLE = False
    print("[WARN] soco nicht installiert. SONOS-Erkennung simuliert.")

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

app = Flask(__name__)
CORS(app)

# Basis-Pfade
BASE_DIR = Path(__file__).parent
ASSETS_DIR = BASE_DIR / "assets"
GONGS_DIR = ASSETS_DIR / "gongs"
RECORDINGS_DIR = ASSETS_DIR / "recordings"
SCHNELL_DIR = BASE_DIR / "schnelldurchsagen"

# Verzeichnisse anlegen
for d in [GONGS_DIR, RECORDINGS_DIR, SCHNELL_DIR / "Allgemein", SCHNELL_DIR / "Notfall", SCHNELL_DIR / "Information"]:
    d.mkdir(parents=True, exist_ok=True)

# Aufnahme-State
recording_state = {
    "active": False,
    "frames": [],
    "stream": None,
    "audio": None,
    "last_file": None
}


def safe_get(device, attr, default=""):
    """Sicherer Attribut-Zugriff auf SoCo-Objekte."""
    try:
        return getattr(device, attr)
    except Exception:
        return default


# ─── Hilfsfunktionen ────────────────────────────────────────────────────────

def get_sonos_speakers():
    """Alle SONOS-Lautsprecher im Netzwerk erkennen."""
    speakers = []
    if SOCO_AVAILABLE:
        try:
            devices = soco.discover(timeout=5)
            if devices:
                for device in devices:
                    try:
                        # model_name existiert in neueren soco-Versionen nicht mehr
                        # speaker_info liefert zuverlässige Gerätedaten
                        model = ""
                        try:
                            info = device.get_speaker_info(refresh=True)
                            model = info.get("model_name", info.get("model_number", "SONOS"))
                        except Exception:
                            model = "SONOS"

                        speakers.append({
                            "uid": safe_get(device, "uid", f"uid-{device.ip_address}"),
                            "name": safe_get(device, "player_name", device.ip_address),
                            "ip": safe_get(device, "ip_address", ""),
                            "volume": safe_get(device, "volume", 30),
                            "model": model
                        })
                    except Exception as e:
                        print(f"[WARN] Lautsprecher konnte nicht geladen werden: {e}")
        except Exception as e:
            print(f"[ERROR] SONOS Discovery: {e}")
    else:
        # Demo-Modus ohne echte SONOS-Hardware
        speakers = [
            {"uid": "DEMO-001", "name": "Wohnzimmer (Demo)", "ip": "192.168.1.100", "volume": 30, "model": "SONOS Play:1"},
            {"uid": "DEMO-002", "name": "Küche (Demo)", "ip": "192.168.1.101", "volume": 25, "model": "SONOS One"},
        ]
    return speakers


def play_on_sonos(speaker_uids, audio_file_path, volume=None, gong_file=None):
    """Audiodatei auf ausgewählten SONOS-Lautsprechern abspielen."""
    if not SOCO_AVAILABLE:
        print(f"[DEMO] Würde abspielen: {audio_file_path} auf {speaker_uids}")
        return {"success": True, "message": "Demo-Modus: Datei würde abgespielt"}

    results = []
    try:
        devices = soco.discover(timeout=5) or []
        selected = [d for d in devices if safe_get(d, "uid") in speaker_uids]

        # Lokale Datei über HTTP-Server bereitstellen
        import socket
        local_ip = "192.168.1.85"  # Wird dynamisch ermittelt
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        base_url = f"http://{local_ip}:5000"

        for device in selected:
            try:
                if volume is not None:
                    device.volume = int(volume)

                # Zuerst Gong abspielen (falls gewählt)
                if gong_file:
                    gong_path = str(GONGS_DIR / gong_file)
                    if os.path.exists(gong_path):
                        gong_url = f"{base_url}/audio/gongs/{gong_file}"
                        device.play_uri(gong_url)
                        time.sleep(get_audio_duration(gong_path) + 0.5)

                # Hauptdurchsage
                rel_path = os.path.relpath(audio_file_path, BASE_DIR)
                rel_path_url = rel_path.replace("\\", "/")
                audio_url = f"{base_url}/audio/serve?path={rel_path_url}"
                device.play_uri(audio_url)

                results.append({"speaker": safe_get(device, "player_name", device.ip_address), "success": True})
            except Exception as e:
                results.append({"speaker": safe_get(device, "player_name", "?"), "success": False, "error": str(e)})

        return {"success": True, "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_audio_duration(filepath):
    """Dauer einer WAV-Datei in Sekunden."""
    try:
        with wave.open(str(filepath), 'rb') as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate)
    except Exception:
        return 3.0


def get_folder_structure(base_path):
    """Ordnerstruktur mit Audiodateien auslesen."""
    result = []
    base = Path(base_path)
    if not base.exists():
        return result

    for item in sorted(base.iterdir()):
        if item.is_dir():
            files = []
            for f in sorted(item.iterdir()):
                if f.suffix.lower() in [".mp3", ".wav", ".ogg", ".aac"]:
                    files.append({
                        "name": f.stem,
                        "filename": f.name,
                        "path": str(f.relative_to(BASE_DIR)).replace("\\", "/"),
                        "size": f.stat().st_size
                    })
            result.append({
                "folder": item.name,
                "files": files
            })
        elif item.suffix.lower() in [".mp3", ".wav", ".ogg", ".aac"]:
            result.append({
                "folder": "/",
                "files": [{
                    "name": item.stem,
                    "filename": item.name,
                    "path": str(item.relative_to(BASE_DIR)).replace("\\", "/"),
                    "size": item.stat().st_size
                }]
            })
    return result


# ─── Flask-Routen ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/speakers")
def api_speakers():
    speakers = get_sonos_speakers()
    return jsonify({"speakers": speakers})


@app.route("/api/speakers/<uid>/volume", methods=["POST"])
def api_set_volume(uid):
    data = request.get_json()
    volume = data.get("volume", 30)
    if not SOCO_AVAILABLE:
        return jsonify({"success": True, "message": "Demo-Modus"})
    try:
        devices = soco.discover(timeout=3) or []
        for d in devices:
            if safe_get(d, "uid") == uid:
                d.volume = int(volume)
                return jsonify({"success": True})
        return jsonify({"success": False, "error": "Lautsprecher nicht gefunden"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/gongs")
def api_gongs():
    gongs = []
    if GONGS_DIR.exists():
        for f in sorted(GONGS_DIR.iterdir()):
            if f.suffix.lower() in [".mp3", ".wav", ".ogg"]:
                gongs.append({"name": f.stem, "filename": f.name})
    return jsonify({"gongs": gongs})


@app.route("/api/schnelldurchsagen")
def api_schnelldurchsagen():
    folders = get_folder_structure(SCHNELL_DIR)
    return jsonify({"folders": folders})


@app.route("/api/recordings")
def api_recordings():
    recordings = []
    if RECORDINGS_DIR.exists():
        for f in sorted(RECORDINGS_DIR.iterdir(), reverse=True):
            if f.suffix.lower() in [".mp3", ".wav"]:
                recordings.append({
                    "name": f.stem,
                    "filename": f.name,
                    "path": str(f.relative_to(BASE_DIR)).replace("\\", "/"),
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime
                })
    return jsonify({"recordings": recordings})


@app.route("/api/play", methods=["POST"])
def api_play():
    data = request.get_json()
    speaker_uids = data.get("speakers", [])
    audio_path = data.get("path", "")
    volume = data.get("volume", None)
    gong_file = data.get("gong", None)

    if not speaker_uids:
        return jsonify({"success": False, "error": "Kein Lautsprecher ausgewählt"})
    if not audio_path:
        return jsonify({"success": False, "error": "Keine Audiodatei angegeben"})

    full_path = BASE_DIR / audio_path
    if not full_path.exists():
        return jsonify({"success": False, "error": f"Datei nicht gefunden: {audio_path}"})

    result = play_on_sonos(speaker_uids, str(full_path), volume, gong_file)
    return jsonify(result)


@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    if not PYAUDIO_AVAILABLE:
        return jsonify({"success": False, "error": "pyaudio nicht installiert. Bitte: pip install pyaudio"})
    if recording_state["active"]:
        return jsonify({"success": False, "error": "Aufnahme läuft bereits"})

    try:
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=44100,
            input=True,
            frames_per_buffer=1024
        )
        recording_state["active"] = True
        recording_state["frames"] = []
        recording_state["stream"] = stream
        recording_state["audio"] = pa

        def record_thread():
            while recording_state["active"]:
                try:
                    data = stream.read(1024, exception_on_overflow=False)
                    recording_state["frames"].append(data)
                except Exception:
                    break

        t = threading.Thread(target=record_thread, daemon=True)
        t.start()
        return jsonify({"success": True, "message": "Aufnahme gestartet"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    if not recording_state["active"]:
        return jsonify({"success": False, "error": "Keine Aufnahme aktiv"})

    recording_state["active"] = False
    time.sleep(0.2)

    try:
        stream = recording_state["stream"]
        pa = recording_state["audio"]
        if stream:
            stream.stop_stream()
            stream.close()
        if pa:
            pa.terminate()

        ts = time.strftime("%Y%m%d_%H%M%S")
        wav_path = RECORDINGS_DIR / f"aufnahme_{ts}.wav"

        with wave.open(str(wav_path), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b''.join(recording_state["frames"]))

        final_path = wav_path
        if PYDUB_AVAILABLE:
            try:
                mp3_path = RECORDINGS_DIR / f"aufnahme_{ts}.mp3"
                AudioSegment.from_wav(str(wav_path)).export(str(mp3_path), format="mp3")
                wav_path.unlink()
                final_path = mp3_path
            except Exception:
                pass

        recording_state["last_file"] = str(final_path.relative_to(BASE_DIR)).replace("\\", "/")
        return jsonify({
            "success": True,
            "filename": final_path.name,
            "path": str(final_path.relative_to(BASE_DIR)).replace("\\", "/"),
            "duration": len(recording_state["frames"]) * 1024 / 44100
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/audio/gongs/<filename>")
def serve_gong(filename):
    return send_from_directory(str(GONGS_DIR), filename)


@app.route("/audio/serve")
def serve_audio():
    path = request.args.get("path", "")
    full = BASE_DIR / path
    if full.exists() and full.suffix.lower() in [".mp3", ".wav", ".ogg", ".aac"]:
        return send_from_directory(str(full.parent), full.name)
    return jsonify({"error": "Datei nicht gefunden"}), 404


@app.route("/audio/recordings/<filename>")
def serve_recording(filename):
    return send_from_directory(str(RECORDINGS_DIR), filename)


if __name__ == "__main__":
    print("🔊 SONOS Durchsagesystem startet...")
    print(f"   Öffne http://localhost:5000 im Browser")
    print(f"   Gong-Dateien in: {GONGS_DIR}")
    print(f"   Schnelldurchsagen in: {SCHNELL_DIR}")
    print(f"   SONOS-Bibliothek: {'✓ verfügbar' if SOCO_AVAILABLE else '✗ nicht installiert (Demo-Modus)'}")
    print(f"   Mikrofon-Aufnahme: {'✓ verfügbar' if PYAUDIO_AVAILABLE else '✗ nicht installiert'}")
    app.run(host="0.0.0.0", port=5000, debug=False)
