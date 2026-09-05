#!/usr/bin/env python3
"""Fetch dei feed PF Concept (fornitore 34).

Quattro feed JSON (condizioni d'uso PF: URL e dati prezzi/stock sono
CONFIDENZIALI -> gli URL con token stanno SOLO nelle variabili d'ambiente):
  PF_PRODUCTFEED_URL     prodotti IT (giornaliero)         - default URL pubblico IT v3
  PF_PRODUCTFEEDWS_URL   prodotti WS (giornaliero)         - default URL pubblico IT v3
  PF_PRICEFEED_URL       prezzi (settimanale, token)       - OBBLIGATORIA
  PF_STOCKFEED_URL       stock 2x/giorno (token)           - OBBLIGATORIA

Uso: fetch_pfconcept.py --out DIR [--dumps-cache DIR] [--refresh tutto|prezzi|stock]
Scrive {out}/snapshot/{prodotti,prodotti_ws,prezzi,stock}.json; i feed non
richiesti dal refresh arrivano dalla cache (che fa anche da fallback di rete).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import requests

FEEDS = {
    "prodotti": ("PF_PRODUCTFEED_URL",
                 "https://www.pfconcept.com/portal/datafeed/productfeed_it_v3.json"),
    "prodotti_ws": ("PF_PRODUCTFEEDWS_URL",
                    "https://www.pfconcept.com/portal/datafeed/productfeedws_it_v3.json"),
    "prezzi": ("PF_PRICEFEED_URL", None),
    "prezzi_ws": ("PF_PRICEFEEDWS_URL",
                  "https://www.pfconcept.com/portal/datafeed/pricefeedWS_CIT1_v3.json"),
    "stock": ("PF_STOCKFEED_URL", None),
}
DA_SCARICARE = {
    "tutto": {"prodotti", "prodotti_ws", "prezzi", "prezzi_ws", "stock"},
    "prezzi": {"prezzi", "prezzi_ws", "stock"},
    "stock": {"stock"},
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dumps-cache", default=None)
    ap.add_argument("--refresh", default="tutto", choices=sorted(DA_SCARICARE))
    ap.add_argument("--all", action="store_true")  # per uniformita' con i gemelli
    args = ap.parse_args(argv)

    out = Path(args.out) / "snapshot"
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.dumps_cache) if args.dumps_cache else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    scarica = DA_SCARICARE[args.refresh]

    for nome, (env, default_url) in FEEDS.items():
        dest = out / f"{nome}.json"
        in_cache = cache / f"{nome}.json" if cache else None
        if nome not in scarica:
            if in_cache and in_cache.exists():
                shutil.copyfile(in_cache, dest)
                continue
            # cache mancante: si scarica comunque
        url = os.environ.get(env, "").strip() or default_url
        if not url:
            if in_cache and in_cache.exists():
                print(f"{env} non configurata: uso la cache per {nome}", file=sys.stderr)
                shutil.copyfile(in_cache, dest)
                continue
            print(f"{env} non configurata e nessuna cache per {nome}", file=sys.stderr)
            return 2
        try:
            r = requests.get(url, timeout=900)
            r.raise_for_status()
            dati = r.json()
            if not isinstance(dati, dict) or not dati:
                raise ValueError(f"feed {nome} inatteso: {type(dati).__name__}")
            testo = json.dumps(dati, ensure_ascii=False)
            dest.write_text(testo, encoding="utf-8")
            if in_cache:
                in_cache.write_text(testo, encoding="utf-8")
            print(f"feed {nome} scaricato ({len(testo) // 1024} KiB)")
        except Exception as e:                                # noqa: BLE001
            print(f"download feed {nome} fallito: {e}", file=sys.stderr)
            if in_cache and in_cache.exists():
                print(f"uso la cache dell'ultimo {nome} buono", file=sys.stderr)
                shutil.copyfile(in_cache, dest)
            else:
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
