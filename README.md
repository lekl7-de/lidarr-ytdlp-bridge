# Lidarr ⇄ yt-dlp Translation Layer

A single Python web server that simultaneously acts as both a

* **Torznab indexer** (search)
* **qBittorrent-compatible download client** (downloads)

for Lidarr. In the background, it uses `yt-dlp` to interact with YouTube.

## How It Works

1. **Search (Auto Search or Interactive Search):**
   Lidarr queries the indexer using `t=search&q=...`. The server executes `yt-dlp ytsearch...` and returns the results as a Torznab RSS feed. Each result receives a "magnet link" that internally references the corresponding YouTube video ID.

2. **Download Request:**
   Lidarr sends this magnet link to the (pretended) qBittorrent client via `/api/v2/torrents/add`. The server extracts the YouTube video ID and starts a background `yt-dlp` process (best available audio, converted to MP3). It also downloads the thumbnail, crops it to a square format, and embeds it into the MP3 as cover art (ID3 APIC) using `mutagen`.

3. **Progress Reporting:**
   Lidarr periodically polls `/api/v2/torrents/info`. The server reports download progress and status until the file is finished. Once complete, it reports `state: uploading` ("finished, seeding")—this is the trick that makes Lidarr recognize the download as completed.

4. **Import:**
   From this point onward, Lidarr behaves normally (file appears in the download directory → imported into the library).

## cookies.txt (Optional)

If a file exists at the path configured via `COOKIES_FILE` (default: `/config/cookies.txt`), it will automatically be passed to **every** `yt-dlp` invocation using the `--cookies` option.

This is useful for age-restricted or region-locked content. If the file is not present, everything works anonymously as usual.

## Setup

1. Place this folder (`lidarr-ytdlp-bridge/`) next to your existing `docker-compose.yml`.

2. Use `docker-compose.example.yml` as a template and merge it into your existing Compose configuration.

   **Important:** Both the bridge container and Lidarr must mount the same host path for `/downloads`. If they do, Lidarr does not require any "Remote Path Mapping".

3. Adjust the following values:

   * `API_KEY`
   * `QBIT_USERNAME`
   * `QBIT_PASSWORD`

   These values can be chosen freely, but make sure to remember them—you will need them when configuring Lidarr.

4. Start the service:

   ```bash
   docker compose up -d --build lidarr-ytdlp-bridge
   ```

## Configuring Lidarr

### Add the Indexer

`Settings → Indexers → Add (+) → Torznab → Generic Torznab`

| Field          | Value                             |
| -------------- | --------------------------------- |
| Name           | YT-DLP Bridge                     |
| URL            | `http://lidarr-ytdlp-bridge:9117` |
| API Path       | `/api`                            |
| API Key        | your `API_KEY`                    |
| Categories     | `3000` (Audio)                    |
| Search Options | Leave all enabled                 |

→ The "Test" button should succeed.

### Add the Download Client

`Settings → Download Clients → Add (+) → qBittorrent`

| Field    | Value                  |
| -------- | ---------------------- |
| Name     | YT-DLP Downloader      |
| Host     | `lidarr-ytdlp-bridge`  |
| Port     | `9117`                 |
| Username | your `QBIT_USERNAME`   |
| Password | your `QBIT_PASSWORD`   |
| Category | `lidarr-yt` (optional) |

→ The "Test" button should succeed.

After that, simply open an album and run **Interactive Search**, or let **Auto Search** do its job. The YouTube results will appear just like regular releases.

## Known Limitations

* The **file size** shown during search is only an approximation (duration × 320 kbps), since the actual file size is unknown until the download completes. This does not bother Lidarr, but it may affect quality/size profiles if you have strict limits configured.

* The magnet cache (mapping hashes to video IDs) is stored in memory only. Restarting the container **between** search and download is generally harmless because the video ID is also embedded in the displayed name as a fallback. However, this fallback will not work indefinitely if a long time passes and the name information is lost.

* No real BitTorrent or Newznab infrastructure is used. The application merely emulates the parts of those APIs that Lidarr actually requires.

* By default, only 2 downloads run in parallel (`ThreadPoolExecutor(max_workers=2)` in `app/main.py`). Increase this value if needed.

## Environment Variables

| Variable        | Default               | Description                                    |
| --------------- | --------------------- | ---------------------------------------------- |
| `API_KEY`       | `changeme`            | API key for the indexer                        |
| `QBIT_USERNAME` | `admin`               | Login username for the fake qBittorrent client |
| `QBIT_PASSWORD` | `adminadmin`          | Password for the fake qBittorrent client       |
| `DOWNLOAD_DIR`  | `/downloads`          | Destination directory for completed MP3 files  |
| `COOKIES_FILE`  | `/config/cookies.txt` | Optional cookies.txt file                      |
| `SEARCH_LIMIT`  | `20`                  | Number of search results returned per request  |
