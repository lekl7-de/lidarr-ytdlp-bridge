import os
import re
import io
import glob
import requests
import yt_dlp
from urllib.parse import quote_plus
from PIL import Image
from mutagen.id3 import (
    ID3, APIC, TIT2, TPE1, TALB, TDRC, TRCK, TPE2,
    error as ID3Error,
)

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
            release_title = f"{full_title}.MP3-320-ytdlp"

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


def search_album_playlists(query: str, limit: int = 3) -> list[dict]:
    """
    Sucht gezielt nach echten YouTube-Playlists zu einer Anfrage (z.B.
    "Artist Album") und liefert pro gefundener Playlist die vollstaendige
    Tracklist zurueck. Liefert eine leere Liste, wenn nichts Passendes
    gefunden wird (kein Fallback auf einzelne Videos -- das macht
    search_youtube() bereits separat).
    """
    query = (query or "").strip()
    if not query:
        return []

    # "sp=EgIQAw%3D%3D" ist YouTubes Filter fuer "Playlists" in der
    # Ergebnisliste der normalen Websuche.
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgIQAw%3D%3D"

    opts = _common_opts()
    opts.update({"extract_flat": True, "skip_download": True, "playlistend": limit})

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
    except Exception as e:
        print(f"[album-search] Playlist-Suche fehlgeschlagen: {e}", flush=True)
        return []

    candidates = [e for e in (info.get("entries") or []) if e and e.get("id")]

    albums = []
    for cand in candidates[:limit]:
        playlist_id = cand["id"]
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        try:
            tracks = _expand_playlist(playlist_url)
        except Exception as e:
            print(f"[album-search] Konnte Playlist {playlist_id} nicht laden: {e}", flush=True)
            continue
        if not tracks:
            continue

        raw_playlist_title = cand.get("title") or query
        playlist_title = _clean_title(raw_playlist_title)
        release_title = f"{playlist_title} [Playlist, {len(tracks)} Tracks].MP3-320-ytdlp"

        # Keine zuverlaessige Dauer bei flacher Extraktion -> grobe Pauschale
        # pro Track fuer die Groessenanzeige in Lidarr.
        size_estimate = len(tracks) * 5_000_000

        albums.append({
            "playlist_id": playlist_id,
            "title": release_title,
            "tracks": tracks,
            "size": size_estimate,
        })
    return albums


