"""Pending reviews survive unrelated imports; all inputs are synthetic."""
from skald import guide
from test_onboard_wizard import FrontDoorCase


class ImportReviewTests(FrontDoorCase):
    def plain(self, title, person='ana', *flags):
        self.export.write_text(title + '\n' if title else '')
        return self.cli('import', 'plain', str(self.export), '--person', person, '--yes', *flags)[0]

    def netflix(self, title, person='ben', *flags):
        file = self.root / 'netflix.csv'
        file.write_text('Title,Date\n' + f'{title},10/11/26\n')
        return self.cli('import', 'netflix', str(file), '--person', person, '--yes', *flags)[0]

    def test_different_sources_and_people_keep_both_reviews(self):
        first = self.plain('Synthetic Pending One')
        second = self.netflix('Synthetic Pending Two')
        self.assertTrue(first['review'])
        self.assertTrue(second['review'])
        self.assertEqual(guide.review(self.state), first['review'] + second['review'])
        self.assertEqual(second['review_file'], 'review.json')

    def test_reimport_replaces_only_same_source_and_person(self):
        self.plain('Synthetic Old')
        other_person = self.plain('Synthetic Other Person', 'ben')
        other_source = self.netflix('Synthetic Other Source', 'ana')
        replacement = self.plain('Synthetic Replacement')
        self.assertEqual(guide.review(self.state),
                         other_person['review'] + other_source['review'] + replacement['review'])

    def test_empty_reimport_clears_own_reviews_and_reports_remaining_file(self):
        self.plain('Synthetic Old')
        other = self.netflix('Synthetic Other')
        result = self.plain('')
        self.assertEqual(result['candidates'], 0)
        self.assertEqual(guide.review(self.state), other['review'])
        self.assertEqual(result['review_file'], 'review.json')

    def test_profile_mapped_candidates_replace_their_reviews(self):
        self.netflix('Synthetic Old Ana', 'ana')
        self.netflix('Synthetic Old Ben', 'ben')
        other = self.plain('Synthetic Other Source')
        file = self.root / 'profiles.csv'
        file.write_text('Profile Name,Title,Date\n'
                        'Profile A,Synthetic New Ana,10/11/26\n'
                        'Profile B,Synthetic New Ben,10/11/26\n')
        result = self.cli('import', 'netflix', str(file), '--person', 'both', '--yes',
                          '--profile-map', 'Profile A=ana', '--profile-map', 'Profile B=ben')[0]
        self.assertEqual({r['person'] for r in result['review']}, {'ana', 'ben'})
        self.assertEqual(guide.review(self.state), other['review'] + result['review'])

    def test_onboard_reviews_survive_later_import(self):
        self.export.write_text('Synthetic Onboard Pending\n')
        self.stage()
        before = guide.review(self.state)
        self.assertTrue(before)
        result = self.netflix('Synthetic Import Pending')
        self.assertEqual(guide.review(self.state), before + result['review'])

    def test_dry_run_writes_nothing(self):
        self.plain('Synthetic Dry Run', 'ana', '--dry-run')
        self.assertFalse(self.state.exists())
        self.plain('Synthetic Pending')
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        self.netflix('Synthetic Dry Run', 'ben', '--dry-run')
        self.assertEqual({p.name: p.read_bytes() for p in self.state.iterdir()}, before)
