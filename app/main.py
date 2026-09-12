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
from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, state
from .runner import FORNITORI, run_job
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
    from .scheduler import stato_cron
    return {"ok": True, "not_configured": config.not_configured(), "cron": stato_cron}


# ------------------------------------------------------------ run / status --
_run_in_corso: dict = {}


def _esegui(fornitore: str, job: str, workers: int | None = None,
            solo: list[str] | None = None):
    try:
        _run_in_corso[fornitore] = job
        run_job(fornitore, job, workers=workers, solo=solo)
    except Exception as e:                                    # noqa: BLE001
        state.append_run(fornitore, {"job": job, "esito": "eccezione", "errore": str(e)[:500]})
    finally:
        _run_in_corso.pop(fornitore, None)


@app.post("/run/{fornitore}/{job}")
def run(fornitore: str, job: str,
        workers: int | None = Query(default=None, ge=1, le=6),
        solo: str | None = Query(default=None),
        x_api_key: str | None = Header(default=None),
        key: str | None = Query(default=None)):
    _check_key(x_api_key, key)
    if fornitore not in FORNITORI:
        raise HTTPException(404, detail="fornitore sconosciuto")
    if _run_in_corso.get(fornitore):
        return JSONResponse({"ok": False, "reason": "run_gia_in_corso",
                             "job_attivo": _run_in_corso[fornitore]}, status_code=409)
    chiavi = [c for c in (solo or "").replace(" ", ",").split(",") if c] or None
    threading.Thread(target=_esegui, args=(fornitore, job, workers, chiavi),
                     daemon=True).start()
    return {"ok": True, "avviato": job, "fornitore": fornitore,
            "workers": workers, "solo": chiavi}


@app.get("/status")
def status(x_api_key: str | None = Header(default=None), key: str | None = Query(default=None)):
    _check_key(x_api_key, key)
    return {"in_corso": _run_in_corso,
            "ultimi_run": {f: state.ultimi_run(f) for f in FORNITORI}}


@app.get("/payload/{fornitore}/{chiave}")
def payload(fornitore: str, chiave: str,
            x_api_key: str | None = Header(default=None),
            key: str | None = Query(default=None)):
    """Payload convertito dell'ultimo run (per diagnosi: URL immagini, listini...)."""
    _check_key(x_api_key, key)
    if ".." in chiave or "/" in chiave:
        raise HTTPException(400, detail="chiave non valida")
    p = state.dir_fornitore(fornitore) / "work" / "out" / "json" / f"{chiave}.json"
    if not p.exists():
        raise HTTPException(404, detail="chiave non trovata nell'ultimo run")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/raw/{fornitore}/{ref}")
def raw(fornitore: str, ref: str,
        x_api_key: str | None = Header(default=None),
        key: str | None = Query(default=None)):
    """Record GREZZO del fornitore per una ref, come e' arrivato dalla sua API
    all'ultimo fetch.

    /payload mostra il risultato della conversione; per verificare la
    conversione serve anche il punto di partenza, altrimenti si confronta una
    trasformazione con se stessa. Vale per i fornitori il cui snapshot e' un
    file per ref (makito); dove il feed e' un unico file enorme la rotta lo
    dice invece di riversare decine di MB nel log."""
    _check_key(x_api_key, key)
    if ".." in ref or "/" in ref:
        raise HTTPException(400, detail="ref non valida")
    p = state.dir_fornitore(fornitore) / "work" / "raw" / "snapshot" / f"{ref}.json"
    if not p.exists():
        raise HTTPException(404, detail="ref non trovata nello snapshot dell'ultimo fetch")
    peso = p.stat().st_size
    if peso > 2_000_000:
        raise HTTPException(413, detail=f"snapshot non per-ref: {p.name} pesa {peso} byte")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/schema/{fornitore}")
def schema_fornitore(fornitore: str,
                     x_api_key: str | None = Header(default=None),
                     key: str | None = Query(default=None)):
    """Struttura attesa dei file depositati a mano (fogli + colonne) e
    candidati in attesa dopo un esito 'struttura_file_diversa'. La copia di
    riferimento del primo file elaborato resta sul Volume (schema/riferimento_*)."""
    _check_key(x_api_key, key)
    from .schema_guard import stato
    return stato(state.dir_fornitore(fornitore) / "schema")


@app.post("/schema/{fornitore}/accetta")
def schema_accetta(fornitore: str,
                   x_api_key: str | None = Header(default=None),
                   key: str | None = Query(default=None)):
    """Promuove il nuovo tracciato (candidato) a struttura attesa: da usare
    SOLO quando il cambio di formato del fornitore e' voluto e verificato."""
    _check_key(x_api_key, key)
    from .schema_guard import accetta
    return {"promossi": accetta(state.dir_fornitore(fornitore) / "schema")}


@app.post("/riarma/{fornitore}")
def riarma(fornitore: str, chiavi: list[str] = Body(embed=True),
           x_api_key: str | None = Header(default=None),
           key: str | None = Query(default=None)):
    """Toglie dallo stato gli hash delle chiavi indicate: il prossimo job delta
    le rimanda con payload completo e gestioneImmagini='sostituisci'. Serve per
    il ciclo di riparazione immagini (chiavi individuate su OnPage via MCP)."""
    _check_key(x_api_key, key)
    hashes = state.load_hashes(fornitore)
    riarmate = [k for k in chiavi if hashes.pop(k, None) is not None]
    state.save_hashes(fornitore, hashes)
    return {"chiavi_riarmate": len(riarmate), "sconosciute": len(chiavi) - len(riarmate)}


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



# ---------------------------------------------------- Makito: ordini --------
_ORD_CODICI = ("variant", "reference", "material", "matnr", "sku", "code")


