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

## PF Concept: perche' "prodotti" rimandava ~1.450 articoli a notte (12/09/2026)

Sono le **giacenze**, e il motivo per cui non sembravano loro e' un incastro di
orari, non un campo.

Il feed stock si rigenera **due volte al giorno, alle ~23:18 e alle ~11:04 UTC**
(misurato con `/feed-diff`: la copia in cache delle 11:00 portava
`creationDateTime` 01:18 ora fornitore, quella scaricata alle 12:03 portava
13:04). I cron erano alle 05:00 e alle 11:00, cioe' **prima** della generazione
di mezzogiorno e **dopo** che il job `prodotti` delle 02:20 aveva gia' consumato
quella della notte. Risultato:

- `stock` alle 05:00 e alle 11:00 rileggeva la stessa generazione gia' registrata
  da `prodotti` -> trovava 2 articoli, e sembrava che le giacenze non si
  muovessero;
- la generazione delle 11:04 **non la leggeva nessuno**: le giacenze del
  pomeriggio non arrivavano mai su OnPage;
- tutto il movimento della giornata cadeva addosso a `prodotti`, che per quello
  ci metteva 2h46m.

Misura con `prodotti?prova=1` alle 12:09, sette ore dopo la conversione delle
02:20: **470 articoli su 2.830 da rimandare, di cui 395 per sole giacenze**, 3
per i listini (il feed prezzi si rigenera il sabato alle 05:15) e 73 con lo
stato anteriore alle impronte per campo. Su 24 ore, ~1.450 torna.

Cura: spostare i cron dello stock **dopo** le generazioni, `CRON_PF_STOCK` =
`"30 11,23 * * *"` (variabile su Railway). Cosi' ogni generazione viene letta
dal job che le compete, `prodotti` alle 02:20 trova le giacenze gia' registrate,
e le giacenze del pomeriggio smettono di restare fuori.

## Makito: i due codici articolo (`ref` e `web_reference`, verificato il 12/09/2026)

Il record Makito porta **due** codici articolo, e non sono intercambiabili:

| | esempio | come si ricava |
|---|---|---|
| `ref` | `11068` | il codice pieno |
| `web_reference` | `1068` | per le ref `1xxxx` e' la ref senza la cifra iniziale; per le `2xxxx` coincide |

Sull'intero snapshot (4.609 articoli): **2.284 diverse, 2.323 uguali, 2 senza web_reference,
nessuna collisione** (azione `codici` di `hub-ops`).

I codici variante che Makito stampa (`variant_reference`, es. `1068SCS/T`) sono costruiti sulla
**web_reference**: per questo su OnPage l'articolo 11068 mostra varianti che sembrano avere una
cifra in meno. Non e' un difetto della conversione.

**Per ordinare serve la `ref`.** Letti dalla Orders API gli ordini, le consegne e le fatture veri
dell'ultimo anno (57 ordini / 434 righe, 53 consegne, 51 fatture, azione `ordini`): ogni riga
identifica l'articolo con un solo campo, `material`, che vale o la ref piena (`11934`, `14108`) o
il matnr SAP `ref+colore(3)+taglia(3)` (`14108005000`, `21953002107`). Sei degli articoli ordinati
hanno web_reference diversa (11934, 14774, 14108, 19885, 13250, 16336) e nei documenti compare
**sempre la ref**: la web_reference non appare mai.

Quindi:
- `codiceArticoloFornitore` = `ref` va lasciato dov'e': e' il codice con cui si compra. Volendo
  rendere cercabile anche la web_reference, va **aggiunta** in un campo suo, non sostituita.
- il matnr (`_matnr` in `convert.py`) e' la chiave delle giacenze **ed e' anche il codice di riga
  degli ordini**; oggi non arriva a OnPage. Serve saperlo se un domani gli ordini a Makito
  partiranno da orderEntry.
- la specifica OpenAPI (`orders`) descrive la riga con `variant` e `reference` senza descrizioni:
  nelle risposte vere i campi sono `material` e `quantity`.

## Struttura

```
app/        main.py (API+proxy), scheduler.py, runner.py (registry fornitori),
            sender.py, state.py, config.py
suppliers/
  makito/   moduli portati dal repo F02: fetch_makito.py (con --refresh/--dumps-cache),
            convert.py, validate.py, report.py, config/mapping_02.yaml
  nwg/      NEW WAVE (A1): fetch_nwg.py (feed unico dall'URL in NWG_FEED_URL),
            convert.py, validate.py, config/mapping_a1.yaml. Immagini pubbliche
            (niente proxy); giacenze assenti nel feed (in attesa delle API stock
            NWG); moltiplicatoreVendita null e solo listino "acquisto rivenditori"
            (decisioni Daniele 05/09/2026). Cron CRON_NWG_PRODOTTI, default spento.
```

Job per fornitore: makito stock/prezzi/prodotti/full/bootstrap; nwg prodotti/full/bootstrap.
Run mirati: `POST /run/{fornitore}/{job}?workers=N&solo=CHIAVE1,CHIAVE2`.

Le regole di conversione (colori, listini "acquisto rivenditori", giacenze attuale+arrivi,
cicli MAI inviati perche' gestiti a mano su OnPage) restano in `suppliers/makito/config/mapping_02.yaml`
e sono documentate nel repo F02, che rimane come storico e fallback manuale via GitHub Actions.
