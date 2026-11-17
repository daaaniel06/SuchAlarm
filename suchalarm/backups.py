"""The daily backup: one checked generation of every database per day (worker.daily_backup)."""

import json
import shutil
import sqlite3
import time

from .store import PARTS, db_path

# Files a generation holds besides suchalarm.db and the kinds' files: cadence.db keeps provider pauses, place
# lookups and the queue, conservative to restore (a restored pause only waits longer). The picture cache and
# delivery.db are caches that refill themselves.
BACKUP_EXTRA = ('cadence',)


def make(keep=7):
    """One generation per day in backups/<date>/: every file through SQLite's backup API, checked with
    integrity_check and listed in manifest.json. A generation becomes visible (renamed from .tmp) only when every
    file is complete; the newest KEEP stay. Returns the generation's folder."""
    root = db_path().parent / 'backups'
    root.mkdir(exist_ok=True)
    final = root / time.strftime('%Y-%m-%d')
    for broken in root.glob('*.tmp'):
        shutil.rmtree(broken)  # an interrupted generation
    work = root / (final.name + '.tmp')
    work.mkdir()
    files = {}
    for name in ('suchalarm', *PARTS, *BACKUP_EXTRA):
        if not db_path(name).exists():
            continue
        target = work / f'{name}.db'
        source, copy = sqlite3.connect(db_path(name), timeout=20), sqlite3.connect(target)
        try:
            source.backup(copy)
            if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError(f'Backup of {name}.db failed its integrity check')
        finally:
            copy.close()
            source.close()
        files[target.name] = target.stat().st_size
    manifest = {'created_at': time.time(), 'files': files, 'caches_not_included': ['images/cache.db', 'delivery.db']}
    (work / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    shutil.rmtree(final, ignore_errors=True)
    work.rename(final)
    generations = sorted(p for p in root.iterdir() if p.is_dir() and (p / 'manifest.json').exists())
    for old in generations[:-keep]:
        shutil.rmtree(old)
    if len(generations) >= keep:
        for old in root.glob('*-????-??-??.db'):
            old.unlink()  # single files from before generations, kept until as many generations replace them
    return final
