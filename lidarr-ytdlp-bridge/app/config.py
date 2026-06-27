import os

VERSION = "1.6.0"

# API-Key, den Lidarr beim Indexer mitschicken muss
API_KEY = os.environ.get("API_KEY", "changeme")

# Zugangsdaten fuer die qBittorrent-Emulation (in Lidarr beim Download-Client eintragen)
QBIT_USERNAME = os.environ.get("QBIT_USERNAME", "admin")
QBIT_PASSWORD = os.environ.get("QBIT_PASSWORD", "adminadmin")

# Muss EXAKT der gleiche Pfad sein, den Lidarr fuer den Download-Ordner sieht
# (gleicher Volume-Mount in docker-compose, kein Remote-Path-Mapping noetig)
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")

# Optionale cookies.txt fuer eingeloggte/altersbeschraenkte/regionale YouTube-Inhalte
COOKIES_FILE = os.environ.get("COOKIES_FILE", "/config/cookies.txt")

# Anzahl Suchergebnisse pro Anfrage
SEARCH_LIMIT = int(os.environ.get("SEARCH_LIMIT", "20"))

PORT = int(os.environ.get("PORT", "9117"))

INDEXER_NAME = os.environ.get("INDEXER_NAME", "YT-DLP Bridge")

# Sekunden nach Abschluss bis die heruntergeladene Datei automatisch geloescht wird.
# 0 = nie loeschen.
CLEANUP_AFTER = int(os.environ.get("CLEANUP_AFTER", "3600"))
