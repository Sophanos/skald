"""Stage-3 intake regressions; synthetic exports and offline metadata only."""
from decide_kit import write_legacy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from skald.__main__ import main
from skald.engine import Engine
from skald.ingest import parse
from skald.intake import cross_source, write_private


def imported(action, src, who='ben', **kw):
    return {'key': 'tmdb:87000001', 'title': 'Synthetic Beacon', 'year': '2021', 'kind': 'movie',
            'who': who, 'action': action, 'src': src, 'conf': 1., 'n': 1, 't': '2026-10-11', **kw}


class IntakeS3Tests(unittest.TestCase):
    def setUp(self):
        from skald import members
        members.configure(readonly=True)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target in ('urllib.request.urlopen', 'urllib.request.build_opener'):
            blocker = patch(target, side_effect=AssertionError('network forbidden'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def test_trakt_episode_plays_not_show_total(self):
        cases = [('show', [1] * 8, 'seen', 1., 8),
                 ('show', [1, 2] + [1] * 6, 'like', .7, 8),
                 ('movie', [2], 'like', .7, 2)]
        for kind, plays, signal, conf, n in cases:
            with self.subTest(kind=kind, plays=plays):
                row = {kind: {'title': 'Synthetic', 'year': 2020, 'ids': {'tmdb': 1}},
                       'plays': sum(plays), 'last_watched_at': '2026-10-11'}
                if kind == 'show':
                    row['seasons'] = [{'number': 1, 'episodes': [
                        {'number': i, 'plays': p, 'last_watched_at': '2026-10-11'}
                        for i, p in enumerate(plays, 1)]}]
                path = self.root / 'watched.json'
                path.write_text(json.dumps([row]))
                c, = parse('trakt', path)
                self.assertEqual((c.signal, c.conf, c.n), (signal, conf, n))

    def test_trakt_folder_skips_non_titles_and_non_lists(self):
        unsupported = [('lists-lists.json', [{'name': 'Faves', 'likes': 2, 'ids': {'trakt': 1}}]),
                       ('likes.json', [None, 7, {'list': {'name': 'Faves'}}, {'movie': {'title': ''}}]),
                       ('settings.json', {'private': True}), ('account.json', 'not a title list')]
        for name, payload in unsupported:
            (self.root / name).write_text(json.dumps(payload))
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                rows = [{'movie': {'title': 'Synthetic', 'ids': {'tmdb': 1}}, 'plays': 1}]
                (self.root / 'watched.json').write_text(json.dumps({'watched': rows} if wrapped else rows))
                stats = {}
                c, = parse('trakt', self.root, stats=stats)
                self.assertEqual((c.title, c.signal), ('Synthetic', 'seen'))
                self.assertEqual(stats, {'skipped_trakt': 7})
        (self.root / 'watched.json').unlink()
        for path in (self.root, self.root / 'lists-lists.json'):
            with self.subTest(unsupported_only=str(path)), self.assertRaises(ValueError):
                parse('trakt', path)

    def test_letterboxd_current_rating_newest_diary_and_likes(self):
        cases = [(1.5, 4.5, False, 'love', '2026-10-11'),
                 (4.5, 1.5, False, 'not_for_us', '2026-10-11'),
                 (1.5, None, False, 'love', '2026-10-10'),
                 (4.5, None, False, 'not_for_us', '2026-10-10'),
                 (1.5, 3.5, True, 'love', '2026-10-09'),
                 (4.5, 1.5, True, 'not_for_us', '2026-10-11')]
        for i, (old, current, liked, signal, stamp) in enumerate(cases):
            with self.subTest(old=old, current=current, liked=liked):
                folder = self.root / str(i)
                folder.mkdir()
                header = 'Date,Name,Year,Rating,Watched Date\n'
                diary = f'2015-01-01,Synthetic,2020,{old},2015-01-01\n'
                if current is None:
                    diary += f'2026-10-10,Synthetic,2020,{6-old},2026-10-10\n'
                (folder / 'diary.csv').write_text(header + diary)
                if current is not None:
                    (folder / 'ratings.csv').write_text(header + f'2026-10-11,Synthetic,2020,{current},\n')
                if liked:
                    (folder / 'likes').mkdir()
                    (folder / 'likes/films.csv').write_text('Date,Name,Year\n2026-10-09,Synthetic,2020\n')
                c, = parse('letterboxd', folder)
                self.assertEqual((c.signal, c.last_ts), (signal, stamp))
                self.assertEqual(c.n, 2 if current is None else 1)

    def test_same_batch_seen_like_love_drop_abandon_in_both_orders(self):
        for action, conf in [('seen', 1.), ('like', .7), ('love', 1.)]:
            positive = imported(action, 'trakt', conf=conf)
            abandon = imported('abandon', 'netflix', conf=.4, n=5)
            for rows in ([positive, abandon], [abandon, positive]):
                with self.subTest(action=action, first=rows[0]['src']):
                    out, counts = cross_source(rows, person='ben')
                    self.assertEqual(out, [positive])
                    self.assertEqual((counts['abandon_dropped'], counts['merged'], counts['corroborated']), (1, 0, 0))
                    sequential, _ = cross_source([abandon], existing=[positive],
                        profile=[{'key': positive['key'], 'who': 'ben', 'weight': .1}], person='ben')
                    self.assertEqual(sequential, [])

    def test_ben_both_conflict_resolves_per_person(self):
        for who in ('ben', 'ana'):
            positive = imported('seen', 'trakt', who)
            abandon = imported('abandon', 'netflix', 'both', conf=.4)
            other = 'ana' if who == 'ben' else 'ben'
            expected = sorted([positive, {**abandon, 'who': other}], key=lambda e: e['who'])
            for rows in ([positive, abandon], [abandon, positive]):
                with self.subTest(who=who, first=rows[0]['src']):
                    out, counts = cross_source(rows, person='both')
                    self.assertEqual(sorted(out, key=lambda e: e['who']), expected)
                    self.assertEqual((counts['abandon_dropped'], counts['corroborated']), (1, 0))
        for rows in ([imported('seen', 'trakt', 'both')],
                     [imported('seen', 'trakt', 'both'), imported('abandon', 'netflix', 'both', key='tmdb:2')]):
            with self.subTest(no_overlap=rows):
                self.assertEqual(cross_source(rows, person='both')[0], rows)

    def test_pre_w12b_preview_then_apply_preserves_plan(self):
        data = self.root / 'data'
        data.mkdir()
        write_legacy(data)
        (data / 'profile.json').write_text('{"entries": []}')
        (data / 'facets.json').write_text(json.dumps({'items': {'tmdb:87000001': {
            'kind': 'movie', 'title': 'Synthetic Beacon', 'y': '2021', 'g': []}}}))
        export = self.root / 'plain.txt'
        export.write_text('Synthetic Beacon (2021)\n')
        for status in ('applied', 'skipped'):
            with self.subTest(status=status):
                state = self.root / status
                args = ['--snapshot', str(data), '--state-dir', str(state), '--json', 'onboard',
                        '--person', 'ben', '--non-interactive', '--source', f'plain={export}']
                def cli(*extra):
                    out = io.StringIO()
                    with patch.object(Engine, 'react', return_value={'facets': [], 'rows': {}}), \
                            redirect_stdout(out), redirect_stderr(io.StringIO()):
                        code = main([*args, *extra], out=out)
                    self.assertEqual(code, 0, out.getvalue())
                    return json.loads(out.getvalue())
                staged = cli()
                self.assertEqual(cli('--yes', '--plan', staged['plan'])['added'], 1)
                path = state / 'onboard.json'
                book = json.loads(path.read_text())
                record = book['people']['ben']['plain']
                del record['events']
                record['status'] = status
                write_private(path, json.dumps(book))
                before = (state / 'imports.jsonl').read_bytes()
                preview = cli()
                applied = cli('--yes', '--plan', preview['plan'])
                self.assertEqual((applied['plan'], applied['added']), (preview['plan'], 0))
                self.assertEqual((state / 'imports.jsonl').read_bytes(), before)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_mixed_people_preview_applies_the_same_resolved_payload(self):
        data = self.root / 'data'
        data.mkdir()
        write_legacy(data)
        (data / 'profile.json').write_text('{"entries": []}')
        (data / 'facets.json').write_text(json.dumps({'items': {'tmdb:87000001': {
            'kind': 'movie', 'title': 'Synthetic Beacon', 'y': '2021', 'g': []}}}))
        trakt = self.root / 'trakt.json'
        trakt.write_text(json.dumps([{'movie': {'title': 'Synthetic Beacon', 'year': 2021,
                                              'ids': {'tmdb': 87000001}},
                                     'plays': 1, 'last_watched_at': '2026-10-11'}]))
        netflix = self.root / 'netflix.csv'
        netflix.write_text('Profile Name,Title,Start Time,Duration\n'
                           'Private Profile,Synthetic Beacon,2026-10-11,00:10:00\n')
        sources = [f'trakt={trakt}', f'netflix={netflix}']
        for i, order in enumerate((sources, list(reversed(sources)))):
            with self.subTest(order=order):
                state = self.root / str(i)
                args = ['--snapshot', str(data), '--state-dir', str(state), '--json', 'onboard',
                        '--person', 'ben', '--non-interactive', '--profile-map', 'Private Profile=both',
                        *[v for source in order for v in ('--source', source)]]
                def cli(*extra):
                    out = io.StringIO()
                    with patch.object(Engine, 'react', return_value={'facets': [], 'rows': {}}), \
                            redirect_stdout(out), redirect_stderr(io.StringIO()):
                        code = main([*args, *extra], out=out)
                    self.assertEqual(code, 0, out.getvalue())
                    return json.loads(out.getvalue())
                staged = cli()
                self.assertEqual(staged['merge']['abandon_dropped'], 1)
                result = cli('--yes', '--plan', staged['plan'])
                self.assertEqual((result['plan'], result['added']), (staged['plan'], 2))
                payload = [json.loads(line) for line in (state / 'imports.jsonl').read_text().splitlines()]
                self.assertEqual(sorted((e['who'], e['action'], e['src']) for e in payload),
                                 [('ana', 'abandon', 'netflix'), ('ben', 'seen', 'trakt')])
                self.assertTrue(all(e['key'] == 'tmdb:87000001' for e in payload))
                self.assertNotIn('Private Profile', json.dumps(payload))
                self.assertEqual(cli('--yes', '--plan', staged['plan'])['added'], 0)
                self.assertEqual(stat.S_IMODE((state / 'onboard.json').stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
