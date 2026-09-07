#!/usr/bin/env python3
"""Convertitore GARY'S -> CustomImportRequest OnPage (fornitore A3).

Legge da {snapshot}: catalogo.xlsx (una riga per SKU) + listino.xls (prezzo
d'acquisto per referenza, foglio "PREZZO CON IL TUO SCONTO"). Scrive un JSON
per modello in {out}/json/A3-{modello}.json + anomalie.jsonl.

Convenzioni (decise con Daniele il 07/09/2026):
- chiave articolo "A3-{modello}"; una variante per SKU, chiave
  "A3-{riferimento interno}" (il riferimento e' gia' modello-colore-taglia).
- listini: "acquisto riservato" dal listino Dropbox (per modello, se c'e');
  "acquisto pubblico" dal prezzo sito del catalogo (per SKU, virgola decimale).
- taglia "UD" -> null (l'importer assegna "Unica"); colore filtro web per
  parola chiave spagnola, bicolore "X/Y" -> Multicolor.
- giacenze: quantita' del catalogo (magazzino GARYS, dataArrivo null, >= 0).
- ciclo MAI nel payload (gestito a mano su OnPage). EAN fuori formato.
"""
from __future__ import annotations

import argparse
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import openpyxl
import xlrd
import yaml

COL = {  # intestazioni attese nel catalogo (riga 2 del foglio)
    "ean": "Codice a barre", "modello": "Prodotto/Modello",
    "cod_colore": "Codice colore", "colore": "Colore", "taglia": "Taglia",
    "rif": "Riferimento interno", "nome": "Nome commerciale",
    "prezzo_pubblico": "Prezzo pubblico del sito web",
    "stock": "Quantità disponibile alla vendita",
    "descrizione": "Descrizione per la vendita", "breve": "Descrizione breve",
    "composizione": "Composizione", "trattamento": "Trattamento",
    "img": "URL immagine", "img2": "Link per scaricare le foto", "peso": "Peso",
}


def prezzo(x) -> float | None:
    """Prezzo dal catalogo/listino: '24,03', 24.03 o vuoto."""
    if x in (None, ""):
        return None
    try:
        d = Decimal(str(x).replace(",", "."))
    except ArithmeticError:
        return None
    if d <= 0:
        return None
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def testo(x) -> str:
    return str(x).strip() if x is not None else ""


