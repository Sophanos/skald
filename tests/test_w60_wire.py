"""Search shelf wiring on a temporary synthetic household."""
import json
from pathlib import Path
from unittest.mock import patch
import test_w54_commands as base

class Wire(base.Commands):
    def test_phrases_and_near_misses(self):
        self.s.shelf_change('ana', 'add', text='classic', name='Sunday Classic', when='sun 05-12', apply=True)
        for query, op in [('every sunday morning a classic for us', 'shelf_add'),
                          ('weekday evenings something light for me', 'shelf_add'),
                          ('make me a row of heist films', 'shelf_add'),
                          ('stop the sunday shelf', 'shelf_stop'), ('show 6 shelves', 'shelf_count'),
                          ('fewer shelves', 'shelf_count')]:
            self.assertEqual(self.c.parse_shelf(query, self.s, 'ana')['op'], op)
        for query in ('Sunday Bloody Sunday', 'Heist', 'Stop Making Sense', 'Yes Man'):
            self.assertEqual(self.c.parse_shelf(query), self.c.NONE)

    def test_schedule_confirm_stop_undo_and_about(self):
        plan = self.c.preview(self.s, 'every sunday morning a classic for us', 'ana')
        self.assertEqual(plan['shelf']['scope'], 'together')
        self.assertEqual(plan['shelf']['when'], {'days': ['sun'], 'hours': [5, 12]})
        self.assertFalse(self.s.shelf_list()['wishes'])
        self.c.apply(self.s, plan)
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            self.assertIn('Sunday', self.c.special(self.s, 'my settings', 'movie')['metas'][0]['description'])
        stop = self.c.preview(self.s, 'stop the sunday shelf', 'ana')
        self.c.apply(self.s, stop)
        self.assertFalse(self.s.shelf_list()['wishes'])
        self.c.undo(self.s, ['together'])
        self.assertEqual(len(self.s.shelf_list()['wishes']), 1)

    def test_count_full_and_shared(self):
        plan = self.c.preview(self.s, 'show 6 shelves', 'ana')
        self.assertEqual(plan['name'], 'Count 6 shelves for Ana; always on')
        self.c.apply(self.s, plan)
        self.assertEqual(self.s.shelf_list('ana')['counts']['ana'], 6)
        self.c.undo(self.s, ['ana'])
        self.assertEqual(self.s.shelf_list('ana')['counts']['ana'], 4)
        for i in range(4):
            self.s.shelf_change('ana', 'add', text='classic', name=str(i), apply=True)
        with self.assertRaisesRegex(ValueError, 'stop'):
            self.c.preview(self.s, 'make me a row of heist films', 'ana')
        with self.assertRaisesRegex(ValueError, 'name'):
            self.c.preview(self.s, 'make me a row of heist films', None)

    def test_child_own_shelves_only(self):
        cfg = self.c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        from skald import setup
        setup.publish(Path(self.temp.name), cfg, replace=True)
        self.s.refresh_preferences()
        plan = self.c.preview(self.s, 'make me a row of heist films', 'ana', 'ana')
        self.c.apply(self.s, plan)
        for text in ('every sunday morning a classic for us', 'show 6 shelves for sam'):
            with self.assertRaises(PermissionError):
                self.c.preview(self.s, text, 'ana', 'ana')

    def test_suggestion_confirm_dismiss_and_existing(self):
        now = self.s.time.time()
        records = [{'p': 'ana', 'q': 'cozy', 't': now - i * 7 * 86400} for i in range(3)]
        Path(self.temp.name, 'searches.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            cards = self.c.special(self.s, 'ideas', 'movie')['metas']
            self.assertLessEqual(len(cards), 2)
            self.assertIn('yes ', cards[0]['description'])
            code = cards[0]['description'].split('yes ')[1][:2]
            self.c.special(self.s, 'no '+code, 'movie')
            self.assertEqual(self.c.special(self.s, 'suggest', 'movie')['metas'][0]['name'], 'No suggestions yet')
        Path(self.temp.name, 'suggest-dismissed.json').unlink()
        self.s.shelf_change('together', 'add', text='cozy', apply=True)
        from skald.engine import suggestions
        self.assertEqual(suggestions(self.temp.name, 'ana', self.s.CONFIG)['suggestions'], [])

    def test_search_confirmation_child_filter_and_stale_plan(self):
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            self.s.decide.reset_mock()
            result = self.s.search('movie', 'weekday evenings something light for me', 'en-US')
            self.s.decide.assert_not_called()
            plan = self.c.get_pending(self.s.command_context(), None)
            self.assertIn('weekdays', result['metas'][0]['description'])
            self.assertIn(self.s.CONFIG['region']['tz'], result['metas'][0]['description'])
            self.s.search('movie', 'yes '+self.c.confirm_code(plan), 'en-US')
            self.assertEqual(self.s.shelf_list('ana')['wishes'][0]['text'], 'something light')
        plan = self.c.preview(self.s, 'make me a row of heist films', 'ana')
        self.s.shelf_change('ana', 'add', text='other', apply=True)
        with self.assertRaisesRegex(ValueError, 'preview again'):
            self.c.apply(self.s, plan)

    def test_suggestion_acceptance_and_scheduled_duplicate(self):
        now = self.s.time.time()
        records = [{'p': 'ana', 'q': 'cozy', 't': now-i*7*86400} for i in range(3)]
        Path(self.temp.name, 'searches.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            cards = self.c.special(self.s, 'ideas', 'movie')['metas']
            code = cards[0]['description'].split('yes ')[1][:2]
            self.assertIn('Done:', self.c.special(self.s, 'yes '+code, 'movie')['metas'][0]['name'])
            self.assertEqual(self.c.special(self.s, 'ideas', 'movie')['metas'][0]['name'], 'No suggestions yet')

    def test_child_search_uses_age_filter_and_undo_ownership(self):
        cfg = self.c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        from skald import setup
        setup.publish(Path(self.temp.name), cfg, replace=True)
        self.s.refresh_preferences()
        with self.s.addon_scope('ana'):
            card = self.s.search('movie', 'make me a row of heist films', 'en-US')['metas'][0]
            code = card['description'].split('yes ')[1][:2]
            self.s.search('movie', 'yes '+code, 'en-US')
            self.assertEqual(len(self.s.shelf_list('ana')['wishes']), 1)
            self.s.search('movie', 'undo', 'en-US')
            self.assertFalse(self.s.shelf_list('ana')['wishes'])
        self.s.shelf_change('together', 'add', name='Sunday Classic', text='classic', when='sun 05-12', apply=True)
        with self.assertRaises(PermissionError):
            self.c.preview(self.s, 'stop the sunday shelf', 'ana', 'ana')

    def test_api_child_shelf_preview_confirm(self):
        cfg = self.c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        from skald import setup
        setup.publish(Path(self.temp.name), cfg, replace=True)
        req = {'viewer': 'ana', 'client': {'name': 'child'}, 'body': {'in': {'query': 'make me a row of heist films'}}}
        result = self.c.api_preview(self.s, req)
        req['body'] = {'plan': result['preview']['plan']}
        self.c.api_apply(self.s, req)
        self.assertEqual(len(self.s.shelf_list('ana')['wishes']), 1)

    def test_suggestion_expiry(self):
        now = self.s.time.time()
        records = [{'p': 'ana', 'q': 'cozy', 't': now-i*7*86400} for i in range(3)]
        Path(self.temp.name, 'searches.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            card = self.c.special(self.s, 'ideas', 'movie')['metas'][0]
            code = card['description'].split('yes ')[1][:2]
            with patch.object(self.s.time, 'time', return_value=now+601):
                self.c.special(self.s, 'yes '+code, 'movie')
            self.assertFalse(self.s.shelf_list('ana')['wishes'])

    def test_undo_keeps_later_shelf_edits(self):
        self.c.apply(self.s, self.c.preview(self.s, 'make me a row of heist films', 'ana'))
        self.s.shelf_change('ana', 'replace', slot=1, text='newer wish', apply=True)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.c.undo(self.s, ['ana'])
        self.assertEqual(self.s.shelf_list('ana')['wishes'][0]['text'], 'newer wish')

    def test_new_preview_replaces_suggestions_and_rejects_bad_schedule(self):
        now = self.s.time.time()
        records = [{'p': 'ana', 'q': 'cozy', 't': now-i*7*86400} for i in range(3)]
        Path(self.temp.name, 'searches.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            card = self.c.special(self.s, 'ideas', 'movie')['metas'][0]
            code = card['description'].split('yes ')[1][:2]
            self.c.special(self.s, 'make me a row of heist films', 'movie')
            self.assertFalse(self.s.__dict__.get('_command_suggestions', {}).get(None))
        with self.assertRaises(ValueError):
            self.c.preview(self.s, '', 'ana', answer={'op': 'shelf_add', 'kind': None, 'values': ['classic', 'sun'], 'person': None})

    def test_old_command_journal_undo_upgrades_history_format(self):
        cfg = self.c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'hide': ['anime']}
        from skald import setup
        setup.publish(Path(self.temp.name), cfg, replace=True)
        Path(self.temp.name, 'commands.json').write_text(json.dumps({
            'ana': [{'before': None, 'event': None, 'name': 'Hide anime'}]}))
        marker = Path(self.temp.name, '.skald-format')
        marker.write_text(json.dumps({'format': 1, 'release': self.s.VERSION}))
        out = base.io.StringIO()
        self.assertEqual(base.main(['command', '--person', 'ana', '--undo', '--json'], out=out), 0, out.getvalue())
        self.assertEqual(json.loads(marker.read_text())['format'], 5)
        self.s.refresh_preferences()
        self.assertNotIn('prefs', self.s.CONFIG['taste']['people']['ana'])
        self.assertTrue(self.c.journal(self.s)['ana'][0]['undone'])

    def test_old_command_journal_undo_preserves_current_format(self):
        cfg = self.c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'hide': ['anime']}
        from skald import setup
        setup.publish(Path(self.temp.name), cfg, replace=True)
        Path(self.temp.name, 'commands.json').write_text(json.dumps({
            'ana': [{'before': None, 'event': None, 'name': 'Hide anime'}]}))
        marker = Path(self.temp.name, '.skald-format')
        from skald.migrate import FORMAT
        marker.write_text(json.dumps({'format': FORMAT, 'release': self.s.VERSION}))
        out = base.io.StringIO()
        self.assertEqual(base.main(['command', '--person', 'ana', '--undo', '--json'], out=out), 0, out.getvalue())
        self.assertEqual(json.loads(marker.read_text())['format'], FORMAT)
        self.s.refresh_preferences()
        self.assertNotIn('prefs', self.s.CONFIG['taste']['people']['ana'])
        self.assertEqual([e for e in self.c.journal(self.s)['ana'] if not e.get('undone')], [])
