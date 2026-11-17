"""Isolated, initialized data directories for store tests."""

import os
import tempfile
import unittest
from typing import ClassVar
from unittest.mock import patch

from suchalarm import store


class TempDataTest(unittest.TestCase):
    ENV: ClassVar[dict[str, str]] = {}

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name, **self.ENV})
        self.env.start()
        self.addCleanup(self.env.stop)
        store.init()


class AsyncTempDataTest(TempDataTest, unittest.IsolatedAsyncioTestCase):
    pass
