"""Guardia di struttura sui file depositati a mano (richiesta di Daniele,
07/09/2026): alla prima elaborazione si memorizza l'impronta del file
(fogli + colonne) e una copia di riferimento dell'originale; ai giri
successivi si confronta e, se la struttura e' cambiata, l'import si FERMA
con il dettaglio delle differenze. La struttura attesa resta recuperabile
da GET /schema/{fornitore}; quando un cambio e' voluto, POST
/schema/{fornitore}/accetta promuove la nuova struttura.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


def _testata(righe) -> list[str]:
    """Prima riga con almeno 3 celle piene = intestazione."""
    for riga in righe:
        celle = [str(c).strip() for c in riga if c not in (None, "")]
        if len(celle) >= 3:
            return celle
    return []


def impronta(percorso: Path) -> dict:
    """Impronta strutturale: {foglio: [colonne]} per gli Excel."""
    suff = percorso.suffix.lower()
    if suff == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(percorso, read_only=True)
        return {ws.title: _testata(ws.iter_rows(max_row=10, values_only=True))
                for ws in wb.worksheets}
    if suff == ".xls":
        import xlrd
        wb = xlrd.open_workbook(percorso)
        return {sh.name: _testata([sh.row_values(r) for r in range(min(10, sh.nrows))])
                for sh in wb.sheets()}
    raise ValueError(f"formato non gestito dalla guardia di struttura: {suff}")


def _confronta(atteso: dict, trovato: dict) -> list[str]:
    diff = []
    for foglio, colonne in atteso.items():
        if foglio not in trovato:
            # i fogli possono cambiare nome: basta che le colonne attese
            # esistano in QUALCHE foglio del nuovo file
            if not any(set(colonne) <= set(c) for c in trovato.values()):
                diff.append(f"foglio '{foglio}' non trovato (colonne attese: {colonne[:8]}...)")
            continue
        mancanti = [c for c in colonne if c not in trovato[foglio]]
        if mancanti:
            diff.append(f"foglio '{foglio}': colonne mancanti {mancanti}")
    return diff


def verifica(dir_schema: Path, file: Path) -> list[str]:
    """Confronta il file con lo schema memorizzato. Ritorna la lista delle
    differenze ([] = ok). Alla prima elaborazione memorizza schema + copia
    di riferimento; su differenze salva il candidato per l'eventuale
    promozione via /schema/{fornitore}/accetta."""
    dir_schema.mkdir(parents=True, exist_ok=True)
    nome = file.name
    f_schema = dir_schema / f"{nome}.schema.json"
    corrente = impronta(file)
    if not f_schema.exists():
        f_schema.write_text(json.dumps(corrente, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        shutil.copyfile(file, dir_schema / f"riferimento_{nome}")
        return []
    atteso = json.loads(f_schema.read_text(encoding="utf-8"))
    diff = _confronta(atteso, corrente)
    if diff:
        (dir_schema / f"candidato_{nome}.schema.json").write_text(
            json.dumps(corrente, ensure_ascii=False, indent=1), encoding="utf-8")
    return diff


def stato(dir_schema: Path) -> dict:
    """Schemi memorizzati e candidati in attesa (per GET /schema/{fornitore})."""
    out: dict = {"schemi": {}, "candidati": {}}
    if not dir_schema.exists():
        return out
    for f in sorted(dir_schema.glob("*.schema.json")):
        dest = out["candidati"] if f.name.startswith("candidato_") else out["schemi"]
        dest[f.name.removeprefix("candidato_").removesuffix(".schema.json")] = \
            json.loads(f.read_text(encoding="utf-8"))
    return out


def accetta(dir_schema: Path) -> list[str]:
    """Promuove i candidati a schema atteso. Ritorna i nomi promossi."""
    promossi = []
    for cand in sorted(dir_schema.glob("candidato_*.schema.json")):
        nome = cand.name.removeprefix("candidato_")
        cand.replace(dir_schema / nome)
        promossi.append(nome.removesuffix(".schema.json"))
    return promossi
