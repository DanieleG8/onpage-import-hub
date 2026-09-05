"""onpage-import-hub: API + scheduler + proxy immagini.

Convenzioni riprese da orderEntry: PORT da env con bind 0.0.0.0 (Railway),
secrets solo da variabili d'ambiente con degradazione morbida, endpoint
macchina protetti da header x-api-key confrontato in tempo costante.
"""
from __future__ import annotations

import hmac
import json
import threading

import requests as rq
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, state
from .runner import run_job
from .scheduler import avvia_scheduler

app = FastAPI(title="onpage-import-hub", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- auth ------
def _check_key(x_api_key: str | None, key_qs: str | None):
    if not config.HUB_API_KEY:
        raise HTTPException(200, detail="not_configured")  # convenzione orderEntry
    fornita = x_api_key or key_qs or ""
    if not hmac.compare_digest(fornita.encode(), config.HUB_API_KEY.encode()):
        raise HTTPException(401, detail="unauthorized")


# --------------------------------------------------------------- health -----
@app.get("/health")
def health():
    return {"ok": True, "not_configured": config.not_configured()}


# ------------------------------------------------------------ run / status --
_run_in_corso: dict = {}


def _esegui(fornitore: str, job: str):
    try:
        _run_in_corso[fornitore] = job
        run_job(job)
    except Exception as e:                                    # noqa: BLE001
        state.append_run(fornitore, {"job": job, "esito": "eccezione", "errore": str(e)[:500]})
    finally:
        _run_in_corso.pop(fornitore, None)


@app.post("/run/{fornitore}/{job}")
def run(fornitore: str, job: str,
        x_api_key: str | None = Header(default=None),
        key: str | None = Query(default=None)):
    _check_key(x_api_key, key)
    if fornitore != "makito":
        raise HTTPException(404, detail="fornitore sconosciuto")
    if _run_in_corso.get(fornitore):
        return JSONResponse({"ok": False, "reason": "run_gia_in_corso",
                             "job_attivo": _run_in_corso[fornitore]}, status_code=409)
    threading.Thread(target=_esegui, args=(fornitore, job), daemon=True).start()
    return {"ok": True, "avviato": job}


@app.get("/status")
def status(x_api_key: str | None = Header(default=None), key: str | None = Query(default=None)):
    _check_key(x_api_key, key)
    return {"in_corso": _run_in_corso, "ultimi_run": {"makito": state.ultimi_run("makito")}}


@app.post("/reinvia-falliti/{fornitore}")
def reinvia_falliti(fornitore: str,
                    x_api_key: str | None = Header(default=None),
                    key: str | None = Query(default=None)):
    """Riarma gli articoli il cui ULTIMO invio e' fallito: toglie i loro hash dallo
    stato, cosi' il prossimo job delta li rimanda. Serve dopo un full con errori
    parziali (gli hash dei falliti restano quelli del bootstrap e il delta li salta)."""
    _check_key(x_api_key, key)
    p = state.dir_fornitore(fornitore) / "send_log.jsonl"
    if not p.exists():
        return {"chiavi_riarmate": 0, "chiavi": []}
    esiti: dict[str, bool] = {}
    for l in p.read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            esiti[r["chiave"]] = bool(r.get("ok"))
    falliti = [k for k, ok in esiti.items() if not ok]
    hashes = state.load_hashes(fornitore)
    riarmate = [k for k in falliti if hashes.pop(k, None) is not None]
    state.save_hashes(fornitore, hashes)
    return {"chiavi_riarmate": len(riarmate), "chiavi": sorted(riarmate)[:50]}


@app.get("/send-log/{fornitore}")
def send_log(fornitore: str, n: int = Query(default=50, le=500),
             solo_errori: bool = Query(default=True),
             x_api_key: str | None = Header(default=None), key: str | None = Query(default=None)):
    """Ultime righe del log invii (default: solo i falliti, per diagnosi)."""
    _check_key(x_api_key, key)
    p = state.dir_fornitore(fornitore) / "send_log.jsonl"
    if not p.exists():
        return {"righe": []}
    righe = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if solo_errori:
        righe = [r for r in righe if not r.get("ok")]
    return {"totale": len(righe), "righe": righe[-n:]}


# -------------------------------------------------------- proxy immagini ----
_jwt_lock = threading.Lock()
_jwt_token: str | None = None


def _makito_jwt(forza: bool = False) -> str | None:
    global _jwt_token
    with _jwt_lock:
        if _jwt_token and not forza:
            return _jwt_token
        if not (config.MAKITO_CLIENT_ID and config.MAKITO_CLIENT_SECRET):
            return None
        r = rq.post("https://apis.makito.es/access/auth/login",
                    json={"clientId": config.MAKITO_CLIENT_ID,
                          "clientSecret": config.MAKITO_CLIENT_SECRET}, timeout=60)
        _jwt_token = (r.json() or {}).get("token") if r.status_code == 200 else None
        return _jwt_token


@app.get("/img/makito/{path:path}")
def img_makito(path: str):
    """L'importer OnPage scarica le immagini SENZA autenticazione: questo endpoint
    (pubblico, sola lettura, path confinato agli asset di catalogo) gira la
    richiesta a Makito col JWT e ne rigira i byte."""
    if ".." in path or path.startswith("/"):
        raise HTTPException(400, detail="path non valido")
    token = _makito_jwt()
    if not token:
        raise HTTPException(503, detail="not_configured")
    url = "https://apis.makito.es/catalog/assets/" + path
    r = rq.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=120, stream=True)
    if r.status_code == 401:                                  # token scaduto: un refresh e riprova
        token = _makito_jwt(forza=True)
        r = rq.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=120, stream=True)
    if r.status_code != 200:
        raise HTTPException(r.status_code, detail="asset non disponibile")
    tipo = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else \
           "image/png" if path.lower().endswith(".png") else "application/octet-stream"
    return StreamingResponse(r.iter_content(65536), media_type=tipo)


# ------------------------------------------------------------- scheduler ----
@app.on_event("startup")
def startup():
    avvia_scheduler(_esegui, _run_in_corso)
