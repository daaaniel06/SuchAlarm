"""German number and money formatting for mails and stored texts."""


def amount(cents):
    return f'{cents / 100:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.') + ' €'


def number(value):
    """A size or room count: 62.5 -> "62,5", 2.0 -> "2"."""
    return f'{value:g}'.replace('.', ',')
