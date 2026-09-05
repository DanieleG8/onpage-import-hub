#!/usr/bin/env python3
"""Fetch del feed NEW WAVE / NWG (fornitore A1).

Un unico download JSON (~151 MB, 2.278 prodotti con variazioni colore e SKU)
dall'URL con token fornito da NWG. L'URL sta SOLO nella variabile d'ambiente
NWG_FEED_URL (Railway) e non va mai committato.

Uso: fetch_nwg.py --out DIR [--dumps-cache DIR] [--refresh ...]
Scrive {out}/snapshot/feed.json; la cache (se indicata) conserva l'ultimo feed
buono e fa da fallback se il download fallisce. --refresh e' accettato per
uniformita' col gemello Makito ma il feed e' unico: si riscarica sempre.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import requests


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dumps-cache", default=None)
    ap.add_argument("--refresh", default="tutto")  # ignorato: feed unico
    ap.add_argument("--all", action="store_true")  # idem, per uniformita'
    args = ap.parse_args(argv)

    out = Path(args.out) / "snapshot"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "feed.json"
    cache = Path(args.dumps_cache) / "feed.json" if args.dumps_cache else None

    url = os.environ.get("NWG_FEED_URL", "").strip()
    if not url:
        if cache and cache.exists():
            print("NWG_FEED_URL non configurata: uso la cache", file=sys.stderr)
            shutil.copyfile(cache, dest)
            return 0
        print("NWG_FEED_URL non configurata e nessuna cache disponibile", file=sys.stderr)
        return 2

    try:
        r = requests.get(url, timeout=600)
        r.raise_for_status()
        dati = r.json()               # valida che sia JSON prima di salvare
        if not isinstance(dati, list) or not dati:
            raise ValueError(f"feed inatteso: type={type(dati).__name__} len={len(dati) if isinstance(dati, list) else '-'}")
        dest.write_text(json.dumps(dati, ensure_ascii=False), encoding="utf-8")
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dest, cache)
        print(f"feed NWG scaricato: {len(dati)} prodotti")
        return 0
    except Exception as e:                                    # noqa: BLE001
        print(f"download feed NWG fallito: {e}", file=sys.stderr)
        if cache and cache.exists():
            print("uso la cache dell'ultimo feed buono", file=sys.stderr)
            shutil.copyfile(cache, dest)
            return 0
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
