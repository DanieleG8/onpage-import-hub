#!/usr/bin/env python3
"""Convertitore PF Concept -> CustomImportRequest OnPage (fornitore 34).

Legge da {snapshot}: prodotti.json + prodotti_ws.json (modelli/item),
prezzi.json (scaglioni netti EUR), stock.json (attuale + arrivo datato).
Scrive un JSON per modello in {out}/json/34-{modelCode}.json + anomalie.jsonl.

Convenzioni (decise con Daniele il 05/09/2026):
- chiave articolo "34-{modelCode}"; una variante per item, chiave "34-{itemCode}".
- listini "acquisto riservato" dagli scaglioni nettPrice (priceBar 1/100/250/1000);
  moltiplicatoreVendita 3.5 (dalla raccolta fornitore 34).
- giacenze: stockDirect (dataArrivo null) + stockNextPo con data; StockFuture
  senza data viene ignorato (il formato richiede una data per gli arrivi).
- immagini: nomi file del feed + base_url live link 1600x1600.
- si importa tutto, inclusi isDiscontinued=true e l'assortimento WS.
- ciclo MAI nel payload (gestito a mano su OnPage).
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml


def num(x) -> float | None:
    """Numero da stringa PF ('5,7' con virgola, '' vuota)."""
    if x in (None, ""):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).replace(",", "."))
    except ValueError:
        return None


class Convertitore:
    def __init__(self, cfg: dict, out: Path):
        self.cfg = cfg
        self.out = out
        self.anomalie: list[dict] = []

    def warn(self, chiave: str, tipo: str, msg: str, livello: str = "info"):
        self.anomalie.append({"chiave": chiave, "tipo": tipo, "msg": msg, "livello": livello})

    # -------------------------------------------------------------- immagini --
    def img(self, nome: str | None) -> str | None:
        # dati sporchi visti nel feed (full del 06/09): piu' nomi separati da
        # virgola ("a.jpg, b.jpg" -> si tiene il primo) e nomi con spazi
        # ("X - Copy.jpg" -> percent-encoding, l'importer esige URL validi)
        nome = (nome or "").split(",")[0].strip()
        if not nome:
            return None
        return self.cfg["immagini"]["base_url"] + urllib.parse.quote(nome)

    def galleria(self, image_data: dict, escludi: set) -> list[dict]:
        righe, viste = [], set(escludi)
        for campo, tipologia in self.cfg["immagini"]["tipologie"].items():
            url = self.img((image_data or {}).get(campo))
            if url and url not in viste:
                viste.add(url)
                righe.append({"url": url, "tipologia": tipologia})
        return righe

    # ---------------------------------------------------------------- colore --
    def colore(self, chiave: str, item: dict) -> dict | None:
        colori = ((item.get("colors") or {}).get("color")) or []
        if isinstance(colori, dict):
            colori = [colori]
        if not colori:
            return None
        c = colori[0]
        cfw = self.cfg["colori_filtro_web"]
        if len(colori) > 1:
            filtro = cfw["multicolor"]
        else:
            base = (c.get("baseColor") or "").strip()
            filtro = cfw["mappa"].get(base)
            if base and filtro is None:
                self.warn(chiave, "colore filtro web non mappato", base, "warning")
        esa = (c.get("hexColor") or "").strip().lstrip("#")
        return {"codice": (c.get("colorCode") or "000").strip(),
                "nome": (c.get("colorDesc") or "").strip() or None,
                "esadecimali": [f"#{esa.upper()}"] if esa else [],
                "coloreFiltroWebId": filtro}

    # ---------------------------------------------------------------- listini --
    def listini(self, chiave: str, prezzo_item: dict | None) -> list[dict]:
        if not prezzo_item:
            return []
        tip = self.cfg["listino"]["tipologiaListino"]
        if prezzo_item.get("currency") != self.cfg["listino"]["valuta_attesa"]:
            self.warn(chiave, "valuta inattesa", str(prezzo_item.get("currency")), "error")
        scale = [s for blk in prezzo_item.get("scales") or [] for s in blk.get("scale") or []]
        scale.sort(key=lambda s: s.get("priceBar") or 0)
        righe = []
        for i, s in enumerate(scale):
            prezzo = s.get("nettPrice")
            if prezzo in (None, "", 0):
                continue
            qa = (scale[i + 1]["priceBar"] - 1) if i + 1 < len(scale) else None
            righe.append({"tipologiaListino": tip, "scaglione": str(s.get("priceBar") or 1),
                          "quantitaDa": s.get("priceBar") or 1, "quantitaA": qa,
                          "multipli": 1,
                          "prezzo": float(Decimal(str(prezzo)).quantize(
                              Decimal("0.0001"), rounding=ROUND_HALF_UP))})
        return righe

    # --------------------------------------------------------------- giacenze --
    def giacenze(self, stock_item: dict | None) -> list[dict]:
        if not stock_item:
            return []
        mag = dict(self.cfg["magazzino"])
        righe = []
        attuale = stock_item.get("stockDirect")
        if attuale is not None:
            # nel feed compaiono stock negativi (es. -24): l'importer esige >= 0
            righe.append({"magazzino": mag, "quantita": max(0, int(attuale)),
                          "dataArrivo": None})
        prossimo = stock_item.get("stockNextPo") or 0
        data = (stock_item.get("stockDateNextPo") or "").strip()
        if prossimo > 0 and data:
            righe.append({"magazzino": mag, "quantita": int(prossimo), "dataArrivo": data[:10]})
        return righe

    # --------------------------------------------------------------- articolo --
    def articolo(self, m: dict, prezzi: dict, stock: dict) -> dict | None:
        ref = (m.get("modelCode") or "").strip()
        if not ref:
            self.warn("?", "modello senza modelCode", "scartato", "error")
            return None
        chiave = f"34-{ref}"

        varianti, chiavi_viste = [], set()
        for wrap in m.get("items") or []:
            it = wrap.get("item") or {}
            codice = (it.get("itemCode") or "").strip()
            if not codice:
                self.warn(chiave, "item senza itemCode", "saltato", "warning")
                continue
            chiave_v = f"34-{codice}"
            if chiave_v in chiavi_viste:
                self.warn(chiave, "variante duplicata", f"{chiave_v}: tenuta la prima", "info")
                continue
            chiavi_viste.add(chiave_v)

            mis = it.get("measurements") or {}
            img_var = self.img((it.get("imageData") or {}).get("imageMain"))
            peso_pezzo = num(mis.get("weightGr"))
            taglia = (it.get("size") or "").strip()
            listini = self.listini(chiave, prezzi.get(codice))
            if not listini:
                self.warn(chiave, "senza listino", codice, "info")

            varianti.append({
                "codiceFornitore": codice,
                "codiceVariante": chiave_v,
                "colore": self.colore(chiave, it),
                "taglia": {"codice": taglia, "nome": taglia} if taglia else None,
                "immagine": img_var,
                "immagini": self.galleria(it.get("imageData"), {img_var} if img_var else set()),
                "pezziPerConfezione": None,
                "pezziPerSottoImballo": None,
                "pezziPerImballo": int(num(it.get("qtyPerCarton")) or 0) or None,
                "pezziPerPallet": None,
                "minimoOrdine": None,
                "pesoNetto": round(peso_pezzo / 1000, 3) if peso_pezzo else None,
                "pesoLordo": None,
                "altezza": num(mis.get("heightCm")),
                "larghezza": num(mis.get("widthCm")),
                "lunghezza": num(mis.get("lengthCm")),
                "volume": None,
                "descrizioneDimensioneFornitore": (mis.get("SizeCombined") or "").strip() or None,
                "altezzaImballo": num(it.get("exportHcm")),
                "larghezzaImballo": num(it.get("exportWcm")),
                "lunghezzaImballo": num(it.get("exportLcm")),
                "volumeImballo": None,
                "pesoLordoImballo": num(it.get("grossWeightKg")),
                "giacenze": self.giacenze(stock.get(codice)),
                "listini": listini,
                "moltiplicatoreVendita": None,
            })

        if not varianti:
            self.warn(chiave, "senza varianti", "nessun item nel feed: scartato", "warning")
            return None

        primo = (m.get("items") or [{}])[0].get("item") or {}
        img_art = self.img((primo.get("imageData") or {}).get("imageMain")) or next(
            (v["immagine"] for v in varianti if v["immagine"]), None)
        ambientata = self.img((primo.get("imageData") or {}).get("imageMood1")) \
            or self.img((primo.get("imageData") or {}).get("imageModel"))

        blocchi = [(m.get("extDesc") or "").strip()]
        if (primo.get("material") or "").strip():
            blocchi.append(f"Materiale: {primo['material'].strip()}")
        f = self.cfg["fornitore"]
        return {
            "parametriImport": dict(self.cfg["parametriImport"]),
            "articolo": {
                "codiceArticoloFornitore": ref,
                "chiaveArticolo": chiave,
                "moltiplicatoreVenditaArticolo": None,
                "descrizioneBreve": (m.get("description") or "").strip() or ref,
                "descrizioneLunga": "\n\n".join(b for b in blocchi if b),
                "immagine": img_art,
                "immagineAmbientata": ambientata,
                "immagini": self.galleria(primo.get("imageData"),
                                          {u for u in (img_art, ambientata) if u}),
                # "ciclo" OMESSO di proposito: i cicli si gestiscono a mano su OnPage
                "brand": {"nome": (primo.get("brand") or "").strip() or "PF Concept"},
                "fornitore": {
                    "sigla": f["sigla"], "nome": f["nome"],
                    "moltiplicatoreVendita": f["moltiplicatoreVendita"],
                    "codiceFornitoreERP": {k: str(v) for k, v in f["codiceFornitoreERP"].items()},
                },
                "fornitoreErpIds": list(f["fornitoreErpIds"]),
                "varianti": varianti,
            },
        }

    def esegui(self, snapshot: Path) -> int:
        modelli = []
        for nome in ("prodotti.json", "prodotti_ws.json"):
            p = snapshot / nome
            if not p.exists():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            modelli += [x["model"] for x in d["pfcProductfeed"]["productfeed"]["models"]]

        prezzi: dict[str, dict] = {}
        for nome in ("prezzi.json", "prezzi_ws.json"):
            p = snapshot / nome
            if not p.exists():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            for m in d["PFCPriceFeed"]["priceInfo"][0]["models"][0]["model"]:
                for blk in m.get("items") or []:
                    for it in blk.get("item") or []:
                        prezzi[(it.get("itemcode") or "").strip()] = it

        stock: dict[str, dict] = {}
        p = snapshot / "stock.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            for m in d["PFCStockFeed"]["stockFeed"][0]["models"][0]["model"]:
                for blk in m.get("items") or []:
                    for it in blk.get("item") or []:
                        stock[(it.get("itemCode") or "").strip()] = it

        outj = self.out / "json"
        outj.mkdir(parents=True, exist_ok=True)
        n = 0
        chiavi_viste = set()
        for m in modelli:
            a = self.articolo(m, prezzi, stock)
            if a is None:
                continue
            chiave = a["articolo"]["chiaveArticolo"]
            if chiave in chiavi_viste:
                self.warn(chiave, "modello duplicato", "tenuto il primo", "warning")
                continue
            chiavi_viste.add(chiave)
            (outj / f"{chiave}.json").write_text(
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
