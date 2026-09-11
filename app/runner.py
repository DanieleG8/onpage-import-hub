"""Orchestrazione dei run di import per fornitore (registry multi-fornitore).

Fornitori attivi: makito (02), nwg (A1 New Wave). Ogni fornitore definisce i
suoi job e i moduli fetch/convert/validate (riusati cosi' come sono, con
main(argv)). Job comuni:
  full      invia TUTTO ignorando gli hash (riallineamento)
  bootstrap registra lo stato attuale come "gia' inviato", nessun invio
Job delta (inviano solo cio' che e' cambiato dall'ultimo invio ok):
  makito: stock (ogni ora), prezzi (giornaliero), prodotti (settimanale)
  nwg:    prodotti (feed unico giornaliero)

Delta a doppia impronta {d: hash dati, i: hash immagini}: se cambiano solo i
dati il payload parte senza immagini con gestioneImmagini='aggiungi' (idea di
Daniele, 05/09/2026) e l'importer salta la coda di download.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from . import state
from .config import PUBLIC_BASE_URL
from .sender import invia_lotto

REPO_ROOT = Path(__file__).resolve().parent.parent


def _carica(nome: str, percorso: Path):
    spec = importlib.util.spec_from_file_location(nome, percorso)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nome] = mod
    spec.loader.exec_module(mod)
    return mod


_MAKITO = REPO_ROOT / "suppliers" / "makito"
_NWG = REPO_ROOT / "suppliers" / "nwg"
_PF = REPO_ROOT / "suppliers" / "pfconcept"
sys.path.insert(0, str(_MAKITO))          # i moduli Makito storici si importano tra pari
makito_fetch = _carica("makito_fetch", _MAKITO / "fetch_makito.py")
makito_convert = _carica("makito_convert", _MAKITO / "convert.py")
makito_validate = _carica("makito_validate", _MAKITO / "validate.py")
nwg_fetch = _carica("nwg_fetch", _NWG / "fetch_nwg.py")
nwg_convert = _carica("nwg_convert", _NWG / "convert.py")
nwg_validate = _carica("nwg_validate", _NWG / "validate.py")
pf_fetch = _carica("pf_fetch", _PF / "fetch_pfconcept.py")
pf_convert = _carica("pf_convert", _PF / "convert.py")
pf_validate = _carica("pf_validate", _PF / "validate.py")
_GARYS = REPO_ROOT / "suppliers" / "garys"
garys_fetch = _carica("garys_fetch", _GARYS / "fetch_garys.py")
garys_convert = _carica("garys_convert", _GARYS / "convert.py")
garys_validate = _carica("garys_validate", _GARYS / "validate.py")

ASSET_PREFIX = "https://apis.makito.es/catalog/assets/"

FORNITORI = {
    "makito": {
        "jobs": {"stock": "stock", "prezzi": "prezzi", "prodotti": "tutto",
                 "full": "tutto", "bootstrap": "tutto"},
        "fetch": lambda work, cache, refresh: makito_fetch.main(
            ["--all", "--out", str(work / "raw"), "--refresh", refresh,
             "--dumps-cache", str(cache)]),
        "snapshot": lambda work: work / "raw" / "snapshot",
        "convert": makito_convert, "validate": makito_validate,
        "mapping": _MAKITO / "config" / "mapping_02.yaml",
        "proxy": True,                    # gli asset Makito richiedono il JWT
    },
    "nwg": {
        "jobs": {"prodotti": "tutto", "full": "tutto", "bootstrap": "tutto"},
        "fetch": lambda work, cache, refresh: nwg_fetch.main(
            ["--out", str(work / "raw"), "--dumps-cache", str(cache)]),
        "snapshot": lambda work: work / "raw" / "snapshot",
        "convert": nwg_convert, "validate": nwg_validate,
        "mapping": _NWG / "config" / "mapping_a1.yaml",
        "proxy": False,                   # immagini NWG pubbliche
    },
    "pfconcept": {
        "jobs": {"stock": "stock", "prezzi": "prezzi", "prodotti": "tutto",
                 "full": "tutto", "bootstrap": "tutto"},
        "fetch": lambda work, cache, refresh: pf_fetch.main(
            ["--out", str(work / "raw"), "--refresh", refresh,
             "--dumps-cache", str(cache)]),
        "snapshot": lambda work: work / "raw" / "snapshot",
        "convert": pf_convert, "validate": pf_validate,
        "mapping": _PF / "config" / "mapping_34.yaml",
        "proxy": False,                   # immagini PF pubbliche (live link)
    },
    "garys": {
        "jobs": {"prodotti": "tutto", "full": "tutto", "bootstrap": "tutto"},
        "fetch": lambda work, cache, refresh: garys_fetch.main(
            ["--out", str(work / "raw"), "--dumps-cache", str(cache)]),
        "snapshot": lambda work: work / "raw" / "snapshot",
        "convert": garys_convert, "validate": garys_validate,
        "mapping": _GARYS / "config" / "mapping_a3.yaml",
        "proxy": False,                   # immagini su cdn.shopify.com, pubbliche
        "schema_guard": True,             # file depositati a mano: struttura vigilata
    },
}


def _proxy_url(testo: str) -> str:
    """Gli asset Makito richiedono il JWT: nei payload gli URL passano dal proxy del hub."""
    if not PUBLIC_BASE_URL:
        return testo
    return testo.replace(ASSET_PREFIX, f"{PUBLIC_BASE_URL}/img/makito/")


def _hash_diviso(payload: dict) -> dict:
    """Doppia impronta: 'd' = dati (payload senza immagini), 'i' = solo immagini.
    'c' = impronta per campo, che non entra nel confronto: serve solo a dire
    COSA e' cambiato quando 'd' cambia (vedi _campi_cambiati)."""
    return {"d": state.hash_payload(_senza_immagini(payload)),
            "i": state.hash_payload(_solo_immagini(payload)),
            "c": _hash_campi(payload)}


