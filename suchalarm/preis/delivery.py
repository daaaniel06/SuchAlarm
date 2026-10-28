"""Cached Vinted minimum shipping; one detail page per free queue slot."""

import asyncio
import json
import time
from dataclasses import replace

from ..core import cadence

TTL = 12 * 3600
SCHEMA = """
      CREATE TABLE IF NOT EXISTS deliveries (url TEXT PRIMARY KEY, shipping INTEGER, checked_at REAL DEFAULT 0,
        next_at REAL DEFAULT 0, seen REAL, price INTEGER, failures INTEGER DEFAULT 0);
"""


def _setup(conn):
    conn.executescript(SCHEMA)


def db():
    return cadence.transaction('delivery.db', _setup)


def cached(url):
    with db() as conn:
        row = conn.execute('SELECT shipping,checked_at FROM deliveries WHERE url=?', (url,)).fetchone()
    return row['shipping'] if row and row['checked_at'] > time.time() - TTL else None


def enrich(offers):
    result = []
    for offer in offers:
        if offer.source == 'vinted' and offer.shipping is None:
            offer = replace(offer, shipping=cached(offer.url))
        result.append(offer)
    return result


def remember(offers):
    now = time.time()
    with db() as conn:
        for offer in offers:
            if offer.source == 'vinted' and offer.shipping is None:
                conn.execute(
                    'INSERT INTO deliveries(url,seen,price) VALUES (?,?,?) ON CONFLICT(url) DO UPDATE SET seen=excluded.seen,price=excluded.price',
                    (offer.url, now, offer.price),
                )
        conn.execute('DELETE FROM deliveries WHERE seen<?', (now - 7 * 86400,))


LOOKUP_TIMEOUT = 60
RETRY_BASE, RETRY_MAX = 300, 3600  # a failed lookup of one item: 10, 20, 40 ... minutes, at most an hour


def _next_job(active_urls, now):
    """The item waiting longest for a lookup, cheap ones first; only offers still in recent results."""
    with db() as conn:
        rows = conn.execute('SELECT * FROM deliveries WHERE next_at<=? ORDER BY checked_at,price,seen', (now,))
        return next((dict(r) for r in rows if r['url'] in active_urls), None)


def _update_cached_queries(url, amount):
    """Put the amount into stored query results too, without changing their observation or cadence."""

    def edit(value):
        hits = [offer for offer in value['offers'] if offer['url'] == url]
        for offer in hits:
            offer['shipping'] = amount
        return bool(hits)

    cadence.edit_results('vinted', json.dumps(url), edit)


async def step(active_urls, fetch):
    """At most one detail visit via FETCH(url) -> amount, in a free slot of the Vinted page budget.

    Returns (url, amount) after a visit, else None. A provider refusal pauses Vinted for searches too.
    """
    now = time.time()
    if not active_urls or cadence.ready_at('vinted', now=now) > now:
        return None
    job = _next_job(active_urls, now)
    if not job:
        return None
    try:
        amount = await asyncio.wait_for(fetch(job['url']), timeout=LOOKUP_TIMEOUT)
    except cadence.Deferred:
        return None
    except Exception as exc:
        failures = job['failures'] + 1
        if isinstance(exc, cadence.Refused):
            until = cadence.outcome('vinted', 'page', True)
        else:
            until = now + min(RETRY_MAX, RETRY_BASE * 2 ** min(failures, 4))
        with db() as conn:
            conn.execute('UPDATE deliveries SET failures=?,next_at=? WHERE url=?', (failures, until, job['url']))
        return None
    now = time.time()
    cadence.outcome('vinted', 'page')
    with db() as conn:
        conn.execute(
            'UPDATE deliveries SET shipping=?,checked_at=?,next_at=?,failures=0 WHERE url=?',
            (amount, now, now + TTL, job['url']),
        )
    if amount is not None:
        _update_cached_queries(job['url'], amount)
    return job['url'], amount
