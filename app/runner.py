"""Orchestrazione di un run di import per il fornitore Makito.

Job disponibili (tutti delta: si invia solo cio' che e' cambiato rispetto
all'ultimo invio riuscito, hash in {STATE_DIR}/makito/state.json):
  stock     riscarica solo il dump giacenze (catalogo/prezzi dalla cache)   - ogni ora
  prezzi    riscarica listino+giacenze                                      - giornaliero
  prodotti  riscarica tutto (catalogo, colori, taglie, stock, prezzi)       - settimanale
  full      come prodotti ma invia TUTTO ignorando gli hash (riallineamento)
  bootstrap come prodotti ma NON invia nulla: marca lo stato attuale come "gia'
            inviato" (primo avvio dopo il full fatto su GitHub Actions)

Riusa i moduli di F02 cosi' come sono (fetch/convert/validate hanno main(argv)):
la conversione completa costa ~3 s, quindi ogni job riconverte tutto e il delta
decide cosa inviare.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from . import state
from .config import PUBLIC_BASE_URL
from .sender import invia_lotto

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "suppliers" / "makito"))

import convert as makito_convert          # noqa: E402
import fetch_makito as makito_fetch       # noqa: E402
import validate as makito_validate        # noqa: E402

MAPPING = REPO_ROOT / "suppliers" / "makito" / "config" / "mapping_02.yaml"
ASSET_PREFIX = "https://apis.makito.es/catalog/assets/"


def _proxy_url(testo: str) -> str:
    """Gli asset Makito richiedono il JWT: nei payload gli URL passano dal proxy del hub."""
    if not PUBLIC_BASE_URL:
        return testo
    return testo.replace(ASSET_PREFIX, f"{PUBLIC_BASE_URL}/img/makito/")


def _hash_diviso(payload: dict) -> dict:
    """Doppia impronta: 'd' = dati (payload senza immagini), 'i' = solo immagini.
    Permette il giro veloce solo-dati quando cambiano giacenze/prezzi ma non le foto."""
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


def run_job(job: str) -> dict:
    """Esegue un job end-to-end; ritorna il riepilogo (registrato anche in runs.jsonl)."""
    if job not in ("stock", "prezzi", "prodotti", "full", "bootstrap"):
        raise ValueError(f"job sconosciuto: {job}")
    t0 = time.time()
    base = state.dir_fornitore("makito")
    work = base / "work"
    dumps_cache = base / "dumps"
    refresh = {"stock": "stock", "prezzi": "prezzi", "prodotti": "tutto", "full": "tutto",
               "bootstrap": "tutto"}[job]

    with state.lock_fornitore("makito"):
        # 1. fetch (snapshot completo per ref; i dump non richiesti arrivano dalla cache)
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        cwd = Path.cwd()
        log_fetch = io.StringIO()
        try:
            # fetch_makito scrive DOCS/openapi e docs_pages relativi alla cwd: lo si esegue in work
            import os
            os.chdir(work)
            (work / "DOCS").mkdir(exist_ok=True)
            with redirect_stdout(log_fetch):
                rc = makito_fetch.main(["--all", "--out", str(work / "raw"),
                                        "--refresh", refresh, "--dumps-cache", str(dumps_cache)])
        finally:
            os.chdir(cwd)
        if rc != 0:
            esito = {"job": job, "esito": "errore_fetch", "durata_s": round(time.time() - t0, 1),
                     "log": log_fetch.getvalue()[-2000:]}
            state.append_run("makito", esito)
            return esito

        # 2. convert + validate (sempre completi: costano ~3 s)
        out = work / "out"
        with redirect_stdout(io.StringIO()) as log_conv:
            makito_convert.main(["--snapshot", str(work / "raw" / "snapshot"),
                                 "--config", str(MAPPING), "--out", str(out)])
            rc_val = makito_validate.main(["--out", str(out)])
        if rc_val != 0:
            esito = {"job": job, "esito": "errore_validazione", "durata_s": round(time.time() - t0, 1),
                     "log": log_conv.getvalue()[-2000:]}
            state.append_run("makito", esito)
            return esito

        # 3. delta: si inviano solo gli articoli con payload diverso dall'ultimo inviato ok
        hashes = state.load_hashes("makito")
        if job in ("stock", "prezzi", "prodotti") and not hashes:
            # senza hash un job delta rimanderebbe TUTTI gli articoli (~10 h):
            # succede solo se il bootstrap non e' mai girato o se lo stato e' andato
            # perso (Volume /data non montato). Meglio fermarsi e segnalarlo.
            esito = {"job": job, "esito": "stato_vuoto",
                     "errore": "nessun hash registrato: eseguire bootstrap (o full); "
                               "verificare che il Volume /data sia montato",
                     "durata_s": round(time.time() - t0, 1)}
            state.append_run("makito", esito)
            return esito
        payloads: dict[str, dict] = {}
        stati_nuovi: dict[str, dict] = {}
        solo_dati = 0
        totale = 0
        for f in sorted((out / "json").glob("*.json")):
            totale += 1
            payload = json.loads(_proxy_url(f.read_text(encoding="utf-8")))
            chiave = payload["articolo"]["chiaveArticolo"]
            nuovo = _hash_diviso(payload)
            if job == "bootstrap":
                # niente invio: il carico completo e' gia' su OnPage;
                # da qui in poi partiranno solo le differenze reali
                hashes[chiave] = nuovo
                continue
            vecchio = hashes.get(chiave)
            if isinstance(vecchio, str):
                # stato in formato vecchio (hash unico): se il payload completo e'
                # identico non c'e' nulla da fare, altrimenti reinvio completo
                if job != "full" and vecchio == state.hash_payload(payload):
                    continue
                vecchio = None
            if job != "full" and vecchio and vecchio.get("d") == nuovo["d"] \
                    and vecchio.get("i") == nuovo["i"]:
                continue
            if job != "full" and vecchio and vecchio.get("i") == nuovo["i"]:
                # immagini invariate rispetto all'ultimo invio ok: giro veloce
                # solo-dati (gestioneImmagini='aggiungi', nessun URL passato)
                payload = _senza_immagini(payload)
                solo_dati += 1
            payloads[chiave] = payload
            stati_nuovi[chiave] = nuovo
        if job == "bootstrap":
            state.save_hashes("makito", hashes)
            esito = {"job": job, "esito": "ok", "articoli_totali": totale, "da_inviare": 0,
                     "hash_registrati": totale, "durata_s": round(time.time() - t0, 1)}
            state.append_run("makito", esito)
            return esito

        # 4. invio parallelo
        contatori = invia_lotto("makito", payloads, stati_nuovi=stati_nuovi) if payloads \
            else {"inviati_ok": 0, "errori": 0}
        esito = {"job": job, "esito": "ok" if contatori.get("errori", 0) == 0 else "errori_parziali",
                 "articoli_totali": totale, "da_inviare": len(payloads), "solo_dati": solo_dati,
                 **contatori, "durata_s": round(time.time() - t0, 1)}
        state.append_run("makito", esito)
        return esito
