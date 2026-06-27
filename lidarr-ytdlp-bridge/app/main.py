"""
Lidarr <-> yt-dlp Translation Layer
====================================

Stellt zwei Dinge bereit, die Lidarr nativ versteht:

1. Einen Torznab-Indexer unter /api  (t=caps, t=search, ...)
   -> Lidarr durchsucht damit "ein Indexer-Netzwerk", wir durchsuchen stattdessen YouTube.

2. Eine qBittorrent-Web-API-Emulation unter /api/v2/...
   -> Lidarr denkt, es spricht mit einem echten qBittorrent. Statt eines echten
      Torrents starten wir im Hintergrund einen yt-dlp-Download.

In Lidarr eintragen:
  - Indexer:        Typ "Torznab" (custom), URL http://<host>:9117, API-Key wie unten konfiguriert
  - Download-Client: Typ "qBittorrent",      Host <host>, Port 9117, Benutzer/Passwort wie unten konfiguriert
"""

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import PlainTextResponse, JSONResponse, RedirectResponse

import config
import jobs
import ytdlp_helper

app = FastAPI(title="Lidarr YT-DLP Bridge")
executor = ThreadPoolExecutor(max_workers=2)


def _cleanup_loop():
    """Löscht abgeschlossene Downloads nach CLEANUP_AFTER Sekunden.
    Läuft als Daemon-Thread und prüft jede Minute. Berührt nur Dateien,
    die von dieser Bridge heruntergeladen wurden (content_path aus _jobs)."""
    import shutil
    while True:
        time.sleep(60)
        if config.CLEANUP_AFTER <= 0:
            continue
        now = int(time.time())
        for job in jobs.all_jobs():
            completion_on = job.get("completion_on", 0)
            if not completion_on or now - completion_on < config.CLEANUP_AFTER:
                continue
            content = job.get("content_path", "")
            h = job["hash"]
            if content and os.path.exists(content):
                try:
                    if os.path.isdir(content):
                        shutil.rmtree(content)
                    else:
                        os.remove(content)
                    print(f"[cleanup] Gelöscht: {content}", flush=True)
                except OSError as e:
                    print(f"[cleanup] Fehler beim Löschen von {content}: {e}", flush=True)
            jobs.delete_job(h)


@app.on_event("startup")
async def startup():
    if config.CLEANUP_AFTER > 0:
        threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()

    print(f"", flush=True)
    print(f"╔══════════════════════════════════════════╗", flush=True)
    print(f"║   Lidarr YT-DLP Bridge  v{config.VERSION:<16} ║", flush=True)
    print(f"║   Port {config.PORT}  •  DOWNLOAD_DIR={config.DOWNLOAD_DIR[:13]+'...' if len(config.DOWNLOAD_DIR)>13 else config.DOWNLOAD_DIR:<13} ║", flush=True)
    cleanup_info = f"{config.CLEANUP_AFTER}s" if config.CLEANUP_AFTER > 0 else "nie"
    print(f"║   Cleanup nach: {cleanup_info:<25} ║", flush=True)
    print(f"╚══════════════════════════════════════════╝", flush=True)
    print(f"", flush=True)


@app.middleware("http")
async def log_all_requests(request: Request, call_next):
    """Loggt JEDEN eingehenden Request mit Methode, Pfad und Body.
    Unverzichtbar um zu sehen was Lidarr genau schickt, insbesondere
    beim Delete-Befehl der laut User nie ankam."""
    body = b""
    # Body nur bei nicht-GET-Requests lesen (POST/DELETE etc.)
    if request.method != "GET":
        body = await request.body()
        # Body-Stream fuer den eigentlichen Handler wieder zuruecksetzen
        async def receive():
            return {"type": "http.request", "body": body}
        request._receive = receive

    path_with_query = str(request.url.path)
    if request.url.query:
        path_with_query += f"?{request.url.query}"

    if body:
        body_str = body.decode("utf-8", errors="replace")
        print(f"[req] {request.method} {path_with_query}  body={body_str!r}", flush=True)
    else:
        print(f"[req] {request.method} {path_with_query}", flush=True)

    response = await call_next(request)
    return response


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _run_download(h: str, video_id: str, title: str):
    save_path = config.DOWNLOAD_DIR

    def progress_cb(frac: float):
        jobs.update_job(h, progress=round(min(frac, 1.0), 4))

    try:
        mp3_path = ytdlp_helper.download_track(video_id, title, progress_cb=progress_cb)
        size = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
        jobs.update_job(
            h,
            state="uploading",  # qBittorrent-Status fuer "fertig, am seeden" -> Lidarr wertet das als komplett
            progress=1.0,
            content_path=mp3_path,
            save_path=save_path,
            size=size,
            completion_on=int(time.time()),
        )
    except Exception as e:
        print(f"[download] Fehler bei {video_id}: {e}", flush=True)
        jobs.update_job(h, state="error", error=str(e))


