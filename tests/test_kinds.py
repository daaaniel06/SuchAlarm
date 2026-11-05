import inspect
import unittest

from suchalarm import kinds


class KindInterfaceTests(unittest.TestCase):
    def test_every_kind_offers_the_whole_interface(self):
        for name, kind in kinds.KINDS.items():
            self.assertIsInstance(kind, kinds.Kind, name)
            for step in kinds.ASYNC:
                self.assertTrue(inspect.iscoroutinefunction(getattr(kind, step)), f'{name}.{step} must be async')
            self.assertIsInstance(kind.mail, kinds.Mail, f'{name}.mail')
            self.assertEqual(set(kind.mail.WORDS), set(kinds.KINDS['preis'].mail.WORDS), f'{name}.mail.WORDS')

    def test_sources_belong_to_one_kind(self):
        seen = {}
        for name, kind in kinds.KINDS.items():
            for source in kind.SOURCES:
                self.assertNotIn(source, seen, f'{source} in {seen.get(source)} and {name}')
                seen[source] = name


if __name__ == '__main__':
    unittest.main()
