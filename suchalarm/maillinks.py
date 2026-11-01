"""Links in the digest mail that act without opening the app: save, hide, pause a search, stop the mails.

A link carries its action signed with a secret kept in the settings, so nobody can forge one. Opening it
only shows a page with one button (mail scanners and link previews open links on their own); the button,
or a mail client's one-click unsubscribe (RFC 8058), does it through actions.py, like the page would.
Every action has its opposite for "Rückgängig".
"""

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

from . import actions, store


@dataclass(frozen=True)
class Action:
    opposite: str
    question: str  # what the page asks before
    button: str
    done: str  # what it says after
    run: Callable[[dict], bool]  # True if something changed


ACTIONS = {
    'save': Action('unsave', 'Auf die Merkliste setzen?', 'Merken', 'Gemerkt.',
                   lambda d: actions.set_saved(d['s'], d['u'], True)),
    'unsave': Action('save', 'Von der Merkliste nehmen?', 'Nicht mehr merken', 'Nicht mehr gemerkt.',
                     lambda d: actions.set_saved(d['s'], d['u'], False)),
    'hide': Action('unhide', 'Ausblenden? Er erscheint dann weder in der App noch in Mails.', 'Ausblenden',
                   'Ausgeblendet.', lambda d: actions.set_hidden(d['s'], d['u'], True)),
    'unhide': Action('hide', 'Wieder einblenden?', 'Einblenden', 'Wieder eingeblendet.',
                     lambda d: actions.set_hidden(d['s'], d['u'], False)),
    'pause': Action('resume', 'Diese Suche pausieren?', 'Pausieren', 'Suche pausiert.',
                    lambda d: actions.set_active(d['s'], False)),
    'resume': Action('pause', 'Diese Suche wieder laufen lassen?', 'Fortsetzen', 'Suche läuft wieder.',
                     lambda d: actions.set_active(d['s'], True)),
    'mute': Action('unmute', 'Keine Mails mehr zu dieser Suchart?', 'Abbestellen', 'Mails abbestellt.',
                   lambda d: actions.set_mails(d['k'], False)),
    'unmute': Action('mute', 'Mails wieder einschalten?', 'Wieder einschalten', 'Mails wieder eingeschaltet.',
                     lambda d: actions.set_mails(d['k'], True)),
}  # fmt: skip


def app_url():
    """Link to the app in mails; without APP_URL (set by the infrastructure repo) mails have no links."""
    url = os.environ.get('APP_URL', '').strip().rstrip('/')
    return url if url.startswith(('http://', 'https://')) else ''


def _secret():
    return store.settings()['link_secret'].encode()  # made by store.init()


def _sign(body):
    return base64.urlsafe_b64encode(hmac.digest(_secret(), body.encode(), hashlib.sha256)[:16]).decode().rstrip('=')


def token(action, **data):
    body = base64.urlsafe_b64encode(json.dumps({'a': action, **data}, separators=(',', ':')).encode())
    body_text = body.decode().rstrip('=')
    return f'{body_text}.{_sign(body_text)}'


def read(value):
    """The action of a token, or None if it is broken or forged."""
    body, _, signature = value.partition('.')
    # As bytes: compare_digest refuses str with non-ASCII characters, and a typed-in link may carry some.
    if not hmac.compare_digest(signature.encode(), _sign(body).encode()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    except ValueError:
        return None
    return data if data.get('a') in ACTIONS else None


def opposite(data):
    """The token that takes DATA back."""
    return token(ACTIONS[data['a']].opposite, **{key: value for key, value in data.items() if key != 'a'})


def link(action, **data):
    """The link of an action, or '' without APP_URL."""
    base = app_url()
    return f'{base}/m/{token(action, **data)}' if base else ''


def offer_links(search, offer):
    """Merken, Ausblenden and the hit in the app (a flat's sheet); empty without APP_URL."""
    base = app_url()
    if not base or search.get('id') is None:
        return {}
    pair = {'s': search['id'], 'u': offer['url']}
    return {
        'save': link('save', **pair),
        'hide': link('hide', **pair),
        'detail': f'{base}/#treffer={search["id"]}:{quote(offer["url"], safe="")}',
    }


def describe(data):
    """What the page names: the search, or the kind of mails. Raises actions.Gone without its search."""
    if 'k' in data:
        return 'Mails zu Wohnungen' if data['k'] == 'wohnung' else 'Mails zu Preisen'
    search = store.search(data['s'])
    if not search:
        raise actions.Gone('Diese Suche gibt es nicht mehr.')
    return search['query']
