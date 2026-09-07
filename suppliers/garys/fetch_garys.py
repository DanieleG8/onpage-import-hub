#!/usr/bin/env python3
"""Fetch dei file GARY'S (fornitore A3) dalla cartella Dropbox condivisa.

Un solo download: lo zip della cartella (link condiviso con dl=1), che
contiene il listino ("Listino*.xls") e il catalogo ("*.xlsx") depositati
da Daniele o dal fornitore. L'URL con rlkey sta SOLO nella variabile
d'ambiente GARYS_DROPBOX_URL (Railway) e non va mai committato.

Uso: fetch_garys.py --out DIR [--dumps-cache DIR] [--refresh ...]
Scrive {out}/snapshot/catalogo.xlsx e {out}/snapshot/listino.xls; la cache
conserva gli ultimi file buoni e fa da fallback se il download fallisce.
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


def _dl1(url: str) -> str:
    """Forza dl=1 sull'URL condiviso Dropbox (dl=0 = pagina web, non lo zip)."""
    if "dl=" in url:
        return url.replace("dl=0", "dl=1")
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}dl=1"


def _da_cache(cache: Path | None, out: Path, motivo: str) -> int:
    if cache and all((cache / f).exists() for f in FILE_ATTESI):
        print(f"{motivo}: uso la cache degli ultimi file buoni", file=sys.stderr)
        for f in FILE_ATTESI:
            shutil.copyfile(cache / f, out / f)
        return 0
    print(f"{motivo} e nessuna cache completa disponibile", file=sys.stderr)
    return 2


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

    url = os.environ.get("GARYS_DROPBOX_URL", "").strip()
    if not url:
        return _da_cache(cache, out, "GARYS_DROPBOX_URL non configurata")

    try:
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
                "nella cartella Dropbox mancano i file attesi: serve un .xlsx "
                "(catalogo export Shopify) e un 'Listino*.xls' "
                f"(trovati: {[i.filename for i in z.infolist()][:10]})")
        (out / "catalogo.xlsx").write_bytes(z.read(catalogo))
        (out / "listino.xls").write_bytes(z.read(listino))
        if cache:
            cache.mkdir(parents=True, exist_ok=True)
            for f in FILE_ATTESI:
                shutil.copyfile(out / f, cache / f)
        print(f"GARY'S scaricato: catalogo {catalogo.file_size // 1024} KiB "
              f"({catalogo.filename}), listino {listino.file_size // 1024} KiB")
        return 0
    except Exception as e:                                    # noqa: BLE001
        return _da_cache(cache, out, f"download Dropbox GARY'S fallito: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
