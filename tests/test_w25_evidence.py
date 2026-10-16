"""Source-independent taste evidence, with invented titles and offline metadata."""
import copy
import datetime as dt
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server
from skald import commands, members
from skald.ingest import parse, rating_signal


NOW = dt.datetime(2026, 10, 16, 12, tzinfo=dt.timezone.utc).timestamp()


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(members.configure)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        config = members.fresh_config(['eva', 'oskar'])
        config['connectors']['pmdb'] = {'kind': 'api', 'enabled': True,
            'accounts': {'eva': 'file:eva.token', 'oskar': 'file:oskar.token'}, 'options': {}}
        (self.data / 'skald.json').write_text(json.dumps(config))
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.assertEqual(self.s.PEOPLE, ('eva', 'oskar'), str(self.s.CONFIG_ERROR))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s.time, 'time', return_value=NOW))
        self.enterContext(patch.object(self.s, 'tmdb_details', side_effect=lambda it: ('movie', {
            'id': int(it['id'].removeprefix('tmdb:').removeprefix('tt')), 'title': 'Invented Lantern',
            'release_date': '2020-01-01', 'genres': []})))

    def entry(self, n, signal='abandoned', weight=-1.5, person='eva', **fields):
        return dict(key=f'tmdb:{n}', id=n, kind='movie', stype='movie', title=f'Invented {n}',
                    year='2020', signal=signal, weight=weight, age=10, who='owner',
                    by=[person], flags=[], genres=[], item={}, **fields)

    def imported(self, action, person='eva'):
        return {'t': '2026-10-10', 'id': 'a' * 32, 'key': 'tmdb:10', 'kind': 'movie',
                'title': 'Invented Lantern', 'year': '2020', 'who': person, 'action': action,
                'src': 'imdb', 'conf': 1., 'n': 1}

    def test_rating_meaning_is_transport_invariant(self):
        for rating, expected in ((0, 'disliked'), (4.49, 'disliked'), (4.5, 'watched'),
                                 (5.99, 'watched'), (6, 'liked'), (7, 'liked'),
                                 (7.99, 'liked'), (8, 'loved'), (10, 'loved')):
            with self.subTest(rating=rating):
                player = {'id': 'tmdb:10', 'type': 'movie', 'rating': rating, 'at': NOW * 1000,
                          'profile': 'eva'}
                p = self.s.build_profile([player], [], remote=False)['entries'][0]
                imports = []
                self.s.merge_imports(imports, {}, NOW, [self.imported(rating_signal(rating))])
                self.assertEqual((p['signal'], imports[0]['signal']), (expected, expected))
                self.assertEqual(p['weight'], imports[0]['weight'])

    def test_like_command_matches_like_feedback(self):
        self.assertEqual(commands.REGISTRY['like']['feedback'], 'like')

    def test_stremio_liked_catalog_is_like(self):
        def http(url, **kwargs):
            if url.endswith('/datastoreGet'):
                return {'result': []}
            if '/getAddonKey?' in url:
                return {'key': 'synthetic'}
            return {'metas': [{'id': 'tt10', 'type': 'movie', 'name': 'Invented Lantern'}]
                    if '/liked/' in url and '/movie/' in url else []}
        with patch.object(self.s, '_resolve_auth', return_value='synthetic'), \
                patch.object(self.s, '_stremio_http', side_effect=http):
            doc, _, _ = self.s._connector_stremio({'auth': 'env:SYNTHETIC_KEY', 'person': 'eva'}, NOW, readonly=True)
        self.assertEqual(doc['items'][0]['signal'], 'liked')

    def facets(self):
        def rec(d, k):
            return {'kind': 'movie', 'title': 'Invented Lantern', 'd': [[d, 'Invented Director']],
                    'k': [[k, 'Invented Topic']], 'g': ['Drama'], 'runtime': 100}
        self.s._facets.update({'tmdb:1': rec(1, 1), 'tmdb:2': rec(2, 2),
                               'tmdb:10': rec(1, 10), 'tmdb:11': rec(11, 11)})

    def test_explicit_rejection_survives_transport_and_stop_reading(self):
        self.facets()
        for transport in ('player', 'pmdb', 'import'):
            for early in ('none', 'release', 'stream'):
                with self.subTest(transport=transport, early=early):
                    self.s._facets['tmdb:10']['dr'] = '2026-11-01' if early == 'release' else '2020-01-01'
                    item = {'id': 'tmdb:10', 'type': 'movie', 'title': 'Invented Lantern',
                            'profile': 'eva', 'history_by': ['eva'], 'at': NOW * 1000, 'dropped': True,
                            'duration_min': 2 if early == 'stream' else 80}
                    if transport == 'player':
                        entries = self.s.build_profile([item], [], remote=False)['entries']
                    elif transport == 'pmdb':
                        doc, _, _ = self.s._pmdb_items([], [], [{'id': 10, 'tmdb_id': 10,
                            'media_type': 'movie', 'created': '2026-10-16T12:00:00Z'}], [], NOW, readonly=True)
                        doc['items'][0].update(duration_min=item['duration_min'], person='eva')
                        entries = self.s.build_profile([], [], doc['items'], remote=False)['entries']
                    else:
                        entries = []
                        self.s.merge_imports(entries, {}, NOW, [self.imported('abandon')])
                        entries[0]['item'] = item
                    entries += [self.entry(1, 'loved', 3), self.entry(10, 'liked', 2, 'oskar')]
                    self.s.read_abandoned(entries)
                    own = self.s.person_entries({'entries': entries}, 'eva')[0]
                    self.assertLess(own['weight'], 0)
                    self.assertNotIn(own.get('abandon'), ('related', 'early', 'unsure', 'far', 'pending'))
                    other = self.s.person_entries({'entries': entries}, 'oskar')[0]
                    self.assertEqual((other['signal'], other['weight']), ('liked', 2))

    def test_related_stop_does_not_create_unearned_positive_preference(self):
        self.facets()
        stop = self.entry(10)
        self.s.read_abandoned([self.entry(1, 'loved', 3), stop])
        self.assertEqual((stop['abandon'], stop['signal'], stop['weight']), ('related', 'abandoned', 0))
        entries = [stop]
        self.s.merge_feedback(entries, {stop['key']: stop}, NOW,
            {(stop['key'], 'eva'): {**self.imported('like'), 't': NOW, 'reasons': []}})
        self.s.read_abandoned(entries)
        self.assertGreater(self.s.person_entries({'entries': entries}, 'eva')[0]['weight'], 0)

    def test_missing_facets_leave_stop_pending(self):
        self.facets()
        stop = self.entry(99)
        self.s.read_abandoned([self.entry(1, 'loved', 3), stop])
        self.assertEqual((stop['abandon'], stop['weight']), ('pending', 0))
        self.assertTrue(self.s._abandon_seen(stop))
        self.s._facets[stop['key']] = copy.deepcopy(self.s._facets['tmdb:10'])
        self.s.read_abandoned([self.entry(1, 'loved', 3), stop])
        self.assertEqual(stop['abandon'], 'related')
        other = self.entry(10, person='oskar')
        self.s.read_abandoned([self.entry(1, 'loved', 3), other])
        self.assertEqual((other['abandon'], other['weight']), ('pending', 0))
        self.s.read_abandoned([self.entry(1, 'loved', 3, 'oskar'), other])
        self.assertEqual(other['abandon'], 'related')

    def test_far_reading_is_idempotent(self):
        self.facets()
        entries = [self.entry(1, 'loved', 3), self.entry(11)]
        self.s.read_abandoned(entries)
        self.assertEqual(entries[-1]['weight'], -.5)
        before = copy.deepcopy(entries)
        self.s.read_abandoned(entries)
        self.assertEqual(entries, before)

    def test_corrected_runtime_restarts_from_base_evidence(self):
        self.facets()
        stop = self.entry(11)
        stop['item'] = {'duration_min': 10, 'at': NOW * 1000}
        original = copy.deepcopy(stop)
        watched = self.entry(1, 'loved', 3)
        self.s.read_abandoned([watched, stop])
        self.assertEqual((stop['abandon'], stop['weight']), ('early', 0))
        for facts in ({'runtime': 40}, {'dr': '2026-11-01'}, {'dr': '2020-01-01', 'd': [[1, 'Invented Director']]}):
            with self.subTest(facts=facts):
                self.s._facets[stop['key']].update(facts)
                fresh = copy.deepcopy(original)
                self.s.read_abandoned([watched, fresh])
                self.s.read_abandoned([watched, stop])
                self.assertEqual(stop, fresh)

    def test_shared_stop_reading_is_member_local(self):
        self.facets()
        shared = self.entry(10, person='together')
        shared['who'] = 'tv'
        entries = [self.entry(1, 'loved', 3), self.entry(2, 'loved', 3, 'oskar'), shared]
        self.s.read_abandoned(entries)
        eva = self.s.person_entries({'entries': entries}, 'eva')[-1]
        oskar = self.s.person_entries({'entries': entries}, 'oskar')[-1]
        self.assertEqual((eva['abandon'], eva['weight']), ('related', 0))
        self.assertEqual((oskar['abandon'], oskar['weight']), ('far', -.175))
        self.assertFalse(eva['explicit'])
        shared['opinions'] = {'oskar': {'signal': 'liked', 'weight': 2, 'item': {}, 'flags': []}}
        self.s.read_abandoned(entries)
        oskar = self.s.person_entries({'entries': entries}, 'oskar')[-1]
        self.assertEqual((oskar['signal'], oskar['weight']), ('liked', 2))
        self.assertNotIn('abandon', oskar)

    def test_prompt_never_lists_disliked_title_as_loved(self):
        (self.data / 'taste.json').write_text(json.dumps({'people': {'eva': {}, 'oskar': {}}}))
        dislike = self.entry(10, 'disliked', -3)
        dislike.update(who='told', flags=['told'])
        shared = self.entry(11, 'loved', 3, 'oskar')
        shared.update(who='tv', opinions={'oskar': {'signal': 'disliked', 'weight': -3}})
        text = self.s.profile_text({'entries': [dislike, shared], 'genres': {}, 'searches': [], 'likes': ''})
        self.assertNotIn('Eva loves: Invented 10', text)
        self.assertNotIn('Oskar loves: Invented 11', text)

    def test_prompt_never_turns_neutral_stops_into_negative_instructions(self):
        self.facets()
        anchor = self.entry(1, 'loved', 3)
        related = self.entry(10)
        self.s.read_abandoned([anchor, related])
        self.assertEqual((related['abandon'], related['weight']), ('related', 0))
        unsure = self.entry(12, weight=0, abandon='unsure')
        pending = self.entry(99, weight=0, abandon='pending')
        far = self.entry(11, weight=-.5, abandon='far')
        deliberate = self.entry(20, weight=-1.2, deliberate=True)
        for who in ('owner', 'tv', 'both'):
            with self.subTest(who=who):
                entries = copy.deepcopy([related, unsure, pending, far, deliberate])
                for e in entries:
                    e.update(who=who, wsrc='trakt')
                text = self.s.profile_text({'entries': entries, 'genres': {}, 'searches': [], 'likes': ''})
                for neutral in (related, unsure, pending):
                    self.assertNotIn(neutral['title'], text)
                self.assertIn(far['title'], text)
                self.assertIn(deliberate['title'], text)
                self.assertIn('avoid similar', text)

    def test_export_status_choice_is_distinct_from_progress_inference(self):
        export = self.data / 'export.json'
        export.write_text(json.dumps({'dropped': [{'movie': {'title': 'Invented Lantern',
            'ids': {'tmdb': 10}}}], 'watched': [
            {'show': {'title': 'Invented Season', 'ids': {'tmdb': 11}},
             'episodes_played': 1, 'last_watched_at': '2026-01-01', 'aired_episodes': 20},
            {'movie': {'title': 'Invented Clock', 'ids': {'tmdb': 12}}, 'last_watched_at': '2026-10-01'}]}))
        rows = {r.title: r for r in parse('trakt', export)}
        self.assertEqual(rows['Invented Lantern'].signal, 'not_for_us')
        self.assertEqual(rows['Invented Season'].signal, 'abandon')

    def test_inferred_import_stop_can_still_be_read(self):
        self.facets()
        for src in ('netflix', 'trakt'):
            entries = [self.entry(1, 'loved', 3)]
            self.s.merge_imports(entries, {}, NOW, [{**self.imported('abandon'), 'src': src, 'conf': .4}])
            self.s.read_abandoned(entries)
            self.assertEqual((entries[-1]['abandon'], entries[-1]['weight']), ('related', 0))

    def test_legacy_trakt_status_import_retains_rejection(self):
        self.facets()
        entries = [self.entry(1, 'loved', 3)]
        self.s.merge_imports(entries, {}, NOW, [{**self.imported('abandon'), 'src': 'trakt', 'conf': .8}])
        self.s.read_abandoned(entries)
        self.assertLess(entries[-1]['weight'], 0)
        self.assertTrue(entries[-1]['deliberate'])

    def test_format5_pmdb_drop_roundtrip_recovers_source_rejection(self):
        from skald.__main__ import execute, parser
        from skald.engine import Engine, plain
        self.facets()
        drop = {'id': 'tmdb:10', 'type': 'movie', 'title': 'Invented Lantern', 'year': 2020,
                'person': 'eva', 'src': 'sync:pmdb', 'dropped': True,
                'signal': 'abandoned', 'weight': -1.5, 'at': NOW * 1000}
        (self.data / 'imports.jsonl').write_text(json.dumps({**self.imported('like'), 't': '2026-01-01'}) + '\n')
        fresh = self.s.build_profile([], [], [drop], remote=False)
        legacy = copy.deepcopy(fresh)
        cached = legacy['entries'][0]
        for field in self.s.STOP_FIELDS:
            cached.pop(field, None)
        cached.update(signal='interest', weight=.5, abandon='related')
        legacy['entries'].append(self.entry(1, 'loved', 3))
        (self.data / '.skald-format').write_text(json.dumps({'format': 5, 'release': '1.7.1'}))
        (self.data / 'profile.json').write_text(json.dumps(plain(legacy)))
        (self.data / 'facets.json').write_text(json.dumps({'items': self.s._facets}))
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with redirect_stdout(io.StringIO()), Engine(self.data) as engine:
            with patch('skald.__main__.PEOPLE', ['eva', 'oskar']):
                args = parser().parse_args(['taste', 'show', '--person', 'eva', '--json'])
            result = execute(args, engine, None, None)
            own = next(e for e in result['entries'] if e['key'] == 'tmdb:10')
            self.assertEqual(own['signal'], 'abandoned')
            self.assertAlmostEqual(own['weight'], -1.2)
            self.assertEqual(own['weight'], fresh['entries'][0]['weight'])
            repaired = next(e for e in engine._profile['entries'] if e['key'] == 'tmdb:10')
            self.assertTrue(repaired['deliberate'])
            self.assertNotIn('abandon', repaired)
            engine._refresh()
            self.assertEqual(engine.profile('eva'), result)
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_newer_trakt_drop_survives_intake_over_existing_like(self):
        from skald.__main__ import execute, parser
        from skald.engine import Engine
        self.facets()
        (self.data / 'facets.json').write_text(json.dumps({'items': self.s._facets}))
        (self.data / 'imports.jsonl').write_text(json.dumps({**self.imported('like'), 't': '2026-01-01'}) + '\n')
        export = self.data / 'export.json'
        export.write_text(json.dumps({'dropped': [{'movie': {'title': 'Invented Lantern',
            'year': 2020, 'ids': {'tmdb': 10}}, 'listed_at': '2026-10-10'}]}))
        state = self.data / 'intake-state'
        with redirect_stdout(io.StringIO()), Engine(self.data) as engine:
            self.assertEqual(engine.profile('eva')['entries'][0]['signal'], 'liked')
            with patch('skald.__main__.PEOPLE', ['eva', 'oskar']):
                args = parser().parse_args(['import', 'trakt', str(export), '--person', 'eva',
                                           '--yes', '--max-ai-usd', '0', '--json'])
            with patch('skald.intake.PEOPLE', ['eva', 'oskar']):
                result = execute(args, engine, state, None)
            self.assertEqual([e['action'] for e in result['events']], ['not_for_us'])
            self.assertEqual(result['merge']['inferred_dropped'], 0)
            self.assertEqual(result['merge']['conflicts_existing'], 1)
            self.assertEqual(result['added'], 1)
            own = engine.profile('eva')['entries'][0]
            self.assertEqual(own['signal'], 'disliked')
            self.assertLess(own['weight'], 0)
            engine._refresh()
            self.assertEqual(engine.profile('eva')['entries'][0], own)

    def test_ambiguous_legacy_trakt_import_keeps_existing_household_reading(self):
        self.facets()
        path = self.data / 'imports.jsonl'
        watched = [{'id': 'tmdb:10', 'type': 'movie', 'person': 'eva', 'signal': 'watched',
                    'weight': 2, 'title': 'Invented Lantern', 'at': (NOW - 10 * 86400) * 1000}]
        for conf in (.2, .4, .5):
            with self.subTest(conf=conf):
                raw = {**self.imported('abandon'), 'src': 'trakt', 'conf': conf}
                path.write_text(json.dumps(raw) + '\n')
                before = path.read_bytes()
                for _ in range(2):
                    profile = self.s.build_profile([], [], watched)
                    own = self.s.person_entries(profile, 'eva')[0]
                    self.assertEqual((own['signal'], own['weight']), ('watched', 2))
                    self.assertFalse(own['deliberate'])
                # Cached legacy readings have no original weight to reinterpret safely.
                for reading, signal, weight in (('related', 'interest', .5), ('unsure', 'abandoned', 0),
                                                 ('far', 'abandoned', -.2)):
                    cached = self.entry(10, signal, weight, abandon=reading)
                    cached.update(who='import', flags=['import', 'trakt'])
                    entries = json.loads(json.dumps([cached]))
                    self.s.read_abandoned(entries)
                    self.assertEqual(entries, [cached])
                self.assertEqual(path.read_bytes(), before)

    def test_interview_asks_unsure_before_related_and_skips_pending(self):
        self.facets()
        entries = [self.entry(10), self.entry(11), self.entry(99), self.entry(12), self.entry(13)]
        for e, reading in zip(entries, ('related', 'unsure', 'pending', 'related', 'unsure')):
            e.update(abandon=reading, weight=0)
        self.s._profiles['household'] = {'entries': entries}
        with patch.object(self.s, 'get_model', return_value=None), \
                patch.object(self.s, 'interview_rank', return_value=[]), \
                patch.object(self.s, 'title_view', side_effect=lambda key, kind, person: {'title': key}):
            anchors = self.s.interview_next('eva')['anchors']
        self.assertEqual([a['key'] for a in anchors], ['tmdb:11', 'tmdb:13', 'tmdb:10'])

    def test_interview_refreshes_when_pending_stop_gets_metadata(self):
        self.facets()
        entries = [self.entry(1, 'loved', 3), self.entry(99)]
        self.s.read_abandoned(entries)
        self.s._profiles['household'] = {'entries': entries}
        with patch.object(self.s, 'get_model', return_value=None), \
                patch.object(self.s, 'interview_rank', return_value=[]), \
                patch.object(self.s, 'title_view', side_effect=lambda key, kind, person: {'title': key}):
            self.assertEqual(self.s.interview_next('eva')['anchors'], [])
            self.s._facets['tmdb:99'] = copy.deepcopy(self.s._facets['tmdb:10'])
            self.s.read_abandoned(entries)
            self.assertEqual([a['key'] for a in self.s.interview_next('eva')['anchors']], ['tmdb:99'])

    def test_stop_facet_corrections_invalidate_profile_hash(self):
        self.facets()
        entries = [self.entry(1, 'loved', 3), self.entry(11)]
        self.s.read_abandoned(entries)
        self.s._profiles['household'] = {'entries': entries}
        before = self.s.taste_hash('household', [])
        self.s._facets['tmdb:11']['d'] = [[1, 'Invented Director']]
        self.assertNotEqual(self.s.taste_hash('household', []), before)

    def test_stop_explanations_use_fixed_locale_strings(self):
        expected = {'en': ('Stopped, but close to titles you watched', 'Not enough information yet'),
                    'de': ('Abgebrochen, aber nah an Gesehenem', 'Noch zu wenig Infos'),
                    'sr-latn': ('Prekinuto, ali blisko naslovima koje ste gledali', 'Još nema dovoljno informacija'),
                    'sr-cyrl': ('Прекинуто, али блиско насловима које сте гледали', 'Још нема довољно информација')}
        for lang, texts in expected.items():
            for reading, text in zip(('related', 'pending'), texts):
                with self.subTest(lang=lang, reading=reading), patch.object(self.s, 'LANG', lang):
                    out = commands.explain_text(self.s, {'key': 'tmdb:10', 'kind': 'movie'}, 'eva',
                        {'per_person': {'eva': {'abandon': reading}}})
                    self.assertIn(text, out)

    def test_pmdb_chosen_drop_overrides_a_plain_play_for_that_member(self):
        self.facets()
        watch = {'id': 'tmdb:10', 'type': 'movie', 'history_by': ['eva'],
                 'played': True, 'at': (NOW - 20 * 86400) * 1000}
        drop = {'id': 'tmdb:10', 'type': 'movie', 'person': 'eva', 'src': 'sync:pmdb',
                'dropped': True, 'signal': 'abandoned', 'weight': -1.5, 'at': NOW * 1000}
        liked = {**drop, 'person': 'oskar', 'dropped': False, 'signal': 'liked', 'weight': 2, 'src': 'trakt'}
        profile = self.s.build_profile([watch], [], [drop, liked], remote=False)
        self.assertLess(self.s.person_entries(profile, 'eva')[0]['weight'], 0)
        self.assertEqual(self.s.person_entries(profile, 'oskar')[0]['signal'], 'liked')

    def test_deliberate_import_survives_a_merge_into_a_shared_play(self):
        self.facets()
        shared = self.entry(10, 'watched', 2, 'together')
        shared.update(who='tv', item={'played': True})
        entries = [shared, self.entry(1, 'loved', 3)]
        self.s.merge_imports(entries, {shared['key']: shared}, NOW, [self.imported('abandon')])
        self.s.read_abandoned(entries)
        own = self.s.person_entries({'entries': entries}, 'eva')[0]
        self.assertLess(own['weight'], 0)
        self.assertTrue(own['deliberate'])
        self.assertNotIn('abandon', own)
        self.assertGreater(self.s.person_entries({'entries': entries}, 'oskar')[0]['weight'], 0)

    def test_stremio_cached_liked_catalog_remaps_when_unavailable(self):
        (self.data / 'stremio-eva.history.json').write_text(json.dumps({'version': 5, 'library': {},
            'likes': {'movie:tt10': {'id': 'tmdb:10', 'type': 'movie', 'rating': 8,
                                   'signal': 'loved', 'weight': 3}}, 'items': []}))
        with patch.object(self.s, '_resolve_auth', return_value='synthetic'), \
                patch.object(self.s, '_stremio_http', side_effect=[{'result': []}, OSError('offline')]):
            doc, _, stats = self.s._connector_stremio({'auth': 'file:eva.token', 'person': 'eva'}, NOW, readonly=True)
        self.assertFalse(stats['likes_available'])
        self.assertEqual((doc['items'][0]['signal'], doc['items'][0]['weight']), ('liked', 2))

    def test_shared_stop_reading_is_idempotent(self):
        self.facets()
        entries = [self.entry(1, 'loved', 3), self.entry(10, person='together')]
        self.s.read_abandoned(entries)
        self.assertEqual(list(entries[-1]['abandon_members']), ['eva', 'oskar'])
        before = copy.deepcopy(entries)
        self.s.read_abandoned(entries)
        self.assertEqual(entries, before)

    def test_forget_and_erase_remove_optional_stop_evidence(self):
        from skald import erase
        self.facets()
        entries = [self.entry(1, 'loved', 3), self.entry(10, person='together')]
        self.s.read_abandoned(entries)
        self.assertIn('eva', entries[-1]['abandon_members'])
        self.s._profiles['household'] = {'entries': entries}
        cached = self.data / 'profile.json'
        cached.write_text(json.dumps({'entries': entries}))
        plan = commands.preview(self.s, '', viewer='oskar', answer={'op': 'member_forget', 'person': 'eva'})
        plan['source'] = 'cli'
        commands.apply(self.s, plan)
        self.assertFalse(cached.exists())
        self.assertFalse(self.s._profiles)
        cached.write_text(json.dumps({'entries': entries}))
        self.s._profiles['household'] = {'entries': entries}
        erase.apply(self.s, {**erase.preview(self.s), 'source': 'cli', 'confirm': 'ERASE'})
        self.assertFalse(cached.exists())
        self.assertFalse(self.s._profiles)


if __name__ == '__main__':
    unittest.main()
