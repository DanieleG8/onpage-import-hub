"""Invio parallelo all'importer Custom OnPage.

Stessa chiamata di F02/src/send.py (POST un articolo per volta), ma con pool di
worker e retry sugli errori transitori. Il collo di bottiglia misurato e'
l'importer stesso (~7 s/articolo): con SEND_WORKERS=3 il costo effettivo scende
a ~2,3 s/articolo. Gli hash di stato vengono aggiornati SOLO sugli esiti ok:
un articolo fallito rientra automaticamente nel delta del giro successivo.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from . import state
from .config import IMPORTER_TOKEN, IMPORTER_URL, SEND_WORKERS

# visti sul campo, tutti transitori e riusciti a un tentativo successivo:
# errorType "system" ("Cannot assign null to property ...$json_data"),
# 404 "No query results for model [ProjectJobFile]" (race interna dell'exporter),
# 504 Gateway Time-out di nginx (importer oltre i 60 s su un articolo).
# Si ritenta quindi tutto tranne gli errori di validazione (deterministici).
RETRY_SYSTEM = 2
RETRY_WAIT_S = (30, 120)


def _post(payload: dict, timeout=180) -> dict:
    headers = {"content-type": "application/json", "accept": "application/json"}
    if IMPORTER_TOKEN:
        headers["authorization"] = f"Bearer {IMPORTER_TOKEN}"
    r = requests.post(IMPORTER_URL, json=payload, headers=headers, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {"error": True, "errorType": "system", "message": r.text[:2000]}
    body["_http_status"] = r.status_code
    return body


def invia_uno(chiave: str, payload: dict) -> dict:
    """Invia un articolo con retry su errori di rete e 'system'. Ritorna il record di log."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "chiave": chiave}
    t0 = time.time()
    tentativi = 1 + RETRY_SYSTEM
    for i in range(tentativi):
        try:
            body = _post(payload)
        except requests.RequestException as e:
            body = {"error": True, "errorType": "network", "message": str(e)}
        ok = (body.get("_http_status", 599) < 400) and not body.get("error")
        # "JSON data not provided" arriva marcato "validation" ma e' un glitch
        # transitorio dell'exporter (visto risolversi da solo): va ritentato.
        transitorio_mascherato = "JSON data not provided" in str(body.get("message", ""))
        riprovabile = (not ok) and (body.get("errorType") != "validation"
                                    or transitorio_mascherato)
        if ok or not riprovabile or i == tentativi - 1:
            rec.update(ok=ok, response={k: v for k, v in body.items() if k != "_http_status"},
                       http_status=body.get("_http_status"), tentativi=i + 1,
                       elapsed_s=round(time.time() - t0, 2))
            return rec
        time.sleep(RETRY_WAIT_S[min(i, len(RETRY_WAIT_S) - 1)])
    return rec  # non raggiunto


def invia_lotto(fornitore: str, payloads: dict[str, dict], workers: int | None = None,
                stati_nuovi: dict[str, object] | None = None) -> dict:
    """Invia i payload (chiave -> CustomImportRequest) in parallelo.
    Aggiorna send_log e, per gli ok, gli hash di stato: il valore salvato per una
    chiave e' stati_nuovi[chiave] se fornito (doppia impronta dati/immagini del
    runner), altrimenti l'hash del payload inviato. Ritorna contatori."""
    workers = workers or SEND_WORKERS
    hashes = state.load_hashes(fornitore)
    n_ok = n_err = 0
    # quanti ok hanno avuto bisogno di un secondo o terzo tentativo: e' la
    # misura che dice se i retry si ripagano. Un 504 o un ProjectJobFile 404
    # costano ~250 s a chiave fra attese e tentativi (44 di questi hanno fatto
    # durare 1h20m il run stock di makito dell'11/09): se nessun ok arrivasse
    # mai al secondo giro, tanto varrebbe lasciarli al run successivo, che li
    # ritenta comunque perche' l'hash non viene salvato.
    n_ok_ritentati = 0
    errori: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(invia_uno, k, p): k for k, p in payloads.items()}
        for fut in as_completed(futures):
            rec = fut.result()
            state.append_send_log(fornitore, rec)
            if rec.get("ok"):
                n_ok += 1
                if (rec.get("tentativi") or 1) > 1:
                    n_ok_ritentati += 1
                hashes[rec["chiave"]] = (stati_nuovi or {}).get(rec["chiave"]) \
                    or state.hash_payload(payloads[rec["chiave"]])
                if n_ok % 50 == 0:
                    state.save_hashes(fornitore, hashes)  # checkpoint periodico
            else:
                n_err += 1
                errori.append(rec["chiave"])
    state.save_hashes(fornitore, hashes)
    return {"inviati_ok": n_ok, "errori": n_err,
            **({"ok_solo_dopo_retry": n_ok_ritentati} if n_ok_ritentati else {}),
            "chiavi_errore": errori[:50]}
