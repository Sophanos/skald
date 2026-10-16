"""Synthetic experience-tag lifecycle and explicit retag requests."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import load_server, close_server
from skald import commands, members, setup
from skald.__main__ import main


class TagTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        setup.publish(self.data, members.fresh_config(['eva', 'oskar']))
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'OR_KEY', 'offline'))
        self.enterContext(patch.object(self.s, '_defer_enrichment', False))
        self.clock = self.enterContext(patch.object(self.s.time, 'time', return_value=1800000000.))
        self.enterContext(redirect_stdout(io.StringIO()))

    def record(self, i, **extra):
        rec = dict(kind='movie', title=f'Cloud Lantern {i}', o='An invented puzzle.',
                   y='2020', g=['Mystery'], k=[[1, 'Puzzle']], t=[])
        rec.update(extra)
        self.s._facets[f'tmdb:{i}'] = rec
        return rec

    def run_tags(self, reply, pairs=None, **kwargs):
        with patch.object(self.s, 'llm_text', return_value=reply) as call:
            done = self.s.tag_experiences(pairs if pairs is not None else
                [(k, r['kind']) for k, r in self.s._facets.items()], fetch_facets=False, **kwargs)
        return done, call

    def test_tag_lifecycle_distinguishes_missing_partial_invalid_and_complete(self):
        s = self.s
        self.record(1)
        self.record(2, x={'dread': 3})
        self.record(3, x={'dread': '3', 'feel_good': 0, 'heist': True, 'cozy_case': 3})
        self.record(4, x={'made_up': 2, 'heist': 2})
        self.record(5, x={})
        self.record(6, x={'dread': 2, 'heist': 3})
        self.record(7)
        self.assertEqual([s.tag_state(r)['status'] for r in s._facets.values()],
                         ['missing', 'partial', 'invalid', 'invalid', 'invalid', 'ok', 'missing'])
        for who in ('eva', 'oskar'):
            self.assertEqual(s.frec('tmdb:3', 'movie')['x'], {'cozy_case': 3}, who)
            self.assertEqual(s.frec('tmdb:4', 'movie')['x'], {'heist': 2}, who)
        done, _ = self.run_tags('1|cozy_case:3\n2|cozy_case:3,heist:2\n3|dread:30,heist:2\n'
                                '4|made_up:3,heist:3\n5|cozy_case:3,heist:2\n'
                                '6|dread:2,heist:3\n7|untaggable')
        self.assertEqual(done, 6)
        self.assertEqual([s.tag_state(r)['status'] for r in s._facets.values()],
                         ['partial', 'ok', 'invalid', 'invalid', 'ok', 'ok', 'untaggable'])
        self.assertEqual(s.frec('tmdb:3', 'movie')['x'], {'heist': 2})
        state = s._facets['tmdb:1']['x_state']
        self.assertEqual(state['schema'], s.TAG_SCHEMA)
        self.assertEqual(state['fingerprint'], s.tag_fingerprint(s._facets['tmdb:1']))

    def test_read_validation_is_persisted_and_bad_values_never_score(self):
        s = self.s
        rec = self.record(1, x={'dread': [], 'heist': 2, 'unknown': 3})
        (self.data/'facets.json').write_text(json.dumps({'items': s._facets}))
        s._facets.clear()
        s.load_facets()
        self.assertEqual(s._facets['tmdb:1']['x'], {'heist': 2})
        self.assertEqual(s.tag_state(s._facets['tmdb:1'])['status'], 'invalid')
        self.assertEqual(s.facet_pairs(rec)[-1], ('x:heist', s.EXPERIENCES['heist'][0]))
        s.save_facets()
        self.assertEqual(json.loads((self.data/'facets.json').read_text())['items']['tmdb:1']['x'], {'heist': 2})

    def test_retry_bound_cooldown_and_fair_queue(self):
        s = self.s
        for i in range(1, 4):
            self.record(i)
        self.run_tags('1|unknown:3', max_batches=1)
        _, call = self.run_tags('1|unknown:3')
        call.assert_not_called()
        for _ in range(s.TAG_ATTEMPTS - 1):
            self.clock.return_value += s.TAG_COOLDOWN
            self.run_tags('1|unknown:3')
        self.clock.return_value += s.TAG_COOLDOWN
        _, call = self.run_tags('1|unknown:3')
        call.assert_not_called()
        self.record(4)
        done, call = self.run_tags('1|dread:3,heist:2', max_batches=1)
        self.assertEqual(done, 1)
        self.assertIn('Cloud Lantern 4', call.call_args.args[1])

    def test_partial_is_kept_and_retried_only_once(self):
        s = self.s
        self.record(1)
        self.run_tags('1|dread:3')
        self.clock.return_value += s.TAG_COOLDOWN
        self.run_tags('1|dread:3')
        self.clock.return_value += s.TAG_COOLDOWN
        _, call = self.run_tags('1|dread:3')
        call.assert_not_called()
        self.assertEqual(s.frec('tmdb:1', 'movie')['x'], {'dread': 3})
        self.assertEqual(s.tag_state(s._facets['tmdb:1'])['status'], 'partial')

    def test_budget_and_no_key_do_not_spend_attempts(self):
        s = self.s
        before = copy.deepcopy(self.record(1, x={}))
        with patch.object(s, 'llm_text', side_effect=s.BudgetExceeded('fixture cap')):
            self.assertEqual(s.tag_experiences([('tmdb:1', 'movie')]), 0)
        self.assertEqual(s._facets['tmdb:1'], before)
        with patch.object(s, 'OR_KEY', ''):
            _, call = self.run_tags('1|dread:3,heist:2')
        call.assert_not_called()
        self.assertEqual(s._facets['tmdb:1'], before)
        self.assertFalse(s._tagging)

    def test_manual_tags_and_inflight_manual_change_are_kept(self):
        s = self.s
        self.record(1, x={'heist': 3}, x_state={'status': 'manual'})
        self.record(2)
        def respond(*args):
            s._facets['tmdb:2'].update(x={'cozy_case': 3}, x_state={'status': 'manual'})
            return '1|dread:3,heist:2'
        with patch.object(s, 'llm_text', side_effect=respond):
            s.tag_experiences([('tmdb:1', 'movie'), ('tmdb:2', 'movie')])
        self.assertEqual(s.frec('tmdb:1', 'movie')['x'], {'heist': 3})
        self.assertEqual(s.frec('tmdb:2', 'movie')['x'], {'cozy_case': 3})

    def test_schema_and_prompt_inputs_make_tags_stale(self):
        s = self.s
        self.record(1)
        self.run_tags('1|dread:3,heist:2')
        rec = s._facets['tmdb:1']
        self.assertFalse(s.tag_state(rec)['stale'])
        with patch.object(s, 'TAG_SCHEMA', s.TAG_SCHEMA + 1):
            self.assertTrue(s.tag_state(rec)['stale'])
            self.clock.return_value += s.TAG_COOLDOWN
            self.assertEqual(self.run_tags('1|cozy_case:3,heist:2')[0], 1)
        for field, value in (('title', 'Other Lantern'), ('y', '2021'), ('kind', 'tv'),
                             ('g', ['Drama']), ('k', [[2, 'Other']]), ('t', ['Other']), ('o', 'Changed')):
            changed = {**rec, field: value}
            self.assertNotEqual(s.tag_fingerprint(rec), s.tag_fingerprint(changed), field)
        self.assertEqual(s.tag_fingerprint(rec), s.tag_fingerprint({**rec, 'a': [[9, 'Actor']]}))

    def test_batch_cap_and_explicit_queue_reaches_worker_and_keeps_manual(self):
        from skald import tags
        s = self.s
        for i in range(1, 30):
            self.record(i, x={'heist': 3, 'dread': 2})
        self.record(30, x={'heist': 3}, x_state={'status': 'manual'})
        result = tags.queue(s, stale=True)
        self.assertEqual(result['queued'], 29)
        self.assertEqual(result['counts']['manual'], 1)
        done, call = self.run_tags('\n'.join(f'{i}|heist:3,dread:2' for i in range(1, 26)), pairs=[], max_batches=1)
        self.assertEqual(done, 25)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(len(tags.pending(s)), 4)
        self.assertEqual(s.frec('tmdb:30', 'movie')['x'], {'heist': 3})

    def test_cli_json_counts_and_commander_op(self):
        from skald import tags
        s = self.s
        self.record(1, x={'heist': 3, 'dread': 2})
        self.record(2, x={'heist': 3}, x_state={'status': 'manual'})
        (self.data/'facets.json').write_text(json.dumps({'items': s._facets}))
        out = io.StringIO()
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_CONFIG': ''}):
            self.assertEqual(main(['tags', '--retag', 'Cloud Lantern 1', '--json'], out=out), 0, out.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual(result['queued'], 1)
        self.assertEqual(result['counts']['manual'], 1)
        self.assertIn('tags_retag', commands.OPS)
        change = dict(op='tags_retag', kind=None, values=[], person=None, language='de')
        plan = commands.preview(s, 'fixture', 'eva', answer=change)
        self.assertEqual(plan['name'], s.t('tags.preview', lang='de', count=1))
        result = commands.apply(s, plan)
        self.assertEqual(result['tags']['queued'], 1)
        with self.assertRaises(PermissionError):
            commands.preview(s, 'fixture', s.GUEST, answer=change)
        with patch.object(s, 'adult_people', return_value=['oskar']):
            with self.assertRaises(PermissionError):
                commands.preview(s, 'fixture', 'eva', answer=change)
        self.assertEqual(tags.queue(s, title='Cloud Lantern 2')['queued'], 0)

    def test_partial_retry_that_fails_is_not_repeated(self):
        self.record(1)
        self.run_tags('1|dread:3')
        self.clock.return_value += self.s.TAG_COOLDOWN
        self.run_tags('bad response')
        self.clock.return_value += self.s.TAG_COOLDOWN
        _, call = self.run_tags('bad response')
        call.assert_not_called()
        self.assertEqual(self.s.frec('tmdb:1', 'movie')['x'], {'dread': 3})

    def test_untaggable_stops_until_inputs_change_and_input_race_is_discarded(self):
        self.record(1)
        self.run_tags('1|untaggable')
        self.clock.return_value += self.s.TAG_COOLDOWN
        _, call = self.run_tags('1|dread:3,heist:2')
        call.assert_not_called()
        self.s._facets['tmdb:1']['o'] = 'A new invented story.'
        self.assertEqual(self.run_tags('1|dread:3,heist:2')[0], 1)
        self.s._facets['tmdb:1']['o'] = 'A different invented story.'
        self.clock.return_value += self.s.TAG_COOLDOWN
        def respond(*args):
            self.s._facets['tmdb:1']['o'] = 'Changed during request.'
            return '1|cozy_case:3,heist:2'
        with patch.object(self.s, 'llm_text', side_effect=respond):
            self.assertEqual(self.s.tag_experiences([('tmdb:1', 'movie')]), 0)
        self.assertTrue(self.s.tag_state(self.s._facets['tmdb:1'])['stale'])

    def test_frozen_missing_only_gate_rejects_retag_of_any_labelled_record(self):
        from eval.variants import experience_overlay
        s = self.s
        for old in ({}, {'heist': 2}, {'heist': 2, 'dread': 3}):
            self.record(1, x=old)
            with self.assertRaisesRegex(ValueError, 'missing experience tags only'):
                with experience_overlay(s, {'tmdb:1': {'kind': 'movie', 'x': {'heist': 3}}}):
                    self.fail('labelled record entered E1')
        self.record(1)
        with experience_overlay(s, {'tmdb:1': {'kind': 'movie', 'x': {'heist': 3}}}):
            self.assertEqual(s.frec('tmdb:1', 'movie')['x'], {'heist': 3})
        self.assertNotIn('x', s._facets['tmdb:1'])

    def test_retag_refresh_publishes_model_and_percentages_together(self):
        from skald import tags
        from test_stop_report import entry
        s = self.s
        entries = [entry(i) for i in range(1, 21)]
        p = {'entries': entries, 'seen': {e['key'] for e in entries}, 'hash': 'fixture'}
        for i in range(1, 251):
            self.record(i, x={'dread': 3, 'heist': 2} if i <= 20 else {'feel_good': 3, 'banter_duo': 2})
        with patch.object(s, '_defer_enrichment', True):
            s._profiles['household'] = p
            old = s.get_model()
        old_aff, old_bg = copy.deepcopy(old['paff']), copy.deepcopy(old['pbg'])
        tags.queue(s, title='Cloud Lantern 21')
        with patch.object(s, 'llm_text', return_value='1|dread:3,heist:2'):
            self.assertEqual(s.tag_experiences([], max_batches=1), 1)
        self.assertIs(s.get_model(False), old)
        self.assertEqual(old['paff'], old_aff)
        self.assertEqual(old['pbg'], old_bg)
        with patch.object(s, '_feed_started', True), patch.object(s, 'refresh_profiles', return_value={}), \
                patch.object(s, 'build_feeds'), patch.object(s, '_defer_enrichment', True):
            s.feed_pass(s.local_now(), {})
            fresh = s.get_model(False)
            expected = s.build_model(p, enrich=False)
        self.assertIsNot(fresh, old)
        self.assertEqual(fresh['tag_version'], s._tag_version)
        self.assertEqual(fresh['paff'], expected['paff'])
        self.assertEqual(fresh['pbg'], expected['pbg'])
        self.assertNotEqual(fresh['pbg']['eva'], old_bg['eva'])

    def test_queue_has_no_person_fields_and_household_erase_removes_it(self):
        from skald import erase, tags
        self.record(1)
        tags.queue(self.s, title='Cloud Lantern 1')
        text = (self.data/tags.FILE).read_text()
        self.assertNotIn('eva', text)
        self.assertNotIn('oskar', text)
        plan = {**erase.preview(self.s), 'source': 'cli', 'confirm': 'ERASE'}
        erase.apply(self.s, plan)
        self.assertFalse((self.data/tags.FILE).exists())

    def test_partial_after_invalid_response_still_gets_one_retry(self):
        self.record(1)
        self.run_tags('1|unknown:3')
        self.clock.return_value += self.s.TAG_COOLDOWN
        self.run_tags('1|heist:3')
        self.clock.return_value += self.s.TAG_COOLDOWN
        done, call = self.run_tags('1|heist:3,dread:2')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(done, 1)

    def test_commander_confirmation_queues_without_calling_the_model_tagger(self):
        from skald import tags
        s = self.s
        self.record(1)
        change = dict(op='tags_retag', kind='title', values=['Cloud Lantern 1'], person=None, language='de')
        with patch.object(commands, 'context', return_value=('w25', None, 'eva')), \
                patch.object(s, 'decide', return_value={'answer': change}) as decide, \
                patch.object(s, 'llm_text') as tagger:
            commands.special(s, 'Bitte die Erlebnis-Tags neu prüfen', 'movie', routed=True)
            decide.assert_called_once()
            self.assertEqual(decide.call_args.args[0], 'parse_command')
            self.assertEqual(tags.pending(s), {})
            plan = commands.get_pending(s, 'w25')
            self.assertEqual(plan['change']['op'], 'tags_retag')
            result = commands.special(s, 'yes ' + commands.confirm_code(plan), 'movie')
            self.assertIn(s.t('tags.queued', lang='de', count=1), result['metas'][0]['description'])
            tagger.assert_not_called()
        self.assertEqual(set(tags.pending(s)), {'tmdb:1'})

    def test_corrupt_queue_uses_fixed_text_and_preserves_existing_file(self):
        from skald import tags
        self.record(1)
        path = self.data/tags.FILE
        path.write_text('{broken')
        with self.assertRaisesRegex(ValueError, self.s.t('tags.queue_error')):
            tags.queue(self.s, title='Cloud Lantern 1')
        self.assertEqual(path.read_text(), '{broken')
        _, call = self.run_tags('1|heist:3,dread:2')
        call.assert_not_called()

    def test_first_partial_at_attempt_bound_gets_exactly_one_final_retry(self):
        self.record(1)
        for _ in range(self.s.TAG_ATTEMPTS - 1):
            self.run_tags('1|unknown:3')
            self.clock.return_value += self.s.TAG_COOLDOWN
        self.run_tags('1|heist:3')
        self.clock.return_value += self.s.TAG_COOLDOWN
        _, call = self.run_tags('1|heist:3')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.s._facets['tmdb:1']['x_state']['attempts'], self.s.TAG_ATTEMPTS + 1)
        self.clock.return_value += self.s.TAG_COOLDOWN
        _, call = self.run_tags('1|heist:3')
        call.assert_not_called()

    def test_served_scores_reasons_and_tags_stay_together_during_retag(self):
        import threading
        from test_stop_report import entry
        s = self.s
        entries = [dict(entry(i, 'eva' if i <= 40 else 'oskar'), explicit=True) for i in range(1, 81)]
        s._profiles['household'] = {'entries': entries, 'seen': {e['key'] for e in entries}, 'hash': 'closure'}
        for i in range(1, 302):
            self.record(i, x={'dread': 3, 'heist': 2} if i <= 80 else {'feel_good': 3, 'banter_duo': 2})
        s._facets['tmdb:301']['k'] = []
        with patch.object(s, '_defer_enrichment', True):
            old = s.get_model()
        before = s.explain('tmdb:301', 'movie')
        for person in ('eva', 'oskar'):
            self.assertEqual(before['per_person'][person]['percentile'], 40)
            self.assertEqual(s.match_note('tmdb:301', person=person, kind='movie'), [])
        old_scores = {p: s.person_score(old, 'tmdb:301', p, 'movie') for p in ('eva', 'oskar')}
        written, resume = threading.Event(), threading.Event()
        replace = s.os.replace
        def pause_write(source, destination):
            if Path(destination) == self.data/'facets.json' and not written.is_set():
                written.set()
                if not resume.wait(5):
                    raise AssertionError('retag write was not resumed')
            return replace(source, destination)
        def response(system, titles, *args):
            return '\n'.join(f'{i}|' + ('dread:3,heist:2' if 'Cloud Lantern 301 ' in line else
                                      'feel_good:3,banter_duo:2')
                             for i, line in enumerate(titles.splitlines(), 1))
        pairs = [('tmdb:301', 'movie')] + [(e['key'], 'movie') for e in entries]
        with patch.object(s, 'llm_text', side_effect=response), patch.object(s.os, 'replace', side_effect=pause_write):
            pending = s._outer.submit(s.tag_experiences, pairs, fetch_facets=False)
            try:
                self.assertTrue(written.wait(5))
                during = s.explain('tmdb:301', 'movie')
                for person in ('eva', 'oskar'):
                    self.assertEqual(during['per_person'][person]['percentile'], 40, person)
                    self.assertEqual(s.match_note('tmdb:301', person=person, kind='movie'), [], person)
                self.assertEqual(during['experiences'], before['experiences'])
            finally:
                resume.set()
                self.assertEqual(pending.result(timeout=5), 81)
        self.assertEqual(s.explain('tmdb:301', 'movie')['experiences'], before['experiences'])
        with patch.object(s, '_defer_enrichment', True):
            fresh = s.get_model()
        after = s.explain('tmdb:301', 'movie')
        self.assertEqual(after['experiences'], {'dread': 3, 'heist': 2})
        for person in ('eva', 'oskar'):
            self.assertEqual(after['per_person'][person]['percentile'], 40, person)
            self.assertEqual(s.match_note('tmdb:301', person=person, kind='movie'), [], person)
            self.assertEqual(s.person_score(old, 'tmdb:301', person, 'movie'), old_scores[person])
        self.assertNotEqual(fresh['paff'], old['paff'])

    def test_weekly_metadata_refresh_keeps_manual_tags_and_lifecycle(self):
        s = self.s
        original = self.record(1, title='Cloud Lantern', x={'dread': 3, 'heist': 2})
        state = dict(status='manual', schema=s.TAG_SCHEMA, fingerprint=s.tag_fingerprint(original), attempts=0)
        original['x_state'] = state
        updated_tags = {'cozy_case': 3, 'heist': 2}
        def tmdb(path, **params):
            if path == 'discover/movie':
                return {'results': [{'id': 1, 'title': 'Cloud Lantern'}]}
            self.assertEqual(path, 'movie/1')
            # A person's edit landing while the external metadata request runs must survive too.
            self.record(1, title='Cloud Lantern', x=updated_tags, x_state=state)
            return {'id': 1, 'title': 'Cloud Lantern', 'overview': 'An updated invented story.',
                    'release_date': '2020-01-01', 'genres': [{'id': 9648, 'name': 'Mystery'}]}
        with patch.object(s, 'tmdb', side_effect=tmdb), patch.object(s, 'llm_text') as tagger:
            s.week_digital(s.LANG)
            refreshed = s.frec('tmdb:1', 'movie')
            self.assertEqual(refreshed['o'], 'An updated invented story.')
            self.assertEqual(refreshed.get('x'), updated_tags)
            self.assertEqual(refreshed.get('x_state'), state)
            s.tag_experiences([('tmdb:1', 'movie')], fetch_facets=False)
            tagger.assert_not_called()
