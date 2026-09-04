# onpage-import-hub

Servizio Railway che gestisce gli **import articoli verso il PIM OnPage** (importer Custom
`PantaSupplierFCustom`) per tutti i fornitori: oggi **Makito (02)**, poi Torregrossa (79) e altri.
Nato per sostituire l'esecuzione su GitHub Actions (limite 6 h/job, minuti mensili) dopo il primo
caricamento completo fatto dal repo `F02`.

## Come funziona

- **Delta sync**: a ogni run si riscaricano i dump Makito necessari, si riconvertono TUTTI gli
  articoli (~3 s) e si invia all'importer **solo cio' che e' cambiato** rispetto all'ultimo invio
  riuscito (hash per articolo in `/data/makito/state.json`). L'importer costa ~7 s/articolo:
  e' il delta che rende sostenibili le cadenze.
- **Invio parallelo**: `SEND_WORKERS` worker (default 3) con retry sugli errori transitori.
- **Proxy immagini** `GET /img/makito/<path>`: gli asset di apis.makito.es richiedono il JWT,
  l'importer OnPage scarica senza autenticazione -> i payload usano gli URL del hub, che gira
  la richiesta a Makito col token. (Sostituisce la "staffetta" F02immagini.)
- **Job** (`POST /run/makito/{job}` con header `x-api-key`):
  `stock` (dump giacenze, resto dalla cache) ~ ogni ora - `prezzi` (listino+giacenze) ~ giornaliero -
  `prodotti` (tutto) ~ settimanale - `full` (riallineamento: invia tutto) - `bootstrap`
  (primo avvio: registra gli hash senza inviare nulla).
- **Scheduler** interno (cron UTC nelle env `CRON_MAKITO_*`; vuoto = disattivato).
- Stato su **Railway Volume** montato su `/data` (hash, send_log.jsonl, runs.jsonl, cache dump).

## Setup Railway (una tantum)

1. Nel progetto Railway di orderEntry: **New Service -> Deploy from GitHub repo** (questo repo).
2. Aggiungere un **Volume** montato su `/data`.
3. Variabili: `MAKITO_CLIENT_ID`, `MAKITO_CLIENT_SECRET`, `HUB_API_KEY` (stringa lunga a piacere),
   `PUBLIC_BASE_URL` (l'URL pubblico del servizio), opzionali `SEND_WORKERS`, `CRON_MAKITO_*`,
   `IMPORTER_TOKEN`.
4. Primo avvio: `POST /run/makito/bootstrap` (registra lo stato del full gia' caricato da Actions,
   nessun reinvio), poi `POST /run/makito/stock` di collaudo.

## Struttura

```
app/        main.py (API+proxy), scheduler.py, runner.py, sender.py, state.py, config.py
suppliers/
  makito/   moduli portati dal repo F02: fetch_makito.py (con --refresh/--dumps-cache),
            convert.py, validate.py, report.py, config/mapping_02.yaml
```

Le regole di conversione (colori, listini "acquisto rivenditori", giacenze attuale+arrivi,
cicli MAI inviati perche' gestiti a mano su OnPage) restano in `suppliers/makito/config/mapping_02.yaml`
e sono documentate nel repo F02, che rimane come storico e fallback manuale via GitHub Actions.
