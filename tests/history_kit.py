"""Synthetic connector inputs shared by tests and the existing fresh-household smoke."""
import datetime
import io
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
NOW = 1791734400.0


def watch_db(path, cap=False):
    with sqlite3.connect(path) as con:
        con.executescript((ROOT / 'tests/fixtures/connectors/watch_state.sql').read_text())
        if cap:
            con.executemany('INSERT INTO watch_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [('a1b2c3d4-device', 'movie', 'tmdb:' + str(9000000+i), int(NOW*1000)-100000-i,
                  1, 'movie', 0, 100, 100, None, None, 0, None, None) for i in range(125)])
    return {'path': str(Path(path).resolve()), 'profiles': {'a1b2c3d4': 'household', 'b1b2c3d4': 'ana'},
            'archived': {'c1b2c3d4': 'retired'}}


class Reply(io.BytesIO):
    def __init__(self, value, pages=1):
        super().__init__(json.dumps(value).encode())
        self.headers = {'X-Pagination-Page-Count': str(pages)}


def trakt_pages():
    folder = ROOT / 'tests/fixtures/ingest/trakt-export'
    read = lambda name: json.loads((folder / name).read_text())
    movies, shows, ratings = read('watched-movies.json'), read('arbitrary-c.json'), read('arbitrary-a.json')
    history = [row for row in read('arbitrary-b.json') if 'watched_at' in row]
    # Summaries are reshaped to complete history pages only in this synthetic fixture.
    for row in movies:
        history.extend({'movie': row['movie'], 'watched_at': row['last_watched_at']} for _ in range(row['plays']))
    for row in shows:
        for season in row['seasons']:
            for ep in season['episodes']:
                history.extend({'show': row['show'], 'watched_at': ep['last_watched_at'],
                                'episode': {'season': season['number'], 'number': ep['number']}}
                               for _ in range(ep['plays']))
    for i, row in enumerate(history, 1):
        row['id'] = i
    # History-only export rows also appear in the complete watched API response.
    watched_ids = {row['show']['ids']['tmdb'] for row in shows}
    missing = {}
    for row in history:
        if 'show' not in row or row['show']['ids']['tmdb'] in watched_ids:
            continue
        show = missing.setdefault(row['show']['ids']['tmdb'], {'show': row['show'], 'plays': 1, 'seasons': {}})
        episode = row['episode']
        season = show['seasons'].setdefault(episode['season'], {'number': episode['season'], 'episodes': {}})
        ep = season['episodes'].setdefault(episode['number'], {'number': episode['number'], 'plays': 0})
        ep['plays'] += 1
        ep['last_watched_at'] = row['watched_at']
    for show in missing.values():
        for season in show['seasons'].values():
            season['episodes'] = list(season['episodes'].values())
        show['seasons'] = list(show['seasons'].values())
        shows.append(show)
    return {'/sync/history': history, '/sync/watched/movies': movies, '/sync/watched/shows': shows,
            '/sync/ratings': ratings}


def trakt_web_export(pages):
    """Independent fixture reshape for the offline importer, including rating-only interests."""
    out = {'exported_at': datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
           'movies': [], 'shows': []}
    for kind, plural in (('movie', 'movies'), ('show', 'shows')):
        rows = {}
        sources = pages['/sync/watched/' + plural] + pages['/sync/history'] + pages['/sync/ratings']
        for source in sources:
            if kind not in source or source in pages['/sync/ratings'] and source.get('episode'):
                continue
            title = source[kind]
            tid = title['ids']['tmdb']
            if tid not in rows:
                rows[tid] = {'tmdb': tid, 'imdb': title['ids'].get('imdb'), 'title': title['title'], 'year': title.get('year'),
                             'genres': title.get('genres', []), 'lang': title.get('language'), 'status': title.get('status')}
                rows[tid].update({'plays': []} if kind == 'movie' else {'eps_total': title.get('aired_episodes'), 'eps': []})
            if kind == 'show' and source.get('aired_episodes') is not None:
                rows[tid]['eps_total'] = source['aired_episodes']
            if 'watched_at' in source:
                minute = int(datetime.datetime.fromisoformat(source['watched_at'].replace('Z', '+00:00')).replace(tzinfo=datetime.timezone.utc).timestamp() / 60 + .5)
                if kind == 'movie':
                    rows[tid]['plays'].append(minute)
                else:
                    ep = source['episode']
                    rows[tid]['eps'].append([ep['season'], ep['number'], minute, ep.get('runtime', 0)])
        out[plural] = list(rows.values())
    return out
