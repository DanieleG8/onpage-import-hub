"""Configurazione del hub: tutto da variabili d'ambiente, con degradazione morbida
(convenzione orderEntry: un secret mancante spegne la feature, non crasha il servizio)."""
from __future__ import annotations

import os
from pathlib import Path

# stato persistente (Railway Volume montato su /data; in locale una dir qualsiasi)
STATE_DIR = Path(os.environ.get("STATE_DIR", "/data"))

# URL pubblico del hub (per gli URL immagine via proxy nei payload)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# auth degli endpoint macchina (/run, /status, /report)
HUB_API_KEY = os.environ.get("HUB_API_KEY", "")

# credenziali Makito (portale nuovo apis.makito.es)
MAKITO_CLIENT_ID = os.environ.get("MAKITO_CLIENT_ID", "")
MAKITO_CLIENT_SECRET = os.environ.get("MAKITO_CLIENT_SECRET", "")

# importer Custom OnPage
IMPORTER_URL = os.environ.get(
    "IMPORTER_URL",
    "https://exporter.plesk.onpage.it/api/projects/PantaSupplierFCustom/run-and-download")
IMPORTER_TOKEN = os.environ.get("IMPORTER_TOKEN", "")

# invio parallelo
SEND_WORKERS = int(os.environ.get("SEND_WORKERS", "3"))

# scheduler (cron in UTC); vuoto = job disattivato
CRON_MAKITO_STOCK = os.environ.get("CRON_MAKITO_STOCK", "12 * * * *")        # ogni ora
CRON_MAKITO_PREZZI = os.environ.get("CRON_MAKITO_PREZZI", "40 4 * * *")      # ogni giorno
CRON_MAKITO_PRODOTTI = os.environ.get("CRON_MAKITO_PRODOTTI", "10 3 * * 1")  # lunedi'


def not_configured() -> list[str]:
    """Nomi delle variabili obbligatorie mancanti (per /health e i log)."""
    mancanti = []
    if not MAKITO_CLIENT_ID or not MAKITO_CLIENT_SECRET:
        mancanti.append("MAKITO_CLIENT_ID/MAKITO_CLIENT_SECRET")
    if not HUB_API_KEY:
        mancanti.append("HUB_API_KEY")
    if not PUBLIC_BASE_URL:
        mancanti.append("PUBLIC_BASE_URL")
    return mancanti
