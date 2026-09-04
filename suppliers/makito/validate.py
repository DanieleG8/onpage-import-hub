#!/usr/bin/env python3
"""
validate.py - Controllo strutturale dei JSON in out/json contro DOCS/importer_format.md (CustomImportRequest).

Uso:  python src/validate.py --out out
Esce con codice 1 se trova errori.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TIPOLOGIE = {"acquisto pubblico", "acquisto riservato", "acquisto rivenditori", "vendita agenti"}
DATA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check(payload: dict, name: str, errors: list[str]):
    e = lambda m: errors.append(f"{name}: {m}")
    pi = payload.get("parametriImport")
    if not isinstance(pi, dict):
        e("parametriImport mancante")
    else:
        if not isinstance(pi.get("rendiObsoletiArticoliNonPresenti"), bool):
            e("parametriImport.rendiObsoletiArticoliNonPresenti deve essere bool")
        for k in ("gestioneImmagini", "gestioneGiacenze"):
            if pi.get(k) not in ("sostituisci", "aggiungi"):
                e(f"parametriImport.{k} non valido: {pi.get(k)!r}")
    a = payload.get("articolo")
    if not isinstance(a, dict):
        e("articolo mancante")
        return
    for k in ("codiceArticoloFornitore", "chiaveArticolo", "descrizioneBreve", "descrizioneLunga"):
        if not isinstance(a.get(k), str) or not a[k].strip():
            e(f"articolo.{k} mancante o vuoto")
    f = a.get("fornitore")
    if not isinstance(f, dict):
        e("articolo.fornitore mancante")
    else:
        for k in ("sigla", "nome"):
            if not isinstance(f.get(k), str) or not f[k]:
                e(f"articolo.fornitore.{k} mancante")
        # insidia F79 (CLAUDE.md par.6): 3.5 della raccolta fornitore, mai 1.06 di fornitori_erp
        if f.get("moltiplicatoreVendita") != 3.5:
            e(f"articolo.fornitore.moltiplicatoreVendita deve essere 3.5, non {f.get('moltiplicatoreVendita')!r}")
        if not isinstance(f.get("codiceFornitoreERP"), dict) or \
                not all(isinstance(v, str) for v in f.get("codiceFornitoreERP", {}).values()):
            e("articolo.fornitore.codiceFornitoreERP deve essere un oggetto string->string")
        if a.get("chiaveArticolo") != f"{f.get('sigla')}-{a.get('codiceArticoloFornitore')}":
            e("chiaveArticolo non coerente con sigla-codice")
    if a.get("fornitoreErpIds") != [92329388]:
        e(f"fornitoreErpIds deve essere [92329388] (fornitori_erp Makito), non {a.get('fornitoreErpIds')!r}")
    if "ciclo" in a:
        e("articolo.ciclo non va inviato: i cicli sono gestiti a mano su OnPage (decisione 03/09/2026)")
    if a.get("brand") is not None and not a["brand"].get("nome"):
        e("articolo.brand.nome mancante (o brand deve essere null)")
    for img in [a.get("immagine")] + [i.get("url") for i in a.get("immagini") or []]:
        if img is not None and not str(img).startswith("https://"):
            e(f"immagine non HTTPS: {img}")
    vs = a.get("varianti")
    if not isinstance(vs, list) or not vs:
        e("articolo.varianti vuoto")
        return
    keys = set()
    for i, v in enumerate(vs):
        p = f"varianti[{i}]"
        for k in ("codiceFornitore", "codiceVariante"):
            if not isinstance(v.get(k), str) or not v[k]:
                e(f"{p}.{k} mancante")
        if v.get("codiceVariante") in keys:
            e(f"{p}.codiceVariante duplicato: {v.get('codiceVariante')}")
        keys.add(v.get("codiceVariante"))
        if "colore" not in v or "taglia" not in v:
            e(f"{p}: colore e taglia devono essere presenti (anche se null)")
        c = v.get("colore")
        if c is not None:
            for k in ("codice", "nome"):
                if not isinstance(c.get(k), str) or not c[k]:
                    e(f"{p}.colore.{k} mancante")
            if not isinstance(c.get("esadecimali"), list):
                e(f"{p}.colore.esadecimali deve essere array")
            for h in c.get("esadecimali", []):
                if not (isinstance(h, str) and len(h) == 7 and h.startswith("#")):
                    e(f"{p}.colore.esadecimali valore non valido: {h!r}")
            if "?" in (c.get("nome") or ""):
                e(f"{p}.colore.nome contiene '?' (colore non decodificato): {c['nome']}")
            if "coloreFiltroWebId" in c and not isinstance(c["coloreFiltroWebId"], int):
                e(f"{p}.colore.coloreFiltroWebId deve essere int")
        if v.get("immagine") is not None and not str(v["immagine"]).startswith("https://"):
            e(f"{p}.immagine non HTTPS: {v['immagine']}")
        gs = v.get("giacenze")
        if not isinstance(gs, list):
            e(f"{p}.giacenze deve essere array")
        else:
            for j, g in enumerate(gs):
                if not isinstance((g.get("magazzino") or {}).get("codice"), str):
                    e(f"{p}.giacenze[{j}].magazzino.codice mancante")
                if not isinstance(g.get("quantita"), int):
                    e(f"{p}.giacenze[{j}].quantita deve essere int")
                if g.get("dataArrivo") is not None and not DATA_RE.match(str(g["dataArrivo"])):
                    e(f"{p}.giacenze[{j}].dataArrivo non ISO YYYY-MM-DD: {g['dataArrivo']!r}")
        ls = v.get("listini")
        if not isinstance(ls, list):
            e(f"{p}.listini deve essere array (vuoto ammesso: articoli senza prezzo)")
        else:
            for j, l in enumerate(ls):
                if l.get("tipologiaListino") not in TIPOLOGIE:
                    e(f"{p}.listini[{j}].tipologiaListino non valido: {l.get('tipologiaListino')!r}")
                if not isinstance(l.get("prezzo"), (int, float)) or l["prezzo"] <= 0:
                    e(f"{p}.listini[{j}].prezzo non valido: {l.get('prezzo')!r}")
                if not isinstance(l.get("quantitaDa"), int) or l["quantitaDa"] < 1:
                    e(f"{p}.listini[{j}].quantitaDa non valido")
                if l.get("quantitaA") is not None and (not isinstance(l["quantitaA"], int) or l["quantitaA"] < l["quantitaDa"]):
                    e(f"{p}.listini[{j}].quantitaA non valido")
                if not isinstance(l.get("multipli"), int):
                    e(f"{p}.listini[{j}].multipli deve essere int")
                if not isinstance(l.get("scaglione"), str):
                    e(f"{p}.listini[{j}].scaglione deve essere string")
        for k in ("pezziPerConfezione", "pezziPerSottoImballo", "pezziPerImballo", "pezziPerPallet", "minimoOrdine"):
            if v.get(k) is not None and not isinstance(v[k], int):
                e(f"{p}.{k} deve essere int o null")
        for k in ("pesoNetto", "pesoLordo", "altezza", "larghezza", "lunghezza", "volume",
                  "altezzaImballo", "larghezzaImballo", "lunghezzaImballo", "volumeImballo", "pesoLordoImballo"):
            if v.get(k) is not None and not isinstance(v[k], (int, float)):
                e(f"{p}.{k} deve essere numero o null")
        if v.get(k := "_matnr") is not None:
            e(f"{p}: campo di servizio {k} non rimosso")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("out"))
    args = ap.parse_args(argv)
    errors: list[str] = []
    files = sorted((args.out / "json").glob("*.json"))
    all_keys = set()
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        check(payload, f.stem, errors)
        for v in payload.get("articolo", {}).get("varianti", []):
            k = v.get("codiceVariante")
            if k in all_keys:
                errors.append(f"{f.stem}: codiceVariante {k} gia' usato in un altro articolo")
            all_keys.add(k)
    print(f"file: {len(files)}  varianti: {len(all_keys)}  errori: {len(errors)}")
    for m in errors[:100]:
        print(" -", m)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
