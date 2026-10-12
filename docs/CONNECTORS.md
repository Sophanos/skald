# Teach Skald what you like

A connector reads history from another app. It does not provide playback.
Run setup on the machine holding DATA:
`docker compose exec engine skald setup --connect SOURCE`.
It tells you where to create credentials, saves them privately and tests the source.
Restart the engine after changing its configuration.

Test again with `skald connectors test`, or name one source:
`skald connectors test jellyfin --json`.
The test is read-only. It reports connection, authentication, unmapped accounts
and recent plays. A failed test exits with an error.
Account names are hidden by default; `--show` reveals unmapped names locally.
Counts follow the existing adapters' limits; they are a preview, not a full audit.

## Trakt

**You get:** watch history and ratings for the selected household member.
**You need:** a Trakt account, an API app and an authorized access token.
Create the app at https://trakt.tv/oauth/applications. This may require Trakt VIP.
Supply the access token first, then the app's client id at the private prompts.

```sh
skald setup --connect trakt --person alex
skald connectors test trakt
```

For an agent, put the token and client id in existing environment variables and use
`--yes --auth-env TRAKT_TOKEN --client-id-env TRAKT_CLIENT_ID --person alex`.
Alternatively, `--secret-stdin` reads two lines: access token, then client id.
The setup prompts save credentials under DATA/credentials/ with mode 0600.
Never put real credentials in arguments.

**Limits:** setup selects one member at a time. Repeat it with `--person` for another member;
it preserves earlier accounts. Multiple account tokens are kept in private credential files.
Existing per-member Trakt accounts are also tested.
A test uses the current token without refreshing it or writing history.
If it has expired, authorize again. The existing `skald trakt login --person alex`
device flow needs configured client id and client secret references and a writable token file.
Normal polling can renew a configured refresh token; a plain access token must be replaced when it expires.
Trakt export imports work without an API app.

## MDBList

**You get:** watched movies, watched shows from episodes, and watchlist interests for each member.
**You need:** a free MDBList account and its API key from https://mdblist.com/preferences.
MDBList must record plays first: use the Plex webhook URL generated in MDBList Preferences,
or install the MDBList plugin for Emby or Jellyfin. MDBList can also sync from Trakt.
Skald reads MDBList; it does not install scrobblers or send plays.

```sh
skald setup --connect mdblist --person alex
skald connectors test mdblist
skald setup --status
```

Paste the key at the private prompt. For scripts, `--yes --secret-stdin` reads one key line
from stdin. Keys are saved per person under DATA/credentials/ with mode 0600;
keep DATA outside your checkout. Never put the key in arguments or URLs you share.
Repeat setup for another member; earlier connections are preserved.
The connection test is read-only and never writes history.

Restart after setup: `docker compose restart engine` (or `docker compose -f compose.stack.example.yaml restart skald`).
The running engine syncs automatically like Trakt; no separate sync command is needed.
It saves activity timestamps per person and skips unchanged watched and watchlist lists.
Re-reading the same history adds no duplicate signals. IMDb ids use the existing TMDB lookup
when no TMDB id is present; items without either id are skipped.

Disconnect with `skald setup --disconnect mdblist --person alex`, then restart the engine.
This removes the stored key and stops using that member's MDBList history; the history file is retained.
A rejected key needs reconnecting with `--secret-stdin` to replace it. A rate limit stops the
current sync and leaves the last good history; polling tries again later.

**Limits:** the free account allows 1,000 requests per day and 1,000 reads per five minutes.
Pages hold up to 500 items. Ratings are not supported. Show and season summary shapes are
unverified; only summaries with show ids and a watched timestamp can be used.

## Jellyfin

**You get:** watched titles, favourites and viewing progress mapped to household members.
**You need:** a reachable Jellyfin server and an API key.
Create one in Jellyfin: Dashboard → Advanced → API Keys → Add.

```sh
skald setup --connect jellyfin --source-url http://jellyfin:8096
skald connectors test jellyfin
```

Setup lists users and asks which member each belongs to.
Use `together` for shared viewing. Agents supply `--map USER_ID=alex` for each user,
plus `--yes --auth-env JELLYFIN_TOKEN`. Use `--secret-stdin` to read the key from stdin.

**Limits:** the key must be able to list users and read their viewing history.
The adapter returns up to 120 recent titles per mapped user.
Titles need TMDB identifiers. The last-30-days count covers played titles and episodes
in that result; Jellyfin does not provide a full dated play log through this adapter.
This connector should work, not yet tested by us on a real Jellyfin server.
The AIOStreams-to-Jellyfin-app watching path is tested by us; it uses the database connector below.

