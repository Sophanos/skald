"""Watching instructions shared by setup and docs/WATCH.md."""
from urllib.parse import quote, urlsplit

RECIPES = {
    'stremio': '''Stremio direct — should work, not yet tested by us.
Open your printed stremio:// link (or Together's) on the device that has Stremio → Install.
Or paste the member URL into Stremio's addon search. `skald addon-url --stremio --qr --person NAME` prints a QR code for a phone.
Skald only adds rows and search; keep your stream addons for playback.
Stremio viewing reaches Skald directly: run `skald setup --connect stremio --person NAME` for each account. Trakt stays an option: turn it on in Stremio and run `skald setup --connect trakt`. You can also connect MDBList or import history.
Get a free key at mdblist.com/preferences; record plays with its Plex webhook or Emby/Jellyfin plugins.''',
    'aiostreams': '''AIOStreams in Stremio, self-hosted on the same machine — tested by us.
Stremio viewing reaches Skald directly: run `skald setup --connect stremio --person NAME` for each account. Trakt stays an option: turn it on in Stremio and run `skald setup --connect trakt`. You can also connect MDBList or import history.
Get a free key at mdblist.com/preferences; record plays with its Plex webhook or Emby/Jellyfin plugins.
Run `skald setup aiostreams` first. It makes a login with Skald and title details.
Existing users: `skald setup aiostreams --uuid <your uuid>` adds Skald to your current login
and changes nothing else. It asks before saving the additions.
For ElfHosted, use `--aio-url YOUR_AIOSTREAMS_HTTPS_ADDRESS --base YOUR_SKALD_HTTPS_ADDRESS`.
ElfHosted should work, not yet tested by us.
By hand instead: add Skald as a custom addon by its manifest URL: {manifest}
On the same Docker network, use {internal};
otherwise use the printed HTTPS URL. The internal URL includes the private addon token.
copy the exact URL that `skald setup --watch aiostreams` prints.
Add The Movie Database addon with only Meta/details on, so title pages show.
The TMDB key under Services → Metadata does not fill title pages.
Rows appear in Skald's order; setup keeps that order when new rows are added.
Set `MANIFEST_CACHE_TTL=*:21600,skald:600` in .env.aiostreams to re-read Skald's rows
every 10 minutes; "skald" is its Docker hostname. Restart AIOStreams after changing the setting.
Without it, wait up to 6 hours or restart AIOStreams after rows change.
Rows appear once they have enough titles; a new household starts with a few and gains more as Skald learns your taste.''',
    'jellyfin-app': '''Apple TV / Jellyfin apps such as Odin — tested by us.
Run `skald setup aiostreams` first. It makes your login with Skald and title details.
Existing users: `skald setup aiostreams --uuid <your uuid>` adds Skald to your current login
and changes nothing else. It asks before saving the additions.
By hand instead: add Skald to AIOStreams as a custom addon: {internal}
Add The Movie Database addon with only Meta/details on, so title pages show.
The TMDB key under Services → Metadata does not fill title pages.
Use the printed HTTPS URL if AIOStreams is outside this Docker network.
AIOStreams can pretend to be a Jellyfin server. Enable JELLYFIN_ENABLED.
Only the first JELLYFIN_MAX_LIBRARIES catalogs become libraries (default 20).
Raise it if you have more rows; we use 40.
In the app, add the printed server. Log in with the printed user and password.
Skald rows appear as libraries on Home.
Rows mixing films and series are served as type "tv" so series show up.
To learn from viewing there, run skald setup --connect aiostreams-db.
A shared login can count as "together".
Set `MANIFEST_CACHE_TTL=*:21600,skald:600` in .env.aiostreams to re-read Skald's rows
every 10 minutes; "skald" is its Docker hostname. Restart AIOStreams after changing the setting.
Rows appear once they have enough titles; a new household starts with a few and gains more as Skald learns your taste.''',
}
FILES = '''Import a file on your laptop, then preview before sending:
Your linked server resolves titles with --remote. The laptop needs no TMDB key.
Without server lookups, use --resolve-online with TMDB_API on the laptop.
python3 -m skald import netflix ViewingActivity.csv --person {person} --remote --dry-run
Other sources: imdb ratings.csv; letterboxd ratings.csv; trakt export.zip;
youtube watch-history.json or watch-history.html; chrome History (copy the browser database first).
Use python3 -m skald onboard --sources for export instructions.
Save a reviewed import with the same command without --dry-run and with --yes.
python3 -m skald push imports --remote
After reviewing the preview: python3 -m skald push imports --remote --apply'''


def stremio_links(url):
    """One-click Stremio install links for an HTTPS manifest URL; None for other addresses."""
    if not url.startswith('https://'):
        return None
    return {'app': 'stremio://' + url[len('https://'):],
            'web': 'https://web.stremio.com/#/addons?addon=' + quote(url, safe='')}


def recipe(watch, manifest, lang, internal=None):
    url = urlsplit(manifest)
    internal = internal or ('http://' + ('engine' if url.netloc else 'skald') + ':8080' + (url.path if url.netloc else f'/<addon-token>/{lang}/manifest.json'))
    return RECIPES[watch].format(manifest=manifest, lang=lang, internal=internal)


def watch_doc():
    return '# Where you watch\n\nChoose a ready-to-run [Compose setup](../examples/README.md).\n\n' + RECOMMENDED + '\n' + '\n\n'.join(recipe(k, 'YOUR_PRINTED_MANIFEST_URL', '<lang>') for k in RECIPES) + '\n\n`More from <Name>` rows offer other films by directors and series by creators behind at least two titles you liked or finished; together rows need that evidence from every member.\n\n' + FILES.format(person='NAME') + '\n\nPlex playback should work, not yet tested by us. Skald does not supply streams.\n'

RECOMMENDED = '''## Recommended: the setup we run

Tested by us. OpenRouter adds natural-language AI search to this setup.
Only AIOStreams needs an HTTPS address. Skald stays inside the Docker network.

```text
Apple TV app (Odin) → AIOStreams (Jellyfin mode, HTTPS) → Skald (internal)
```

1. Run `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`.
   Skald runs as user 65534 and keeps DATA private.
2. Copy `.env.example` to `.env` and `.env.aiostreams.example` to `.env.aiostreams`.
   Set TMDB and OpenRouter keys in `.env`. Make the AIOStreams secret with
   `openssl rand -hex 32`; set its public HTTPS address in `.env.aiostreams`.
3. Run `docker compose -f compose.stack.example.yaml up -d --build`.
4. Run `docker compose -f compose.stack.example.yaml exec skald skald setup`.
5. Run `docker compose -f compose.stack.example.yaml exec skald skald setup aiostreams`.
   It makes your AIOStreams login with Skald and title details, and prints the Odin login.
6. Add the printed server in Odin. Log in with the printed user and password.

If rows come back at the bottom in AIOStreams, run `skald setup aiostreams` again.

By hand instead:

- Add Skald as a custom addon with the printed internal URL.
- Add The Movie Database addon with only Meta/details on, so title pages show.
- The TMDB key under Services → Metadata does not fill title pages.

Stremio can use the AIOStreams Stremio link printed by setup; self-hosted on the same machine is tested by us.
AIOStreams is a separate project with its own [docs and licence](https://github.com/Viren070/AIOStreams).
We reference its published image. Configure its stream addons for playback.
'''


def aiostreams_manual(manifest):
    return ('By hand instead:\n'
            'Add Skald as a custom addon: ' + manifest + '\n'
            'Add The Movie Database addon with only Meta/details on, so title pages show.\n'
            'The TMDB key under Services → Metadata does not fill title pages.')
