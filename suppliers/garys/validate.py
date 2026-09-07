#!/usr/bin/env python3
"""Validazione strutturale dei payload GARY'S (fornitore A3) prima dell'invio.

Controlli sul contratto CustomImportRequest e sulle decisioni del 07/09/2026:
chiavi coerenti, immagini HTTPS, ciclo MAI presente, moltiplicatoreVendita
null, listini solo "acquisto riservato"/"acquisto pubblico" con prezzo > 0
(scaglione stringa, multipli numerico), giacenze >= 0 su magazzino GARYS.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

RE_CHIAVE_ART = re.compile(r"^A3-[A-Za-z0-9]+$")
TIPOLOGIE = {"acquisto riservato", "acquisto pubblico"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    errori = []

    def e(chiave, msg):
        errori.append(f"{chiave}: {msg}")

    files = sorted((Path(args.out) / "json").glob("A3-*.json"))
    for fp in files:
        d = json.loads(fp.read_text(encoding="utf-8"))
        a = d.get("articolo") or {}
        chiave = a.get("chiaveArticolo") or fp.stem
        if not RE_CHIAVE_ART.match(a.get("chiaveArticolo") or ""):
            e(chiave, "chiaveArticolo non valida")
        if "ciclo" in a:
            e(chiave, "articolo.ciclo non va inviato (cicli gestiti a mano su OnPage)")
        forn = a.get("fornitore") or {}
        if forn.get("sigla") != "A3" or (forn.get("codiceFornitoreERP") or {}).get("erp_id") != "77110":
            e(chiave, f"fornitore inatteso: {forn}")
        if forn.get("moltiplicatoreVendita") is not None:
            e(chiave, "moltiplicatoreVendita deve restare null (scheda A3 senza valore)")
        if (d.get("parametriImport") or {}).get("rendiObsoletiArticoliNonPresenti"):
            e(chiave, "rendiObsoletiArticoliNonPresenti deve restare false")
        for url in [a.get("immagine")] + [i.get("url") for i in a.get("immagini") or []]:
            if url and not url.startswith("https://"):
                e(chiave, f"immagine non HTTPS: {url[:80]}")
            if url and (" " in url or "," in url):
                e(chiave, f"URL immagine sporco: {url[:80]}")
        chiavi_v = set()
        for v in a.get("varianti") or []:
            cv = v.get("codiceVariante") or ""
            if not cv.startswith("A3-"):
                e(chiave, f"codiceVariante incoerente: {cv}")
            if cv in chiavi_v:
                e(chiave, f"variante duplicata: {cv}")
            chiavi_v.add(cv)
            col = v.get("colore")
            if col is not None and not (col.get("codice") and col.get("nome")):
                e(chiave, f"{cv}: colore senza codice o nome")
            tg = v.get("taglia")
            if tg is not None and not tg.get("codice"):
                e(chiave, f"{cv}: taglia senza codice")
            for g in v.get("giacenze") or []:
                if (g.get("magazzino") or {}).get("codice") != "GARYS":
                    e(chiave, f"{cv}: magazzino inatteso {g.get('magazzino')}")
                if not isinstance(g.get("quantita"), int) or g["quantita"] < 0:
                    e(chiave, f"{cv}: quantita giacenza non valida {g.get('quantita')}")
                if g.get("dataArrivo") is not None:
                    e(chiave, f"{cv}: dataArrivo inattesa (il catalogo non ha arrivi)")
            for l in v.get("listini") or []:
                if l.get("tipologiaListino") not in TIPOLOGIE:
                    e(chiave, f"{cv}: tipologia listino inattesa {l.get('tipologiaListino')}")
                if not l.get("prezzo") or l["prezzo"] <= 0:
                    e(chiave, f"{cv}: prezzo listino non valido {l.get('prezzo')}")
                if not isinstance(l.get("scaglione"), str) or not l["scaglione"]:
                    e(chiave, f"{cv}: scaglione deve essere stringa non vuota")
                if not isinstance(l.get("multipli"), (int, float)):
                    e(chiave, f"{cv}: multipli deve essere numerico")

    for msg in errori[:40]:
        print("ERRORE:", msg)
    print(f"validati {len(files)} payload; errori: {len(errori)}")
    return 1 if errori else 0


if __name__ == "__main__":
    raise SystemExit(main())