## Plex

**You get:** watched titles and progress visible to the account that owns the token.
**You need:** a reachable Plex server and its account token.
In Plex Web, open a media item → Get Info → View XML.
Copy the `X-Plex-Token` value privately.

```sh
skald setup --connect plex --source-url http://plex:32400 --person alex
skald connectors test plex
```

Setup reads the token's account and asks which member to use; `together` is allowed.
Agents use `--yes --auth-env PLEX_TOKEN` or `--secret-stdin`.

**Limits:** one token maps one account. It does not infer other household accounts.
The adapter returns up to 120 recent titles. Titles need TMDB identifiers.
Counts use the latest play date, rather than every repeat viewing.
Plex should work, not yet tested by us.

## AIOStreams database

**You get:** watched episodes, films, progress, favourites and ratings from AIOStreams viewing.
Only viewing in Jellyfin apps such as Odin is recorded here; Stremio viewing is not.
**You need:** the actual SQLite database mounted read-only inside the engine.
Mount its directory so SQLite can see the matching `-wal` and `-shm` files too.
The engine user must be able to read them. ElfHosted may not expose this database.

```sh
skald setup --connect aiostreams-db --source-path /aio-data/db.sqlite
skald connectors test aiostreams_db
```

Setup lists profile identifiers and asks for member mappings.
Agents use `--yes --map a1b2c3d4=alex --map b1b2c3d4=together`.
A shared AIOStreams login can count as `together`.
No API secret is needed.

**Limits:** the adapter uses the first eight characters of the profile UUID
(the long identifier AIOStreams assigns a login). It needs the supported `watch_state` table.
It reads up to 120 recent titles. Recent-play counts use the latest date for each title;
they may include earlier episodes of a series played again recently.
Setup does not copy or write the database. Profile names cannot be recovered from that table.
The AIOStreams Jellyfin-app viewing path is tested by us.

## Files

**You get:** ratings and viewing history from exports you choose.
**You need:** Python 3.12 on your laptop and the original export.

```sh
skald setup --connect files
python3 -m skald onboard --sources
```

| Export | Import command (add your member with `--person alex --remote`) |
|---|---|
| Netflix viewing CSV | `python3 -m skald import netflix ViewingActivity.csv --dry-run` |
| IMDb ratings/watchlist CSV | `python3 -m skald import imdb ratings.csv --dry-run` |
| Letterboxd export folder | `python3 -m skald import letterboxd export-folder --dry-run` |
| Trakt JSON export folder | `python3 -m skald import trakt export-folder --dry-run` |
| YouTube Takeout JSON | `python3 -m skald import youtube watch-history.json --dry-run` |
| Chrome database copy | `python3 -m skald import chrome History --dry-run` |
| One title per line | `python3 -m skald import plain titles.txt --dry-run` |

Review first. Remove `--dry-run` and add `--yes` to save an import.
With `--remote`, your linked server resolves titles using its TMDB key; the laptop needs no TMDB key.
Only title, year, kind and external ids go to the server for lookup, including in dry runs.
Without server lookups, use `--resolve-online` and set `TMDB_API` in the laptop environment.
Keep originals local. Run `push imports --remote` to preview the send;
after a yes, add `--apply`. There is no SSH in this route.
A token linked with `--person` sends only that member's titles and reports skipped titles in preview and apply.
For everyone, make a household token with `skald setup link --name laptop` (no `--person`).
The older route without `--remote` uses SSH and requires your explicit `SKALD_SSH_HOST`.

**Limits:** ambiguous titles stay in a private review file. Do not guess.
Netflix exports with multiple profiles need `--profile-map SOURCE_NAME=MEMBER`.
Chrome imports need a stable copy of the history database, not a browser's actively changing file.
Imports do not create streams or guarantee that a title is available to watch.

## Who can access the data

The threat model assumes one user on the machine owns DATA and controls its files.
Keep DATA and DATA/credentials/ private (0700); keep credentials, `.env` and backups private (0600).
Setup creates private credential files. On read, connectors reject unsafe paths and writable shared directories.
People who can run code as that same user can read or change the household data.
Member addon URLs select whose rows you see; anyone with the household addon token can select another member.
API tokens can limit access to a member and to permitted actions.
Keep private links out of proxy access and error logs. Rotate a link or revoke a token if it leaks.
