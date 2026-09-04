#!/usr/bin/env python3
"""
convert.py - Snapshot Makito (data/snapshot/ o out/full/snapshot/) -> JSON per l'importer Custom OnPage.

Uso:
    python src/convert.py --snapshot data/snapshot --config config/mapping_02.yaml --out out

Produce:
    out/json/02-<ref>.json     un file per articolo (formato CustomImportRequest)
    out/articoli.jsonl         riepilogo piatto per report/verifica
    out/varianti.jsonl
    out/anomalie.jsonl         tutto cio' che va confermato prima dell'invio
    out/manifest.json          conteggi + hash config (per il log dell'invio)

Ogni file di snapshot <ref>.json contiene: catalogo (voci del dump /catalog/files),
stock_dump (righe {material, quantity[, availableDate]}), listino_dump
(voci {material, currency, baseQuantity, scales}). _global.json porta /colors e /sizes.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

MM_PER_CM = Decimal(10)
G_PER_KG = Decimal(1000)


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s{2,}", " ", text).strip()


def to_int(v):
    if v is None or v is False or v == "":
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def dec(v):
    if v is None or v is False or v == "":
        return None
    try:
        return Decimal(str(v))
    except ArithmeticError:
        return None


def num(d: Decimal | None, decimali=2):
    """Decimal -> numero JSON (int se intero), arrotondamento HALF_UP."""
    if d is None:
        return None
    q = d.quantize(Decimal(1).scaleb(-decimali), rounding=ROUND_HALF_UP)
    return int(q) if q == q.to_integral_value() else float(q)


def mm_to_cm(v):
    d = dec(v)
    return num(d / MM_PER_CM, 2) if d is not None else None


def title_lang(titles: dict | None, prefer=("it", "es", "en")) -> str | None:
    for lang in prefer:
        if titles and titles.get(lang):
            return titles[lang]
    return next(iter(titles.values()), None) if titles else None


class Converter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.anomalie: list[dict] = []
        self.colori: dict[str, dict] = {}     # colorCode -> {nome}
        self.taglie: dict[str, dict] = {}     # sizeCode -> {nome}
        self.colori_usati: dict[str, dict] = {}   # per il foglio Colori del report

    def warn(self, codice: str, tipo: str, dettaglio: str, gravita: str = "da confermare"):
        self.anomalie.append({"codice": codice, "gravita": gravita, "tipo": tipo, "dettaglio": dettaglio})

    # ---- liste globali ------------------------------------------------------
    def load_global(self, path: Path):
        if not path.exists():
            self.warn("*", "global assente", f"{path} non trovato: nomi colore/taglia non risolvibili", "bloccante")
            return
        g = json.loads(path.read_text(encoding="utf-8"))
        for c in (g.get("catalog:/colors", {}).get("body") or []):
            if isinstance(c, dict) and c.get("colorCode"):
                self.colori[str(c["colorCode"])] = {"nome": title_lang(c.get("colorTitle"))}
        for s in (g.get("catalog:/sizes", {}).get("body") or []):
            if isinstance(s, dict) and s.get("sizeCode"):
                self.taglie[str(s["sizeCode"])] = {"nome": title_lang(s.get("sizeTitle"))}

    # ---- colore / taglia ----------------------------------------------------
    def colore(self, ref: str, cc: str) -> dict | None:
        if cc is None or cc in (self.cfg.get("colori_null_per_codici") or []):
            return None   # "S/C" (sin color): l'importer assegna da solo "Unico"
        override = (self.cfg.get("colori_override") or {}).get(cc)
        nome = (override or {}).get("nome") or (self.colori.get(cc) or {}).get("nome")
        if not nome:
            self.warn(ref, "colore sconosciuto", f"codice colore {cc} assente da /colors: aggiungere a colori_override")
            nome = f"{cc}?"
        col = {"codice": cc, "nome": nome.capitalize() if nome.isupper() else nome, "esadecimali": []}
        regola_usata = None
        if override:
            if override.get("esadecimali"):
                col["esadecimali"] = list(override["esadecimali"])
            if override.get("coloreFiltroWebId"):
                col["coloreFiltroWebId"] = override["coloreFiltroWebId"]
            regola_usata = "override"
        else:
            for rule in self.cfg.get("colori_regole") or []:
                if re.search(rule["match"], nome, re.I):
                    if rule.get("id"):
                        col["coloreFiltroWebId"] = rule["id"]
                    if rule.get("esadecimale"):
                        col["esadecimali"] = [rule["esadecimale"]]
                    regola_usata = rule["match"]     # id null = forma/soggetto: ok senza filtro
                    break
        if regola_usata is None:
            self.warn(ref, "colore senza filtro web",
                      f"colore {cc} '{nome}': nessuna regola matcha, aggiungere regola o override")
        self.colori_usati.setdefault(cc, {"codice": cc, "nome": nome, "regola": regola_usata,
                                          "esadecimale": (col["esadecimali"] or [None])[0],
                                          "coloreFiltroWebId": col.get("coloreFiltroWebId"), "refs": []})
        self.colori_usati[cc]["refs"].append(ref)
        return col

    def taglia(self, ref: str, sc: str) -> dict | None:
        if sc is None or sc in (self.cfg.get("taglie", {}).get("null_per_codici") or []):
            return None   # l'importer assegna "Unica"
        nome = (self.taglie.get(sc) or {}).get("nome")
        if not nome:
            self.warn(ref, "taglia sconosciuta", f"codice taglia {sc} assente da /sizes")
            nome = sc
        return {"codice": sc, "nome": nome}

    # ---- giacenze / listini -------------------------------------------------
    def giacenze(self, rows: list) -> list:
        mag = {"codice": self.cfg["magazzino"]["codice"], "nome": self.cfg["magazzino"]["nome"]}
        out, visti = [], set()
        for r in rows:
            q = to_int(r.get("quantity"))
            if q is None:
                continue
            data = (r.get("availableDate") or None)
            data = data[:10] if data else None
            chiave = (r.get("material"), q, data)
            if chiave in visti:
                continue   # il feed ripete le righe datate con id interni diversi
            visti.add(chiave)
            out.append({"magazzino": mag, "quantita": q, "dataArrivo": data})
        out.sort(key=lambda g: (g["dataArrivo"] is not None, g["dataArrivo"] or ""))
        return out

    def listini(self, ref: str, voci: list) -> list:
        decimali = int(self.cfg["listino"].get("decimali", 4))
        tipologia = self.cfg["listino"]["tipologie"][0]["tipologiaListino"]
        out = []
        for voce in voci:
            if str(voce.get("material")) != str(ref):
                continue
            if voce.get("currency") not in (None, "EUR"):
                self.warn(ref, "valuta non EUR", f"listino in {voce.get('currency')}")
            base_q = dec(voce.get("baseQuantity")) or Decimal(1)
            scales = sorted(voce.get("scales") or [], key=lambda s: int(dec(s.get("quantity")) or 0))
            for i, s in enumerate(scales):
                q_da = to_int(s.get("quantity")) or 1
                q_a = (to_int(scales[i + 1].get("quantity")) - 1) if i + 1 < len(scales) else None
                amount = dec(s.get("amount"))
                if amount is None:
                    continue
                prezzo = num(amount / base_q, decimali)
                if not prezzo or prezzo <= 0:
                    self.warn(ref, "prezzo nullo", f"scaglione da {q_da}: amount {s.get('amount')}", "bloccante")
                    continue
                out.append({"tipologiaListino": tipologia, "scaglione": f"{i + 1:02d}",
                            "quantitaDa": q_da, "quantitaA": q_a, "multipli": 1, "prezzo": prezzo})
        return out

    # ---- articolo -----------------------------------------------------------
    def articolo(self, snap: dict) -> dict | None:
        entries = snap.get("catalogo") or []
        if not entries:
            return None
        e = entries[0]
        ref = str(e["ref"])
        if len(entries) > 1:
            diversi = any(json.dumps(x, sort_keys=True) != json.dumps(e, sort_keys=True) for x in entries[1:])
            if diversi:
                self.warn(ref, "voci catalogo multiple", f"{len(entries)} voci diverse nel dump per la stessa ref")

        breve = " - ".join(x for x in (e.get("name"), e.get("type")) if x) or ref
        lunga_parti = [strip_html(e.get("description") or "")]
        oss = strip_html(e.get("observations") or "")
        if oss and oss not in lunga_parti[0]:
            lunga_parti.append(oss)
        lunga = "\n\n".join(p for p in lunga_parti if p) or breve

        dim_extra = []
        if e.get("diameter"):
            dim_extra.append(f"Diametro {mm_to_cm(e['diameter'])} cm")
        if e.get("material"):
            dim_extra.append(f"Materiale: {e['material']}")

        listini = self.listini(ref, snap.get("listino_dump") or [])
        if not listini:
            # decisione Daniele 03/09/2026: NON scartare, importare anche senza prezzo
            self.warn(ref, "senza listino", "nessun prezzo nel price-list: importato con listini vuoti", "info")

        stock_per_mat: dict[str, list] = {}
        for r in snap.get("stock_dump") or []:
            stock_per_mat.setdefault(str(r.get("material")), []).append(r)

        peso_netto = dec(e.get("weight"))
        peso_netto = num(peso_netto / G_PER_KG, 3) if peso_netto else None
        var_common = {
            "pezziPerConfezione": to_int(e.get("pf_units")),
            "pezziPerSottoImballo": to_int(e.get("pi1_units")),
            "pezziPerImballo": to_int(e.get("ptc_units")),
            "pezziPerPallet": to_int(e.get("pallet_units")),
            "minimoOrdine": None,
            "pesoNetto": peso_netto,                                     # g -> kg
            "pesoLordo": num((dec(e.get("pf_weight")) or None) and dec(e.get("pf_weight")) / G_PER_KG, 3),
            "altezza": mm_to_cm(e.get("height")),
            "larghezza": mm_to_cm(e.get("width")),
            "lunghezza": mm_to_cm(e.get("length")),
            "volume": None,
            "descrizioneDimensioneFornitore": "; ".join(dim_extra) or None,
            "altezzaImballo": mm_to_cm(e.get("ptc_height")),
            "larghezzaImballo": mm_to_cm(e.get("ptc_width")),
            "lunghezzaImballo": mm_to_cm(e.get("ptc_length")),
            "volumeImballo": None,
            "pesoLordoImballo": num(dec(e.get("ptc_weight")), 3),        # gia' in kg
        }

        varianti = []
        variants = e.get("variants") or []
        if not variants:
            self.warn(ref, "senza varianti", "nessuna variante nel catalogo: creata variante unica senza colore/taglia", "info")
            variants = [{"variant_reference": ref, "variant_colorcode": None, "variant_size": None}]
        chiavi_viste = set()
        for v in variants:
            cc, sc = v.get("variant_colorcode"), v.get("variant_size")
            # nel feed capita variant_size/colorcode null o corto ("0"): nei matnr SAP
            # il segmento e' sempre di 3 cifre
            cc_eff = str(cc).zfill(3) if cc is not None else "000"
            sc_eff = str(sc).zfill(3) if sc is not None else "000"
            matnr = f"{ref}{cc_eff}{sc_eff}"
            chiave_v = f"02-{ref}-{cc_eff}-{sc_eff}"
            if chiave_v in chiavi_viste:
                # righe variante con lo stesso colore+taglia (tipico del materiale
                # promozionale, tutte S/C+S/T): si tiene la prima
                self.warn(ref, "variante duplicata", f"{chiave_v} (matnr {matnr}): tenuta la prima, "
                          f"scartata '{v.get('variant_name')}'", "info")
                continue
            chiavi_viste.add(chiave_v)
            varianti.append({
                "codiceFornitore": v.get("variant_reference") or matnr,
                "codiceVariante": chiave_v,
                "colore": self.colore(ref, cc),
                "taglia": self.taglia(ref, sc),
                "immagine": v.get("variant_image"),
                "immagini": [],
                **var_common,
                "giacenze": self.giacenze(stock_per_mat.get(matnr, [])),
                "listini": listini,
                "moltiplicatoreVendita": None,
                "_matnr": matnr,
                "_variant_name": v.get("variant_name"),
            })

        f = self.cfg["fornitore"]
        return {
            "parametriImport": dict(self.cfg["parametriImport"]),
            "articolo": {
                "codiceArticoloFornitore": ref,
                "chiaveArticolo": f"02-{ref}",
                "moltiplicatoreVenditaArticolo": None,
                "descrizioneBreve": breve,
                "descrizioneLunga": lunga,
                "immagine": e.get("image"),
                "immagineAmbientata": None,
                "immagini": [{"url": u, "tipologia": self.cfg.get("immagini", {}).get("tipologia_gallery", "Immagine dettaglio")}
                             for u in (e.get("detail_images") or [])],
                # "ciclo" OMESSO di proposito (decisione Daniele 03/09/2026): i cicli li
                # inserisce a mano su OnPage e la sync non deve toccarli ai giri successivi
                "brand": {"nome": e.get("brand") or self.cfg.get("brand", {}).get("default") or "MAKITO"},
                "fornitore": {
                    "sigla": f["sigla"], "nome": f["nome"],
                    "moltiplicatoreVendita": f["moltiplicatoreVendita"],   # 3.5, MAI 1.06 (CLAUDE.md par.6)
                    "codiceFornitoreERP": {k: str(v) for k, v in f["codiceFornitoreERP"].items()},
                },
                "fornitoreErpIds": list(f["fornitoreErpIds"]),
                "varianti": varianti,
            },
        }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", type=Path, default=Path("data/snapshot"))
    ap.add_argument("--config", type=Path, default=Path("config/mapping_02.yaml"))
    ap.add_argument("--out", type=Path, default=Path("out"))
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    conv = Converter(cfg)
    conv.load_global(args.snapshot / "_global.json")

    json_dir = args.out / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    for old in json_dir.glob("*.json"):
        old.unlink()

    articoli, varianti = [], []
    files = sorted(p for p in args.snapshot.glob("*.json") if p.stem != "_global")
    for p in files:
        snap = json.loads(p.read_text(encoding="utf-8"))
        payload = conv.articolo(snap)
        if payload is None:
            continue
        a = payload["articolo"]
        # i campi di servizio _matnr/_variant_name restano solo nei riepiloghi
        for v in a["varianti"]:
            varianti.append({"chiaveArticolo": a["chiaveArticolo"], "codiceVariante": v["codiceVariante"],
                             "matnr": v.pop("_matnr"), "nome": v.pop("_variant_name"),
                             "colore": (v["colore"] or {}).get("nome"),
                             "coloreFiltroWebId": (v["colore"] or {}).get("coloreFiltroWebId"),
                             "taglia": (v["taglia"] or {}).get("nome"),
                             "giacenza_attuale": sum(g["quantita"] for g in v["giacenze"] if not g["dataArrivo"]),
                             "arrivi": [(g["dataArrivo"], g["quantita"]) for g in v["giacenze"] if g["dataArrivo"]],
                             "prezzo": v["listini"][0]["prezzo"] if v["listini"] else None})
        (json_dir / f"{a['chiaveArticolo']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        articoli.append({"chiave": a["chiaveArticolo"], "ref": a["codiceArticoloFornitore"],
                         "descrizioneBreve": a["descrizioneBreve"], "brand": a["brand"]["nome"],
                         "varianti": len(a["varianti"]), "listini_per_variante": len(a["varianti"][0]["listini"]),
                         "immagine": a["immagine"]})

    def write_jsonl(path: Path, rows):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    write_jsonl(args.out / "articoli.jsonl", articoli)
    write_jsonl(args.out / "varianti.jsonl", varianti)
    write_jsonl(args.out / "anomalie.jsonl", conv.anomalie)
    (args.out / "colori_usati.json").write_text(
        json.dumps(conv.colori_usati, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest = {"articoli": len(articoli), "varianti": len(varianti), "anomalie": len(conv.anomalie),
                "config_sha1": hashlib.sha1(args.config.read_bytes()).hexdigest()}
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    bloccanti = sum(1 for a in conv.anomalie if a["gravita"] == "bloccante")
    print(f"articoli: {len(articoli)}  varianti: {len(varianti)}  "
          f"anomalie: {len(conv.anomalie)} (bloccanti: {bloccanti})")
    for a in conv.anomalie[:20]:
        print(f"  [{a['gravita']}] {a['codice']}: {a['tipo']} - {a['dettaglio'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
