#!/usr/bin/env python3
"""Convertitore NEW WAVE / NWG -> CustomImportRequest OnPage (fornitore A1).

Legge {snapshot}/feed.json (lista prodotti con variations[].skus[]) e scrive un
JSON per articolo in {out}/json/A1-{productNumber}.json + {out}/anomalie.jsonl.

Convenzioni (decise con Daniele il 05/09/2026):
- chiave articolo "A1-{productNumber}"; una variante OnPage per SKU (colore x taglia),
  chiave "A1-{sku}" (lo sku NWG e' gia' {productNumber}-{colorCode}-{codiceTaglia}).
- moltiplicatoreVendita null (verra' impostato in futuro); SOLO listino
  "acquisto rivenditori" da customerPrice EUR, scaglione unico da 1; niente retailPrice.
- webColors multipli (";") -> filtro web Multicolor.
- giacenze: assenti nel feed -> lista vuota (in attesa delle API stock NWG).
- ciclo MAI nel payload (gestito a mano su OnPage).
- INSIDIA unita' di misura: nel feed le dimensioni sono in METRI qualunque sia
  l'etichetta ({"mm": 0.6, "cm": 0.06} = 60 cm): si converte dal campo "mm" x100.
"""
from __future__ import annotations

import argparse
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml


def cm(mis: dict | None) -> float | None:
    """Dimensione in cm dal feed NWG (valori in metri sotto la chiave 'mm')."""
    if not isinstance(mis, dict):
        return None
    v = mis.get("mm")
    return round(v * 100, 2) if v else None


def kg(mis: dict | None) -> float | None:
    if not isinstance(mis, dict):
        return None
    v = mis.get("kg")
    return round(v, 3) if v else None


def it(x) -> str:
    """Estrae il testo italiano da un campo localizzato {'it': ...}."""
    if isinstance(x, dict):
        x = x.get("it") or ""
    return (x or "").strip()


