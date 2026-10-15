# Teach Skald what you like

A connector reads history from another app. It does not provide playback.
Run setup on the machine holding DATA:
`docker compose exec engine skald setup --connect SOURCE`.
It tells you where to create credentials, saves them privately and tests the source.
If `skald.json` already exists, this configures only that connector. It needs no
player address or watch app and prints no addon URLs. With no household yet,
it runs the full setup wizard first.
Restart the engine after changing its configuration.

Test again with `skald connectors test`, or name one source:
`skald connectors test jellyfin --json`.
The test is read-only. It reports connection, authentication, unmapped accounts
and recent plays. A failed test exits with an error.
Account names are hidden by default; `--show` reveals unmapped names locally.
Counts follow the existing adapters' limits; they are a preview, not a full audit.

## Choose a source

Start with the app you already watch in. Each row is one command on the server unless it says laptop.

| You watch or track with | Connect | You need | Tested by us |
|---|---|---|---|
| Odin or another Jellyfin app through AIOStreams | AIOStreams database | nothing; mount the database read-only | yes |
| Stremio | Stremio | sign in once through a link, or its auth key | library yes; likes sample data only |
| Trakt | a Trakt export file (laptop) or a Trakt account | the export: nothing; the account: a Trakt API app, which may need VIP | sample files only |
| PublicMetaDB | PublicMetaDB | a free key per person | yes |
| MDBList | MDBList | a free key, and MDBList must already record your plays | partly |
| A Jellyfin or Plex server | Jellyfin or Plex | an API key or account token | not yet |
| Netflix, IMDb, Letterboxd, YouTube, Chrome | an export file (laptop) | the export file | sample files only |

Fewest steps: AIOStreams database or Stremio. Without an account anywhere, import a file.
You can connect several sources; Skald folds the same title together.

## Sources

Accounts connect on the server; exports import on a laptop. Trakt works both ways.

Run connector commands on the server. Prefix them with `docker compose -f compose.stack.example.yaml exec skald` for the stack, or `docker compose exec engine` for Stremio direct. Run imports on a linked laptop with Python 3.12. Replace `NAME` with a configured member and filenames with your exports. Import commands resolve and review titles, then save only on the laptop. Preview the push before sending resolved signals with `--apply`. After changing connectors, restart Skald now (`docker compose restart engine`, or `docker compose -f compose.stack.example.yaml restart skald` for the stack) so it starts reading.

