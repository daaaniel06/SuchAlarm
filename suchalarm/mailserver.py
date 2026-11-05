"""The mailbox that sends the mails: from SMTP_* in compose.yml, which wins, or else from the settings page.

Its own module, without the search kinds, so the kinds can ask whether mails can go out at all (mailer.py builds
and sends them and needs the kinds for that)."""

import os
from dataclasses import dataclass
from email.utils import formataddr, parseaddr

from . import store


@dataclass(frozen=True)
class Server:
    """Where mails go out: from SMTP_* in compose.yml, which wins, or else from the settings page."""

    host: str
    port: int
    security: str
    user: str
    password: str
    sender: str  # the From header
    fixed: bool  # set in compose.yml; the page shows it but cannot change it

    @property
    def address(self):
        return parseaddr(self.sender)[1]


def security_for(port):
    """Port 465 speaks TLS from the start; every other port upgrades with STARTTLS."""
    return 'ssl' if port == 465 else 'starttls'


def server():
    env = os.environ
    if env.get('SMTP_HOST'):
        return Server(
            host=env['SMTP_HOST'],
            port=int(env.get('SMTP_PORT', '587')),
            security=env.get('SMTP_SECURITY', 'starttls'),
            user=env.get('SMTP_USER', ''),
            password=env.get('SMTP_PASSWORD', ''),
            sender=env.get('SMTP_FROM', ''),
            fixed=True,
        )
    saved = store.mail_server()
    return Server(
        host=saved['host'],
        port=saved['port'],
        security=security_for(saved['port']),
        user=saved['sender'],  # the mailbox logs in with its own address
        password=saved['password'],
        sender=formataddr(('Suchalarm', saved['sender'])) if saved['sender'] else '',
        fixed=False,
    )


def configured(current=None):
    current = current or server()
    return bool(current.host and current.sender)