def _run_album_download(h: str, entry: dict, save_path: str):
    import re as _re
    import shutil
    tracks = entry["tracks"]
    album_title = _clean_album_title(entry["title"])
    safe_album_dir = _re.sub(r'[\\/:*?"<>|]', "_", album_title).strip() or entry["playlist_id"]
    album_dir = os.path.join(config.DOWNLOAD_DIR, safe_album_dir)
    os.makedirs(album_dir, exist_ok=True)

    # Artist und Album-Name aus dem Playlist-Titel herausparsen
    # Erwartetes Format: "Artist - Album [Playlist, N Tracks].MP3-320-ytdlp"
    artist_guess = ""
    album_guess = album_title
    m = _re.match(r"^(.+?)\s*[-–—]\s*(.+?)(?:\s*\[Playlist)", album_title)
    if m:
        artist_guess = m.group(1).strip()
        album_guess  = m.group(2).strip()

    total = len(tracks) or 1
    done = 0
    total_size = 0

    for t in tracks:
        track_label = f"{t.get('track_no', done + 1):02d} - {t['title']}"
        try:
            mp3_path = ytdlp_helper.download_track(
                t["video_id"],
                track_label,
                target_dir=album_dir,
                artist_override=artist_guess,
                album_override=album_guess,
                track_no=t.get("track_no", done + 1),
                track_total=total,
            )
            total_size += os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
        except Exception as e:
            print(f"[album-download] Track {t.get('video_id')} fehlgeschlagen: {e}", flush=True)
        done += 1
        jobs.update_job(h, progress=round(done / total, 4), tracks_done=done, size=total_size)

    jobs.update_job(
        h,
        state="uploading",
        progress=1.0,
        content_path=album_dir,
        save_path=save_path,
        size=total_size,
        completion_on=int(time.time()),
    )


def _clean_album_title(release_title: str) -> str:
    """Entfernt das '.MP3-320-ytdlp'-Suffix aus dem internen Release-Titel
    fuer die Verwendung als Ordnername."""
    return release_title.removesuffix(".MP3-320-ytdlp").strip()


# ---------------------------------------------------------------------------
# Torznab-Indexer
# ---------------------------------------------------------------------------

CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="{name}"/>
  <limits max="100" default="{limit}"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="no" supportedParams=""/>
    <movie-search available="no" supportedParams=""/>
    <music-search available="yes" supportedParams="q,artist,album"/>
    <audio-search available="yes" supportedParams="q"/>
  </searching>
  <categories>
    <category id="3000" name="Audio">
      <subcat id="3010" name="MP3"/>
    </category>
  </categories>