def _hash_campi(payload: dict) -> dict:
    """Un'impronta per campo dell'articolo, piu' le varianti spezzate.

    Nasce da una domanda senza risposta (pfconcept, 09-11/09/2026): il job
    `prodotti` rimanda ~1.450 articoli ogni notte, il job `stock` nelle ore
    successive ne trova 2. Qualcosa cambia una volta al giorno e non sono le
    giacenze, ma per saperlo servirebbe il payload di ieri -- 56 MB per
    fornitore. Venti impronte per articolo costano mille volte meno e
    rispondono alla stessa domanda.

    Le varianti sono spezzate perche' e' li' che vive tutto, e separate per
    ORDINE e INSIEME: se cambia 'varianti:ordine' ma non 'varianti:insieme',
    il feed ha semplicemente restituito le stesse varianti in ordine diverso e
    il delta sta inseguendo un fantasma (hash_payload ordina le chiavi dei
    dizionari, non gli elementi delle liste).
    """
    # 12 caratteri bastano per dire "diverso": lo stato si riscrive ogni 50
    # invii e con lo sha1 intero crescerebbe di qualche MB per fornitore, da
    # riversare sul Volume decine di volte per run.
    def h(x) -> str:
        return state.hash_payload(x)[:12]

    a = payload["articolo"]
    campi = {k: h(v) for k, v in a.items() if k != "varianti"}
    var = a.get("varianti") or []
    per_codice = sorted(var, key=lambda v: str(v.get("codiceVariante")))
    campi["varianti:ordine"] = h([v.get("codiceVariante") for v in var])
    campi["varianti:insieme"] = h(sorted(str(v.get("codiceVariante")) for v in var))
    campi["varianti:giacenze"] = h([v.get("giacenze") for v in per_codice])
    campi["varianti:listini"] = h([v.get("listini") for v in per_codice])
    campi["varianti:anagrafica"] = h(
        [{k: val for k, val in v.items()
          if k not in ("giacenze", "listini", "immagine", "immagini")} for v in per_codice])
    return campi


