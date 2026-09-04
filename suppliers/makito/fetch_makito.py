#!/usr/bin/env python3
"""
fetch_makito.py - scarica dall'API Makito uno snapshot locale (un JSON per ref).

Primo stadio della pipeline (CLAUDE.md par.5): il fetch e' separato dalla conversione,
cosi' la sync e' riproducibile e i cambi del feed restano diffabili nel repo.

API supportate (CLAUDE.md par.3):
- portale nuovo https://apis.makito.es - OAuth client credentials
  (secrets MAKITO_CLIENT_ID / MAKITO_CLIENT_SECRET). La specifica OpenAPI viene
  scaricata dallo Swagger e salvata in DOCS/openapi/<servizio>.json (fonte di verita'
  per i nomi campo); gli endpoint GET da chiamare vengono ricavati dalla specifica.
- API storica https://data.makito.es - login email/password
  (secrets MAKITO_EMAIL / MAKITO_PASSWORD), endpoint dal manuale
  DOCS/documentacionapimakito.md. Usata come fallback se mancano le credenziali
  OAuth, o esplicitamente con --api storica.

Output:
  data/snapshot/<ref>.json     dati grezzi di tutti gli endpoint per la ref
  data/snapshot/_global.json   liste globali (colori, categorie...) lingua 3
  data/fetch_run.json          log della run (url, status, tempi) - scritto anche su errore
  data/stock_confronto.md      confronto stock vs stock_availability per le ref scaricate
  DOCS/openapi/<servizio>.json specifiche OpenAPI dei microservizi (quando trovate)

Uso:
  python src/fetch_makito.py --sample 3          # prime 3 ref del catalogo
  python src/fetch_makito.py --refs 4762 5580    # ref esplicite
  python src/fetch_makito.py --all --out out/full   # tutte le ref (sync completa)
  python src/fetch_makito.py --discover-only     # solo specifica OpenAPI, nessun dato
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

NEW_BASE = "https://apis.makito.es"
OLD_BASE = "https://data.makito.es"
LANG_IT = "3"          # lingua italiana (manuale Makito)
SLEEP = 0.3            # pausa tra chiamate (rate limiting prudente)
TIMEOUT = 60

class RunLog:
    """Log della run, riscritto su disco a ogni evento: sopravvive ai crash."""

    def __init__(self, path: Path):
        self.path = path
        self.records: list[dict] = []
        self.meta = {"started_at": datetime.now(timezone.utc).isoformat(), "api": None,
                     "spec_url": None, "token_url": None, "errors": []}

    def add(self, **rec):
        self.records.append(rec)
        self.save()

    def error(self, msg: str):
        print(f"ERRORE: {msg}", file=sys.stderr)
        self.meta["errors"].append(msg)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"meta": self.meta, "chiamate": self.records},
                                        ensure_ascii=False, indent=1), encoding="utf-8")


def http_get(session: requests.Session, url: str, log: RunLog, *, params=None, headers=None):
    """GET con log; ritorna (status, body_json_o_testo)."""
    t0 = time.time()
    status, body = None, None
    try:
        r = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
        status = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = {"_non_json": r.text[:2000]}
    except requests.RequestException as e:
        body = {"_network_error": str(e)}
    log.add(url=url, params=params, status=status, elapsed_s=round(time.time() - t0, 2),
            ok=isinstance(status, int) and status < 400)
    time.sleep(SLEEP)
    return status, body


# ---------------------------------------------------------------------------
# Portale nuovo apis.makito.es: discovery della specifica OpenAPI + OAuth
# ---------------------------------------------------------------------------

SPEC_CANDIDATES = [
    "/docs/v3/api-docs", "/docs/v3/api-docs/swagger-config", "/v3/api-docs",   # springdoc (Spring Boot)
    "/docs/openapi.json", "/docs/swagger.json", "/docs/api-docs.json", "/docs/v1/openapi.json",
    "/openapi.json", "/swagger.json", "/swagger/v1/swagger.json", "/api-docs",
    "/api/openapi.json", "/api/swagger.json", "/docs/index.json",
]


def _save_docs_page(out_dir: Path, url: str, body):
    out_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("//", 1)[-1])[:120]
    text = body.get("_non_json") if isinstance(body, dict) else None
    (out_dir / name).write_text(text if text is not None else json.dumps(body, ensure_ascii=False, indent=1),
                                encoding="utf-8")


def _spec_url_candidates(text: str, base_url: str) -> list[str]:
    """Estrae dai sorgenti dello Swagger UI ogni possibile URL di specifica."""
    out = []
    for m in re.findall(r'["\']([^"\'\s]*(?:openapi|swagger|api-docs)[^"\'\s]*?)["\']', text) + \
             re.findall(r'["\']([^"\'\s]+\.(?:json|ya?ml))["\']', text) + \
             re.findall(r'\burl\s*[:=]\s*["\']([^"\']+)["\']', text):
        if m.endswith((".js", ".css", ".png", ".html")):
            continue
        if m.startswith("http"):
            out.append(m)
        elif m.startswith("/"):
            out.append(NEW_BASE + m)
        else:
            out.append(base_url.rsplit("/", 1)[0] + "/" + m)
    return out


def _spec_ok(url: str, body) -> bool:
    if not (isinstance(body, dict) and ("openapi" in body or "swagger" in body) and "paths" in body):
        return False
    # lo swagger-initializer di default punta al "petstore" dimostrativo: non e' l'API Makito
    ident = (url + json.dumps(body.get("info", {}), ensure_ascii=False)).lower()
    return "petstore" not in ident


def discover_specs(session: requests.Session, log: RunLog, out: Path) -> dict[str, dict]:
    """Scarica le specifiche OpenAPI dei microservizi del portale nuovo.

    Il portale e' springdoc: /docs/v3/api-docs/swagger-config elenca i servizi
    (access, catalog, orders, price-list, print-config, print-price-list, stock).
    Ogni specifica viene salvata in DOCS/openapi/<servizio>.json; ritorna
    {servizio: {"base": url_server, "spec": spec}}."""
    pages_dir = out / "docs_pages"
    services: dict[str, dict] = {}

    status, cfg = http_get(session, NEW_BASE + "/docs/v3/api-docs/swagger-config", log)
    urls = []
    if status == 200 and isinstance(cfg, dict) and cfg.get("urls"):
        _save_docs_page(pages_dir, NEW_BASE + "/docs/v3/api-docs/swagger-config", cfg)
        urls = [u["url"] for u in cfg["urls"] if isinstance(u, dict) and u.get("url")]
    else:
        # fallback: cerca l'URL della specifica dentro lo Swagger UI
        s2, body = http_get(session, NEW_BASE + "/docs/index.html", log)
        html = body.get("_non_json", "") if isinstance(body, dict) else ""
        _save_docs_page(pages_dir, NEW_BASE + "/docs/index.html", body)
        urls = [u for u in _spec_url_candidates(html, NEW_BASE + "/docs/index.html")]
        urls += [NEW_BASE + p for p in SPEC_CANDIDATES]

    spec_dir = Path("DOCS/openapi")
    for url in dict.fromkeys(u if u.startswith("http") else NEW_BASE + u for u in urls):
        status, spec = http_get(session, url, log)
        if status != 200 or not _spec_ok(url, spec):
            continue
        # nome servizio dal primo segmento del path (…/catalog/v3/api-docs/client -> catalog)
        m = re.match(r"^https?://[^/]+/([^/]+)/v3/api-docs", url)
        name = m.group(1) if m else re.sub(r"[^a-z0-9]+", "-", (spec.get("info", {}).get("title") or "api").lower()).strip("-")
        servers = [s.get("url", "").rstrip("/") for s in (spec.get("servers") or []) if s.get("url", "").startswith("http")]
        base = servers[0] if servers else url.split("/v3/api-docs")[0]
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / f"{name}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        services[name] = {"base": base, "spec": spec}
        print(f"Specifica {name}: {url} ({len(spec.get('paths', {}))} path, base {base})")
    if services:
        log.meta["spec_url"] = NEW_BASE + "/docs/v3/api-docs/swagger-config"
        log.meta["servizi"] = sorted(services)
    else:
        log.error(f"nessuna specifica OpenAPI trovata su {NEW_BASE}")
    return services


TOKEN_KEYS = ("token", "access_token", "accesstoken", "jwt", "bearer", "id_token", "accessjwt")


def extract_token(obj) -> str | None:
    """Cerca ricorsivamente un token nella risposta (chiavi tipo token/accessToken/jwt)."""
    if isinstance(obj, str):
        s = obj.strip().strip('"')
        return s if (s.count(".") == 2 or len(s) >= 20) else None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in TOKEN_KEYS and isinstance(v, str) and v:
                return v
        for v in obj.values():
            t = extract_token(v)
            if t:
                return t
    return None


def oauth_token(session: requests.Session, services: dict[str, dict], log: RunLog) -> str | None:
    """Token JWT dal portale nuovo: POST {base}/auth/login con body {clientId, clientSecret},
    risposta {token} da usare come Bearer su tutti i microservizi. Il login vive sul
    servizio "access" (Access API); gli altri servizi restano come fallback."""
    cid, secret = os.environ.get("MAKITO_CLIENT_ID"), os.environ.get("MAKITO_CLIENT_SECRET")
    if not (cid and secret):
        return None
    ordered = sorted(services, key=lambda n: (n != "access",))
    bases = [services[n]["base"] for n in ordered] + [NEW_BASE + "/access", NEW_BASE]
    creds = {"clientId": cid, "clientSecret": secret}
    for base in dict.fromkeys(bases):
        url = base + "/auth/login"
        t0 = time.time()
        try:
            r = session.post(url, json=creds, timeout=TIMEOUT)
            ok = r.status_code == 200
            log.add(url=url, method="POST(token)", status=r.status_code,
                    elapsed_s=round(time.time() - t0, 2), ok=ok)
            if ok:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                tok = extract_token(body)
                if tok:
                    log.meta["token_url"] = url
                    print(f"Autenticazione OK su {url}")
                    return tok
        except requests.RequestException as e:
            log.add(url=url, method="POST(token)", status=None, ok=False, err=str(e)[:200])
        time.sleep(SLEEP)
    log.error("autenticazione fallita su tutti gli endpoint di login provati")
    return None


def find_dicts_with_key(obj, key: str, out=None):
    if out is None:
        out = []
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj)
        for v in obj.values():
            find_dicts_with_key(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            find_dicts_with_key(v, key, out)
    return out


# ---------------------------------------------------------------------------
# Portale nuovo: API "snapshot" (verificato dalle specifiche il 03/09/2026)
#   catalog:    GET /files?format=JSON&lang=it&catalog=GENERAL_CATALOG  (dump catalogo)
#               GET /colors, /sizes (liste globali multilingua), /assets/... (immagini)
#   stock:      GET /files?format=JSON[&plant=1000&storageLocation=1000] (dump giacenze)
#               GET /stocks/{material} (interrogazione puntuale per matnr)
#   price-list: GET /files?format=JSON (listino dell'utente autenticato)
# orders e print-* sono fuori perimetro (decisione Daniele 03/09/2026).
# ---------------------------------------------------------------------------

REF_KEYS = ("ref", "reference", "prodReference", "productReference")
MATERIAL_KEYS = ("matnr", "material", "materialNumber")


def http_get_full(session, url, log, *, params=None):
    """Come http_get ma senza troncare il testo non-JSON (serve per i dump /files)."""
    t0 = time.time()
    status, body, text = None, None, None
    try:
        r = session.get(url, params=params, timeout=300)
        status = r.status_code
        try:
            body = r.json()
        except ValueError:
            text = r.text
    except requests.RequestException as e:
        body = {"_network_error": str(e)}
    log.add(url=url, params=params, status=status, elapsed_s=round(time.time() - t0, 2),
            ok=isinstance(status, int) and status < 400,
            bytes=len(text) if text is not None else None)
    time.sleep(SLEEP)
    return status, (body if body is not None else {"_non_json": text[:2000], "_bytes": len(text)})


def summarize(body, max_chars=600):
    """Riepilogo compatto di un dump per lo snapshot _global (il dump intero non va in git)."""
    if isinstance(body, list):
        keys = sorted(body[0].keys()) if body and isinstance(body[0], dict) else None
        return {"tipo": "lista", "elementi": len(body), "chiavi_primo": keys,
                "primo": json.loads(json.dumps(body[0], ensure_ascii=False)[:max_chars] + '"' * 0) if False else None,
                "esempio": json.dumps(body[0], ensure_ascii=False)[:max_chars] if body else None}
    if isinstance(body, dict):
        return {"tipo": "oggetto", "chiavi": sorted(body.keys())[:40],
                "esempio": json.dumps(body, ensure_ascii=False)[:max_chars]}
    return {"tipo": type(body).__name__, "esempio": str(body)[:max_chars]}


def _dict_ref(d: dict):
    for k in REF_KEYS:
        v = d.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
    return None


def _dict_materials(obj) -> list[str]:
    """Solo codici materiale SAP (tutti numerici, ref+colore+taglia): il campo
    "material" delle voci di catalogo e' la composizione ("Alluminio") e va ignorato."""
    out = []
    for k in MATERIAL_KEYS:
        for d in find_dicts_with_key(obj, k):
            v = d.get(k)
            if isinstance(v, (str, int)) and re.fullmatch(r"\d{6,}", str(v)):
                out.append(str(v))
    return list(dict.fromkeys(out))