</caps>"""


@app.get("/api")
async def torznab_api(request: Request):
    params = request.query_params
    t = params.get("t", "caps")
    apikey = params.get("apikey", "")

    if t != "caps" and apikey != config.API_KEY:
        return PlainTextResponse("Invalid API key", status_code=401)

    if t == "caps":
        return Response(
            content=CAPS_XML.format(name=escape(config.INDEXER_NAME), limit=config.SEARCH_LIMIT),
            media_type="application/xml",
        )

    # t == search / music / audio-search ...
    query = params.get("q") or " ".join(
        filter(None, [params.get("artist"), params.get("album"), params.get("track")])
    ) or ""

    if not query.strip():
        # Lidarr testet die Verbindung mit einer leeren Suche (nur cat/extended,
        # kein q/artist/album) und erwartet dabei mindestens ein Ergebnis, sonst
        # wird der Test als fehlgeschlagen gewertet. Daher: ein harmloses
        # Dummy-Item zurueckgeben, das bei echten Suchen (mit q) nie auftaucht.
        track_results = [{
            "video_id": "dQw4w9WgXcQ",
            "title": "Indexer Connectivity Test",
            "duration": 0,
            "size": 1,
        }]
        album_results = []
    else:
        try:
            track_results = ytdlp_helper.search_youtube(query)
        except Exception as e:
            print(f"[search] Fehler: {e}", flush=True)
            track_results = []
        try:
            album_results = ytdlp_helper.search_album_playlists(query)
        except Exception as e:
            print(f"[album-search] Fehler: {e}", flush=True)
            album_results = []

    items = []
    now = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())
    base = str(request.base_url).rstrip("/")

    for r in track_results:
        magnet = jobs.build_magnet(r["video_id"], r["title"])
        h = jobs.make_hash(f"track:{r['video_id']}")
        grab_url = f"{base}/grab/{h}"
        title_xml = escape(r["title"])
        items.append(f"""
  <item>
    <title>{title_xml}</title>
    <guid isPermaLink="false">{r['video_id']}</guid>
    <link>{escape(grab_url)}</link>
    <comments>https://www.youtube.com/watch?v={r['video_id']}</comments>
    <pubDate>{now}</pubDate>
    <size>{r['size']}</size>
    <category>3000</category>
    <enclosure url="{escape(grab_url)}" length="{r['size']}" type="application/x-bittorrent"/>
    <torznab:attr name="category" value="3000"/>
    <torznab:attr name="seeders" value="999"/>
    <torznab:attr name="peers" value="999"/>
    <torznab:attr name="minimumratio" value="1"/>
    <torznab:attr name="minimumseedtime" value="0"/>
    <torznab:attr name="magneturl" value="{escape(magnet)}"/>
  </item>""")

    for a in album_results:
        magnet = jobs.build_album_magnet(a["playlist_id"], a["title"], a["tracks"])
        h = jobs.make_hash(f"album:{a['playlist_id']}")
        grab_url = f"{base}/grab/{h}"
        title_xml = escape(a["title"])
        items.append(f"""
  <item>
    <title>{title_xml}</title>
    <guid isPermaLink="false">album-{a['playlist_id']}</guid>
    <link>{escape(grab_url)}</link>
    <comments>https://www.youtube.com/playlist?list={a['playlist_id']}</comments>
    <pubDate>{now}</pubDate>
    <size>{a['size']}</size>
    <category>3000</category>
    <enclosure url="{escape(grab_url)}" length="{a['size']}" type="application/x-bittorrent"/>
    <torznab:attr name="category" value="3000"/>
    <torznab:attr name="seeders" value="999"/>
    <torznab:attr name="peers" value="999"/>
    <torznab:attr name="minimumratio" value="1"/>
    <torznab:attr name="minimumseedtime" value="0"/>
    <torznab:attr name="magneturl" value="{escape(magnet)}"/>
  </item>""")

    items_xml = "".join(items)

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:torznab="http://torznab.com/schemas/2015/feed">
<channel>
  <title>{escape(config.INDEXER_NAME)}</title>
  <description>YouTube via yt-dlp</description>
  {items_xml}
</channel>
</rss>"""
    return Response(content=rss, media_type="application/xml")


@app.get("/grab/{h}")
async def grab(h: str):
    """
    Lidarr ruft diese URL per HTTP GET auf, um die 'Release-Datei' zu holen.
    Statt echter Torrent-Bytes liefern wir einen 302-Redirect auf den
    Magnet-Link -- exakt das Muster, das z.B. Jackett bei reinen
    Magnet-Trackern verwendet und das Lidarr/Sonarr/Radarr nativ unterstuetzen
    (im Gegensatz zu einem rohen "magnet:"-Text direkt im <link>-Feld, das
    beim internen URI-Parsing fehlschlagen kann).
    """
    entry = jobs.resolve_hash(h)
    if not entry:
        return PlainTextResponse("Unknown release", status_code=404)
    magnet = jobs.rebuild_magnet(entry)
    return RedirectResponse(url=magnet, status_code=302)


# ---------------------------------------------------------------------------
# qBittorrent-API-Emulation
# ---------------------------------------------------------------------------

