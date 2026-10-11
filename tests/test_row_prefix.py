"""Household row ids share one validated prefix, without changing stored data."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from skald import members, setup
from skald.__main__ import execute, parser
from test_api import raw_request


class RowPrefix(unittest.TestCase):
    def test_default_validation_and_legacy_setting(self):
        self.assertEqual(members.validate_config(members.LEGACY)['row_prefix'], 'skald')
        for value in ('x', 'Upper', 'two.parts', 'a' * 17, '', 42):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'row_prefix'):
                members.validate_config({**members.LEGACY, 'row_prefix': value})
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            cfg = {**members.LEGACY, 'row_prefix': 'house42'}
            (data/'legacy.json').write_text(json.dumps(cfg))
            feedback = '{"row":"house42.foryou"}\n'
            (data/'feedback.jsonl').write_text(feedback)
            with setup.server(data) as s:
                self.assertEqual(s.rid('foryou'), 'house42.foryou')
                ids = [r['id'] for r in s.manifest(s.LANG)['catalogs']]
                self.assertTrue(ids)
                self.assertTrue(all(cid.startswith(s.rid('')) for cid in ids))
                self.assertEqual(s.REMOTE_ROWS & {'house42.interview'}, {'house42.interview'})
                self.assertIn("row === 'house42.interview'", s.REMOTE_JS)
                with patch.object(s, 'search', return_value={'metas': []}) as search:
                    result = raw_request(s, f"/{s.LANG}/catalog/movie/{s.rid('search')}/search=fixture.json", handler=s.Handler)
                    self.assertEqual(result[0], 200)
                    search.assert_called_once()
                engine = SimpleNamespace(_server=s, rows=lambda lang: {'rows': [{'id': s.rid('foryou'), 'titles': []}]})
                for name in ('foryou', s.rid('foryou')):
                    args = parser().parse_args(['rows', '--row', name])
                    self.assertEqual(execute(args, engine, None, None)['rows'][0]['id'], s.rid('foryou'))
            self.assertEqual((data/'feedback.jsonl').read_text(), feedback)
