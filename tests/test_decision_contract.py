import tempfile
import unittest
from decide_kit import close_server, check_contract, load_server


class DecisionContractTests(unittest.TestCase):
    def test_all_types(self):
        with tempfile.TemporaryDirectory() as data:
            server = load_server(data)
            try:
                for name in server.DECISIONS:
                    with self.subTest(type=name):
                        check_contract(self, server, name)
            finally:
                close_server(server)