@app.post("/api/v2/auth/login")
async def qbit_login(username: str = Form(""), password: str = Form("")):
    if username != config.QBIT_USERNAME or password != config.QBIT_PASSWORD:
        return PlainTextResponse("Fails.", status_code=403)
    resp = PlainTextResponse("Ok.")
    resp.set_cookie("SID", "lidarr-ytdlp-bridge-session")
    return resp


@app.get("/api/v2/auth/logout")
async def qbit_logout():
    return PlainTextResponse("Ok.")


@app.get("/api/v2/app/version")
async def qbit_version():
    return PlainTextResponse("v4.6.2")


@app.get("/api/v2/app/webapiVersion")
async def qbit_webapi_version():
    return PlainTextResponse("2.8.3")


@app.get("/api/v2/app/preferences")
async def qbit_preferences():
    return JSONResponse({
        "save_path": config.DOWNLOAD_DIR,
        "temp_path_enabled": False,
        "temp_path": config.DOWNLOAD_DIR,
        "export_dir": "",
        "export_dir_fin": "",
        "preallocate_all": False,
        "queueing_enabled": False,
        "max_active_downloads": 5,
        "max_active_torrents": 5,
        "max_active_uploads": 5,
        "dht": True,
        "pex": True,
        "lsd": True,
    })


_categories: dict[str, dict] = {
    "lidarr-yt": {"name": "lidarr-yt", "savePath": config.DOWNLOAD_DIR}
}


@app.get("/api/v2/torrents/categories")
async def qbit_categories():
    # Lidarr fragt das beim Verbindungstest ab, ohne diesen Endpoint schlaegt
    # "Test" mit "Failed to connect to qBittorrent" fehl.
    return JSONResponse(_categories)


@app.post("/api/v2/torrents/createCategory")
async def qbit_create_category(category: str = Form(""), savePath: str = Form("")):
    if category:
        _categories[category] = {"name": category, "savePath": savePath or config.DOWNLOAD_DIR}
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/editCategory")
async def qbit_edit_category(category: str = Form(""), savePath: str = Form("")):
    # Lidarr ruft das beim Speichern/Testen des Download-Clients fuer die
    # konfigurierte Category auf. Fehlte dieser Endpoint (404), zeigte Lidarr
    # "Configuration of label failed".
    if category:
        _categories[category] = {"name": category, "savePath": savePath or config.DOWNLOAD_DIR}
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/removeCategories")
async def qbit_remove_categories(categories: str = Form("")):
    for c in categories.split("\n"):
        _categories.pop(c.strip(), None)
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/add")
async def qbit_torrents_add(request: Request):
    form = await request.form()
    urls_raw = form.get("urls", "")
    save_path = form.get("savepath") or config.DOWNLOAD_DIR

    added = []
    for magnet in [u.strip() for u in urls_raw.splitlines() if u.strip()]:
        h = _extract_hash(magnet)
        if not h:
            continue
        entry = jobs.resolve_hash(h)
        if not entry:
            # Fallback: Video-ID aus dem dn-Namen "[VIDEOID]" extrahieren
            # (funktioniert nur fuer einzelne Tracks, nicht fuer Alben)
            entry = _fallback_from_dn(magnet)
        if not entry:
            print(f"[add] Konnte Release nicht aus Magnet ermitteln: {magnet}", flush=True)
            continue

        if entry.get("kind") == "album":
            jobs.create_album_job(h, entry["playlist_id"], entry["title"], entry["tracks"], save_path)
            executor.submit(_run_album_download, h, entry, save_path)
        else:
            jobs.create_job(h, entry["video_id"], entry["title"], save_path)
            executor.submit(_run_download, h, entry["video_id"], entry["title"])
        added.append(h)

    if not added:
        return PlainTextResponse("Fails.", status_code=400)
    return PlainTextResponse("Ok.")


def _extract_hash(magnet: str) -> str | None:
    import re
    m = re.search(r"btih:([a-fA-F0-9]{40})", magnet)
    return m.group(1).lower() if m else None


def _fallback_from_dn(magnet: str) -> dict | None:
    import re
    from urllib.parse import unquote
    m = re.search(r"dn=([^&]+)", magnet)
    if not m:
        return None
    dn = unquote(m.group(1))
    vid_match = re.search(r"\[([A-Za-z0-9_-]{11})\]\s*$", dn)
    if not vid_match:
        return None
    video_id = vid_match.group(1)
    title = dn[: vid_match.start()].strip()
    return {"kind": "track", "video_id": video_id, "title": title}