class Convertitore:
    def __init__(self, cfg: dict, out: Path):
        self.cfg = cfg
        self.out = out
        self.anomalie: list[dict] = []

    def warn(self, chiave: str, tipo: str, msg: str, livello: str = "info"):
        self.anomalie.append({"chiave": chiave, "tipo": tipo, "msg": msg, "livello": livello})

    # ---------------------------------------------------------------- colore --
    def filtro_web(self, chiave: str, nome: str) -> int | None:
        cfw = self.cfg["colori_filtro_web"]
        if "/" in nome:
            return cfw["multicolor"]           # bicolore -> Multicolor
        su = nome.upper()
        for parola, fid in cfw["parole"].items():
            if parola in su:
                return fid
        if nome:
            self.warn(chiave, "colore filtro web non mappato", nome, "warning")
        return None

    def colore(self, chiave: str, r: dict) -> dict | None:
        # regola di Daniele (07/09/2026): codici e nomi colore del fornitore
        # NON si modificano ne' si storpiano (solo trim degli spazi ai bordi)
        codice = testo(r["cod_colore"])
        nome = testo(r["colore"])
        if not codice and not nome:
            return None                        # l'importer assegna "Unico"
        return {"codice": codice or nome, "nome": nome or codice,
                "esadecimali": [], "coloreFiltroWebId": self.filtro_web(chiave, nome)}

    # -------------------------------------------------------------- articolo --
    def articolo(self, modello: str, righe: list[dict], listino_acq: dict) -> dict | None:
        chiave = f"A3-{modello}"
        tip_acq = self.cfg["listino"]["tipologia_acquisto"]
        tip_pub = self.cfg["listino"]["tipologia_pubblico"]
        prezzo_acq = listino_acq.get(modello)

        varianti, chiavi_viste = [], set()
        for r in righe:
            rif = testo(r["rif"])
            if not rif:
                self.warn(chiave, "SKU senza riferimento interno", testo(r["ean"]), "warning")
                continue
            chiave_v = f"A3-{rif}"
            if chiave_v in chiavi_viste:
                self.warn(chiave, "variante duplicata", f"{chiave_v}: tenuta la prima", "info")
                continue
            chiavi_viste.add(chiave_v)

            listini = []
            if prezzo_acq:
                listini.append({"tipologiaListino": tip_acq, "scaglione": "1",
                                "quantitaDa": 1, "quantitaA": None, "multipli": 1,
                                "prezzo": prezzo_acq})
            pub = prezzo(r["prezzo_pubblico"])
            if pub:
                listini.append({"tipologiaListino": tip_pub, "scaglione": "1",
                                "quantitaDa": 1, "quantitaA": None, "multipli": 1,
                                "prezzo": pub})

            taglia = testo(r["taglia"])
            if taglia.upper() in ("UD", "0"):
                taglia = ""                    # taglia unica/assente -> null
                                               # ("0" rifiutata dall'importer, come NWG)
            img = testo(r["img"]) or None
            img2 = testo(r["img2"]) or None
            try:
                stock = max(0, int(float(str(r["stock"]).replace(",", "."))))
            except (TypeError, ValueError):
                stock = None
            try:
                peso = round(float(str(r["peso"]).replace(",", ".")), 3) or None
            except (TypeError, ValueError):
                peso = None

            varianti.append({
                "codiceFornitore": rif,
                "codiceVariante": chiave_v,
                "colore": self.colore(chiave, r),
                "taglia": {"codice": taglia, "nome": taglia} if taglia else None,
                "immagine": img,
                "immagini": [{"url": img2, "tipologia": "Immagine dettaglio"}]
                            if img2 and img2 != img else [],
                "pezziPerConfezione": None,
                "pezziPerSottoImballo": None,
                "pezziPerImballo": None,
                "pezziPerPallet": None,
                "minimoOrdine": None,
                "pesoNetto": peso,
                "pesoLordo": None,
                "altezza": None, "larghezza": None, "lunghezza": None,
                "volume": None,                # colonna presente ma unita' ignota
                "descrizioneDimensioneFornitore": None,
                "altezzaImballo": None, "larghezzaImballo": None,
                "lunghezzaImballo": None, "volumeImballo": None,
                "pesoLordoImballo": None,
                "giacenze": [{"magazzino": dict(self.cfg["magazzino"]),
                              "quantita": stock, "dataArrivo": None}]
                            if stock is not None else [],
                "listini": listini,
                "moltiplicatoreVendita": None,
            })

        if not varianti:
            self.warn(chiave, "senza varianti", "nessuno SKU valido: scartato", "warning")
            return None
        if not prezzo_acq:
            self.warn(chiave, "senza prezzo d'acquisto a listino",
                      "resta solo l'acquisto pubblico", "info")

        primo = righe[0]
        blocchi = [testo(primo["descrizione"]), testo(primo["breve"])]
        if testo(primo["composizione"]):
            blocchi.append(f"Composizione: {testo(primo['composizione'])}")
        if testo(primo["trattamento"]):
            blocchi.append(testo(primo["trattamento"]))
        img_art = next((v["immagine"] for v in varianti if v["immagine"]), None)

        f = self.cfg["fornitore"]
        return {
            "parametriImport": dict(self.cfg["parametriImport"]),
            "articolo": {
                "codiceArticoloFornitore": modello,
                "chiaveArticolo": chiave,
                "moltiplicatoreVenditaArticolo": None,
                "descrizioneBreve": testo(primo["nome"]) or modello,
                # l'importer esige descrizioneLunga valorizzata: se il catalogo
                # non ha descrizioni si ripiega sul nome commerciale
                "descrizioneLunga": "\n\n".join(b for b in blocchi if b)
                                    or testo(primo["nome"]) or modello,
                "immagine": img_art,
                "immagineAmbientata": None,
                "immagini": [],
                # "ciclo" OMESSO di proposito: i cicli si gestiscono a mano su OnPage
                "brand": {"nome": "Gary's"},
                "fornitore": {
                    "sigla": f["sigla"], "nome": f["nome"],
                    "moltiplicatoreVendita": f["moltiplicatoreVendita"],
                    "codiceFornitoreERP": {k: str(v) for k, v in f["codiceFornitoreERP"].items()},
                },
                "fornitoreErpIds": list(f["fornitoreErpIds"]),
                "varianti": varianti,
            },
        }

    # ---------------------------------------------------------------- lettura --
    def leggi_listino(self, snapshot: Path) -> dict[str, float]:
        wb = xlrd.open_workbook(snapshot / "listino.xls")
        sh = wb.sheet_by_name(self.cfg["listino"]["foglio_acquisto"])
        prezzi: dict[str, float] = {}
        for r in range(sh.nrows):
            ref = testo(sh.cell_value(r, 0))
            if ref.endswith(".0"):
                ref = ref[:-2]
            if not ref or not ref[0].isdigit() and not ref[0].isalpha():
                continue
            for c in range(sh.ncols - 1, 0, -1):   # il prezzo e' nell'ultima colonna piena
                p = prezzo(sh.cell_value(r, c))
                if p:
                    prezzi[ref] = p
                    break
        return prezzi

    def esegui(self, snapshot: Path) -> int:
        listino_acq = self.leggi_listino(snapshot)
        wb = openpyxl.load_workbook(snapshot / "catalogo.xlsx", read_only=True)
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = None
        modelli: dict[str, list[dict]] = {}
        for riga in rows:
            if header is None:
                if riga and COL["modello"] in riga:
                    header = {nome: i for i, nome in enumerate(riga)}
                    mancanti = [c for c in COL.values() if c not in header]
                    if mancanti:
                        raise ValueError(f"colonne mancanti nel catalogo: {mancanti}")
                continue
            r = {k: riga[header[c]] for k, c in COL.items()}
            modello = testo(r["modello"])
            if not modello:
                continue
            modelli.setdefault(modello, []).append(r)
        if header is None:
            raise ValueError("intestazione non trovata nel catalogo.xlsx")

        outj = self.out / "json"
        outj.mkdir(parents=True, exist_ok=True)
        n = 0
        for modello, righe in modelli.items():
            a = self.articolo(modello, righe, listino_acq)
            if a is None:
                continue
            (outj / f"{a['articolo']['chiaveArticolo']}.json").write_text(
                json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
            n += 1
        with (self.out / "anomalie.jsonl").open("w", encoding="utf-8") as fh:
            for r in self.anomalie:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    conv = Convertitore(cfg, Path(args.out))
    n = conv.esegui(Path(args.snapshot))
    errori = sum(1 for a in conv.anomalie if a["livello"] == "error")
    print(f"convertiti {n} articoli; anomalie: {len(conv.anomalie)} (errori: {errori})")
    return 1 if errori else 0


if __name__ == "__main__":
    raise SystemExit(main())