| Source | What it supplies | Connect or import command |
|---|---|---|
| Stremio library | Watched titles, library interests, loves and likes. | `skald setup --connect stremio --person NAME` |
| Trakt | Watch history and ratings; export at [Trakt Settings → Data](https://app.trakt.tv/settings/data) with "Export now". | `skald setup --connect trakt --person NAME`; export: `python3 -m skald import trakt trakt-export.zip --person NAME --remote` |
| MDBList | Watched titles and watchlist interests. | `skald setup --connect mdblist --person NAME` |
| PublicMetaDB | Watched titles, watchlist and resume interests, abandoned titles. | `skald setup --connect pmdb --person NAME` |
| Jellyfin server | Watched titles, favourites and progress; setup maps users. | `skald setup --connect jellyfin --source-url http://jellyfin:8096` |
| Plex | Watched titles and progress for the token's account. | `skald setup --connect plex --source-url http://plex:32400 --person NAME` |
| AIOStreams database | Watched state, progress, favourites and ratings from Odin and other Jellyfin apps. | `skald setup --connect aiostreams-db --source-path /aio-data/db.sqlite` |
| Netflix | [Viewing activity](https://help.netflix.com/en/node/101917) ("Download all"); profile mapping for shared exports. | `python3 -m skald import netflix ViewingActivity.csv --person NAME --remote` |
| IMDb | Ratings or watchlist CSV. | `python3 -m skald import imdb ratings.csv --person NAME --remote` |
| Letterboxd | Ratings, diary, watched, likes and watchlist export. | `python3 -m skald import letterboxd export-folder --person NAME --remote` |
| YouTube | Takeout watch history, JSON or HTML. | `python3 -m skald import youtube watch-history.json --person NAME --remote` |
| Chrome | Title interests from allowed sites in a history database copy. | `python3 -m skald import chrome History --person NAME --remote` |
| Plain text | One title per line. | `python3 -m skald import plain titles.txt --person NAME --remote` |

Stremio viewing reaches Skald directly. AIOStreams records Jellyfin-app viewing, not Stremio playback. Trakt remains optional; its API app may need VIP, but JSON imports need no API app. Mount the AIOStreams database directory read-only before connecting it. Netflix exports with named profiles need `--profile-map "PROFILE=NAME"` for each profile. The sections below explain source limits and credentials.

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

## PublicMetaDB (PMDB)

**You get:** watched movies and show episodes, watchlist interests, resume interests
and abandoned titles, separately for each member.
**You need:** that person's key from https://publicmetadb.com:
Settings → API → Create. Each person uses their own key.

```sh
skald setup --connect pmdb --person NAME
skald connectors test pmdb
```

Enter the key at the private prompt, or use `--yes --key-stdin` to read one line
from stdin. Setup stores a separate mode 0600 file under DATA/credentials/.
Never put a key in arguments. Repeat for another member; earlier accounts stay.
A failed setup probe rolls back the new configuration and key.
Restart the engine after setup with `docker compose restart engine`.

Skald uses GET only: `/api/external/watched`, `/resume`, `/dropped`, `/lists`
and the items of lists whose type is `watchlist`. Custom lists add no signals.
Ratings routes are ignored because they contain community ratings, not your ratings.
Resume progress is stored as a fraction and means interest, even near 100%; it is
never a play. Show ids identify shows, with season and episode kept separately.
Missing watch dates stay unknown. Rewatches use the same aggregation as Trakt.
PMDB evidence has provenance `sync:pmdb` and 0.8 trust. Duplicate titles for the
same person and media type fold with Trakt and player history; ratings and stronger
completion evidence win without adding duplicate weights or play counts.

All pages must succeed before a person's history replaces its last good cache.
The shared limits are 100 requests and 20 MiB per poll, with 10-second timeouts.
A 429 stops the poll and delays the next sync using bounded Retry-After or backoff.
Normal adaptive scheduling stays the same. Probes save no history or cursors;
they report plays in the last 30 days for each configured account. Doctor separates
rejected keys, server bot checks, rate limits, timeouts and malformed responses without showing bodies.

Disconnect with `skald setup --disconnect pmdb --person NAME`, then restart.
The key is removed; history stays available for backups and explicit forgetting.
**Limits:** The API shape was confirmed against a live account in October 2026:
a paged envelope with `items`, `page`, `perPage`, `total` and `totalPages`, using
an Authorization Bearer key. PublicMetaDB may block datacenter servers with a
bot check. Skald reports that the key was not checked and keeps the last good
history. Ask PublicMetaDB to allow API requests from your server.

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

[The source table](#sources) lists exports, what they supply and exact import commands.
First [link your laptop](SETUP.md#6-link-a-laptop-optional), then:

1. Import and answer unclear title matches: `python3 -m skald import imdb ratings.csv --person NAME --remote`.
2. Preview the send: `python3 -m skald push imports --remote`.
3. Send the reviewed signals: `python3 -m skald push imports --remote --apply`.

Import saves resolved signals only on the laptop; unclear titles stay in a private review file.
With `--remote`, your linked server resolves titles using its TMDB key; the laptop needs no TMDB key.
Only title, year, kind and external ids go to the server for lookup, including in dry runs.
Without server lookups, use `--resolve-online` and set `TMDB_API` in the laptop environment.
Keep originals local. Only the push with `--apply` writes signals to the server. There is no SSH in this route.
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

## Stremio

**You get:** watched movies and shows, library interests, and loved/liked titles per person.
**You need:** that person's Stremio account and a working TMDB key on the engine.
Stremio viewing reaches Skald directly; Trakt stays an option.

```sh
skald setup --connect stremio --person alex
skald connectors test stremio
skald setup --status
```

Leave the key prompt blank. Setup prints a Stremio link and its QR code: open the link
(or scan the code with a phone), sign in to Stremio, and setup continues by itself within
five minutes. Skald never sees the password. If Stremio cannot create a link, setup asks for
email and password at private prompts instead; the password goes once to Stremio login and is
never saved. Only the auth key is saved under DATA/credentials/ with mode 0600, separately per person.
For scripts, `--yes --key-stdin` (or `--secret-stdin`) reads one auth key line from stdin.
Never put keys or passwords in command arguments.
To find a key by hand, sign into Stremio Web and run this in the browser console:

```js
JSON.parse(localStorage.getItem("profile")).auth.key
```

Keep the result private. Repeat setup for each member; earlier connections stay intact.
The connection test is read-only. Restart the engine after setup:
`docker compose restart engine` (or `docker compose -f compose.stack.example.yaml restart skald`).
The engine syncs automatically: one library request, one likes-service key lookup and four loved/liked catalog requests
per person, with 10-second request timeouts. Unchanged library state, including `_mtime`,
reuses resolved titles; repeat syncs add no duplicate events. IMDb ids resolve through TMDB.

Watched means a positive watch count or watched flag, or at least 70% progress.
The watch time comes from `lastWatched`. A retained library item without a watch becomes
an added interest; partial progress alone does not count as a watch. Titles watched outside
the library count as watched; other removed items and unwatched temporary items do not add signals. Loved maps to rating 10 and liked to 8,
including titles outside the library. An explicit Trakt rating for the same member/title wins.
Loves and likes come from Stremio's likes service and are best effort. If they are unavailable,
the library still syncs and Skald keeps that person's cached likes.

Disconnect with `skald setup --disconnect stremio --person alex`, then restart the engine.
This removes the stored key and stops using that person's Stremio history; the history file stays.
Replace a rejected key with `--key-stdin`. Authentication failures report “key rejected”; a
rate limit stops the sync, preserves the last good history, and retries later.

**Limits:** Stremio fields are best effort. We tested the link sign-in and library sync with a real
account in October 2026; loves and likes were checked with sample data only.
Only IMDb movie and series ids are supported. Series state supplies the latest episode,
not a complete episode history; absent watch timestamps stay unknown.