def _dedup_rows(rows: list) -> list:
    """Il dump stock ripete le righe datate con id interni diversi: dedup per valore."""
    out, visti = [], set()
    for d in rows:
        fp = json.dumps(d, ensure_ascii=False, sort_keys=True)
        if fp not in visti:
            visti.add(fp)
            out.append(d)
    return out


def fetch_new_api(session, services: dict[str, dict], refs, sample, log,
                  refresh: str = "tutto", cache_dir=None) -> dict[str, dict]:
    """Fetch dal portale nuovo: dump /files dei tre servizi in perimetro.

    refresh: "tutto" scarica ogni dump; "stock" solo il dump giacenze; "prezzi"
    giacenze+listino. I dump non richiesti vengono letti dalla cache su disco
    (cache_dir), scritta a ogni download: e' cio' che rende leggere le sync
    orarie/giornaliere del hub."""
    cache_dir = Path(cache_dir) if cache_dir else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    def cache_path(nome):
        return cache_dir / (nome.replace("@", "_").replace("/", "_") + ".json") if cache_dir else None

    def da_cache(nome):
        cp = cache_path(nome)
        if cp and cp.exists():
            return json.loads(cp.read_text(encoding="utf-8"))
        return None

    def in_cache(nome, body):
        cp = cache_path(nome)
        if cp:
            cp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    cat = services.get("catalog")
    global_data, snapshots = {}, {}

    # 1. liste globali multilingua (colori e taglie)
    if cat:
        for path in ("/colors", "/sizes"):
            nome = "liste" + path.replace("/", "_")
            body = da_cache(nome) if refresh != "tutto" else None
            if body is None:
                status, body = http_get(session, cat["base"] + path, log)
                if status == 200:
                    in_cache(nome, body)
            else:
                status = "cache"
            global_data[f"catalog:{path}"] = {"status": status, "body": body}

    # 2. dump completi dei servizi snapshot (catalogo: GENERAL + THEINFINITE, decisione 03/09/2026)
    dumps: dict[str, object] = {}
    da_scaricare = {"tutto": {"catalog", "catalog@THEINFINITE", "stock", "price-list"},
                    "prezzi": {"stock", "price-list"},
                    "stock": {"stock"}}[refresh]
    for name, params in (("catalog", {"format": "JSON", "lang": "it", "catalog": "GENERAL_CATALOG"}),
                         ("catalog@THEINFINITE", {"format": "JSON", "lang": "it", "catalog": "THEINFINITE_CATALOG"}),
                         ("stock", {"format": "JSON"}),
                         ("price-list", {"format": "JSON"})):
        svc = services.get(name.split("@")[0])
        if not svc:
            continue
        if name not in da_scaricare:
            body = da_cache("dump_" + name)
            if body is not None:
                dumps[name] = body
                global_data[f"{name}:/files"] = {"status": "cache", "riepilogo": summarize(body)}
                continue
            # cache assente: si scarica comunque
        status, body = http_get_full(session, svc["base"] + "/files", log, params=params)
        if status == 200 and not (isinstance(body, dict) and ("_non_json" in body or "_network_error" in body)):
            dumps[name] = body
            in_cache("dump_" + name, body)
        global_data[f"{name}:/files"] = {"status": status, "riepilogo": summarize(body)}

    if "catalog" not in dumps:
        log.error("dump catalogo non disponibile o non-JSON: vedi _global per il riepilogo")
        return {"_global": global_data}

    # 3. prodotti (da entrambi i cataloghi, con etichetta di provenienza) e ref campione
    prods_by_ref: dict[str, list] = {}
    for cat_name, tag in (("catalog", "GENERAL_CATALOG"), ("catalog@THEINFINITE", "THEINFINITE_CATALOG")):
        for d in (dumps.get(cat_name) or {}).get("products") or []:
            r = _dict_ref(d) if isinstance(d, dict) else None
            if r:
                d.setdefault("_catalogo", tag)
                prods_by_ref.setdefault(r, []).append(d)
    log.meta["prodotti_nel_dump"] = len(prods_by_ref)
    print(f"Prodotti distinti nel dump catalogo: {len(prods_by_ref)}")
    if not refs:
        refs = list(prods_by_ref) if sample < 0 else list(prods_by_ref)[:sample]
    if not refs:
        log.error("nessuna ref trovata nel dump catalogo")
        return {"_global": global_data}
    print(f"Ref campione: {refs}")

    # indici per estrazione veloce (necessari in modalita' --all su migliaia di ref):
    # righe stock raggruppate per ref (= matnr senza i 6 char colore+taglia),
    # voci listino per material (= ref)
    stock_by_ref: dict[str, list] = {}
    for r in (dumps.get("stock") or {}).get("stocks") or []:
        mat = str(r.get("material") or "")
        if len(mat) > 6:
            stock_by_ref.setdefault(mat[:-6], []).append(r)
    price_by_ref: dict[str, list] = {}
    for r in (dumps.get("price-list") or {}).get("priceList") or []:
        price_by_ref.setdefault(str(r.get("material")), []).append(r)

    # 4. per ogni ref: voce di catalogo + righe stock/listino dei dump; lo stock puntuale
    #    live di verifica solo quando le ref sono poche (campione)
    stk = services.get("stock") if len(refs) <= 10 else None
    for ref in refs:
        ref = str(ref)
        snap = {"catalogo": prods_by_ref.get(ref, []),
                "stock_dump": _dedup_rows(stock_by_ref.get(ref, [])),
                "listino_dump": price_by_ref.get(ref, [])}
        if stk:
            materials = {str(r.get("material")) for r in snap["stock_dump"]}
            live = {}
            for mat in sorted(materials)[:40]:
                status, body = http_get(session, stk["base"] + f"/stocks/{mat}", log)
                live[mat] = {"status": status, "body": body}
            snap["stock_puntuale"] = live
        snapshots[ref] = snap
    snapshots["_global"] = global_data
    return snapshots


