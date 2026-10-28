"""Suchalarm: saved searches checked in a gentle rhythm, with one core and one module per search kind.

Importing any part of the package registers the sources of every search kind with the core, so
core modules never meet an unknown source, whichever module is imported first.
"""

from . import preis, wohnung  # noqa: F401
