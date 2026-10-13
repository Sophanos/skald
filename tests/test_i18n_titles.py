"""TMDB provenance controls title fallback; all payloads are synthetic."""
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server


class I18nTitles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.s = load_server(temp.name)
        cls.addClassCleanup(close_server, cls.s)

    def test_translated_titles_survive(self):
        cases = [
            ('el-GR', 'Το ταξίδι', 'Journey', 'en'),
            ('sr-RS', 'Путовање', 'Путешествие', 'ru'),
            ('zh-CN', '旅程', 'Journey', 'en'), ('zh-TW', '旅程', 'Journey', 'en'),
            ('de-DE', '旅程', 'Journey', 'en'),
        ]
        for lang, title, original, original_language in cases:
            for field in ('title', 'name'):
                with self.subTest(lang=lang, field=field):
                    it = {field: title, 'original_' + field: original, 'original_language': original_language}
                    self.assertEqual(self.s.display_name(it, {field: 'Journey'}, lang), title)

    def test_original_in_requested_language_survives(self):
        for original_language in ('ja', 'ja-JP'):
            for field in ('title', 'name'):
                with self.subTest(original_language=original_language, field=field):
                    it = {field: 'たびのモノガタリ', 'original_' + field: 'たびのモノガタリ',
                          'original_language': original_language}
                    self.assertEqual(self.s.display_name(it, {field: 'Journey'}, 'ja-JP'), it[field])

    def test_untranslated_foreign_original_falls_back(self):
        for lang, title, original_language in [('sr-RS', 'Путешествие', 'ru'), ('de-DE', '旅程', 'zh')]:
            for field in ('title', 'name'):
                with self.subTest(lang=lang, field=field):
                    it = {field: title, 'original_' + field: title, 'original_language': original_language}
                    self.assertEqual(self.s.display_name(it, {field: 'Journey'}, lang), 'Journey')

    def test_fallback_and_latin_compatibility(self):
        cases = [
            ('de-DE', '旅程', 'Journey'), ('el-GR', '旅程', 'Journey'),
            ('sr-RS', '여행', 'Journey'), ('zz-ZZ', '旅程', 'Journey'),
            ('zz-ZZ', 'Život', 'Život'), ('de-DE', 'Über das Meer', 'Über das Meer'),
            ('fr-FR', 'Été', 'Été'), ('en-US', 'Journey', 'Journey'),
            ('de-DE', '旅程 A', '旅程 A'), ('de-DE', '×', '×'),
            ('ar', '١٢٣', 'Journey'), ('el-GR', ';', 'Journey'),
            ('el-GR', 'Το ταξίδι', 'Journey'), ('sr-RS', 'Путовање', 'Journey'),
            ('ja-JP', 'たびのモノガタリ', 'Journey'), ('zh-CN', '旅程', 'Journey'),
        ]
        for lang, title, expected in cases:
            with self.subTest(lang=lang, title=title):
                self.assertEqual(self.s.display_name({'title': title}, {'title': 'Journey'}, lang), expected)
        self.assertEqual(self.s.display_name({'name': '旅程'}, {}, 'de-DE'), '旅程')
        self.assertEqual(self.s.display_name({}, {'title': 'Journey'}, 'zh-CN'), 'Journey')

    def test_search_and_metadata_use_requested_language(self):
        s = self.s
        it = {'id': 990001, 'title': 'Το ταξίδι', 'original_title': 'Journey', 'original_language': 'en'}
        en = {'title': 'Journey'}
        with patch.object(s, 'refresh_preferences'), patch.object(s, 'safety_subject', return_value='both'), \
                patch.object(s, 'kids_age', return_value=None), \
                patch.object(s, 'genres', return_value={}), patch.object(s, 'search_both', return_value=([it], {it['id']: en})):
            self.assertEqual(s.search('movie', it['title'], 'el-GR', title_only=True)['metas'][0]['name'], it['title'])
        for convert in (s.to_meta, s.row_meta):
            self.assertEqual(convert(it, 'movie', 'movie', {}, en, mark='*', lang='el-GR')['name'], '*' + it['title'])

    def test_with_art_row_uses_requested_language(self):
        s = self.s
        it = {'id': 990002, 'title': '旅程', 'original_title': 'Journey', 'original_language': 'en', 'vote_count': 250}
        def tmdb(path, **params):
            return {'results': [it if params['language'] == 'zh-TW' else {**it, 'title': 'Journey'}]}
        with patch.object(s, 'tmdb', side_effect=tmdb), patch.object(s, 'genres', return_value={}):
            self.assertEqual(s.toprated_feed('zh-TW')['movie'][0]['name'], it['title'])

    def test_anime_keeps_kana_without_english_tmdb_title(self):
        s = self.s
        it = {'id': 990003, 'name': 'たびのモノガタリ', 'original_name': 'たびのモノガタリ', 'original_language': 'ja'}
        media = [{'title': {'english': 'Journey', 'romaji': 'Tabi'}, 'startDate': {}}]
        with patch.object(s, 'anilist', return_value=media), patch.object(s, 'genres', return_value={}), \
                patch.object(s, 'resolve_anime', return_value=(it, {})), \
                patch.object(s, 'taste_order', side_effect=lambda xs, **kw: xs):
            self.assertEqual(s.anime_feed('ja-JP', upcoming=True)['series'][0]['name'], it['name'])

    def test_tmdb_cache_keeps_provenance(self):
        s = self.s
        it = {'id': 990004, 'title': 'Путешествие', 'original_title': 'Путешествие', 'original_language': 'ru'}
        payload = io.BytesIO(json.dumps({'results': [it]}).encode())
        with patch('urllib.request.urlopen', return_value=payload) as upstream:
            s.tmdb('search/movie', query='synthetic-cache-title', language='sr-RS')
            cached = s.tmdb('search/movie', query='synthetic-cache-title', language='sr-RS')['results'][0]
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(cached, it)
        self.assertEqual(s.display_name(cached, {'title': 'Journey'}, 'sr-RS'), 'Journey')

    def test_script_tables_removed(self):
        self.assertFalse(hasattr(self.s, 'TITLE_SCRIPT_RANGES'))
        self.assertFalse(hasattr(self.s, 'TITLE_LANGUAGE_SCRIPTS'))

    def test_language_names_keep_documentary_prompt_unchanged(self):
        self.assertEqual(self.s.LANGUAGE_NAMES, {
            'de': 'German', 'en': 'English', 'fr': 'French', 'es': 'Spanish', 'it': 'Italian',
            'nl': 'Dutch', 'pt': 'Portuguese', 'sv': 'Swedish', 'da': 'Danish', 'nb': 'Norwegian',
            'fi': 'Finnish', 'pl': 'Polish', 'ja': 'Japanese', 'ko': 'Korean',
        })