def _to_qbit_torrent(job: dict) -> dict:
    progress = job["progress"]
    # Lidarr prueft ob size > 0 und amount_left == 0 um "fertig" zu erkennen.
    # Waehrend des Downloads haben wir oft size=0 (unbekannt) -- dann eine
    # Mindestgroesse annehmen damit die Konsistenz stimmt.
    size = job["size"] or (1 if progress < 1.0 else 4_000_000)
    done = progress >= 1.0
    return {
        "hash": job["hash"],
        "name": job["name"],
        "size": size,
        "progress": progress,
        "dlspeed": 0,
        "upspeed": 0,
        "eta": 0 if done else 8640000,
        # "stalledUP" = fertiger Download, wartet auf Seeder/Import.
        # Lidarr und Sonarr erkennen diesen State zuverlaessiger als
        # "abgeschlossen" als "uploading", was manchmal als "noch aktiv"
        # interpretiert wird.
        "state": "stalledUP" if done else job["state"],
        "category": job.get("category", "lidarr-yt"),
        "save_path": job["save_path"],
        "content_path": job["content_path"] or job["save_path"],
        "added_on": job["added_on"],
        "completion_on": job["completion_on"] or (int(time.time()) if done else 0),
        "amount_left": 0 if done else max(size - int(size * progress), 1),
        "num_seeds": 999,
        "num_leechs": 0,
        "ratio": 1.0,
        "tags": "",
    }


@app.get("/api/v2/torrents/info")
async def qbit_torrents_info(request: Request, hashes: str | None = None):
    category = request.query_params.get("category")
    all_j = jobs.all_jobs()
    if hashes:
        wanted = set(hashes.split("|"))
        all_j = [j for j in all_j if j["hash"] in wanted]
    if category:
        all_j = [j for j in all_j if j.get("category", "") == category]
    return JSONResponse([_to_qbit_torrent(j) for j in all_j])


@app.post("/api/v2/torrents/delete")
async def qbit_torrents_delete(request: Request):
    # Lidarr sendet deleteFiles als Form-Parameter, aber unterschiedliche
    # Versionen nutzen unterschiedliche Schreibweisen (deleteFiles / deletefiles).
    # Daher Form manuell parsen und case-insensitiv auswerten.
    form = await request.form()
    hashes_raw = form.get("hashes") or form.get("Hashes") or ""
    delete_files_raw = (
        form.get("deleteFiles") or form.get("deletefiles") or
        form.get("DeleteFiles") or "false"
    )
    delete_files = delete_files_raw.lower() == "true"

    print(f"[delete] hashes={hashes_raw!r} deleteFiles={delete_files}", flush=True)

    for h in [x.strip() for x in hashes_raw.split("|") if x.strip()]:
        job = jobs.get_job(h)
        content = job.get("content_path") if job else None
        print(f"[delete] hash={h} content_path={content!r} delete_files={delete_files}", flush=True)

        if content and os.path.exists(content):
            # Wir loeschen immer wenn Lidarr uns dazu auffordert (deleteFiles=true)
            # ODER wenn delete_files nicht gesetzt ist aber die Datei noch da ist
            # (Lidarr hat sie per Copy importiert, nicht per Move).
            if delete_files:
                try:
                    if os.path.isdir(content):
                        import shutil
                        shutil.rmtree(content)
                        print(f"[delete] Ordner geloescht: {content}", flush=True)
                    else:
                        os.remove(content)
                        print(f"[delete] Datei geloescht: {content}", flush=True)
                except OSError as e:
                    print(f"[delete] Fehler beim Loeschen von {content}: {e}", flush=True)
        elif content:
            print(f"[delete] Datei nicht mehr vorhanden (bereits verschoben?): {content}", flush=True)

        jobs.delete_job(h)
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/pause")
async def qbit_torrents_pause():
    return PlainTextResponse("Ok.")


@app.post("/api/v2/torrents/resume")
async def qbit_torrents_resume():
    return PlainTextResponse("Ok.")


@app.get("/")
async def root():
    return PlainTextResponse("Lidarr YT-DLP Bridge laeuft.")