# ---------------------------------------------------------------------------
# API storica data.makito.es (manuale DOCS/documentacionapimakito.md)
# ---------------------------------------------------------------------------

def fetch_old_api(session, refs, sample, log) -> dict[str, dict] | None:
    email, password = os.environ.get("MAKITO_EMAIL"), os.environ.get("MAKITO_PASSWORD")
    if not (email and password):
        return None
    try:
        r = session.post(OLD_BASE + "/api/login", json={"email": email, "password": password}, timeout=TIMEOUT)
        token = r.json().get("token")
    except (requests.RequestException, ValueError) as e:
        log.error(f"login API storica fallito: {e}")
        return None
    if not token:
        log.error(f"login API storica rifiutato (status {r.status_code})")
        return None
    print("Login API storica OK")
    session.headers["Authorization"] = f"Bearer {token}"

    global_data = {}
    for path in (f"/api/colors/{LANG_IT}", f"/api/categories/{LANG_IT}"):
        status, body = http_get(session, OLD_BASE + path, log)
        global_data[path] = {"status": status, "body": body}

    if not refs:
        # il manuale documenta /api/products/{ref} con page/per_page: proviamo il listato paginato
        for attempt in ("/api/products", "/api/products/"):
            status, body = http_get(session, OLD_BASE + attempt, log, params={"page": 1, "per_page": max(sample * 3, 10)})
            if status == 200:
                global_data[attempt] = {"status": status, "body": body}
                prods = find_dicts_with_key(body, "ref")
                refs = list(dict.fromkeys(str(p["ref"]) for p in prods))[:sample]
                break
        if not refs:
            log.error("nessuna ref trovata dal listato prodotti dell'API storica")
            return {"_global": global_data}

    snapshots = {"_global": global_data}
    per_ref = ["/api/products/{ref}", "/api/variants/{ref}", "/api/descriptions/{ref}",
               "/api/descriptionslang/" + LANG_IT + "/{ref}", "/api/descriptionext/{ref}",
               "/api/descriptionextlang/" + LANG_IT + "/{ref}", "/api/images/{ref}",
               "/api/imgResources/{ref}", "/api/productPrice/{ref}", "/api/observations/{ref}/" + LANG_IT]
    for ref in refs:
        snap = {}
        for tpl in per_ref:
            path = tpl.replace("{ref}", str(ref))
            status, body = http_get(session, OLD_BASE + path, log)
            snap[tpl] = {"status": status, "body": body}
        matnrs = [str(d["matnr"]) for d in find_dicts_with_key(snap.get("/api/variants/{ref}"), "matnr")]
        for matnr in dict.fromkeys(matnrs):
            for tpl in ("/api/stockVariants/{matnr}", "/api/stock/{ref}/{matnr}"):
                path = tpl.replace("{ref}", str(ref)).replace("{matnr}", matnr)
                status, body = http_get(session, OLD_BASE + path, log)
                snap.setdefault(tpl, {})[matnr] = {"status": status, "body": body}
        snapshots[ref] = snap
    return snapshots


