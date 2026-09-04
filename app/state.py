"""Stato persistente del hub sul Volume Railway (STATE_DIR).

Layout per fornitore (es. makito):
  {STATE_DIR}/{fornitore}/state.json       {chiave: sha1 dell'ultimo payload inviato ok}
  {STATE_DIR}/{fornitore}/send_log.jsonl   log invii append-only
  {STATE_DIR}/{fornitore}/runs.jsonl       riepilogo di ogni run (job, durate, contatori)
  {STATE_DIR}/{fornitore}/dumps/           cache dei dump /files per i refresh mirati
  {STATE_DIR}/{fornitore}/work/            area di lavoro (snapshot+payload dell'ultimo run)
  {STATE_DIR}/{fornitore}/.lock            lock: un solo job per fornitore alla volta
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_DIR


def dir_fornitore(fornitore: str) -> Path:
    d = STATE_DIR / fornitore
    d.mkdir(parents=True, exist_ok=True)
    return d


def hash_payload(payload: dict) -> str:
    canonico = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonico.encode("utf-8")).hexdigest()


def load_hashes(fornitore: str) -> dict[str, str]:
    p = dir_fornitore(fornitore) / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_hashes(fornitore: str, hashes: dict[str, str]):
    p = dir_fornitore(fornitore) / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(hashes, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def append_send_log(fornitore: str, rec: dict):
    with (dir_fornitore(fornitore) / "send_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_run(fornitore: str, rec: dict):
    rec = {"ts": datetime.now(timezone.utc).isoformat(), **rec}
    with (dir_fornitore(fornitore) / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ultimi_run(fornitore: str, n=20) -> list[dict]:
    p = dir_fornitore(fornitore) / "runs.jsonl"
    if not p.exists():
        return []
    righe = p.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in righe[-n:] if l.strip()]


@contextmanager
def lock_fornitore(fornitore: str, blocking: bool = False):
    """File-lock per fornitore. blocking=False -> ValueError se un job e' gia' attivo."""
    p = dir_fornitore(fornitore) / ".lock"
    f = p.open("w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ValueError(f"un job per '{fornitore}' e' gia' in esecuzione")
        f.write(str(time.time()))
        f.flush()
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