@app.get("/makito/ordini")
def makito_ordini(giorni: int = Query(default=365, ge=1, le=365),
                  x_api_key: str | None = Header(default=None),
                  key: str | None = Query(default=None)):
    """Storico ordini/consegne/fatture dal portale Makito. SOLA LETTURA (solo GET).

    Serve a rispondere a una domanda che la specifica OpenAPI non risolve: la
    riga d'ordine (ItemDTO) ha sia "variant" sia "reference", entrambi senza
    descrizione. Sapere quale dei due porta il codice articolo - e se quel
    codice e' la ref o la web_reference - decide se possiamo cambiare il codice
    fornitore mostrato su OnPage. I documenti veri lo dicono; la specifica no.

    Ritorna solo i campi che somigliano a un codice, piu' i conteggi: niente
    prezzi, niente anagrafiche cliente."""
    _check_key(x_api_key, key)
    token = _makito_jwt()
    if not token:
        raise HTTPException(503, detail="not_configured")
    import datetime as _dt
    oggi = _dt.date.today()
    da = (oggi - _dt.timedelta(days=giorni)).isoformat()
    hdr = {"Authorization": f"Bearer {token}"}
    out: dict = {"periodo": {"da": da, "a": oggi.isoformat()}}
    for gruppo in ("sales-order", "deliveries", "billings"):
        url = f"https://apis.makito.es/orders/{gruppo}"
        try:
            r = rq.get(url, headers=hdr, params={"fromDate": da, "toDate": oggi.isoformat()},
                       timeout=120)
        except rq.RequestException as e:                      # noqa: BLE001
            out[gruppo] = {"errore": str(e)[:200]}
            continue
        if r.status_code != 200:
            out[gruppo] = {"http": r.status_code, "corpo": r.text[:300]}
            continue
        try:
            body = r.json()
        except ValueError:
            out[gruppo] = {"http": 200, "corpo_non_json": r.text[:300]}
            continue
        docs = body if isinstance(body, list) else \
            next((v for v in (body or {}).values() if isinstance(v, list)), [])
        righe = [i for d in docs if isinstance(d, dict) for i in (d.get("items") or [])]
        campioni, visti = [], set()
        for i in righe:
            cod = {k: i.get(k) for k in _ORD_CODICI if i.get(k) not in (None, "")}
            firma = json.dumps(cod, sort_keys=True)
            if firma not in visti:
                visti.add(firma)
                campioni.append(cod)
            if len(campioni) >= 25:
                break
        out[gruppo] = {
            "http": 200,
            "documenti": len(docs),
            "righe": len(righe),
            "campi_documento": sorted(docs[0]) if docs and isinstance(docs[0], dict) else [],
            "campi_riga": sorted(righe[0]) if righe and isinstance(righe[0], dict) else [],
            "codici_riga": campioni,
        }
    return out


@app.get("/makito/codici")
def makito_codici(ref: str = Query(default=""),
                  campione: int = Query(default=20, ge=0, le=200),
                  x_api_key: str | None = Header(default=None),
                  key: str | None = Query(default=None)):
    """Censimento dei due codici articolo Makito sull'intero snapshot.

    Il record del fornitore porta sia "ref" (11068) sia "web_reference" (1068):
    i codici variante sono costruiti sulla seconda, il matnr con cui si
    agganciano le giacenze sulla prima. Prima di cambiare quale delle due
    finisce nel codice fornitore su OnPage serve sapere quanti articoli
    cambierebbero e se due ref condividono la stessa web_reference.

    In "ref" si possono elencare (separate da virgola) le ref da mostrare
    comunque, per controllare in anticipo articoli gia' finiti nelle offerte."""
    _check_key(x_api_key, key)
    d = state.dir_fornitore("makito") / "work" / "raw" / "snapshot"
    if not d.is_dir():
        raise HTTPException(404, detail="nessuno snapshot makito sul volume")
    volute = {r.strip() for r in ref.split(",") if r.strip()}
    conte = {"file": 0, "uguali": 0, "diversi": 0, "mancante": 0, "illeggibili": 0}
    per_web: dict[str, list[str]] = {}
    esempi_diversi: list[dict] = []
    richieste: list[dict] = []
    for f in sorted(d.glob("*.json")):
        conte["file"] += 1
        try:
            e = (json.loads(f.read_text(encoding="utf-8")).get("catalogo") or [{}])[0]
        except Exception:                                     # noqa: BLE001
            conte["illeggibili"] += 1
            continue
        r_ref = str(e.get("ref") or f.stem)
        web = e.get("web_reference")
        web = str(web).strip() if web not in (None, "") else ""
        if not web:
            conte["mancante"] += 1
        elif web == r_ref:
            conte["uguali"] += 1
        else:
            conte["diversi"] += 1
            if len(esempi_diversi) < campione:
                esempi_diversi.append({"ref": r_ref, "web_reference": web})
        if web:
            per_web.setdefault(web, []).append(r_ref)
        if r_ref in volute:
            richieste.append({"ref": r_ref, "web_reference": web,
                              "cambia": bool(web) and web != r_ref})
    collisioni = {w: rs for w, rs in per_web.items() if len(rs) > 1}
    return {
        "conteggi": conte,
        "collisioni": {"quante": len(collisioni),
                       "esempi": dict(sorted(collisioni.items())[:20])},
        "esempi_diversi": esempi_diversi,
        "ref_richieste": richieste,
        "ref_non_trovate": sorted(volute - {r["ref"] for r in richieste}),
    }

# ------------------------------------------------------------- scheduler ----
@app.on_event("startup")
def startup():
    avvia_scheduler(_esegui, _run_in_corso)
