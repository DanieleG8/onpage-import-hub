#!/usr/bin/env python3
"""Validazione strutturale dei payload NWG (fornitore A1) prima dell'invio.

Controlli sul contratto CustomImportRequest e sulle decisioni di progetto:
chiavi coerenti, immagini HTTPS, ciclo MAI presente, moltiplicatoreVendita
null (verra' impostato in futuro su OnPage), solo listini "acquisto
rivenditori" con prezzo > 0, giacenze vuote (niente API stock per ora).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

RE_CHIAVE_ART = re.compile(r"^A1-[A-Za-z0-9]+$")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    errori = []

    def e(chiave, msg):
        errori.append(f"{chiave}: {msg}")

    files = sorted((Path(args.out) / "json").glob("A1-*.json"))
    for fp in files:
        d = json.loads(fp.read_text(encoding="utf-8"))
        a = d.get("articolo") or {}
        chiave = a.get("chiaveArticolo") or fp.stem
        if not RE_CHIAVE_ART.match(a.get("chiaveArticolo") or ""):
            e(chiave, "chiaveArticolo non valida")
        # L'importer rifiuta la stringa vuota su questi due con "Campo
        # obbligatorio mancante", sempre, per la stessa chiave a ogni giro: e'
        # un errore che deve fermarsi qui, non consumare un invio ogni notte
        # (A1-C19222 lo ha fatto dal 10/09/2026). makito/validate.py lo
        # controllava gia'; qui mancava.
        for campo in ("descrizioneBreve", "descrizioneLunga"):
            if not isinstance(a.get(campo), str) or not a[campo].strip():
                e(chiave, f"articolo.{campo} mancante o vuoto")
        if "ciclo" in a:
            e(chiave, "articolo.ciclo non va inviato (cicli gestiti a mano su OnPage)")
        forn = a.get("fornitore") or {}
        if forn.get("sigla") != "A1" or forn.get("nome") != "NEW WAVE":
            e(chiave, f"fornitore inatteso: {forn}")
        if forn.get("moltiplicatoreVendita") is not None:
            e(chiave, "moltiplicatoreVendita deve restare null (decisione 05/09/2026)")
        for url in [a.get("immagine"), a.get("immagineAmbientata")] + \
                   [i.get("url") for i in a.get("immagini") or []]:
            if url and not url.startswith("https://"):
                e(chiave, f"immagine non HTTPS: {url[:80]}")
        chiavi_v = set()
        for v in a.get("varianti") or []:
            cv = v.get("codiceVariante") or ""
            if not cv.startswith(f"{a.get('chiaveArticolo')}-"):
                e(chiave, f"codiceVariante incoerente: {cv}")
            if cv in chiavi_v:
                e(chiave, f"variante duplicata: {cv}")
            chiavi_v.add(cv)
            if v.get("giacenze"):
                e(chiave, f"{cv}: giacenze inattese (il feed NWG non le fornisce)")
            for l in v.get("listini") or []:
                if l.get("tipologiaListino") != "acquisto rivenditori":
                    e(chiave, f"{cv}: tipologia listino inattesa {l.get('tipologiaListino')}")
                if not l.get("prezzo") or l["prezzo"] <= 0:
                    e(chiave, f"{cv}: prezzo non positivo")
            if v.get("immagine") and not v["immagine"].startswith("https://"):
                e(chiave, f"{cv}: immagine variante non HTTPS")
        if not a.get("varianti"):
            e(chiave, "articolo senza varianti")

    print(f"validati {len(files)} payload; errori: {len(errori)}")
    for r in errori[:50]:
        print("  [error]", r)
    return 1 if errori else 0


if __name__ == "__main__":
    raise SystemExit(main())
