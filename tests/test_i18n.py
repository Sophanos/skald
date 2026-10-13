"""Fixed player text and synthetic translation fallback contracts."""
import ast
import fnmatch
import json
from pathlib import Path
import re
from string import Formatter
import unittest
from unittest.mock import patch

from skald import i18n

ROOT = Path(__file__).resolve().parents[1]


class I18nTests(unittest.TestCase):
    def test_fallbacks(self):
        cases = [
            ('sr-Latn', ['sr-latn', 'en']),
            ('SR-lAtN-rs', ['sr-latn-rs', 'sr-latn', 'en']),
            ('sr', ['sr-cyrl', 'sr', 'en']),
            ('sr-RS', ['sr-rs', 'sr-cyrl', 'sr', 'en']),
            ('sr-Cyrl', ['sr-cyrl', 'sr', 'en']),
            ('sr-Cyrl-RS', ['sr-cyrl-rs', 'sr-cyrl', 'sr', 'en']),
            ('bs', ['bs', 'en']),
            ('bs-BA', ['bs-ba', 'bs', 'en']),
            ('bs-Cyrl', ['bs-cyrl', 'en']),
            ('bs-Cyrl-BA', ['bs-cyrl-ba', 'bs-cyrl', 'en']),
            ('zh-CN', ['zh-cn', 'zh-hans', 'en']),
            ('zh-SG', ['zh-sg', 'zh-hans', 'en']),
            ('zh-TW', ['zh-tw', 'zh-hant', 'en']),
            ('zh-HK', ['zh-hk', 'zh-hant', 'en']),
            ('zh-Hans-CN', ['zh-hans-cn', 'zh-hans', 'en']),
            ('zh-Hant-TW', ['zh-hant-tw', 'zh-hant', 'en']),
            ('pt-BR', ['pt-br', 'pt', 'en']),
            ('pt-PT', ['pt-pt', 'pt', 'en']),
            ('pt', ['pt', 'en']),
            ('es-MX', ['es-mx', 'es-419', 'es', 'en']),
            ('es-Latn-MX', ['es-latn-mx', 'es-latn', 'en']),
            ('de-DE', ['de-de', 'de', 'en']),
            ('uk', ['uk', 'en']),
            ('uk-UA', ['uk-ua', 'uk', 'en']),
            ('uk-Cyrl-UA', ['uk-cyrl-ua', 'uk-cyrl', 'en']),
            ('sh', ['sr-latn', 'en']),
            ('sh-RS', ['sr-latn-rs', 'sr-latn', 'en']),
        ]
        locales = {code for _, chain in cases for code in chain} | {'hr', 'ru', 'zh', 'sh', 'sh-rs'}
        tables = {code: {'test': code} for code in locales}
        for lang, chain in cases:
            with self.subTest(lang=lang), patch.object(i18n, '_STRINGS', tables.copy()) as catalogs:
                for expected in chain:
                    self.assertEqual(i18n.t('test', lang), expected)
                    catalogs.pop(expected)

    def test_missing_key_and_fields(self):
        with patch.object(i18n, '_STRINGS', {'en': {'test': 'Hello {name} {count}', 'only.en': 'English', 'other': 'Other'},
                                           'de': {'other': 'Andere', 'test': 'Hallo {name} {count}'}}):
            self.assertEqual(i18n.t('absent', 'de'), 'absent')
            self.assertEqual(i18n.t('other', 'de'), 'Andere')
            self.assertEqual(i18n.t('test', 'fr', name='Ada', count=2), 'Hello Ada 2')
            self.assertEqual(i18n.t('test', 'de', name='Ada'), 'Hello {name} {count}')
            self.assertEqual(i18n.t('test'), 'Hello {name} {count}')
            self.assertEqual(i18n.t('only.en', 'de-DE'), 'English')
            self.assertEqual(i18n.t('other', 'de-DE'), 'Andere')

    def test_translation_placeholders_match_english(self):
        source = '{name} has {count}'
        cases = ['Hallo {name}', 'Hallo {name} {count} {extra}',
                 'Hallo {title} {count}', 'Hallo {name:{width}} {count}', 'Hallo {name']
        fields = dict(name='Ada', count=2, title='Title', extra='Extra', width=5)
        for translation in cases:
            tables = {'en': {'test': source}, 'de-de': {'test': translation},
                      'de': {'test': '{count} fuer {name}'}}
            with self.subTest(translation=translation), patch.object(i18n, '_STRINGS', tables):
                self.assertEqual(i18n.t('test', 'de-DE', **fields), '2 fuer Ada')
                tables['de']['test'] = 'Hallo {name}'
                self.assertEqual(i18n.t('test', 'de-DE', **fields), 'Ada has 2')
        with patch.object(i18n, '_STRINGS', {'en': {'test': source},
                                           'de': {'test': '{count}: {name} {name}'}}):
            self.assertEqual(i18n.t('test', 'de', **fields), '2: Ada Ada')

    def test_english_templates(self):
        strings = json.loads((ROOT / 'skald/locales/en.json').read_text())
        self.assertTrue(strings)
        for key, value in strings.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)
                self.assertTrue(value.strip())
                fields = [name for _, name, _, _ in Formatter().parse(value) if name is not None]
                self.assertTrue(all(name.isidentifier() for name in fields))
                self.assertEqual(i18n.t(key), value)
                self.assertEqual(i18n.t(key, **dict.fromkeys(fields, 'fixture')),
                                 value.format(**dict.fromkeys(fields, 'fixture')))

    def test_loaded_once(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('text was reloaded')):
            self.assertEqual(i18n.t('catalog.search'), 'Search')

    def test_player_regions_use_keys(self):
        tree = ast.parse((ROOT / 'server.py').read_text())
        strings = json.loads((ROOT / 'skald/locales/en.json').read_text())
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id in ('t', '_translate') and call.args):
                continue
            key = call.args[0]
            keys = (key.body, key.orelse) if isinstance(key, ast.IfExp) else (key,)
            for key in keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    self.assertIn(key.value, strings, f'missing English key at server.py:{call.lineno}')
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
                continue
            name = node.targets[0].id
            if name == 'LANE_TEMPLATE':
                for key in ast.literal_eval(node.value).values():
                    self.assertIn(key, strings)
            elif name == 'ANIME_SEASONS':
                for season in ast.literal_eval(node.value):
                    self.assertIn('season.' + season.lower(), strings)
            elif name in ('WD_SHORT', 'MONTHS'):
                prefix = 'weekday.' if name == 'WD_SHORT' else 'month.'
                for i in range(*(ast.literal_eval(arg) for arg in node.value.generators[0].iter.args)):
                    self.assertIn(prefix + str(i), strings)
        functions = {'feed_rows', 'row_name', 'memory_why', 'seed_phrase', 'reasons',
                     'confidence', 'episode_label', 'manifest', 'remote_rows', 'rows_view', 'guest_rows_view'}
        definitions = {'DOC_ROWS', 'DOC_TREND', 'STUDIOS', 'LANE_TEMPLATE', 'MOODS', 'EXPERIENCES'}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in functions:
                nodes = node.body[1:] if ast.get_docstring(node) else node.body
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in definitions for t in node.targets):
                value = node.value
                index = 1 if node.targets[0].id == 'EXPERIENCES' else 0
                nodes = ([v.elts[index] if isinstance(v, ast.Tuple) else v for v in value.values]
                         if isinstance(value, ast.Dict) else [value])
            else:
                continue
            for part in nodes:
                for literal in ast.walk(part):
                    if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                        self.assertIsNone(re.search(r'\b[A-Z][a-z]+|[A-Za-z]+\s+[A-Za-z]+', literal.value),
                                          f'player text at server.py:{literal.lineno}: {literal.value!r}')

    def test_key_guard_rejects_missing_source_and_typos(self):
        source = (ROOT / 'server.py').read_text()
        strings = json.loads((ROOT / 'skald/locales/en.json').read_text())
        read_text = Path.read_text
        for key in ('row.foryou', 'row.seed.watch', 'row.lane.from', 'month.1',
                    'season.fall', 'experience.cozy_case', 'row.unknown'):
            english = {k: v for k, v in strings.items() if k != key}
            server = source.replace('t("row.foryou")', 't("row.unknown")') if key == 'row.unknown' else source
            def read(path, *args, **kwargs):
                if path == ROOT / 'server.py':
                    return server
                if path == ROOT / 'skald/locales/en.json':
                    return json.dumps(english)
                return read_text(path, *args, **kwargs)
            with self.subTest(key=key), patch.object(Path, 'read_text', read):
                with self.assertRaisesRegex(AssertionError, re.escape(key)):
                    self.test_player_regions_use_keys()

    def test_translations_are_in_docker_package(self):
        self.assertIn('COPY skald/ /app/skald/', (ROOT / 'Dockerfile').read_text())
        included = True
        for rule in (ROOT / '.dockerignore').read_text().splitlines():
            if fnmatch.fnmatch('skald/locales/en.json', rule.lstrip('!')):
                included = rule.startswith('!')
        self.assertTrue(included)
