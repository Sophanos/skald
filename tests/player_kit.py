"""Synthetic, unverified player transports; never opens a socket."""
import json
import urllib.parse

from history_kit import ROOT, Reply


def player_fixture(name):
    return json.loads((ROOT / 'tests/fixtures/connectors' / (name + '.json')).read_text())


def jellyfin_transport(fixture):
    def transport(req, timeout):
        url = urllib.parse.urlsplit(req.full_url)
        query = urllib.parse.parse_qs(url.query)
        if not url.path.endswith('/Items'):
            return Reply(fixture['series'][urllib.parse.unquote(url.path.rsplit('/', 1)[1])])
        filtered = query['Filters'][0]
        def included(row):
            data = row.get('UserData', {})
            return not data or (data.get('Played') or data.get('PlayCount', 0) > 0 if filtered == 'IsPlayed' else
                data.get('PlaybackPositionTicks', 0) > 0 if filtered == 'IsResumable' else data.get('IsFavorite'))
        types = query.get('IncludeItemTypes', ['Movie,Episode'])[0].split(',')
        rows = [row for row in fixture['items'] if included(row) and (row.get('Type') != 'Series' or 'Series' in types)]
        start, limit = int(query.get('StartIndex', ['0'])[0]), int(query.get('Limit', ['100'])[0])
        return Reply({'Items': rows[start:start + limit], 'TotalRecordCount': len(rows), 'StartIndex': start})
    return transport


def plex_transport(fixture):
    def transport(req, timeout):
        url = urllib.parse.urlsplit(req.full_url)
        query = urllib.parse.parse_qs(url.query)
        if url.path == '/library/sections':
            field, rows = 'Directory', fixture['sections']
        elif url.path.endswith('/all'):
            field, rows = 'Metadata', fixture['libraries'][urllib.parse.unquote(url.path.split('/')[-2])]
        else:
            field, rows = 'Metadata', fixture['leaves'][urllib.parse.unquote(url.path.split('/')[-2])]
        start, limit = int(query.get('X-Plex-Container-Start', ['0'])[0]), int(query.get('X-Plex-Container-Size', ['100'])[0])
        return Reply({'MediaContainer': {field: rows[start:start + limit], 'offset': start,
                      'size': len(rows[start:start + limit]), 'totalSize': len(rows)}})
    return transport