class Convertitore:
    def __init__(self, cfg: dict, out: Path):
        self.cfg = cfg
        self.out = out
        self.anomalie: list[dict] = []

    def warn(self, chiave: str, tipo: str, msg: str, livello: str = "info"):
        self.anomalie.append({"chiave": chiave, "tipo": tipo, "msg": msg, "livello": livello})

    # ------------------------------------------------------------- immagini --
    def tipologia(self, tipo: str, angolo: str) -> str:
        im = self.cfg["immagini"]
        if tipo == "Imagepicture":
            return im["tipologia_ambientata"]
        if tipo == "Modelpicture":
            return im["tipologia_modello"]
        return im["tipologie"].get(angolo or "none", "Immagine dettaglio")

    def galleria(self, immagini: list, escludi: set[str]) -> list[dict]:
        """Gallery deduplicata (il feed ripete le stesse URL piu' volte)."""
        viste, righe = set(escludi), []
        for i in immagini or []:
            url = (i.get("standard") or "").strip()
            if not url or url in viste:
                continue
            viste.add(url)
            righe.append({"url": url, "tipologia": self.tipologia(i.get("type"), i.get("angle"))})
        return righe

    @staticmethod
    def front(immagini: list) -> str | None:
        for i in immagini or []:
            if i.get("type") == "Productpicture" and i.get("angle") == "front" and i.get("standard"):
                return i["standard"].strip()
        for i in immagini or []:                       # fallback: prima Productpicture qualsiasi
            if i.get("type") == "Productpicture" and i.get("standard"):
                return i["standard"].strip()
        return None

    @staticmethod
    def ambientata(immagini: list) -> str | None:
        for i in immagini or []:
            if i.get("type") == "Imagepicture" and i.get("standard"):
                return i["standard"].strip()
        return None

    # --------------------------------------------------------------- colore --
    def colore(self, chiave: str, var: dict) -> dict | None:
        codice = (var.get("colorCode") or "").strip()
        nome = it(var.get("color"))
        if not codice and not nome:
            return None                                # l'importer assegna "Unico"
        web = (var.get("webColors") or "").strip()
        cfw = self.cfg["colori_filtro_web"]
        filtro = None
        if web:
            if ";" in web:
                filtro = cfw["multicolor"]             # colori doppi -> Multicolor
            else:
                filtro = cfw["mappa"].get(web)
                if filtro is None:
                    self.warn(chiave, "colore filtro web non mappato", web, "warning")
        return {"codice": codice or "000", "nome": nome or None,
                "esadecimali": [], "coloreFiltroWebId": filtro}

    # ----------------------------------------------------------- descrizione --
    def descrizione_lunga(self, p: dict) -> str:
        blocchi = [it(p.get("description"))]
        catalogo = it(p.get("productCatalogText"))
        if catalogo and catalogo != blocchi[0]:
            blocchi.append(catalogo)
        for b in self.cfg["descrizione"]["blocchi"]:
            v = it(p.get(b["campo"]))
            if v:
                blocchi.append(f"{b['etichetta']}: {v.replace(';', ', ')}")
        return "\n\n".join(x for x in blocchi if x)

    # -------------------------------------------------------------- articolo --
    def articolo(self, p: dict) -> dict | None:
        ref = (p.get("productNumber") or "").strip()
        if not ref:
            self.warn("?", "prodotto senza productNumber", "scartato", "error")
            return None
        chiave = f"A1-{ref}"
        tip_listino = self.cfg["listino"]["tipologiaListino"]

        varianti, chiavi_viste = [], set()
        for var in p.get("variations") or []:
            colore = self.colore(chiave, var)
            img_var = self.front(var.get("images"))
            gal_var = self.galleria(var.get("images"), {img_var} if img_var else set())
            for s in var.get("skus") or []:
                sku = (s.get("sku") or "").strip()
                if not sku:
                    self.warn(chiave, "sku senza codice", str(s.get("itemDescription")), "warning")
                    continue
                # di norma sku = {ref}-{colore}-{taglia}; nei mono-SKU sku == ref
                # e serve il suffisso per non collidere con la chiave articolo
                chiave_v = f"A1-{sku}" if sku != ref else f"A1-{ref}-000-000"
                if chiave_v in chiavi_viste:
                    self.warn(chiave, "variante duplicata", f"{chiave_v}: tenuta la prima", "info")
                    continue
                chiavi_viste.add(chiave_v)

                listini = []
                prezzo = (s.get("customerPrice") or {}).get("value")
                valuta = (s.get("customerPrice") or {}).get("currency")
                if prezzo:
                    if valuta != self.cfg["listino"]["valuta_attesa"]:
                        self.warn(chiave, "valuta inattesa", f"{sku}: {valuta}", "error")
                    listini = [{"tipologiaListino": tip_listino, "scaglione": 1,
                                "quantitaDa": 1, "quantitaA": None, "multipli": None,
                                "prezzo": float(Decimal(str(prezzo)).quantize(
                                    Decimal("0.0001"), rounding=ROUND_HALF_UP))}]
                else:
                    self.warn(chiave, "sku senza prezzo", sku, "info")

                nome_taglia = (s.get("name") or "").strip()
                cod_taglia = (s.get("size") or "").strip()
                varianti.append({
                    "codiceFornitore": sku,
                    "codiceVariante": chiave_v,
                    "colore": colore,
                    "taglia": {"codice": cod_taglia or nome_taglia or "000",
                               "nome": nome_taglia or None} if (cod_taglia or nome_taglia) else None,
                    "immagine": img_var,
                    "immagini": gal_var,
                    "pezziPerConfezione": None,
                    "pezziPerSottoImballo": s.get("inboxItems") or None,
                    "pezziPerImballo": s.get("boxItems") or None,
                    "pezziPerPallet": s.get("pallItems") or None,
                    "minimoOrdine": None,
                    "pesoNetto": kg(s.get("netWeight")),
                    "pesoLordo": kg(s.get("grossWeight")),
                    "altezza": cm(s.get("defaultSalesUnitHeight")),
                    "larghezza": cm(s.get("defaultSalesUnitWidth")),
                    "lunghezza": cm(s.get("defaultSalesUnitLength")),
                    "volume": (s.get("netVolume") or {}).get("m3") or None,
                    "descrizioneDimensioneFornitore": None,
                    "altezzaImballo": cm(s.get("boxHeight")),
                    "larghezzaImballo": cm(s.get("boxWidth")),
                    "lunghezzaImballo": cm(s.get("boxLength")),
                    "volumeImballo": (s.get("boxGrossVolume") or {}).get("m3") or None,
                    "pesoLordoImballo": kg(s.get("boxGrossWeight")),
                    "giacenze": [],                    # non nel feed: API stock NWG in arrivo
                    "listini": listini,
                    "moltiplicatoreVendita": None,
                })

        if not varianti:
            self.warn(chiave, "senza varianti", "nessuno sku nel feed: scartato", "warning")
            return None

        img_art = self.front(p.get("images")) or next(
            (v["immagine"] for v in varianti if v["immagine"]), None)
        if not img_art:
            self.warn(chiave, "senza immagine", "nessuna Productpicture nel feed", "info")
        gal_art = self.galleria(p.get("images"), {img_art} if img_art else set())

        f = self.cfg["fornitore"]
        return {
            "parametriImport": dict(self.cfg["parametriImport"]),
            "articolo": {
                "codiceArticoloFornitore": ref,
                "chiaveArticolo": chiave,
                "moltiplicatoreVenditaArticolo": None,
                "descrizioneBreve": it(p.get("productName")) or ref,
                "descrizioneLunga": self.descrizione_lunga(p),
                "immagine": img_art,
                "immagineAmbientata": self.ambientata(p.get("images")),
                "immagini": gal_art,
                # "ciclo" OMESSO di proposito: i cicli si gestiscono a mano su OnPage
                "brand": {"nome": (p.get("productBrandName") or "").strip() or "NEW WAVE"},
                "fornitore": {
                    "sigla": f["sigla"], "nome": f["nome"],
                    "moltiplicatoreVendita": f["moltiplicatoreVendita"],
                    "codiceFornitoreERP": f["codiceFornitoreERP"],
                },
                "fornitoreErpIds": list(f["fornitoreErpIds"]),
                "varianti": varianti,
            },
        }

    def esegui(self, prodotti: list) -> int:
        outj = self.out / "json"
        outj.mkdir(parents=True, exist_ok=True)
        n = 0
        for p in prodotti:
            a = self.articolo(p)
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
    prodotti = json.loads((Path(args.snapshot) / "feed.json").read_text(encoding="utf-8"))
    conv = Convertitore(cfg, Path(args.out))
    n = conv.esegui(prodotti)
    errori = sum(1 for a in conv.anomalie if a["livello"] == "error")
    print(f"convertiti {n} articoli; anomalie: {len(conv.anomalie)} (errori: {errori})")
    return 1 if errori else 0


if __name__ == "__main__":
    raise SystemExit(main())
