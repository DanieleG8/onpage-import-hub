#!/usr/bin/env python3
"""
report.py - Anteprima Excel di cio' che verra' inviato all'importer (per revisione di Daniele).

Uso:  python src/report.py --out out
Produce out/report_anteprima_02.xlsx con fogli:
  Articoli   chiave, descrizioni, brand, n. varianti, prezzo, immagine
  Varianti   chiave variante, matnr, colore (nome/filtro/hex), taglia, giacenze, prezzo
  Colori     mappa colori usata (codice Makito -> nome, filtro web, hex, regola) da validare
  Anomalie   tutto cio' che va confermato prima dell'invio
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

FILTRI_WEB = {58873859: "Nero", 58873860: "Bianco", 58873861: "Grigio", 58873862: "Rosso/Bordeaux",
              58873863: "Giallo", 58873864: "Verde", 58873866: "Azzurro/Blu", 58873867: "Rosa/Fucsia/Viola",
              58873868: "Marrone/Kaki", 58873870: "Arancione/Corallo", 58873872: "Fantasia/Multicolor",
              58873875: "Naturale/Beige", 58873877: "Metallizzati", 58873880: "Trasparente"}


def read_jsonl(p: Path):
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def sheet(wb, title, header, rows, widths=None):
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDEBF7")
    for r in rows:
        ws.append(r)
    for i, w in enumerate(widths or [], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    return ws


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("out"))
    args = ap.parse_args(argv)

    articoli = read_jsonl(args.out / "articoli.jsonl")
    varianti = read_jsonl(args.out / "varianti.jsonl")
    anomalie = read_jsonl(args.out / "anomalie.jsonl")
    colori = json.loads((args.out / "colori_usati.json").read_text(encoding="utf-8")) \
        if (args.out / "colori_usati.json").exists() else {}

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    dettagli = {}   # descrizione lunga per articolo, dal JSON pronto per l'invio
    for f in (args.out / "json").glob("*.json"):
        a = json.loads(f.read_text(encoding="utf-8"))["articolo"]
        dettagli[a["chiaveArticolo"]] = a

    sheet(wb, "Articoli",
          ["chiave", "ref", "descrizione breve", "descrizione lunga", "brand", "ciclo",
           "varianti", "scaglioni", "prezzo min (acquisto rivenditori)", "immagine"],
          [[a["chiave"], a["ref"], a["descrizioneBreve"],
            dettagli.get(a["chiave"], {}).get("descrizioneLunga", "")[:500],
            a["brand"], "(gestiti a mano su OnPage)", a["varianti"], a["listini_per_variante"],
            min((v["prezzo"] for v in varianti if v["chiaveArticolo"] == a["chiave"]
                 and v["prezzo"]), default=None),
            a["immagine"]] for a in articoli],
          widths=[12, 8, 40, 80, 10, 18, 9, 9, 16, 60])

    sheet(wb, "Varianti",
          ["chiave variante", "matnr", "nome Makito", "colore", "filtro web", "taglia",
           "giacenza attuale", "arrivi futuri", "prezzo (acquisto rivenditori)", ],
          [[v["codiceVariante"], v["matnr"], v.get("nome"), v.get("colore"),
            FILTRI_WEB.get(v.get("coloreFiltroWebId"), "-" if v.get("colore") is None else "(nessuno)"),
            v.get("taglia") or "(Unica)",
            v["giacenza_attuale"],
            "; ".join(f"{q} pz il {d}" for d, q in v.get("arrivi") or []) or "-",
            v["prezzo"]] for v in varianti],
          widths=[22, 14, 34, 16, 16, 10, 15, 40, 14])

    sheet(wb, "Colori",
          ["codice Makito", "nome (it)", "filtro web assegnato", "esadecimale", "regola usata", "usato dalle ref"],
          [[c["codice"], c["nome"],
            FILTRI_WEB.get(c.get("coloreFiltroWebId"), "(forma/soggetto)" if c.get("regola") else "MANCANTE"),
            c.get("esadecimale"), c.get("regola"), ", ".join(sorted(set(c["refs"])))]
           for c in sorted(colori.values(), key=lambda x: x["codice"])],
          widths=[14, 24, 20, 12, 30, 30])

    sheet(wb, "Anomalie",
          ["gravita", "codice", "tipo", "dettaglio"],
          [[a["gravita"], a["codice"], a["tipo"], a["dettaglio"]] for a in anomalie] or
          [["-", "-", "nessuna anomalia", "-"]],
          widths=[14, 10, 26, 100])

    dest = args.out / "report_anteprima_02.xlsx"
    wb.save(dest)
    print(f"report: {dest}  (articoli {len(articoli)}, varianti {len(varianti)}, "
          f"colori {len(colori)}, anomalie {len(anomalie)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
