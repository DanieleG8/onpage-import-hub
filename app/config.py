"""Configurazione del hub: tutto da variabili d'ambiente, con degradazione morbida
(convenzione orderEntry: un secret mancante spegne la feature, non crasha il servizio)."""
from __future__ import annotations

import os
from pathlib import Path

# stato persistente (Railway Volume montato su /data; in locale una dir qualsiasi)
STATE_DIR = Path(os.environ.get("STATE_DIR", "/data")).resolve()

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

# scheduler (cron in UTC). Per Makito la variabile VUOTA ricade sul default
# (il 06/09 delle CRON_MAKITO_* vuote su Railway hanno spento i giri per ore);
# per spegnere un cron Makito mettere "off" (scartato e visibile in /health).
# Per NWG/PF invece vuoto = job disattivato (default: spenti fino al collaudo).
CRON_MAKITO_STOCK = os.environ.get("CRON_MAKITO_STOCK") or "12 * * * *"        # ogni ora
CRON_MAKITO_PREZZI = os.environ.get("CRON_MAKITO_PREZZI") or "40 4 * * *"      # ogni giorno
CRON_MAKITO_PRODOTTI = os.environ.get("CRON_MAKITO_PRODOTTI") or "10 3 * * 1"  # lunedi'
# NWG: spento di default finche' il collaudo non e' concluso
CRON_NWG_PRODOTTI = os.environ.get("CRON_NWG_PRODOTTI", "")
# PF Concept: spenti di default finche' il collaudo non e' concluso.
# Cadenze consigliate dal fornitore: stock 2x/giorno (mattina e ~13:00),
# prodotti giornaliero, prezzi settimanale (aggiornati nel weekend).
#
# ATTENZIONE agli orari (misurato il 12/09/2026 con /feed-diff). Il feed stock
# si rigenera due volte al giorno, alle ~23:18 e alle ~11:04 UTC. Con lo stock
# alle 05:00 e alle 11:00 succedeva questo:
#   - 05:00 e 11:00 leggevano la generazione delle 23:18, gia' consumata dal
#     job prodotti delle 02:20 -> trovavano 2 articoli;
#   - la generazione delle 11:04 non la leggeva NESSUNO;
#   - tutto il movimento di giacenze del giorno finiva addosso a "prodotti",
#     che ci metteva 2h46m per ~1.450 articoli.
# I cron dello stock vanno DOPO le generazioni: "30 11,23 * * *".
CRON_PF_STOCK = os.environ.get("CRON_PF_STOCK", "")        # consigliato "30 11,23 * * *"
CRON_PF_PRODOTTI = os.environ.get("CRON_PF_PRODOTTI", "")  # es. "20 2 * * *"
CRON_PF_PREZZI = os.environ.get("CRON_PF_PREZZI", "")      # es. "50 2 * * 1"
# GARY'S: i file arrivano dal deposito manuale su Dropbox -> niente cron di
# default; quando il deposito sara' automatico basta valorizzare la variabile.
CRON_GARYS_PRODOTTI = os.environ.get("CRON_GARYS_PRODOTTI", "")


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
