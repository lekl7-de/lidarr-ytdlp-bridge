"""
Verwaltet zwei Dinge im Speicher:

1. _magnet_cache: Mapping von einem (fake) Infohash auf die echten YouTube-Metadaten.
   Wird beim Erzeugen der Suchergebnisse befuellt, damit wir beim "Download"-Klick
   wieder wissen, welches YouTube-Video gemeint war.

2. _jobs: Der aktuelle Status aller laufenden/abgeschlossenen Downloads, im qBittorrent-
   Torrent-Objekt-Format, damit /torrents/info direkt damit antworten kann.
"""

import time
import hashlib
import threading
from urllib.parse import quote

_lock = threading.Lock()
_magnet_cache: dict[str, dict] = {}
_jobs: dict[str, dict] = {}


def make_hash(video_id: str) -> str:
    """Erzeugt einen deterministischen 40-stelligen Hex-String (sieht aus wie ein Infohash)."""
    return hashlib.sha1(video_id.encode("utf-8")).hexdigest()


def register_result(video_id: str, title: str) -> str:
    h = make_hash(video_id)
    with _lock:
        _magnet_cache[h] = {"video_id": video_id, "title": title, "ts": time.time()}
    return h


def build_magnet(video_id: str, title: str) -> str:
    h = register_result(video_id, title)
    # Video-ID zusaetzlich im "dn"-Namen einbetten als Fallback, falls der Cache
    # mal verloren geht (z.B. Container-Neustart zwischen Suche und Download-Klick)
    dn = quote(f"{title} [{video_id}]")
    # Lidarr lehnt Magnet-Links ohne Tracker ab, wenn es DHT als deaktiviert
    # ansieht ("Magnet Links without trackers not supported if DHT is
    # disabled"). Da wir eh nie echtes Bittorrent machen, reichen ein paar
    # oeffentliche Dummy-Tracker, um diese Validierung zu erfuellen.
    trackers = [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://tracker.openbittorrent.com:6969/announce",
        "udp://open.stealth.si:80/announce",
    ]
    tr_params = "".join(f"&tr={quote(t, safe='')}" for t in trackers)
    return f"magnet:?xt=urn:btih:{h}&dn={dn}{tr_params}"


def resolve_hash(h: str) -> dict | None:
    with _lock:
        entry = _magnet_cache.get(h)
    return dict(entry) if entry else None


def create_job(h: str, video_id: str, title: str, save_path: str) -> dict:
    with _lock:
        job = {
            "hash": h,
            "video_id": video_id,
            "name": title,
            "state": "downloading",
            "progress": 0.0,
            "size": 0,
            "save_path": save_path,
            "content_path": "",
            "added_on": int(time.time()),
            "completion_on": 0,
            "category": "lidarr-yt",
            "error": "",
        }
        _jobs[h] = job
        return dict(job)


def update_job(h: str, **kwargs):
    with _lock:
        if h in _jobs:
            _jobs[h].update(kwargs)


def get_job(h: str) -> dict | None:
    with _lock:
        job = _jobs.get(h)
        return dict(job) if job else None


def all_jobs() -> list[dict]:
    with _lock:
        return [dict(j) for j in _jobs.values()]


def delete_job(h: str):
    with _lock:
        _jobs.pop(h, None)
