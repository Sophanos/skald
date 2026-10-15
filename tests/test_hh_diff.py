"""Private Home diffs use only frozen synthetic inputs and public source copies."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from eval.household import __main__ as cli, snapshot
from skald.members import fresh_config, rid

ROOT = Path(__file__).resolve().parents[1]
NOW = 1792065600


class HomeDiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = self.root / 'synthetic'
        data.mkdir()
        cfg = fresh_config(['ana', 'ben', 'kim', 'lena'])
        cfg['household']['guests'] = ['lena']
        cfg['household']['home'] = {'family_caps': {'mood': 1, 'lane': 2}}
        cfg['household']['rows_from_default'] = True
        cfg['household']['retired_rows'] = [rid('mood2', cfg)]
        cfg['taste']['people']['kim']['prefs'] = {'kids_age': 9}
        cfg['household']['rows'] = ['digital', 'leap']
        cfg['household']['row_order'] = ['digital', 'leap']
        metas = [dict(id=f'tmdb:{i}', name=f'Invented Card {i} Example', type='movie', poster='p',
                      background='b', _row={'g': ['Drama'], 'rating': 8, 'votes': 1000,
                                            'date': '2020-01-01'}) for i in range(100, 128)]
        inputs = {'skald.json': cfg, 'history.json': {'items': []},
                  'facets.json': {'items': {m['id']: {'kind': 'movie', 'g': ['Drama'],
                                                    'cr': {'US': 'PG'}} for m in metas}},
                  'emb.json': {'dim': 256, 'v': {}, 'kinds': {}},
                  'feeds.json': {rid(row, cfg)+'|en-US': {'metas': {typ: cards}}
                                 for row, typ, cards in (('digital', 'movie', metas[:14]), ('leap', 'tv', metas[14:]))}}
        self.inputs = inputs
        for name, value in inputs.items():
            (data / name).write_text(json.dumps(value))
        self.frozen = snapshot.freeze(data, self.root / 'frozen', inputs=list(inputs),
                                      generation=lambda: NOW, enabled=True)
        self.copy = self.root / 'build'
        for source in [ROOT / 'server.py', *sorted((ROOT / 'skald').rglob('*.py')),
                       *sorted((ROOT / 'skald/locales').glob('*.json'))]:
            target = self.copy / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def diff(self, *extra, code=0):
        out = io.StringIO()
        with redirect_stdout(out):
            result = cli.main(['diff', '--snapshot', str(self.frozen.parent), '--a', str(ROOT),
                               '--b', str(self.copy), *extra])
        self.assertEqual(result, code, out.getvalue())
        return out.getvalue()

    def refreeze(self):
        data = self.root / 'synthetic'
        for name, value in self.inputs.items():
            (data / name).write_text(json.dumps(value))
        self.frozen = snapshot.freeze(data, self.root / 'updated', inputs=list(self.inputs),
                                      generation=lambda: NOW, enabled=True)

    def test_freeze_preserves_row_language(self):
        self.inputs['skald.json']['region']['row_lang'] = 'de'
        self.refreeze()
        self.assertEqual(snapshot.load(self.frozen)['inputs']['skald.json']['region']['row_lang'], 'de')

    def test_compare_replays_only_active_snapshot_members(self):
        cfg = self.inputs['skald.json']
        cfg['household']['retired'] = ['ben']
        data = self.root / 'synthetic'
        (data / 'skald.json').write_text(json.dumps(cfg))
        played = NOW - 18 * 86400
        record = {'media_kind': 'movie', 'action': 'played', 'source': 'synthetic',
                  'reliable': True, 'occurred_at': played, 'observed_at': played,
                  'timestamp_precision': 'second'}
        (data / 'playback.jsonl').write_text('\n'.join(
            json.dumps({**record, 'member': member, 'provider_id': f'tmdb:{i}'})
            for i, member in enumerate(cfg['household']['members'], 100)))
        frozen = snapshot.freeze(data, self.root / 'retired', inputs=list(snapshot.INPUTS),
                                 generation=lambda: NOW, enabled=True)
        self.assertEqual(snapshot.load(frozen)['inputs']['skald.json']['household']['retired'], ['ben'])
        out = self.root / 'comparison'
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main(['compare', '--snapshot', str(frozen.parent), '--a', str(ROOT),
                               '--b', str(self.copy), '--out', str(out)])
        self.assertEqual(result, 2, output.getvalue())
        report = json.loads((out / 'report.json').read_text())
        self.assertTrue(report['aa_identical'])
        self.assertEqual(set(report['home']['scopes']), {'ana', 'kim', 'lena'})
        self.assertEqual(report['home']['support']['weeks'], 12)
        self.assertEqual(report['home']['support']['targets'], 3)

    def test_language_override_precedes_feed_loading(self):
        cfg = self.inputs['skald.json']
        cfg['region']['row_lang'] = 'en'
        feeds = self.inputs['feeds.json']
        for key, record in list(feeds.items()):
            feeds[key.replace('|en-US', '|de-DE')] = {**record, 'stamp': 'row_lang=de-DE/synthetic'}
        request = {'inputs': self.inputs, 'members': ['ana'], 'origins': [NOW],
                   'row_details': True, 'lang': 'de-DE'}
        home = cli._replay(ROOT, request)[0]
        digital = next(r for r in home['rows'] if r['id'] == rid('digital', cfg))
        self.assertEqual(digital['count'], 14)
        self.assertEqual(digital['label'], 'Neu zum Streamen')

    def test_requested_language_keeps_matching_cached_row_locale(self):
        cfg = self.inputs['skald.json']
        cfg['region']['row_lang'] = 'de'
        feeds = self.inputs['feeds.json']
        for key, record in list(feeds.items()):
            feeds[key.replace('|en-US', '|de-DE')] = {**record, 'stamp': 'row_lang=de/synthetic'}
        self.refreeze()
        for lang, label in (('de-DE', 'Neu zum Streamen'), ('en-US', 'Latest Digital Releases')):
            with self.subTest(lang=lang):
                rows = json.loads(self.diff('--member', 'ana', '--lang', lang, '--json'))['homes'][0]['a']
                digital = next(r for r in rows if r['id'] == rid('digital', cfg))
                self.assertEqual(digital['count'], 14)
                self.assertEqual(digital['label'], label)

    def test_manifest_minimum_removes_row_and_updates_positions(self):
        cfg = self.inputs['skald.json']
        cfg['household']['rows'] = ['digital', 'studios', 'leap']
        cfg['household']['row_order'] = ['digital', 'studios', 'leap']
        cfg['household']['retired_rows'].append(rid('paramount', cfg))
        metas = self.inputs['feeds.json'][rid('leap', cfg)+'|en-US']['metas']['tv']
        self.inputs['feeds.json'][rid('hbo', cfg)+'|en-US'] = {
            'metas': {'tv': [{**m, 'id': f'tmdb:{i}', 'name': f'Invented HBO {i} Card'}
                             for i, m in enumerate(metas, 200)]}}
        self.refreeze()
        with (self.copy / 'server.py').open('a') as stream:
            stream.write("\n_original_served = served_row\ndef served_row(cid, typ, lang, *args, **kwargs):\n    metas = _original_served(cid, typ, lang, *args, **kwargs)\n    return metas[:ROW_MIN-1] if cid == rid('hbo') else metas\n")
        home = json.loads(self.diff('--member', 'ana', '--json'))['homes'][0]
        self.assertEqual([r['id'] for r in home['changes']['removed']], [rid('hbo', cfg)])
        self.assertEqual(home['changes']['added'], [])
        self.assertEqual(home['changes']['moved'], [{'id': rid('leap', cfg), 'type': 'tv', 'a': 3, 'b': 2}])
        self.assertEqual([r['position'] for r in home['b']], [1, 2])

    def test_manifest_hides_split_catalog_types(self):
        cfg = self.inputs['skald.json']
        cfg['household']['rows'] = ['top10']
        cfg['taste']['people']['ana']['prefs'] = {'hide': ['movies']}
        metas = self.inputs['feeds.json'][rid('digital', cfg)+'|en-US']['metas']['movie']
        self.inputs['feeds.json'][rid('top10', cfg)+'|en-US'] = {'metas': {
            'movie': metas, 'series': [{**m, 'id': f'tmdb:{i}', 'type': 'series'}
                                     for i, m in enumerate(metas, 200)]}}
        self.refreeze()
        rows = json.loads(self.diff('--member', 'ana', '--json'))['homes'][0]['a']
        self.assertEqual([(r['id'], r['type']) for r in rows], [(rid('top10', cfg), 'series')])

    def test_manifest_duplicate_suffix_changes_when_collision_resolves(self):
        cfg = self.inputs['skald.json']
        cfg['household']['row_labels'] = {'digital': 'Same', 'leap': 'Same'}
        self.refreeze()
        with (self.copy / 'server.py').open('a') as stream:
            stream.write("\n_original_enabled = row_enabled\ndef row_enabled(cid):\n    return cid != rid('digital') and _original_enabled(cid)\n")
        home = json.loads(self.diff('--member', 'ana', '--json'))['homes'][0]
        self.assertEqual([r['label'] for r in home['a']], ['Same', 'Same · 2'])
        self.assertEqual([r['label'] for r in home['b']], ['Same'])
        self.assertEqual(home['changes']['renamed'], [
            {'id': rid('leap', cfg), 'type': 'tv', 'a': 'Same · 2', 'b': 'Same'}])

    def test_identical_then_one_rename_and_removal_without_writes(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = json.loads(self.diff('--json'))
        self.assertEqual([h['member'] for h in result['homes']], ['ana', 'ben', 'kim', 'lena', 'together'])
        self.assertEqual(snapshot.load(self.frozen)['inputs']['skald.json']['household']['guests'], ['lena'])
        frozen_home = snapshot.load(self.frozen)['inputs']['skald.json']['household']
        self.assertEqual(frozen_home['home'], {'family_caps': {'mood': 1, 'lane': 2}})
        self.assertTrue(frozen_home['rows_from_default'])
        self.assertEqual(frozen_home['retired_rows'], [rid('mood2', fresh_config(['ana']))])
        self.assertTrue(all(not any(h['changes'].values()) for h in result['homes']))
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        locale = self.copy / 'skald/locales/en.json'
        locale.write_text(locale.read_text().replace('Latest Digital Releases', 'Invented New Label'))
        server = self.copy / 'server.py'
        with server.open('a') as stream:
            stream.write("\n_original_enabled = row_enabled\ndef row_enabled(cid):\n    return cid != rid('leap') and _original_enabled(cid)\n")
        result = json.loads(self.diff('--member', 'ana', '--json'))['homes'][0]
        self.assertEqual(len(result['changes']['renamed']), 1)
        self.assertEqual(len(result['changes']['removed']), 1)
        self.assertEqual(result['changes']['added'], [])
        self.assertEqual(result['changes']['cards_changed'], [])
        self.assertEqual(result['a'][0]['count'], 14)
        self.assertEqual(len(result['a'][0]['cards']), 10)
        self.assertEqual(result['b'][0]['label'], 'Invented New Label')
        self.assertIn('Invented New Label', self.diff('--member', 'ana'))

    def test_network_attempt_is_counted_on_its_row(self):
        server = self.copy / 'server.py'
        with server.open('a') as stream:
            stream.write("\n_original_served = served_row\ndef served_row(cid, typ, lang, *args, **kwargs):\n    if cid == rid('digital'):\n        try:\n            urllib.request.urlopen('https://synthetic.invalid')\n        except AssertionError:\n            pass\n        try:\n            llm_sections('synthetic')\n        except AssertionError:\n            pass\n        tmdb('synthetic')\n    return _original_served(cid, typ, lang, *args, **kwargs)\n")
        result = json.loads(self.diff('--member', 'ana', '--json'))['homes'][0]
        blocked = next(r for r in result['b'] if r['id'] == rid('digital', fresh_config(['ana'])))
        self.assertEqual(blocked['status'], 'needs network, not built offline')
        self.assertEqual(blocked['network_attempts'], 3)
        self.assertIsNone(blocked['count'])
        self.assertIn('needs network, not built offline', self.diff('--member', 'ana'))

    def test_default_worker_payload_unchanged(self):
        request = {'inputs': snapshot.load(self.frozen)['inputs'], 'members': ['ana'], 'origins': [NOW]}
        plain = cli._replay(ROOT, request)
        detailed = cli._replay(ROOT, {**request, 'row_details': True, 'lang': 'en-US'})
        self.assertEqual(plain, [{k: v for k, v in h.items() if k != 'rows'} for h in detailed])
        self.assertEqual(set(plain[0]), {'specs', 'rankings'})

    def test_blocked_default_row_is_not_hidden_as_empty(self):
        data = self.root / 'synthetic'
        cfg = json.loads((data / 'skald.json').read_text())
        cfg['household']['rows'] = ['moods']
        cfg['household'].pop('row_order')
        (data / 'skald.json').write_text(json.dumps(cfg))
        self.frozen = snapshot.freeze(data, self.root / 'default', inputs=list(snapshot.INPUTS),
                                      generation=lambda: NOW, enabled=True)
        with (self.copy / 'server.py').open('a') as stream:
            stream.write("\n_original_served = served_row\ndef served_row(cid, typ, lang, *args, **kwargs):\n    if cid == rid('mood1'):\n        tmdb('synthetic')\n    return _original_served(cid, typ, lang, *args, **kwargs)\n")
        rows = json.loads(self.diff('--member', 'ana', '--json'))['homes'][0]['b']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'needs network, not built offline')

    def test_generic_failures_and_language(self):
        self.assertIn('HARD FAIL diagnostic', self.diff('--member', '/private/sentinel', code=1))
        text = self.diff('--member', 'ana', '--lang', 'de-DE')
        self.assertIn('Neu zum Streamen', text)
        self.assertNotIn('Latest Digital Releases', text)

    def test_row_changes_keep_order_and_count_new_ids(self):
        from eval.household.diff import changes
        a = [dict(id='one', type='tv', label='Old', cards=[{'id': 'x', 'name': 'X'}]),
             dict(id='two', type='tv', label='Two', cards=[])]
        b = [a[1], {**a[0], 'label': 'New', 'cards': [{'id': 'y', 'name': 'Y'}]}]
        result = changes(a, b)
        self.assertEqual(len(result['renamed']), 1)
        self.assertEqual(len(result['moved']), 2)
        self.assertEqual(result['cards_changed'][0]['new_titles'], 1)
