"""Shared AIOStreams profile mapping with synthetic, offline SQLite data."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from history_kit import NOW, watch_db
from skald import connectors, members, setup
from skald.__main__ import main, parser


class SkipProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'TMDB_API': 'fake'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(members.configure)
        setup.household(self.data, ['alex'], 'en-US', 'UTC', 'United States')
        setup.write_private(self.data/'setup.json', json.dumps({'base': 'http://localhost:8080',
            'watch': 'stremio', 'taste': 'files', 'tmdb_checked': True}))
        self.options = watch_db(self.data/'aio.db')
        self.a, self.b, self.c = 'a1b2c3d4', 'b1b2c3d4', 'c1b2c3d4'

    def configure(self, *mappings):
        flags = [flag for mapping in mappings for flag in ('--map', mapping)]
        out = io.StringIO()
        with patch.object(setup, 'check_tmdb', return_value=True), \
                patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
            code = main(['setup', '--connect', 'aiostreams-db', '--source-path', self.options['path'],
                         '--yes', *flags], out=out)
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def skip_other_households(self):
        return self.configure(self.a + '=alex', self.b + '=skip', self.c + '=skip')

    def test_skip_saves_complete_mapping_and_reads_only_member(self):
        raw = (self.data/'aio.db').read_bytes()
        self.skip_other_households()
        cfg = json.loads((self.data/'skald.json').read_text())
        opts = cfg['connectors']['aiostreams_db']['options']
        self.assertEqual(opts['profiles'], {self.a: 'alex'})
        self.assertEqual(opts['archived'], {self.b: 'household', self.c: 'household'})
        with setup.server(self.data) as s:
            result = connectors.test(s, 'aiostreams_db', now=NOW)
            self.assertTrue(result['ok'])
            self.assertTrue(result['connectors'][0]['mapping_complete'])
            items = s._aiostreams_db(opts)['items']
            self.assertEqual({it['profile'] for it in items}, {'alex'})
            self.assertEqual({it['id'] for it in items}, {'tmdb:1419', 'tmdb:1420', 'tmdb:605'})
            def details(it):
                return ('movie' if it['type'] == 'movie' else 'tv',
                        {'id': int(it['id'].split(':')[1]), 'title': 'Synthetic',
                         'release_date': '2020-01-01', 'genres': [{'name': 'Drama'}]})
            with patch.object(s, 'tmdb_details', side_effect=details), patch('time.time', return_value=NOW):
                profile = s.build_profile(items, [], remote=False)
            self.assertEqual({e['key'] for e in s.person_entries(profile, 'alex')},
                             {it['id'] for it in items})
            s._profiles['household'] = profile
            events, _, _ = s._connector_player({'id': 'aiostreams_db', 'cursor': None}, NOW)
            self.assertEqual({ev['key'] for ev in events}, {'tmdb:1419', 'tmdb:1420'})
            self.assertEqual(json.loads((self.data/'history.json').read_text())['items'], items)
        self.assertEqual((self.data/'aio.db').read_bytes(), raw)

    def test_remapping_moves_prefix_between_profiles_and_archived(self):
        self.skip_other_households()
        self.configure(self.b + '=alex')
        opts = json.loads((self.data/'skald.json').read_text())['connectors']['aiostreams_db']['options']
        self.assertEqual(opts['profiles'], {self.a: 'alex', self.b: 'alex'})
        self.assertEqual(opts['archived'], {self.c: 'household'})
        self.configure(self.b + '=skip')
        opts = json.loads((self.data/'skald.json').read_text())['connectors']['aiostreams_db']['options']
        self.assertNotIn(self.b, opts['profiles'])
        self.assertEqual(opts['archived'][self.b], 'household')

    def test_remap_to_skip_rewrites_saved_history_and_invalidates_profile_hash(self):
        self.configure(self.a + '=alex', self.b + '=alex', self.c + '=skip')
        with setup.server(self.data) as s:
            opts = s.CONFIG['connectors']['aiostreams_db']['options']
            s._connector_write('history', s._aiostreams_db(opts))
            old_items = s.read_history()
            self.assertIn('tmdb:603', {it['id'] for it in old_items})
            old_hash = s.taste_hash('household', old_items)
            s._profiles['household'] = {'hash': old_hash}
            args = parser().parse_args(['setup', '--connect', 'aiostreams-db', '--yes',
                                       '--map', self.b + '=skip'])
            args.connect = 'aiostreams_db'
            self.assertTrue(setup.configure_connector(s, args, lambda value, label, default, flag:
                value if value is not None else default, [], lambda: None)['ok'])
            items = s.read_history()
            self.assertEqual({it['id'] for it in items}, {'tmdb:1419', 'tmdb:1420', 'tmdb:605'})
            self.assertNotEqual(s.taste_hash('household', items), s._profiles['household']['hash'])

    def test_stored_member_named_skip_is_preserved_and_explicit_skip_rejected(self):
        cfg = json.loads((self.data/'skald.json').read_text())
        cfg['household']['members'].append('skip')
        cfg['household']['labels']['skip'] = 'Skip'
        cfg['connectors']['aiostreams_db'] = {'kind': 'player', 'enabled': True, 'auth': None,
            'options': {'path': self.options['path'], 'profiles': {self.a: 'skip', self.b: 'alex'},
                        'archived': {self.c: 'household'}}}
        cfg['history']['source'] = 'aiostreams_db'
        setup.publish(self.data, cfg, replace=True)
        self.configure()
        saved = json.loads((self.data/'skald.json').read_text())
        opts = saved['connectors']['aiostreams_db']['options']
        with self.subTest('stored member'):
            self.assertEqual(opts['profiles'].get(self.a), 'skip')
            self.assertNotIn(self.a, opts['archived'])
        with self.subTest('explicit skip'), self.assertRaisesRegex(ValueError, 'member named skip'):
            setup.parse_mapping([self.b + '=skip'], saved, 'aiostreams_db')

    def test_all_skip_raises_without_saving(self):
        before = (self.data/'skald.json').read_bytes()
        args = parser().parse_args(['setup', '--connect', 'aiostreams-db', '--source-path', self.options['path'],
            '--yes', '--map', self.a + '=skip', '--map', self.b + '=skip', '--map', self.c + '=skip'])
        args.connect = 'aiostreams_db'
        with setup.server(self.data) as s, self.assertRaisesRegex(ValueError, 'at least one profile.*member or together'):
            setup.configure_connector(s, args, lambda value, *rest: value, [], lambda: None)
        self.assertEqual((self.data/'skald.json').read_bytes(), before)

    def test_skip_rejected_for_other_sources_and_unknown_names_rejected(self):
        cfg = members.fresh_config(['alex'])
        for cid in ('jellyfin', 'plex'):
            with self.subTest(cid=cid), self.assertRaisesRegex(ValueError, 'or SOURCE_ID=skip for aiostreams-db'):
                setup.parse_mapping(['one=skip'], cfg, cid)
        with self.assertRaisesRegex(ValueError, 'use --map'):
            setup.parse_mapping(['one=stranger'], cfg, 'aiostreams_db')

    def test_interactive_skip(self):
        args = parser().parse_args(['setup', '--connect', 'aiostreams-db', '--source-path', self.options['path']])
        args.connect = 'aiostreams_db'
        def ask(value, label, default, flag):
            if value:
                return value
            self.assertIn('skip', label)
            return 'alex' if self.a in label else 'skip'
        with setup.server(self.data) as s:
            self.assertTrue(setup.configure_connector(s, args, ask, [], lambda: None)['ok'])
            self.assertEqual(s.CONFIG['connectors']['aiostreams_db']['options']['archived'],
                             {self.b: 'household', self.c: 'household'})

    def test_status_and_connector_text_show_count_without_prefixes(self):
        output = self.skip_other_households()
        out = io.StringIO()
        self.assertEqual(main(['setup', '--status'], out=out), 0)
        for text in (output, out.getvalue()):
            self.assertIn('2 profiles skipped', text)
            for prefix in (self.a, self.b, self.c):
                self.assertNotIn(prefix, text)
