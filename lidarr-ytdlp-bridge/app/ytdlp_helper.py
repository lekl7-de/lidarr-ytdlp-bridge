import os
import re
import io
import glob
import requests
import yt_dlp
from PIL import Image
from mutagen.id3 import ID3, APIC, error as ID3Error

from config import COOKIES_FILE, DOWNLOAD_DIR, SEARCH_LIMIT


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
            title = entry.get("title") or "Unknown"
            uploader = entry.get("uploader") or entry.get("channel") or ""
            duration = entry.get("duration") or 0
            full_title = f"{uploader} - {title}".strip(" -") or title
            # Pseudo-Release-Tag anhaengen, damit man in Lidarr ueber Custom
            # Formats gezielt auf "ytdlp"/"MP3-250" matchen/scoren kann.
            release_title = f"{full_title}.MP3-250-YTDLP"

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