def _expand_playlist(playlist_url: str) -> list[dict]:
    """Liest alle Tracks einer Playlist (flach, also ohne pro Video extra
    Metadaten zu ziehen -- schnell, reicht fuer Titel+ID)."""
    opts = _common_opts()
    opts.update({"extract_flat": "in_playlist", "skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    tracks = []
    for idx, e in enumerate(info.get("entries") or [], start=1):
        if not e or not e.get("id"):
            continue
        tracks.append({
            "video_id": e["id"],
            "title": _clean_title(e.get("title") or f"Track {idx}"),
            "track_no": idx,
        })
    return tracks


def _crop_square(img: Image.Image) -> Image.Image:
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _embed_tags(
    mp3_path: str,
    *,
    artist: str = "",
    title: str = "",
    album: str = "",
    year: str = "",
    track_no: int | None = None,
    track_total: int | None = None,
    thumbnail_url: str = "",
):
    """
    Bettet alle relevanten ID3-Tags in eine MP3 ein.
    Lidarr liest beim Import primär diese Tags (nicht den Dateinamen), daher
    sind korrekte Werte hier entscheidend fuer den Auto-Import.
    """
    try:
        try:
            tags = ID3(mp3_path)
        except ID3Error:
            tags = ID3()

        if title:
            tags["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            tags["TPE1"] = TPE1(encoding=3, text=artist)
            tags["TPE2"] = TPE2(encoding=3, text=artist)  # Album Artist
        if album:
            tags["TALB"] = TALB(encoding=3, text=album)
        if year:
            tags["TDRC"] = TDRC(encoding=3, text=year)
        if track_no is not None:
            trck = str(track_no)
            if track_total:
                trck = f"{track_no}/{track_total}"
            tags["TRCK"] = TRCK(encoding=3, text=trck)

        # Cover-Art
        if thumbnail_url:
            try:
                resp = requests.get(thumbnail_url, timeout=20)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                w, h = img.size
                side = min(w, h)
                img = img.crop(((w - side) // 2, (h - side) // 2,
                                (w + side) // 2, (h + side) // 2))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92)
                tags.delall("APIC")
                tags.add(APIC(
                    encoding=3, mime="image/jpeg", type=3,
                    desc="Cover", data=buf.getvalue(),
                ))
            except Exception as e:
                print(f"[tags] Cover konnte nicht geladen werden: {e}", flush=True)

        tags.save(mp3_path)
    except Exception as e:
        print(f"[tags] Fehler beim Einbetten der Tags in {mp3_path}: {e}", flush=True)


def download_track(
    video_id: str,
    title: str,
    progress_cb=None,
    target_dir: str | None = None,
    # Optionale Overrides fuer Album-Downloads (Lidarr liest diese Tags)
    artist_override: str = "",
    album_override: str = "",
    track_no: int | None = None,
    track_total: int | None = None,
) -> str:
    """
    Laedt das beste verfuegbare Audio herunter, konvertiert nach MP3 und
    bettet ID3-Tags (Artist, Title, Album, Year, TrackNo) sowie ein
    quadratisch zugeschnittenes Thumbnail als Cover ein.
    Gibt den finalen Dateipfad zurueck.

    target_dir:       optionaler Zielordner (z.B. fuer Album-Downloads).
    artist_override:  Artist-Tag explizit setzen (z.B. fuer Album-Tracks).
    album_override:   Album-Tag explizit setzen.
    track_no:         Tracknummer innerhalb des Albums.
    track_total:      Gesamtanzahl Tracks im Album.
    """
    target_dir = target_dir or DOWNLOAD_DIR
    os.makedirs(target_dir, exist_ok=True)

    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or video_id
    out_template = os.path.join(target_dir, f"{safe_title}.%(ext)s")

    meta: dict = {}

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
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            # FFmpegMetadata schreibt yt-dlp-Metadaten (Titel, Uploader etc.)
            # schon grob in die MP3 -- wir ueberschreiben das danach mit
            # saubereren Werten via mutagen.
            {"key": "FFmpegMetadata"},
        ],
        "progress_hooks": [hook],
        "writethumbnail": False,
    })

    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        meta["thumbnail"] = info.get("thumbnail", "")
        meta["raw_title"]  = _clean_title(info.get("title") or title)
        meta["uploader"]   = _clean_uploader(
            info.get("uploader") or info.get("channel") or ""
        )
        meta["year"] = (info.get("upload_date") or "")[:4]

    candidates = glob.glob(os.path.join(target_dir, f"{safe_title}.mp3"))
    if not candidates:
        candidates = sorted(
            glob.glob(os.path.join(target_dir, f"{safe_title}*.mp3")),
            key=os.path.getmtime, reverse=True,
        )
    if not candidates:
        raise FileNotFoundError("MP3-Datei wurde nach dem Download nicht gefunden")
    mp3_path = candidates[0]

    # Artist: Override (bei Album-Downloads), sonst aus yt-dlp-Metadaten
    artist = artist_override or meta["uploader"]

    # Track-Titel: wenn der Video-Titel den Artist schon enthaelt, nur den
    # Teil nach dem ersten Trenner nehmen (z.B. "d4vd - Romantic Homicide"
    # -> "Romantic Homicide"), damit der Title-Tag sauber ist.
    track_title = meta["raw_title"]
    if _title_has_artist_prefix(artist, track_title):
        track_title = re.split(r"\s*[-–—]\s*", track_title, maxsplit=1)[1].strip()

    _embed_tags(
        mp3_path,
        artist=artist,
        title=track_title,
        # TALB bei Singles absichtlich leer lassen: Lidarr weiss bereits
        # welches Album es importieren will (aus seiner eigenen Datenbank).
        # Setzen wir TALB auf den Track-Titel, vergleicht Lidarr "Mittelmeer"
        # mit "Nulldreinull" -> 44% Match -> Import fehlgeschlagen.
        # Fuer Album-Downloads (album_override gesetzt) behalten wir den Wert.
        album=album_override,
        year=meta["year"],
        track_no=track_no,
        track_total=track_total,
        thumbnail_url=meta["thumbnail"],
    )

    if progress_cb:
        progress_cb(1.0)

    return mp3_path
