"""Scheduler dei job ricorrenti (APScheduler, cron in UTC).

Cadenze decise da Daniele (04/09/2026): giacenze ogni ora, prezzi ogni giorno,
prodotti una volta a settimana. Le espressioni sono nelle env CRON_MAKITO_*
(vuote = job disattivato). Un run schedulato salta il giro se un altro job
per lo stesso fornitore e' gia' attivo (lock in state.py + registro in_corso).
"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config


def avvia_scheduler(esegui, in_corso: dict) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")

    def pianifica(fornitore: str, nome_job: str, cron: str):
        if not cron.strip():
            return

        def tick():
            if in_corso.get(fornitore):
                return  # giro saltato: un job e' gia' attivo, il prossimo recupera
            esegui(fornitore, nome_job)

        sched.add_job(tick, CronTrigger.from_crontab(cron), id=f"{fornitore}-{nome_job}",
                      coalesce=True, max_instances=1, misfire_grace_time=600)

    pianifica("makito", "stock", config.CRON_MAKITO_STOCK)
    pianifica("makito", "prezzi", config.CRON_MAKITO_PREZZI)
    pianifica("makito", "prodotti", config.CRON_MAKITO_PRODOTTI)
    pianifica("nwg", "prodotti", config.CRON_NWG_PRODOTTI)
    sched.start()
    return sched
