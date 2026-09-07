#!/usr/bin/env python3
"""Fetch dei file GARY'S (fornitore A3) dalla cartella SharePoint dell'importatore.

Sorgente primaria: SharePoint (Microsoft Graph, credenziali applicazione):
  sito   SP_SITE    (default: pantapubblicitait.sharepoint.com:/sites/FL-GESTIONALE)
  path   {SP_BASE}/A3   (default SP_BASE: importatore)
  env    SP_TENANT_ID, SP_CLIENT_ID, SP_CLIENT_SECRET (Railway, mai nel repo)
Nella cartella si depositano il catalogo (*.xlsx, export Shopify) e il
listino ("Listino*.xls"). Fallback: cartella Dropbox condivisa
(GARYS_DROPBOX_URL, zip con dl=1), poi la cache dell'ultimo giro buono.

Uso: fetch_garys.py --out DIR [--dumps-cache DIR] [--refresh ...]
Scrive {out}/snapshot/catalogo.xlsx e {out}/snapshot/listino.xls.
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import zipfile
from pathlib import Path

import requests

FILE_ATTESI = ("catalogo.xlsx", "listino.xls")
GRAPH = "https://graph.microsoft.com/v1.0"


# ------------------------------------------------------------- sharepoint --
def _sp_token() -> str:
    tenant = os.environ["SP_TENANT_ID"].strip()
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials",
              "client_id": os.environ["SP_CLIENT_ID"].strip(),
              "client_secret": os.environ["SP_CLIENT_SECRET"].strip(),
              "scope": "https://graph.microsoft.com/.default"},
        timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


def _sp_scarica(out: Path) -> None:
    """Scarica catalogo e listino da {SP_BASE}/A3 del sito SP_SITE."""
    sito = os.environ.get("SP_SITE",
                          "pantapubblicitait.sharepoint.com:/sites/FL-GESTIONALE").strip()
    base = os.environ.get("SP_BASE", "importatore").strip().strip("/")
    h = {"authorization": f"Bearer {_sp_token()}"}

    r = requests.get(f"{GRAPH}/sites/{sito}", headers=h, timeout=60)
    r.raise_for_status()
    site_id = r.json()["id"]
    r = requests.get(f"{GRAPH}/sites/{site_id}/drive/root:/{base}/A3:/children",
                     headers=h, timeout=60)
    r.raise_for_status()
    catalogo = listino = None
    for item in r.json().get("value", []):
        nome = item.get("name", "").lower()
        if "file" not in item:
            continue
        if nome.endswith(".xlsx") and not nome.startswith("~"):
            catalogo = item
        elif nome.endswith(".xls") and "listino" in nome:
            listino = item
    if catalogo is None or listino is None:
        raise ValueError(
            f"nella cartella SharePoint {base}/A3 mancano i file attesi: serve un "
            ".xlsx (catalogo) e un 'Listino*.xls' (trovati: "
            f"{[i.get('name') for i in r.json().get('value', [])][:10]})")
    for item, dest in ((catalogo, "catalogo.xlsx"), (listino, "listino.xls")):
        url = item.get("@microsoft.graph.downloadUrl")
        rf = requests.get(url, timeout=300)
        rf.raise_for_status()
        (out / dest).write_bytes(rf.content)
    print(f"GARY'S da SharePoint: catalogo {catalogo['size'] // 1024} KiB "
          f"({catalogo['name']}), listino {listino['size'] // 1024} KiB ({listino['name']})")


# ---------------------------------------------------------------- dropbox --
def _dl1(url: str) -> str:
    if "dl=" in url:
        return url.replace("dl=0", "dl=1")
    return f"{url}{'&' if '?' in url else '?'}dl=1"


def _dropbox_scarica(out: Path, url: str) -> None:
    r = requests.get(_dl1(url), timeout=600)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    catalogo = listino = None
    for info in z.infolist():
        nome = info.filename.rsplit("/", 1)[-1].lower()
        if nome.endswith(".xlsx") and not nome.startswith("~"):
            catalogo = info
        elif nome.endswith(".xls") and "listino" in nome:
            listino = info
    if catalogo is None or listino is None:
        raise ValueError(
            "nella cartella Dropbox mancano i file attesi (.xlsx + Listino*.xls): "
            f"{[i.filename for i in z.infolist()][:10]}")
    (out / "catalogo.xlsx").write_bytes(z.read(catalogo))
    (out / "listino.xls").write_bytes(z.read(listino))
    print(f"GARY'S da Dropbox: catalogo {catalogo.file_size // 1024} KiB, "
          f"listino {listino.file_size // 1024} KiB")


# ------------------------------------------------------------------- main --
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dumps-cache", default=None)
    ap.add_argument("--refresh", default="tutto")  # feed unico: si riscarica sempre
    ap.add_argument("--all", action="store_true")  # per uniformita' coi gemelli
    args = ap.parse_args(argv)

    out = Path(args.out) / "snapshot"
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.dumps_cache) if args.dumps_cache else None

    problemi = []
    if all(os.environ.get(k, "").strip()
           for k in ("SP_TENANT_ID", "SP_CLIENT_ID", "SP_CLIENT_SECRET")):
        try:
            _sp_scarica(out)
        except Exception as e:                                # noqa: BLE001
            problemi.append(f"SharePoint fallito: {e}")
    else:
        problemi.append("credenziali SP_* non configurate")

    if problemi:
        url = os.environ.get("GARYS_DROPBOX_URL", "").strip()
        if url:
            try:
                _dropbox_scarica(out, url)
                problemi = []
            except Exception as e:                            # noqa: BLE001
                problemi.append(f"Dropbox fallito: {e}")
        else:
            problemi.append("GARYS_DROPBOX_URL non configurata")

    if problemi:
        for p in problemi:
            print(p)          # su stdout: il runner lo salva nel log dell'esito
        if cache and all((cache / f).exists() for f in FILE_ATTESI):
            print("uso la cache degli ultimi file buoni")
            for f in FILE_ATTESI:
                shutil.copyfile(cache / f, out / f)
            return 0
        print("nessuna cache completa disponibile")
        return 2

    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        for f in FILE_ATTESI:
            shutil.copyfile(out / f, cache / f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