# ---------------------------------------------------------------------------
# Confronto stock vs stock_availability (PUNTO APERTO CLAUDE.md par.4)
# ---------------------------------------------------------------------------

def stock_report(snapshots: dict, out_path: Path):
    """Confronto leggibile delle giacenze per le ref scaricate.

    Nuova API: {material, quantity[, availableDate]} - una riga senza data =
    giacenza attuale, righe con availableDate = arrivi futuri (mappa 1:1 su
    giacenze[] Panta: dataArrivo null / data ISO).
    API storica: {matnr, stock, stock_availability, date} - semantica da
    confermare con Daniele (PUNTO APERTO CLAUDE.md par.4)."""
    lines = ["# Giacenze Makito - dati grezzi per le ref campione", "",
             f"Generato: {datetime.now(timezone.utc).isoformat()}", ""]
    for ref, snap in sorted(snapshots.items()):
        if ref == "_global":
            continue
        lines.append(f"## ref {ref}")
        lines.append("")
        rows = snap.get("stock_dump") if isinstance(snap, dict) else None
        if rows is not None:                       # nuova API
            lines.append("| material | giacenza attuale | arrivo futuro (availableDate) |")
            lines.append("|---|---|---|")
            per_mat: dict[str, dict] = {}
            for d in rows:
                m = per_mat.setdefault(str(d.get("material")), {"now": None, "next": []})
                if d.get("availableDate"):
                    m["next"].append((d["availableDate"][:10], d.get("quantity")))
                else:
                    m["now"] = d.get("quantity")
            for mat, m in sorted(per_mat.items()):
                arrivi = "; ".join(f"{q} pz il {dt}" for dt, q in sorted(m["next"])) or "-"
                lines.append(f"| {mat} | {m['now']} | {arrivi} |")
            live = snap.get("stock_puntuale")
            diff = [mat for mat, v in (live or {}).items()
                    if isinstance(v.get("body"), list) and
                    sorted((r.get("quantity"), (r.get("availableDate") or "")[:10]) for r in v["body"]) !=
                    sorted([(q, dt) for dt, q in per_mat.get(mat, {}).get("next", [])] +
                           ([(per_mat[mat]["now"], "")] if per_mat.get(mat, {}).get("now") is not None else []))]
            if live is not None:
                lines.append("")
                lines.append(f"Verifica /stocks/{{material}} puntuale vs dump: "
                             f"{'OK, coincidono' if not diff else 'DIFFERENZE su ' + ', '.join(diff)}")
        else:                                      # API storica
            lines.append("| matnr | stock | stock_availability | date |")
            lines.append("|---|---|---|---|")
            for d in find_dicts_with_key(snap, "stock_availability"):
                lines.append(f"| {d.get('matnr')} | {d.get('stock')} | {d.get('stock_availability')} | {d.get('date')} |")
        lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    n_ref = sum(1 for r in snapshots if r != "_global")
    if n_ref <= 10:
        print("\n".join(lines))
    else:
        print(f"stock_confronto.md scritto per {n_ref} ref (stampa omessa)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refs", nargs="*", help="ref esplicite da scaricare")
    ap.add_argument("--sample", type=int, default=3, help="quante ref di prova se --refs manca")
    ap.add_argument("--all", action="store_true", help="snapshot di TUTTE le ref dei dump (sync completa)")
    ap.add_argument("--refresh", choices=["tutto", "prezzi", "stock"], default="tutto",
                    help="quali dump riscaricare; gli altri arrivano dalla cache (--dumps-cache)")
    ap.add_argument("--dumps-cache", type=Path, default=None,
                    help="directory cache dei dump per gli aggiornamenti mirati")
    ap.add_argument("--discover-only", action="store_true", help="scarica solo la specifica OpenAPI")
    ap.add_argument("--api", choices=["nuova", "storica", "auto"], default="auto")
    ap.add_argument("--out", type=Path, default=Path("data"))
    args = ap.parse_args(argv)

    log = RunLog(args.out / "fetch_run.json")
    session = requests.Session()
    session.headers["accept"] = "application/json"

    have_oauth = bool(os.environ.get("MAKITO_CLIENT_ID") and os.environ.get("MAKITO_CLIENT_SECRET"))
    have_login = bool(os.environ.get("MAKITO_EMAIL") and os.environ.get("MAKITO_PASSWORD"))
    print(f"Credenziali: OAuth={'si' if have_oauth else 'NO'}  email/password={'si' if have_login else 'NO'}")

    services = {}
    if args.api in ("nuova", "auto"):
        services = discover_specs(session, log, args.out)

    if args.discover_only:
        log.meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        log.save()
        return 0 if services else 1

    snapshots = None
    if args.api in ("nuova", "auto") and have_oauth and services:
        token = oauth_token(session, services, log)
        if token:
            session.headers["Authorization"] = f"Bearer {token}"
            log.meta["api"] = "nuova"
            snapshots = fetch_new_api(session, services, args.refs, -1 if getattr(args, "all") else args.sample, log,
                                      refresh=args.refresh, cache_dir=args.dumps_cache)
    if snapshots is None and args.api in ("storica", "auto"):
        s2 = requests.Session()
        s2.headers["accept"] = "application/json"
        old = fetch_old_api(s2, args.refs, args.sample, log)
        if old is not None:
            log.meta["api"] = "storica"
            snapshots = old

    if snapshots is None:
        log.error("nessuna API utilizzabile: controlla i secrets MAKITO_* e la connettivita'")
        log.meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        log.save()
        return 1

    snap_dir = args.out / "snapshot"
    snap_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for ref, snap in snapshots.items():
        name = "_global" if ref == "_global" else re.sub(r"[^A-Za-z0-9._-]", "_", str(ref))
        (snap_dir / f"{name}.json").write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        n += ref != "_global"
    print(f"\nSnapshot salvati: {n} ref (+ _global) in {snap_dir}/ via API {log.meta['api']}")

    stock_report(snapshots, args.out / "stock_confronto.md")
    log.meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    log.save()
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