def _campi_cambiati(vecchio: dict | None, nuovo: dict) -> list[str]:
    """Nomi dei campi la cui impronta e' cambiata. Solo diagnostica."""
    if not vecchio:
        return ["(articolo nuovo)"]
    prima, dopo = vecchio.get("c"), nuovo.get("c") or {}
    if not prima:
        return ["(stato senza impronte per campo)"]
    return sorted(k for k in set(prima) | set(dopo) if prima.get(k) != dopo.get(k))


def _solo_immagini(payload: dict) -> dict:
    a = payload["articolo"]
    return {"immagine": a.get("immagine"), "ambientata": a.get("immagineAmbientata"),
            "gallery": a.get("immagini") or [],
            "varianti": {v.get("codiceVariante"): {"immagine": v.get("immagine"),
                                                   "immagini": v.get("immagini") or []}
                         for v in a.get("varianti") or []}}


def _senza_immagini(payload: dict) -> dict:
    """Copia del payload senza immagini e con gestioneImmagini='aggiungi': con la
    lista vuota l'importer non tocca le immagini esistenti e salta la coda di
    download (idea di Daniele, 05/09/2026) -> chiamate molto piu' veloci."""
    p = json.loads(json.dumps(payload, ensure_ascii=False))
    p["parametriImport"] = {**p.get("parametriImport", {}), "gestioneImmagini": "aggiungi"}
    a = p["articolo"]
    a["immagine"] = None
    a["immagineAmbientata"] = None
    a["immagini"] = []
    for v in a.get("varianti") or []:
        v["immagine"] = None
        v["immagini"] = []
    return p


