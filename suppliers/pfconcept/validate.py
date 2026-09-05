#!/usr/bin/env python3
"""Validazione strutturale dei payload PF Concept (fornitore 34).

Controlli sul contratto CustomImportRequest e sulle decisioni di progetto:
fornitore 34 con moltiplicatore 3.5 ed ERP 47086, listini SOLO "acquisto
riservato" (scaglione stringa, multipli numerico, prezzo > 0), ciclo MAI,
immagini HTTPS, giacenze con dataArrivo nulla o ISO.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

RE_CHIAVE_ART = re.compile(r"^34-[A-Za-z0-9]+$")
RE_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    errori = []

    def e(chiave, msg):
        errori.append(f"{chiave}: {msg}")

    files = sorted((Path(args.out) / "json").glob("34-*.json"))
    for fp in files:
        d = json.loads(fp.read_text(encoding="utf-8"))
        a = d.get("articolo") or {}
        chiave = a.get("chiaveArticolo") or fp.stem
        if not RE_CHIAVE_ART.match(a.get("chiaveArticolo") or ""):
            e(chiave, "chiaveArticolo non valida")
        if "ciclo" in a:
            e(chiave, "articolo.ciclo non va inviato (cicli gestiti a mano su OnPage)")
        forn = a.get("fornitore") or {}
        if forn.get("sigla") != "34" or forn.get("moltiplicatoreVendita") != 3.5:
            e(chiave, f"fornitore inatteso: {forn}")
        if (forn.get("codiceFornitoreERP") or {}).get("erp_id") != "47086":
            e(chiave, "codiceFornitoreERP deve essere 47086")
        if a.get("fornitoreErpIds") != [92328878]:
            e(chiave, f"fornitoreErpIds inatteso: {a.get('fornitoreErpIds')}")
        for url in [a.get("immagine"), a.get("immagineAmbientata")] + \
                   [i.get("url") for i in a.get("immagini") or []]:
            if url and not url.startswith("https://"):
                e(chiave, f"immagine non HTTPS: {url[:80]}")
        chiavi_v = set()
        for v in a.get("varianti") or []:
            cv = v.get("codiceVariante") or ""
            if not cv.startswith("34-"):
                e(chiave, f"codiceVariante incoerente: {cv}")
            if cv in chiavi_v:
                e(chiave, f"variante duplicata: {cv}")
            chiavi_v.add(cv)
            for l in v.get("listini") or []:
                if l.get("tipologiaListino") != "acquisto riservato":
                    e(chiave, f"{cv}: tipologia listino inattesa {l.get('tipologiaListino')}")
                if not isinstance(l.get("scaglione"), str) or not l["scaglione"]:
                    e(chiave, f"{cv}: scaglione deve essere stringa non vuota")
                if not isinstance(l.get("multipli"), (int, float)):
                    e(chiave, f"{cv}: multipli deve essere numerico")
                if not l.get("prezzo") or l["prezzo"] <= 0:
                    e(chiave, f"{cv}: prezzo non positivo")
            for g in v.get("giacenze") or []:
                da = g.get("dataArrivo")
                if da is not None and not RE_DATA.match(da):
                    e(chiave, f"{cv}: dataArrivo non ISO: {da}")
                if not isinstance(g.get("quantita"), int):
                    e(chiave, f"{cv}: quantita non intera")
        if not a.get("varianti"):
            e(chiave, "articolo senza varianti")

    print(f"validati {len(files)} payload; errori: {len(errori)}")
    for r in errori[:50]:
        print("  [error]", r)
    return 1 if errori else 0


if __name__ == "__main__":
    raise SystemExit(main())
