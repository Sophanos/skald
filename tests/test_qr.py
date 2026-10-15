"""QR codes and one-click Stremio install links; all addresses are synthetic."""
import hashlib
import unittest

from skald import qr, recipes

# Reference hashes come from an independent QR library with the same version, level and mask.
CASES = [
    ('HELLO', 'M', 0, 21, 'b664c5ebcc2451fb6630e63f516abd72b3cdfbf16b9ceb0d92d226f6a6d0cde0'),
    ('stremio://tv.example.org/0123456789abcdef0123456789abcdef/en-US/p/alex/manifest.json', 'M', 2, 37,
     '02aa607f156e64c77860de79ea19ce7b62f644963bcd782733f9220754363355'),
    ('https://web.stremio.com/#/addons?addon=https%3A%2F%2Ftv.example.org%2F0123456789abcdef0123456789abcdef'
     '%2Fen-US%2Fp%2Ftogether%2Fmanifest.json', 'L', 5, 45,
     '2977d76b06ff9328c78829ea90f5d2176424d7e3e8baaff3ad280eae8c80b4d2'),
    ('https://tv.example.org/' + 'abcdefghij' * 24, 'L', 7, 57,
     'e80d572f0bcedb3605ce94cfea3a319a9c77a79bc558d5bf0f398891f2f17a10'),
]


def digest(m):
    return hashlib.sha256('\n'.join(''.join('1' if v else '0' for v in row) for row in m).encode()).hexdigest()


class QrTests(unittest.TestCase):
    def test_matches_reference_codes(self):
        for text, level, mask, size, expected in CASES:
            with self.subTest(text=text[:30], level=level):
                m = qr.matrix(text, level, mask)
                self.assertEqual(len(m), size)
                self.assertEqual(digest(m), expected)

    def test_auto_mask_keeps_function_patterns(self):
        m = qr.matrix(CASES[1][0])
        finder = [[max(abs(r - 3), abs(c - 3)) not in (2, 4) for c in range(7)] for r in range(7)]
        self.assertEqual([row[:7] for row in m[:7]], finder)
        self.assertEqual([row[-7:] for row in m[:7]], finder)
        self.assertEqual([row[:7] for row in m[-7:]], finder)
        self.assertEqual(m[6][8:-8], [c % 2 == 0 for c in range(8, len(m) - 8)])

    def test_too_long_text_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'too long'):
            qr.matrix('x' * 300, 'L')

    def test_terminal_has_quiet_zone_and_two_rows_per_line(self):
        lines = qr.terminal('HELLO').splitlines()
        self.assertEqual(len(lines), (21 + 8 + 1) // 2)
        for line in lines:
            self.assertTrue(line.startswith('\x1b[30;107m') and line.endswith('\x1b[0m'))
            body = line[len('\x1b[30;107m'):-len('\x1b[0m')]
            self.assertEqual(len(body), 29)
            self.assertEqual(body[:4] + body[-4:], ' ' * 8)

    def test_stremio_links(self):
        url = 'https://tv.example.org/tok/en-US/p/alex/manifest.json'
        self.assertEqual(recipes.stremio_links(url), {
            'app': 'stremio://tv.example.org/tok/en-US/p/alex/manifest.json',
            'web': 'https://web.stremio.com/#/addons?addon=https%3A%2F%2Ftv.example.org%2Ftok%2Fen-US%2Fp%2Falex%2Fmanifest.json'})
        self.assertIsNone(recipes.stremio_links('http://skald:8080/tok/en-US/manifest.json'))


if __name__ == '__main__':
    unittest.main()