def run_job(fornitore: str, job: str, workers: int | None = None,
            solo: list[str] | None = None) -> dict:
    """Esegue un job end-to-end; ritorna il riepilogo (registrato in runs.jsonl)."""
    if fornitore not in FORNITORI:
        raise ValueError(f"fornitore sconosciuto: {fornitore}")
    cfg = FORNITORI[fornitore]
    if job not in cfg["jobs"]:
        raise ValueError(f"job sconosciuto per {fornitore}: {job}")
    t0 = time.time()
    base = state.dir_fornitore(fornitore)
    work = base / "work"
    dumps_cache = base / "dumps"
    refresh = cfg["jobs"][job]

    with state.lock_fornitore(fornitore):
        # 1. fetch
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        cwd = Path.cwd()
        log_fetch = io.StringIO()
        try:
            # i fetch storici scrivono file di appoggio relativi alla cwd
            os.chdir(work)
            (work / "DOCS").mkdir(exist_ok=True)
            with redirect_stdout(log_fetch):
                rc = cfg["fetch"](work, dumps_cache, refresh)
        finally:
            os.chdir(cwd)
        if rc != 0:
            esito = {"job": job, "esito": "errore_fetch",
                     "durata_s": round(time.time() - t0, 1),
                     "log": log_fetch.getvalue()[-2000:]}
            state.append_run(fornitore, esito)
            return esito

        # 1b. guardia di struttura sui file depositati a mano: se il tracciato
        # e' cambiato rispetto al riferimento, NIENTE import (Daniele va
        # avvisato; la struttura attesa e' su GET /schema/{fornitore})
        if cfg.get("schema_guard"):
            from .schema_guard import verifica
            differenze = []
            for f in sorted(cfg["snapshot"](work).glob("*.xls*")):
                differenze += [f"{f.name}: {d}" for d in verifica(base / "schema", f)]
            if differenze:
                esito = {"job": job, "esito": "struttura_file_diversa",
                         "differenze": differenze[:20],
                         "durata_s": round(time.time() - t0, 1)}
                state.append_run(fornitore, esito)
                return esito

        # 2. convert + validate (sempre completi)
        out = work / "out"
        with redirect_stdout(io.StringIO()) as log_conv:
            cfg["convert"].main(["--snapshot", str(cfg["snapshot"](work)),
                                 "--config", str(cfg["mapping"]), "--out", str(out)])
            rc_val = cfg["validate"].main(["--out", str(out)])
        if rc_val != 0:
            esito = {"job": job, "esito": "errore_validazione",
                     "durata_s": round(time.time() - t0, 1),
                     "log": log_conv.getvalue()[-2000:]}
            state.append_run(fornitore, esito)
            return esito

        # 3. delta
        hashes = state.load_hashes(fornitore)
        if job not in ("full", "bootstrap") and not hashes:
            # senza hash un job delta rimanderebbe TUTTI gli articoli:
            # bootstrap mai eseguito o stato perso (Volume /data non montato)
            esito = {"job": job, "esito": "stato_vuoto",
                     "errore": "nessun hash registrato: eseguire bootstrap (o full); "
                               "verificare che il Volume /data sia montato",
                     "durata_s": round(time.time() - t0, 1)}
            state.append_run(fornitore, esito)
            return esito
        payloads: dict[str, dict] = {}
        stati_nuovi: dict[str, dict] = {}
        solo_set = set(solo) if solo else None
        solo_dati = 0
        totale = 0
        campi_cambiati: dict[str, int] = {}
        for f in sorted((out / "json").glob("*.json")):
            totale += 1
            testo = f.read_text(encoding="utf-8")
            payload = json.loads(_proxy_url(testo) if cfg["proxy"] else testo)
            chiave = payload["articolo"]["chiaveArticolo"]
            nuovo = _hash_diviso(payload)
            if job == "bootstrap":
                hashes[chiave] = nuovo
                continue
            if solo_set is not None and chiave not in solo_set:
                continue
            vecchio = hashes.get(chiave)
            if isinstance(vecchio, str):
                # stato in formato vecchio (hash unico)
                if job != "full" and vecchio == state.hash_payload(payload):
                    continue
                vecchio = None
            if job != "full" and vecchio and vecchio.get("d") == nuovo["d"] \
                    and vecchio.get("i") == nuovo["i"]:
                continue
            for campo in _campi_cambiati(vecchio, nuovo):
                campi_cambiati[campo] = campi_cambiati.get(campo, 0) + 1
            if job != "full" and vecchio and vecchio.get("i") == nuovo["i"]:
                payload = _senza_immagini(payload)
                solo_dati += 1
            payloads[chiave] = payload
            stati_nuovi[chiave] = nuovo
        if job == "bootstrap":
            state.save_hashes(fornitore, hashes)
            esito = {"job": job, "esito": "ok", "articoli_totali": totale,
                     "da_inviare": 0, "hash_registrati": totale,
                     "durata_s": round(time.time() - t0, 1)}
            state.append_run(fornitore, esito)
            return esito

        # 4. invio parallelo
        contatori = invia_lotto(fornitore, payloads, workers=workers,
                                stati_nuovi=stati_nuovi) if payloads \
            else {"inviati_ok": 0, "errori": 0}
        esito = {"job": job, "esito": "ok" if contatori.get("errori", 0) == 0 else "errori_parziali",
                 "articoli_totali": totale, "da_inviare": len(payloads),
                 "solo_dati": solo_dati,
                 # perche' questi articoli sono stati rimandati, campo per campo:
                 # la riga che dice se il delta sta inseguendo un dato vero
                 **({"campi_cambiati": dict(sorted(campi_cambiati.items(),
                                                   key=lambda kv: -kv[1]))}
                    if campi_cambiati else {}),
                 **contatori,
                 "durata_s": round(time.time() - t0, 1)}
        state.append_run(fornitore, esito)
        return esito
