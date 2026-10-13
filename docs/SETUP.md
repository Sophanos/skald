# Set up Skald

Choose a ready-to-run [Compose setup](../examples/README.md); the tunnel companion reads `TUNNEL_TOKEN` from its private env file.

Skald may suggest a shelf when it sees a weekly habit; it never creates one by itself.
Use `skald suggest --person alex` to review your own habits. Children get no suggestions.

We recommend the [setup we run](WATCH.md): Odin on Apple TV connects to AIOStreams
in Jellyfin mode. AIOStreams reaches Skald inside Docker. Only AIOStreams needs HTTPS.
First run `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`.
Skald runs as user 65534 and keeps DATA private.
Use `compose.stack.example.yaml` and both example env files. Add TMDB and OpenRouter
keys in `.env`. Make `SECRET_KEY` with `openssl rand -hex 32` and set `BASE_URL` to
AIOStreams' public HTTPS address in `.env.aiostreams`. Keep both files private.
AIOStreams stores its database in its own named volume at `/app/data`.
Its database setting is `DATABASE_URI=sqlite://./data/db.sqlite`.
When connecting with `skald setup --connect aiostreams-db`, use `--map PREFIX=skip` or answer `skip` at the profile prompt to ignore another household's profile; at least one profile must map to a member or `together`.
Events already recorded stay when a profile is skipped, just as when it moves between members.
AIOStreams is a separate project with its own [docs and licence](https://github.com/Viren070/AIOStreams).
We reference its published image.

Run `docker compose -f compose.stack.example.yaml up -d --build`, then
`docker compose -f compose.stack.example.yaml exec skald skald setup`.
Then run `skald setup aiostreams`: it makes your login with Skald and title details, and prints the Odin server, user and password.
You can also supply `--internal-base http://skald:8080 --watch jellyfin-app`.
Commands below using `exec engine` describe the Stremio-direct alternative;
for the recommended stack, use `-f compose.stack.example.yaml exec skald` instead.


Run setup where your household data lives. In Docker:
`docker compose exec engine skald setup`.
The folder mounted at `/data` is called DATA below.
With empty DATA, the engine serves `/health` as HTTP 503 with status `not-configured` and checks for `skald.json` every three seconds, then starts automatically when setup saves it.
Start with the [README quickstart](../README.md).
Clone from `https://github.com/Sophanos/skald`.
Delete the `SKALD_MEMBERS`, `SKALD_LANG` and `SKALD_TZ` lines copied into `.env`;
setup asks for these household settings instead.

### One command for a fresh household with AIOStreams

Keep Compose, network addresses, DATA ownership, API keys and the player login
manual. Start the engine with fresh DATA. Mount the AIOStreams database directory
read-only, including its SQLite WAL and SHM files, so the engine can see current
history and settings. Then run inside the engine:

```sh
skald setup household --member sam --lang de-DE --timezone Europe/Berlin --country DE --aio-login PREFIX --source-path /aio-data/db.sqlite --internal-base http://skald:8080 --aio-url http://aiostreams:3000 --password-stdin
```

Send the login password through private stdin, or omit `--password-stdin` to use
the private prompt. Internal and AIOStreams addresses can also come from saved
setup settings. `--rows foryou,week` chooses rows; omitting it uses default rows.
The selected login maps to Sam; every other discovered profile is skipped.
Ambiguous prefixes, including accounts sharing the first eight characters, are
refused. A login without history needs `--uuid-stdin`: send its full UUID on the
first private stdin line and its password on the next line.

Setup checks TMDB, saves one complete config, waits for the engine to load it,
and updates the existing AIOStreams login. It reads the instance access key from
AIOStreams' `settings` table in memory. It prints no passwords, access keys or
authenticated manifest URLs. If the engine or AIOStreams step fails after saving,
fix the reported issue and run `skald setup aiostreams`; saved private credentials
are reused. The household command refuses configured DATA. Use the existing
setup commands for later changes. This flow does not reload a running household.

### Without Docker

Use Python 3.12; no packages need installing. `DATA_DIR` selects the household
folder and defaults to `/data`. Choose a folder you own, create it with mode 0700,
and export your TMDB key in the same shell before setup:

```sh
export DATA_DIR="$PWD/data"
mkdir -p "$DATA_DIR" && chmod 700 "$DATA_DIR"
export TMDB_API=YOUR_KEY
python -m skald setup
python server.py
```

Supply the real key privately so it does not enter shell history. For example,
Python's `getpass` can read it: `export TMDB_API="$(python -c 'import getpass; print(getpass.getpass("TMDB API key: "))')"`.
Stop and restart `python server.py` after changing settings.

### Keep less history

This is off by default. In `skald.json`, add `"keep_raw_days": 30` and/or
`"max_raw_items": 1000` inside `history`, then restart Skald. Both accept
nonnegative integers; `null` or omission keeps everything. A zero count keeps no
raw items. The count limit covers all raw play and import files together.
Set `"keep_raw": false` on a configured connector to fold its raw items daily
regardless of those limits; omission or `true` uses the household limits.

Daily cleanup saves compact title summaries before removing raw items. They keep
the TMDB key, kind, people, last-played month, play count, ratings and taste signals,
without device or client details. Unresolved titles normally stay raw; cleanup
reports how many selected records produced no taste event. A zero count or
`keep_raw: false` removes those records too, losing their taste contribution.
Preview with `skald history --cleanup`; add `--yes` to apply the configured cleanup.
Typed ratings, credentials and saved taste-memory facts stay unchanged.
Backups keep their usual schedule and retention, including older evidence already
archived. Source accounts and original export files are outside this cleanup.

Less history means slightly coarser time decay: summaries use the first day of the
month, which can shift decay thresholds, ordering and borderline abandonment.
Repeated title evidence in the same month is coalesced. In the synthetic fixture,
folding keeps the seen and negative-title sets and reduces 82 profile entries to
81 by coalescing one duplicate shared-title row. Remaining raw TV and account
items take precedence for the same title, media kind and source. Imports keep
their correction order and original undo IDs across folding. Re-uploads compare
folded dates by month; the other import fields must still match. Row metrics treat
folded plays as undated, so a rounded month cannot earn precise-window credit.

Run `skald history --forget-before 2026-09-01 --person alex` on the server to preview
counts, then repeat with `--yes` to apply. `--dry-run` explicitly previews.
Forgetting removes that person's raw evidence and inferred, unconfirmed memory
facts before the date. A cutoff on the first of a month keeps that month; a later
day removes their whole boundary month, since summaries cannot distinguish days. Other members'
evidence and typed opinions stay. A saved cutoff also prevents old connector
evidence from contributing again; existing backups are unchanged.
Forget also invalidates household learning and exposure caches and health repair
copies. Affected member and Together metric cohorts lose old play credit and
show incomplete rates; other members' metric cohorts stay.


## Optional health checks and alerts

Health checks, self-repair and daily digests start off. Enable checks with
`skald setup alerts --enable-health`, then restart the server. Add `--repair`
to allow safe repairs. Checks run every five minutes: stale feeds, dead workers,
three consecutive failed connector polls, AI use at 90% of a cap, pending
AIOStreams sync, less than 512 MiB free, unreadable JSON and newly empty rows.

Repairs have a 30-minute cooldown and at most three attempts per check per UTC
day. Connector retries use increasing backoff and respect upstream rate limits.
Only serving caches (`feeds.json`, `shown.json`, `shown_scopes.json`) may be restored or deleted.
Taste, ratings, history, household config and credentials are never repaired.
Actions go in `DATA/health.jsonl`, capped at 256 KiB. Limits survive restarts;
an unreadable health ledger disables repair until you restore it or remove it.

For Telegram, run
`skald setup alerts --telegram --token-stdin --chat-id ID` and supply the bot
token through private stdin. For a webhook, run
`skald setup alerts --webhook --url-stdin --webhook-style discord` and supply
the URL through private stdin. Styles are `ntfy` (use a topic URL), `gotify`
(use its message endpoint) and `discord`.
Omit the stdin flag to use a private prompt. Values are stored in mode 0600
under `DATA/credentials/` and are never echoed. Configure both channels by
running the commands separately. `skald setup alerts --test` sends a test.

Alerts wait until repair fails or is unavailable. Repeated alerts are deduped;
recovery sends a message. `--quiet-hours 22-07 --timezone Europe/Berlin` defers
messages until morning. `--digest` adds a daily summary. Settings are saved in
`DATA/health-settings.json`; thresholds and repair limits can be changed there.
Use `skald health` or `skald health --json` to see checks and the last repair.
`/health` includes detail for loopback clients or an authenticated household
client with read access; public callers receive only basic liveness.
`skald setup alerts --disable-health` turns checks off after a restart.
Use `--no-repair`, `--no-digest` or `--quiet-hours ""` to clear optional settings.

## 1. Check the machine and keys

**What it does:** checks that DATA is private and writable, then makes one TMDB request.
**What you need:** DATA owned by the engine user (65534 in Compose), mode 0700,
and `TMDB_API=YOUR_KEY` in a private `.env` file (mode 0600).
Get a free key from TMDB's account settings → API.
After changing `.env`, run `docker compose up -d --force-recreate`.
**Worked when:** setup prints “DATA writable and private; TMDB key valid”.

OpenRouter is optional. Add `OPENROUTER_API_KEY=YOUR_KEY` for AI picks,
natural-language search, mood shelves and weekly picks. Rows work without it.

Embeddings use OpenRouter's `/api/v1/embeddings` with `google/gemini-embedding-2`
and 256 dimensions. Set `SKALD_EMBED_MODEL` to any OpenRouter embedding id to
override it, then restart the engine. Skald keeps the old model active while it
re-embeds in the background within the daily embed budget, switching when the new
model covers at least as many needed titles. Errors keep the old vectors for retry;
`/stats` shows the active embedding model and coverage counts.

Optional `SKALD_COMMAND_MODEL` defaults to `~google/gemini-flash-latest`; `SKALD_COMMAND_EFFORT` defaults to `low`.
Within decision budgets, failed settings command calls retry once with `z-ai/glm-5.3-flash` at effort `low` (1200 tokens, six seconds) before returning no change. Override with `SKALD_COMMAND_FALLBACK_MODEL` and `SKALD_COMMAND_FALLBACK_EFFORT`.
AI search understands “anime I have not watched” and “anime I have watched” using the linked member’s history, or current members together on the Together link.

## 2. Add your household

**What it does:** saves one to six members, language, timezone and country.
**What you need:** short lowercase names such as `alex,sam`, at least two letters each;
a language such as `en-US`; a timezone such as `Europe/Berlin`.
**Worked when:** setup prints your names and settings and DATA contains `skald.json`.
It writes atomically. A rerun keeps existing settings and asks only for missing steps.
You can add a member. Setup refuses to remove or rename someone who has data.
The optional `row_prefix` in household config sets catalog ids, for example
`skald.foryou`. It defaults to `skald` and accepts 2–16 lowercase letters and
digits, starting with a letter. Existing households keep their prefix in
`DATA/legacy.json`; stored row ids are not rewritten.

Choose home rows with `skald setup rows episodes,foryou,top10,digital,toprated`. This saves `household.rows`; Skald keeps its usual row order and search still works. Valid keys are episodes, foryou, week, upcoming, catchup, rewatch, because, leap, top10, people (For NAME), moods, lanes, docs, studios, anime, shelves, digital, toprated and newtoyou. The opt-in Latest Digital Releases lists movies released digitally in your country in the last 60 days, most popular first; Top Rated Movies uses TMDB ratings and at least 200 votes. The opt-in New to You row (newtoyou) rotates through the household, one member or Together each day, and mixes what that person keeps loving with a kind of story they have not tried yet; rejecting a pick there counts only lightly against their taste. All three respect age and hide settings. Run `skald setup rows all` to restore the default rows, without these three opt-in lists. Changes ask AIOStreams to refresh its rows.

Each member can set discovery, years, unwanted genres, hidden categories and focus:

```sh
skald setup prefs --person alex --discovery gems --years 1990- --exclude-movie-genres horror --exclude-series-genres reality,talk --hide documentaries,anime --focus-movie-genres horror,drama --focus-languages es --focus-countries ES
skald setup --status
skald setup prefs --person alex --clear
```

Use `balanced` for the usual order, `mainstream` for popular titles, or `gems` for
high ratings with lower popularity. Years can be `1990-2010`, `1990-`, or
`--years=-2010`; `--years=-` removes the year limit. Unknown release years are
left out when a year limit is set. Genre names are TMDB's English names, ignoring
case, separately for movies and series. Use quotes for names with spaces, such as
`--exclude-movie-genres "Science Fiction,Horror"`. An empty genre flag clears that
kind's exclusions. Omitted flags keep the member's other preferences.
Use `--hide movies,series,documentaries,animation,anime` to add hides; `--show anime` removes one.
Anime hides Japanese animation; animation hides all cartoons. Focus ranks without filtering:
`--focus-movie-genres` and `--focus-series-genres` use the same genre names,
`--focus-languages` uses two-letter language codes (such as `es`), and
`--focus-countries` uses two-letter country codes (such as `ES,MX`). Codes are
normalised to lowercase languages and uppercase countries. Empty focus flags
clear their lists. Each matching group adds one point, with ties keeping the
discovery order. Older cached titles without countries get no country point
until the worker fills them; it retries failures and fetches at most 200 per pass.

Interactive setup asks once whether any children watch, then asks for the member and age.
Couples without children never see family rows or kids titles in Together rows.
Family Picks uses the youngest child's age and adult taste, with every member's hides.

Set a child with `skald setup prefs --person mia --kids-age 8` (ages 3..17).
Use `--kids-age off` to clear it. This limits every Skald row, including New Episodes,
and search on the child's member link, or when the child is chosen in Who's watching.
Household-country ratings take priority, with US ratings as a fallback. Use a two-letter
country code in setup outside United States, United Kingdom, Germany, France, Spain,
Italy, Netherlands, Australia and Canada.
Missing ratings hide the title; TMDB ratings can also be wrong. Skald limits only its own
rows and search. Other addons and streams in the app are not filtered, so also use the
app's own profile lock.
Give the child their own link in their own app profile; the shared-link Who's watching picker is best effort because apps may cache rows; after setting an age, clear the app cache or reinstall the addon.

Settings live in `skald.json` under `taste.people.<name>.prefs`, with optional
`kids_age: 8`, `discovery`, `years: [from, to]` (null means open), and
`excluded_genres: {"movie": [...], "series": [...]}`, `hide: ["documentaries", "anime"]`, and
`focus: {"genres": {"movie": [...], "series": [...]}, "languages": [...], "countries": [...]}`. Absent settings keep the usual
rows. Changes apply on the next row request without a restart; watching apps may
cache rows briefly. Started titles in New Episodes skip hides, genre exclusions and years.
Together respects genre and category hides from any member, except movies/series, and keeps its years and order. Other preferences leave search unchanged. `--clear` removes
all settings for that person; status shows each member on one line.

Older households in `DATA/legacy.json` are read-only in setup until you run
`docker compose exec engine skald setup adopt --yes`. It copies the config unchanged
to `skald.json`, enabling `setup link`, `setup --connect` and imports. To roll back,
remove `skald.json` (or the path set by `SKALD_CONFIG`) and `DATA/.skald-household`;
`legacy.json` stays in place.

For agents, every question has a flag. For example:

```sh
docker compose exec engine skald setup --yes --members alex,sam --lang en-US --timezone UTC --country 'United States' --base http://192.168.1.20:8080 --watch stremio --connect files
```

`--yes` never prompts. A missing value gets a one-line fix.
Use `--json` for a machine result. Addon tokens are replaced by `<addon-token>`;
AIOStreams passwords and Stremio login URLs are hidden too; run in text mode to get the private login and URLs. Laptop linking requires text mode too.

## 3. Choose the address

**What it does:** prints private member and Together addon URLs.
**What you need:** an address your watching app can reach.
Compose initially listens only on the host's `127.0.0.1:8080`.
For your LAN, change its port line to `"192.168.1.20:8080:8080"`, using your machine's LAN address,
then recreate the engine. Use that address with `--base`.
For apps outside your network, put an HTTPS proxy or tunnel in front of Skald.
**Worked when:** your app can open a printed manifest URL (the addon description).

`skald setup --base https://tv.example.org --check` explicitly fetches
`https://tv.example.org/en-US/manifest.json` using your chosen language.
A private addon can reject that path; use the printed private URL to check installation.
Setup reports the rejection and a fix. It does not silently send your addon token there.

For the recommended stack, proxy AIOStreams on port 3000. Skald's internal URL
already works between containers; it needs no public address for watching.

### Caddy

Example for Caddy running on the Docker host:

```caddyfile
aio.example.org {
    reverse_proxy 127.0.0.1:3000
}
```

Keep the paths and Host header unchanged. Do not enable request access logging.
Keep private URL paths out of proxy error logs too.
If Caddy is in Docker, share a network and use `aiostreams:3000` instead.
For Stremio direct, proxy Skald on `127.0.0.1:8080`; optional laptop API access uses 8081.

### Cloudflare Tunnel

Install and sign in to `cloudflared`, create a named tunnel and route your hostname to it.
In its config, use your own tunnel id and credential path:

```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: /path/to/tunnel.json
ingress:
  - hostname: aio.example.org
    service: http://127.0.0.1:3000
  - service: http_status:404
```

Run `cloudflared tunnel run YOUR_TUNNEL_ID` on the Docker host.
For Stremio direct, use your Skald hostname and port 8080.
For laptop API access, add a rule for your Skald hostname with `path: /api/v1/.*`
and `service: http://127.0.0.1:8081` before the addon rule.
Use a similar rule for `/taste/.*` if you use the private feedback page.
Keep tunnel credentials and request logs private.
These deployment examples should work, not yet tested by us.

## 4. Choose where you watch

**What it does:** prints the matching [watching recipe](WATCH.md).
**What you need:** `--watch stremio`, `--watch aiostreams` or `--watch jellyfin-app`.
**Worked when:** Skald rows appear in the player.
Run `skald setup aiostreams --uuid <your uuid>` to add Skald and missing title details to an existing login; it keeps your other settings and asks before saving.
Use `--aio-url` for the AIOStreams address; `--member NAME|together|all` makes member logins.
Passwords use a private prompt or `--password-stdin`. Protected instances ask for their access key (`--secret-stdin` reads it first).
New passwords are shown once and saved privately in DATA/credentials/aiostreams.json. Reruns use saved logins.
If rows come back at the bottom in AIOStreams, run `skald setup aiostreams` again.
AIOStreams may need a restart after rows change.

## 5. Connect taste sources

### Which players report plays?

Installing Skald gives a player rows, but does not send playback events back.
Skald saves the latest row composition in `shown.json` and `shown_scopes.json`
for serving. Those files are pins, not a viewing log. The weekly metric records
the titles in actual catalog responses, including cached responses served by
Skald. A player may fetch rows in the background or display its own cached rows
without a new request. This measures delivered titles, not proof that someone
looked at each poster or chose the title because of Skald.

| Player or source | What can feed Skald play events |
|---|---|
| Jellyfin clients, including Odin connected to Jellyfin | The `jellyfin` connector reads played state and last-play times. Map users to household members. |
| Odin connected to AIOStreams in Jellyfin mode | The `aiostreams_db` connector reads AIOStreams watch state through a read-only database mount. A Jellyfin connector can also read a server that exposes and persists this history. The Odin addon installation alone supplies no events. |
| Plex clients | The `plex` connector reads played state. Its configured profile supplies the person; it does not infer separate people from a shared Plex account. |
| Stremio | The `stremio` connector uses that account's private Stremio connector key and watched library state. Alternatively, AIOStreams watch state or a configured Trakt scrobbling path can supply plays. A Skald addon link or stream request alone supplies none. |
| Any player that scrobbles to Trakt | The `trakt_api` connector reads the mapped account's history. The `trakt` file connector reads a prepared local Trakt history file. Trakt must actually receive the player's plays. |
| Players connected to MDBList | The `mdblist` connector reads watched state recorded by supported webhooks, plugins or scripts. A watchlist entry alone supplies no play. |
| Prepared local history | The `history` file connector reads `history.json`. Dated played records can feed the metric; creating the file does not track a player automatically. |
| Taste imports such as Netflix, Trakt and Letterboxd exports | They teach taste through `imports.jsonl`. Stored dates may be viewing dates, rating dates or a default import day, so these claims have no usable play time for this metric. They appear in coverage but receive no credit. IMDb ratings, plain lists, browser history and YouTube interests do not establish a title play. |
| Other players and availability connectors | No events unless one of the paths above records their plays. NZBHydra availability, metadata lookups, searches, ratings and likes alone supply none. |

No connector is enabled by the metric. Configure and test the appropriate source
with `skald setup --connect SOURCE` and `skald connect test SOURCE`.
`skald metrics` lists the dated titles and claims without a usable play time in
the latest local inputs, including sources with zero plays. Per-row source counts
show which sources actually credited that row. They may overlap when two sources
report the same title; they do not add up to extra played titles.
Connectors often keep only the latest play per title, and may miss older plays.
Unresolved ids, missing times, bulk backfill times and disabled connectors do not
receive credit. The metric adds no metadata requests or network calls.

### Local weekly row counts and privacy

Run `skald metrics` inside the engine, or with its local `DATA_DIR` set.
It prints counts only, for the current Monday-start week and previous three
weeks in the household timezone. Each row and viewer gets one count per distinct
title and type in the week it was delivered. `played / shown` credits a dated
play after delivery, at most once per title in that row-week, within 14 days
(both endpoints included). Repeated catalog fetches and multiple sources do not
raise that title's count. A play can credit several rows or delivery weeks.
Recent weeks are still filling in; zero does not prove someone rejected a title.

Personal deliveries match only that person's plays. Together deliveries can
match any mapped member's play or shared household play. Shared plays do not
credit a personal row when the person is unknown. Guests receive no attribution.

Optional household settings in `skald.json`:

```json
"metrics": {
  "enabled": true,
  "attribution_days": 14,
  "shown_retention_days": 30,
  "shown_max_items": 10000
}
```

Metrics are off by default and stay local when enabled. Days accept integers from 1 to 365;
the cap accepts 1 to 100000. Booleans are not numbers. Omitted settings use the
values above, except `enabled`, which defaults to `false`. Changes apply on the next row request or worker pass.
Set `enabled` to `false` to delete `metrics.json` and `metrics_shown.json` and
stop this extra logging. The local metrics command also deletes them when off.
Reading a copy with `--snapshot` never changes that copy.

`metrics.json` holds only weekly counts and source coverage, without title names
or ids. The private `metrics_shown.json` holds title ids, types, rows, viewers and
delivery times needed for attribution, plus folded counts. A daily cleanup folds
counts before deleting old evidence. The cap limits retained delivery timestamps
and removes the oldest title records when needed. Weeks whose evidence was capped
or removed before attribution could finish show `n/a`, with incomplete counts;
that row-week stops accepting new deliveries after a record is fully removed.
Shorter retention than the attribution window can therefore lose credit.
Late imports cannot recover credit after evidence expires. Restarting preserves
counts; old serving pins are not treated as new deliveries.

Serving still needs its latest shown pins and existing learning state, so this
switch does not erase `shown.json`, `shown_scopes.json`, watch history or existing
learning logs in `learn.json` and `online.json`. Backups retain counts but exclude
the new raw attribution file.
Older backups can still contain counts after you switch off. A restored counts-only
backup preserves its numbers but cannot reconstruct attribution evidence. Weeks
still within the attribution window show `n/a` after such a restore.

**What it does:** saves a private credential, maps accounts to people, then runs a read-only test.
**What you need:** a source account or an export. See [each source](CONNECTORS.md).
**Worked when:** the connector test says reachable, auth OK and zero unmapped accounts.
Use `--connect files` for laptop import instructions.
For a free history and watchlist source, get a key at mdblist.com/preferences and run
`skald setup --connect mdblist --person alex`. Paste it at the private prompt.
Use the Plex webhook from MDBList Preferences or its Emby/Jellyfin plugins to record plays.
See [MDBList](CONNECTORS.md#mdblist) for scripts, limits and disconnecting.
For Stremio viewing, library interests and likes, run
`skald setup --connect stremio --person alex` for each account. Paste the auth key privately,
or leave it blank to enter email and password privately. Only the auth key is saved.
Scripts can use `--yes --key-stdin`; `skald setup --status` shows each connection.
See [Stremio](CONNECTORS.md#stremio) for finding the key, checks and disconnecting.
Secrets go through a private prompt, `--secret-stdin`, or a named environment variable.
They never belong in command arguments. After changing connectors, restart the engine.

## 6. Link a laptop (optional)

**What it does:** creates a token and prints laptop commands. The token is shown once.
**What you need:** Python 3.12 and a Skald checkout on the laptop, plus API access. The API lets your laptop send imports to the engine.
Add `API_PORT=8081` to `.env` and a Compose port `"127.0.0.1:8081:8081"`.
Recreate the engine and forward `/api/v1/` through a separate Skald hostname to 8081.
The AIOStreams address serves AIOStreams, so it cannot accept Skald laptop imports.
**Worked when:** the laptop import preview can use the API.

```sh
docker compose exec engine skald setup link --name laptop --person alex --base https://tv.example.org
```

On the laptop, run the printed `python3 -m skald connect URL` and paste the token privately.
It stores the address and token in `~/.config/skald/`, with private permissions.
`SKALD_API` and `SKALD_TOKEN` still override them.
Local import state lives in `$XDG_DATA_HOME/skald`, or `~/.local/share/skald`.
`--state-dir` overrides that folder. Container imports also need a persistent, writable folder.
For shared imports, run the printed `export SKALD_MEMBERS=...` command.
For title lookups with `--resolve-online`, provide your TMDB key in the laptop environment
as `TMDB_API`, using a private prompt rather than typing it into shell history.
Preview with `push imports --remote`; send only after approval with `--apply`.
These commands use HTTP, without SSH.

## 7. Finish and check progress

**What it does:** prints a doctor summary and the next command.
**What you need:** follow that command on the Docker host when it is a Docker command.
**Worked when:** `docker compose exec engine skald setup --status` has no missing steps.
If status asks for a restart, run `docker compose restart engine`, then check again.
The engine records the configuration it loaded at startup; changed settings or a
missing startup record need a restart before status can confirm them.
For older read-only households, status shows the TMDB key as present but not checked
and reports members' rated counts. An unrecorded address or watching app is a note,
not a missing step. Status does not save these records or check the running engine.
Restart the engine after setup so it loads the saved settings, then install the printed URL.
Allow time for the first rows to build. Title search works while rows are building.
Print links again with `skald addon-url --base YOUR_ADDRESS`.
Rotate a leaked addon link with `skald addon-url --rotate-addon-token --base YOUR_ADDRESS`.
That replaces every installed link. Reinstall them in your apps.

### Change settings from search

Search `every sunday morning a classic for us` or `weekday evenings something light for me` for a scheduled shelf in the household timezone (Sunday 05-12 or weekdays 18-24). Use `for MEMBER` for a named person, or `for us`, `for both`, `for both of us`, `for everyone` or `for together` for a Together shelf. `make me a row of heist films` creates an always-on shelf. `stop Classics` or `stop the sunday shelf` previews stopping an existing shelf with that name; unmatched names stay title searches; `undo` restores it. `show 6 shelves` or `fewer shelves` previews a slot count change (0..10). After shelf changes Skald asks AIOStreams to reload row names for logins made by `skald setup aiostreams`; Stremio installed with Skald's own link may need the link reinstalled to show new names. Full shelves ask you to stop one first. `my settings` or `settings` lists personal and Together shelves. A member id or full label also works in `my settings for NAME`, `NAME settings`, `NAME's settings`, `settings NAME` or `settings for NAME`. `help` shows five search examples. `ideas` and `suggest` offer at most two habit suggestions; `yes NN` creates one and `no NN` dismisses it for 90 days. The exact `yes NN` code comes first in each preview or suggestion card name. Open a card to read its details. Command cards appear once, in movie search results; paired series requests return no command cards. Shared links ask for a member. Children can manage their own shelves; fills keep their age filter, and they cannot change Together or other people's shelves.

Search “hide anime”, “no more horror” or “I loved Dark” to preview one change. Type the exact “yes NN” code shown in the preview to apply it within ten minutes. Plain “yes” and partial codes change nothing. A new command replaces the preview.
AIOStreams keeps search answers for 5 minutes by default (CATALOG_CACHE_TTL), so retyping the same text can show the earlier card; change the wording to refresh.
Search “undo” (also “rückgängig”) to revert the latest active command.
“History”, “what did I change” or “Verlauf” lists changes and their age.
Skald keeps up to 200 changes per scope for 180 days; undated old entries stay.
“Undo no horror”, “undo the crime shelf” or “bring back horror” previews an undo.
It reverses that change on current settings and keeps unrelated newer changes.
Matching uses the text, label and values, with the newest exact match preferred.
A scalar such as runtime must still have its recorded value, or undo refuses.
“Redo no horror” or “do again no horror” previews applying the change again.
“Reset filters”, “reset shelves” or “reset everything to default” previews a reset.
German: “Filter zurücksetzen”, “Regale zurücksetzen”, “alles auf Standard zurücksetzen”.
Read the card's list, then use its exact “yes NN” code. One undo restores a reset.
Add “for MEMBER” or “for us”. Together resets adult filters and Together shelves;
personal shelves stay personal. Household row labels and order stay as configured.
Ratings, feedback, learned taste, imports and viewing history stay intact.
“Reset my taste” remains unsupported. Undated old commands support plain undo.
Member links always apply to that member. Shared and Together links use the named person, then Who's watching; add a name if neither identifies a member. “Everyone” means every adult for settings, and Together for shelves.
Children can manage their own shelves; other settings still need a grown-up. “Help” lists examples; “my settings” shows settings and recent commands.

## Shelves

Only slots with a shelf appear in the player. Added or stopped shelves update AIOStreams for saved logins after about a minute.

Each person has `prefs.shelves` slots (default 4); `household.shelves` sets Together slots (default 2). Both accept 0..10. Use `skald shelf count --person ana 6` or `skald shelf count --together 3`. Shelf contents and search answers can still take up to 5 minutes (AIOStreams `CATALOG_CACHE_TTL`).
Add a wish with `skald shelf add --person ana --name "Sunday Classics" --when "sun 05-12" "a classic film"`. Review the preview, then repeat with `--yes` or `--plan HASH`. A parent can add wishes for a child; fills respect their age limit. Stop a wish by name or slot number with `skald shelf stop`.
`--when always` fills once and stays until replaced or stopped. Schedules accept `daily`, `weekdays`, `weekend`, or comma-separated days (`mon,wed,fri`) followed by hours. Hours use the household timezone and a day starting at 05:00; 24..29 means after midnight. Outside the window, the slot is empty. Slots stay declared even when fewer than six eligible titles remain.
OpenRouter handles free text. Without it, use discover filters such as `genre:drama year:1990-2000 country:US` (underscores for genre spaces); other wishes report "needs OpenRouter". Each wish uses at most one AI call per window. Taste and preference changes refilter its cached picks.
