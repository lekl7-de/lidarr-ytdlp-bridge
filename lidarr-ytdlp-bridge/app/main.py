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
        results = [{
            "video_id": "dQw4w9WgXcQ",
            "title": "Indexer Connectivity Test",
            "duration": 0,
            "size": 1,
        }]
    else:
        try:
            results = ytdlp_helper.search_youtube(query)
        except Exception as e:
            print(f"[search] Fehler: {e}", flush=True)
            results = []

    items = []
    now = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())
    base = str(request.base_url).rstrip("/")
    for r in results:
        magnet = jobs.build_magnet(r["video_id"], r["title"])
        h = jobs.make_hash(r["video_id"])
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
    magnet = jobs.build_magnet(entry["video_id"], entry["title"])
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
            entry = _fallback_from_dn(magnet)
        if not entry:
            print(f"[add] Konnte Video-ID nicht aus Magnet ermitteln: {magnet}", flush=True)
            continue

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
    return {"video_id": video_id, "title": title}


def _to_qbit_torrent(job: dict) -> dict:
    progress = job["progress"]
    return {
        "hash": job["hash"],
        "name": job["name"],
        "size": job["size"],
        "progress": progress,
        "dlspeed": 0,
        "upspeed": 0,
        "eta": 0 if progress >= 1 else 8640000,
        "state": job["state"],
        "category": job.get("category", ""),
        "save_path": job["save_path"],
        "content_path": job["content_path"] or job["save_path"],
        "added_on": job["added_on"],
        "completion_on": job["completion_on"],
        "amount_left": 0 if progress >= 1 else max(job["size"] - int(job["size"] * progress), 1),
        "num_seeds": 999,
        "num_leechs": 0,
        "ratio": 1.0,
        "tags": "",
    }


@app.get("/api/v2/torrents/info")
async def qbit_torrents_info(hashes: str | None = None):
    all_j = jobs.all_jobs()
    if hashes:
        wanted = set(hashes.split("|"))
        all_j = [j for j in all_j if j["hash"] in wanted]
    return JSONResponse([_to_qbit_torrent(j) for j in all_j])


@app.post("/api/v2/torrents/delete")
async def qbit_torrents_delete(hashes: str = Form(...), deleteFiles: str = Form("false")):
    for h in hashes.split("|"):
        if deleteFiles.lower() == "true":
            job = jobs.get_job(h)
            if job and job.get("content_path") and os.path.exists(job["content_path"]):
                try:
                    os.remove(job["content_path"])
                except OSError:
                    pass
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
