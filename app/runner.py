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
sys.path.insert(0, str(_MAKITO))          # i moduli Makito storici si importano tra pari
makito_fetch = _carica("makito_fetch", _MAKITO / "fetch_makito.py")
makito_convert = _carica("makito_convert", _MAKITO / "convert.py")
makito_validate = _carica("makito_validate", _MAKITO / "validate.py")
nwg_fetch = _carica("nwg_fetch", _NWG / "fetch_nwg.py")
nwg_convert = _carica("nwg_convert", _NWG / "convert.py")
nwg_validate = _carica("nwg_validate", _NWG / "validate.py")

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
}


def _proxy_url(testo: str) -> str:
    """Gli asset Makito richiedono il JWT: nei payload gli URL passano dal proxy del hub."""
    if not PUBLIC_BASE_URL:
        return testo
    return testo.replace(ASSET_PREFIX, f"{PUBLIC_BASE_URL}/img/makito/")


def _hash_diviso(payload: dict) -> dict:
    """Doppia impronta: 'd' = dati (payload senza immagini), 'i' = solo immagini."""
    return {"d": state.hash_payload(_senza_immagini(payload)),
            "i": state.hash_payload(_solo_immagini(payload))}


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
                 "solo_dati": solo_dati, **contatori,
                 "durata_s": round(time.time() - t0, 1)}
        state.append_run(fornitore, esito)
        return esito
