import os
import re
import io
import glob
import requests
import yt_dlp
from PIL import Image
from mutagen.id3 import ID3, APIC, error as ID3Error

from config import COOKIES_FILE, DOWNLOAD_DIR, SEARCH_LIMIT


# YouTube haengt bei automatisch generierten Kuenstler-Kanaelen "- Topic" an
# den Channel-Namen an. Das verwirrt Lidarrs Artist/Album-Parsing, wenn es
# einfach mit in den Titel uebernommen wird ("Artist - Topic - Songname").
_TOPIC_SUFFIX_RE = re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE)

# Uebliche YouTube-Titel-Anhaengsel, die fuer Lidarrs Release-Parsing nur
# Rauschen sind und teils sogar wie (falsche) Edition/Group-Tags aussehen.
_NOISE_PATTERNS = [
    re.compile(r"\(\s*official\s*(music\s*)?video\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*official\s*audio\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*official\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*lyrics?\s*(video)?\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*visualizer\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*audio\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*hd\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*4k\s*\)", re.IGNORECASE),
    re.compile(r"\[\s*official\s*(music\s*)?video\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*official\s*audio\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*lyrics?\s*(video)?\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*visualizer\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*hd\s*\]", re.IGNORECASE),
    re.compile(r"\[\s*4k\s*\]", re.IGNORECASE),
    re.compile(r"\|\s*official.*$", re.IGNORECASE),
]


def _clean_uploader(uploader: str) -> str:
    return _TOPIC_SUFFIX_RE.sub("", uploader or "").strip()


def _clean_title(title: str) -> str:
    t = title or ""
    for pattern in _NOISE_PATTERNS:
        t = pattern.sub("", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" -|")


def _title_has_artist_prefix(uploader: str, video_title: str) -> bool:
    """Prüft, ob der Video-Titel bereits "Artist - Track" enthält (z.B. bei
    offiziellen Künstlerkanälen), damit wir den Kanalnamen nicht nochmal
    davorsetzen ("d4vd - d4vd - Songname")."""
    if not uploader:
        return False
    parts = re.split(r"\s*[-–—]\s*", video_title, maxsplit=1)
    if len(parts) != 2:
        return False
    return parts[0].strip().lower() == uploader.strip().lower()


def _common_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def search_youtube(query: str, limit: int | None = None) -> list[dict]:
    """Durchsucht YouTube via yt-dlp und liefert eine Liste einfacher Dicts zurueck."""
    limit = limit or SEARCH_LIMIT
    opts = _common_opts()
    opts.update({"extract_flat": False, "skip_download": True})

    results = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        for entry in (info.get("entries") or []):
            if not entry:
                continue
            video_id = entry.get("id")
            raw_title = entry.get("title") or "Unknown"
            raw_uploader = entry.get("uploader") or entry.get("channel") or ""
            duration = entry.get("duration") or 0

            uploader = _clean_uploader(raw_uploader)
            clean_video_title = _clean_title(raw_title)

            if _title_has_artist_prefix(uploader, clean_video_title):
                full_title = clean_video_title
            else:
                full_title = f"{uploader} - {clean_video_title}".strip(" -") or clean_video_title

            # Erscheinungsjahr mitgeben, falls vorhanden -- hilft Lidarr beim
            # Zuordnen zum richtigen (Single-)Album in seiner Datenbank.
            upload_date = entry.get("upload_date")  # Format: YYYYMMDD
            if upload_date and len(upload_date) >= 4:
                full_title = f"{full_title} ({upload_date[:4]})"

            # Pseudo-Release-Tag anhaengen, damit Lidarrs Qualitaets-Parser
            # etwas Bekanntes erkennt (MP3-320 ist eine gueltige Standard-
            # Bitratenstufe, im Gegensatz zu z.B. "MP3-250") und man ueber
            # Custom Formats gezielt auf die Releasegruppe "ytdlp" matchen kann.
            release_title = f"{full_title}.MP3-256-ytdlp"

            # Groesse grob schaetzen (fuer die Anzeige in Lidarr), echte MP3-Groesse
            # kennen wir erst nach dem Download. ~320kbps Annahme.
            size_estimate = int(duration * 320 * 1000 / 8) if duration else 5_000_000

            results.append({
                "video_id": video_id,
                "title": release_title,
                "duration": duration,
                "size": size_estimate,
            })
    return results


def _crop_square(img: Image.Image) -> Image.Image:
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _embed_cover(mp3_path: str, thumbnail_url: str):
    try:
        resp = requests.get(thumbnail_url, timeout=20)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img = _crop_square(img)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        cover_bytes = buf.getvalue()

        try:
            tags = ID3(mp3_path)
        except ID3Error:
            tags = ID3()

        tags.delall("APIC")
        tags.add(APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,  # "Cover (front)"
            desc="Cover",
            data=cover_bytes,
        ))
        tags.save(mp3_path)
    except Exception as e:
        print(f"[cover] Konnte Cover nicht einbetten: {e}", flush=True)


def download_track(video_id: str, title: str, progress_cb=None) -> str:
    """
    Laedt das beste verfuegbare Audio herunter, konvertiert nach MP3 und
    bettet ein quadratisch zugeschnittenes Thumbnail als Cover ein.
    Gibt den finalen Dateipfad zurueck.
    """
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or video_id
    out_template = os.path.join(DOWNLOAD_DIR, f"{safe_title}.%(ext)s")

    state = {"thumbnail": None}

    def hook(d):
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                try:
                    progress_cb(downloaded / total)
                except Exception:
                    pass

    opts = _common_opts()
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
        ],
        "progress_hooks": [hook],
        "writethumbnail": False,  # wir holen das Thumbnail separat und croppen es selbst
    })

    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        state["thumbnail"] = info.get("thumbnail")

    candidates = glob.glob(os.path.join(DOWNLOAD_DIR, f"{safe_title}.mp3"))
    if not candidates:
        candidates = sorted(
            glob.glob(os.path.join(DOWNLOAD_DIR, f"{safe_title}*.mp3")),
            key=os.path.getmtime,
            reverse=True,
        )
    if not candidates:
        raise FileNotFoundError("MP3-Datei wurde nach dem Download nicht gefunden")

    mp3_path = candidates[0]

    if state["thumbnail"]:
        _embed_cover(mp3_path, state["thumbnail"])

    if progress_cb:
        progress_cb(1.0)

    return mp3_path
