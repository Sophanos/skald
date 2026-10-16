# Commands by task

In Docker, prefix server commands with `docker compose exec engine`.
On a laptop, use `python3 -m skald` instead of `skald` (Python 3.12).
Replace `alex` with your configured member name.
For local inspection, use `--snapshot /path/to/copy` before the command.
The copy is read-only. Imports are saved separately in `$XDG_DATA_HOME/skald`,
or `~/.local/share/skald`. Change that with `--state-dir PATH`.
`--json` prints machine results. Use `skald COMMAND --help` for flags.

`skald tags --retag "Cloud Lantern" --json` queues one cached title for another
experience-tag check. Use the exact title or its id. `skald tags --stale --json`
queues tags whose schema or prompt inputs are outdated, including legacy tags
with no provenance. JSON reports the queued count and counts by status: missing,
ok, partial, invalid, untaggable, manual and stale. Stale takes precedence over
the stored status in these counts. These commands run on local `DATA_DIR`.
The existing worker processes the queue within the AI budgets, with a six-hour
cooldown and at most three attempts per schema and input. Partial tags stay and
get one retry, including one final attempt if the third result is partial. A
round has at most four attempts. Manual tags stay. A new explicit request starts another bounded
round after the cooldown. Without OpenRouter the queue waits.
In Commander, a current adult can ask “retag Cloud Lantern” or “recheck all stale
experience tags”, then use the displayed confirmation code. German and Serbian
requests work too. This queues work without changing ratings or watch history.

`skald ai budget --json` reads the household monthly budget in USD, its ceiling
and whether the ceiling comes from the environment or the default. Preview a
change with `skald ai budget 5 --person ana --json`, then repeat with `--yes` or
`--plan HASH`. An explicit `AI_MONTHLY_USD` is the ceiling; otherwise the ceiling
is 25 USD. Without a saved value the budget uses `AI_MONTHLY_USD` when set,
otherwise 3 USD.
Set 0 to turn off paid AI calls until the budget is changed again. Search and
rows keep their existing fallbacks. Changes take effect without a restart.
Only adult household members may change this budget. A 1.4.9 server ignores the
saved budget and uses its environment budget instead.

`skald changes --since 7d --person ana --json` lists saved changes with their id,
time, scope, actor, name and undone flag. Adults see all scopes. Kids and guests
see their own scope. Omit `--person` for the local operator view of all scopes.
The journal retains up to 200 changes per scope for 180 days; older entries may
have an unknown actor or time. Preview one undo with
`skald changes undo ID --person ana --json`, then repeat with `--yes` or
`--plan HASH`. Adults may undo another person's entry; kids and guests may undo
permitted changes in their own scope, including those made by an adult.
Undo a later change to the same setting first. The existing
`skald command --person ana --undo` also undoes a budget change.

Commander understands requests such as “set the AI budget to 5 dollars a month”,
“turn AI off this month” and “what changed this week”. Budget requests in another
currency keep the number and show USD in the preview. Changes still need the
displayed confirmation code. “undo the row rename” can find another person's
change when an adult asks.

`skald ai status --json` reads saved OpenRouter credit, the household budget and ceiling, the last check and failure,
Skald spending today and this month, and daily calls used per group. It works
offline and writes nothing. Add `--refresh` to check the key once now, using
`OPENROUTER_API_KEY` from the environment. `setup --status` shows a credit line.
Ask Commander “how much AI credit is left?” to read the same saved numbers.

## Odin plays

Run these against the engine's local DATA directory. Every command accepts `--json`.

| Command | What it does |
|---|---|
| `skald plays` | Show mode, current path and reason, last and next check, and new plays per person. |
| `skald plays mode auto` | Prefer a readable mounted AIOStreams database, then saved HTTPS logins. Also accepts `db`, `https` and `off`. |
| `skald plays every 15m` | Set the check interval. Use whole minutes or hours from `5m` to `6h`. Default: `15m`. |
| `skald plays person ben off` | Stop reading Ben's plays. Use a configured member name or `together`; `on` resumes reading. Everyone starts on. |
| `skald plays check` | Read once now. A failed read keeps the last good learning input. |
| `skald plays compare` | Read both paths and print differences per person. Writes nothing. |
| `skald plays recent --person ana --limit 20 --json` | Read recent learned plays with episode, time, path and backfill flag. Limit: 1 to 120, default 20. |
| `skald plays why "Invented Lantern" --person ana --json` | Explain a saved title or id with login prefix, path, time and backfill flag. |
| `skald plays recent kim --person ana --json` | Ana reads Kim's plays this week. `recent [NAME]` without a name reads the asker's own plays. |
| `skald plays why "Invented Lantern" kim --person ana --json` | Ana reads why a title was counted for Kim. The optional child name follows the title. |
| `skald plays around kim --person ana --json` | Read Kim's watching days, last day, counts, usual weekdays, stretches and gaps. Requires a current adult asking about a child. |
| `skald connectors mappings --json` | Show eight-character login prefixes, people, paths and mapped, unmapped or conflict status. Writes nothing. |

Settings use the optional top-level `plays` object in `skald.json`: `mode`,
`every` in seconds, `people` as member-to-boolean switches, and `live: false`.
Live push is reserved and stays off. The previous release ignores this object;
FORMAT stays unchanged. The optional private `plays.json` cache stores check
times, normalized HTTPS rows and shared per-person title and episode dedup state.
These optional fields need no migration. Forgetting and raw retention also purge
this cache; forgotten rows are filtered before caching. Access tokens stay in
memory for one read.

HTTPS uses saved AIOStreams logins and a public HTTPS address from setup. Played
items are read newest first, through the last seen minute so a backfill burst
cannot split across checks. Resume and favourite lists are refreshed each time.
Both paths use the existing five-episodes-in-one-minute backfill rule and the
same title aggregation. Both paths validate one login-to-person mapping.
Conflicting and unmapped logins are skipped and reported by their eight-character
prefix. Switching paths keeps the newest timestamp and compares whole seconds
when counting new plays. The mounted database aggregation stays byte-identical.

The AIOStreams Jellyfin API derives likes from ratings, hides stored dropped
flags, can substitute metadata runtime, and omits inactive watch-state rows.
Exact learning parity for those fields cannot be proved from this API. Its
played and favourite lists stop at 500 rows. A read that reaches that cap
without its watermark fails safely and keeps the last good input. Packed
item IDs preserve original TMDB, IMDb and other supported provider IDs; episode
provider IDs are never mistaken for show IDs. Opaque hashed IDs are skipped and
counted in status, since enriched ProviderIds cannot identify the original base
ID reliably. Catalog episode aliases can also differ from the database base ID.
Use `compare --json` to inspect the actual field and identity differences.

In Commander, ask “is Skald seeing what I watch?” or “what did I watch this week?”.
Recent means the past seven days; titles use the existing profile cache, with
IDs as a fallback. Ask “why did you count Invented Lantern as watched?” for the
same provenance as `plays why`. Household adults see their own plays plus Together;
children, guests and away people see their own plays, including explicit history
corrections. Children never see another person's plays. Only household adults can
change mode or interval. Adult guests and away adults can switch their own
collection on or off. Changes use the usual preview, `yes NN` and `undo`.
Moved-out people are no longer collected; their saved history stays attributed
to them. Guests keep their own access until their stay ends.
“Read my Odin watches now” queues a background check and answers “Checking now,
results in a minute”. The household limit is one request per two minutes, recorded
when queued, including failed attempts. HTTP 429 backoff delays scheduled retries.

Current household adults may also read a child's plays and taste. The adult cannot
be a guest or away. Adults still cannot read each other's plays. Reading a child's
plays uses exactly the child's own view, including shared watches that name the child.
In Commander, ask "What did Kim watch this week?" (`plays_recent`, `person: kim`),
"Why did you count Invented Lantern as watched by Kim?" (`plays_explain`,
`values: ["Invented Lantern"]`, `person: kim`) or "What does Kim like?"
(`taste_drivers`, `person: kim`). These reads change nothing.

"When is Kim around?" uses the read-only `plays_around` op with `person: kim` and
no values. Watching days come only from Kim's own mapped Odin/AIOStreams login.
Shared logins never count. Without an own mapped login, the answer gives
`python -m skald setup aiostreams --member kim` instead of counts.
JSON returns `last_day` (or null), `around`, `days_7`, `days_28`, `usual_weekdays`,
`stretches`, `gaps` and fixed translated `text`. Days use the household time zone
and start at 05:00. `around` means watching on at least one of the last seven
logical days, including today. It does not say where the child is.
Usual weekdays use completed Monday-to-Sunday weeks wholly inside the 56-day
window that have watching data. A weekday must appear in at least half of those
weeks; fewer than three such weeks gives null. Weekdays use `mon` through `sun`.
Stretches contain at least three consecutive watching days, newest first, at most
five. Gaps contain at least seven days without watching between two watching
days, newest first, at most three. Both have `start`, `end` and `days`.
The optional private `watching-days.json` file stores only child ids and day lists.
Older servers ignore it, so FORMAT stays unchanged and no migration is needed.

## See your taste

| Command | What it does and an example |
|---|---|
| `me` | Show what is known about you, including your taste archetype: `skald --snapshot /path/copy me --person alex` |
| `taste show` | Show your taste profile: `skald --snapshot /path/copy taste show --person alex` |
| `taste align` | Read the titles and facets shaping your picks, with correction previews: `skald --snapshot /path/copy taste align --person alex --limit 10 --json`. Omit `--person` for the household. Writes nothing. |
| `taste align NAME --person ADULT --json` | Read a child's taste, for example `skald taste align kim --person ana --json`. Here `NAME` is the child and `--person` is the current adult asking. Child reads omit correction previews. With no `NAME`, the existing `--person` subject and household default stay the same. Commander uses `taste_drivers` with `person: kim`. |
| `taste why` | Explain a title's fit: `skald --snapshot /path/copy taste why tmdb:123 --person alex` |
| `taste report` | Summarize recent learning: `skald --snapshot /path/copy taste report --since 7` |
| `taste calibration` | Inspect the reference, score percentiles and cached row match bands without fetching or writing: `skald taste calibration --person alex --json` |
| `explain` | Show scoring details, unseen known rank per person, Home row presence, feeling rows sourced from the tagged catalog and the household taste archetype: `skald --snapshot /path/copy explain tmdb:123 --json`. A served Top Rated pick includes its rating, relative taste match and row position; use `--person ana` for that member. |
| `rows` | Preview rows: `skald --snapshot /path/copy rows --n 10` |
| `rows pool [--json]` | Read today's seed choices and last background pool counts: `skald --snapshot /path/copy rows pool --json`; missing offline endpoints are reported, and counts carry their build time. |
| `rows ready [--person NAME] [--json]` | Read saved release marathons: `skald rows ready --json`. Without `--person`, the operator sees every member's qualification reasons and progress. With `--person`, only that viewer's reasons and seen information appear alongside shared release facts. Explicit `household`, `together` and `guest` views contain shared facts only. When no row shows, a plain line explains why; JSON includes `cause` and `message`. Uses local `DATA_DIR` or `--snapshot` offline; it never fetches TMDB or rebuilds feeds. Inspect the two slots with `rows why ready1` and `rows why ready2`. Commander can answer “what should I watch before the new Invented Ember?” from the same data. “Hide the get ready row” and “show it again” control the `ready` family. |
| `rows reach [--person NAME] [--top N]` | Check how many best unseen known matches appear on each profile's Home, with row ids and the first drop reason for missing titles: `skald --snapshot /path/copy rows reach --top 20 --json`. Omit `--person` for the household and every member. Reads stored data offline; it does not source or rebuild feeds. `--top` defaults to 20 and accepts 1..10000. |
| `rows plan` | Explain every registered home row, including hidden rows: `skald rows plan --person household --lang en-US --json`. Top Rated cards include type, position, TMDB rating, relative taste match (0..100) and blend score. Feeling rows include `candidate_sources`: catalog and recommendation counts before vetting or balancing, and `missing_cards` skipped because stored artwork is missing. Themed vetting examines the first 40 cards. Counts appear after a rebuild. Themed rows include `season.months` (month numbers, `"any"`, or `null` when unknown) and `season.hidden` for the household's local date. Reads local `DATA_DIR` or `--snapshot`; `--person` also accepts `together`, a member or `guest`. |
| `rows why ROW` | Explain one row and its kept and dropped cards: `skald rows why mood1 --person ana --json`. Because rows in `rows plan` and `rows why` include `slot_kind` and `slot_since` (the local date that kind began). Get Ready summaries in `rows plan` and `rows why` use the same privacy projection as `rows ready`: omit `--person` for the full operator view. Use `skald rows why toprated --person ana --json` to list ranked movies and series with rating and taste. Commander can read the same list through its Home plan operation, or hide and show the row with `hide top rated` and `show top rated series`. Both types share the `toprated` control. Uses the same read-only build as `rows plan`. |
| `rows prefer` | Preview a household mood or lane limit change: `skald rows prefer --theme mood --more` or `--theme lane --fewer`. Automatic Home defaults to three each; Home can show at most four mood rows and five lane rows. Apply with `--plan HASH` from the preview or `--yes`. Preview and apply refuse a reduction below six populated rows, or the current count if already lower, and never allow zero populated rows. Changes use the existing atomic `skald.json` write; the server reloads them on its next row request or worker pass. Hiding or showing a row preserves both family limits. An explicit household `rows` selection bypasses these limits. |
| `rows refresh ROW` | Preview refreshing one row: `skald rows refresh mood1`. Apply with `--plan HASH` or `--yes`; the CLI atomically queues row ids in `DATA_DIR/row_refresh.json`, without writing feeds or calling upstream services. The worker clears those rows' stamps and deletes the request at the start of its next feed pass. Older servers ignore the request. A grouped mood or lane build can also rebuild its siblings. Queued CLI refreshes have no undo. |
| `rows week [--weeks N] [--person NAME] [--json]` | Read saved weekly picks offline, including on an older snapshot. Defaults to the latest saved week; `--weeks 1..8` includes that week and earlier calendar weeks. For example: `skald --snapshot /path/copy rows week --weeks 4 --person ana --json`. |
| `rows lab [--json]` | Read new row ideas, their state, play moments, showing days and keep or retirement reasons offline. With row metrics off, it says the lab is paused. Supports `--snapshot` for reads. |
| `rows lab --keep ID`, `--retire ID`, `--off` or `--on` | Preview one household lab control. Repeat with `--yes` or `--plan HASH` to apply to local `DATA_DIR`. Use `command --person ana --undo` to undo the last control. Keep promotes an idea; manual retirement is final unless undone. |
| `rows moments [--json]` | Read showings and attributed plays per row and moment offline. Old evidence stays unknown. Imports without usable play times are excluded and counted separately. Supports `--snapshot`. |
| `learn` | Inspect viewing signals: `skald --snapshot /path/copy learn` |
| `suggest` | Read your weekly habits: `skald suggest --person alex`. Use `--dismiss ID` to hide a theme for 90 days in private DATA_DIR. Listing writes nothing; a read-only copy also works with `--snapshot`. |
| `memory` | Inspect saved preferences: `skald --snapshot /path/copy memory` |
| `history` | Preview forgetting old evidence: `skald history --forget-before 2026-09-01 --person alex`. `--dry-run` is the default; repeat with `--yes` to write local DATA_DIR. A mid-month cutoff removes that person's whole boundary month; typed ratings and existing backups stay. |
| `history --cleanup` | Preview configured raw retention cleanup and its unresolved residual count. Add `--yes` to apply; disabled retention writes nothing. |
| `metrics` | Show counts per row, person and week for the current week and previous three: `skald metrics`. Reads local `DATA_DIR`; `skald --snapshot /path/copy metrics --days 7` includes weeks touching those days. Lists play sources, never titles. |
| `metrics learning` | Read mature row learning without writing: `skald metrics learning --person alex --json`. Omit `--person` for the household. Shows decayed showings (S), credited plays (P), posterior (p), discovery baseline (p0), mature week support, learning (L) and why neutral. Mood and lane slots share family evidence. |
| `health` | Read saved server checks, the last repair and alert availability: `skald health --json`. Reads local DATA_DIR or an explicit copy; makes no network requests. |
| `search --explain-timing` | Read the last 15 saved search timings: `skald search --explain-timing --json`. Uses local `DATA_DIR` or `--snapshot`, with no network requests or writes. |

`taste report` shows "Learned from N of M titles" for each adult: positive taste
titles with a facet record out of their positive taste titles. Shared watches count
for each adult. `--json` includes these counts under `coverage`, by member.

Search timing includes total, title check, remaining settle wait, router, picker
and resolution milliseconds. Resolution overlaps generation, so stages do not
add up to the total. `first_line_ms` starts at picker submission; `early` means
15 filtered, ranked picks were ready after 2.7 seconds. `picker_state` says
whether generation was still running when the answer was served. `cached`
means the AI answer was reused. Duplicate searches within 30 seconds keep the
existing single log record. Old records may lack newer timing fields.
Resolved public TMDB metadata is disposable in
`DATA_DIR/.cache/search-resolve.sqlite3`: at most 5,000 entries, 8 MiB of keys
and values, and a 12 MiB database. Stop the engine and delete this file to
refresh its metadata on the next search.

Each For You candidate pool invocation makes at most 34 TMDB list requests and
fetches details for at most 50 uncached candidates, best ranked first. Ranking
uses cached taste facets first, then list ratings and vote counts for unknown
titles. The remaining details fill in through the cache on later builds.
The background feed build invokes the pool twice, for the prompt and for cards,
so its pools use at most 68 list requests and 100 candidate detail requests.
These bounds exclude taste model preparation, genre lists and resolving AI picks.

## Teach it

| Command | What it does and an example |
|---|---|
| `interview` | Rate starter titles: `skald interview --person alex --remote` |
| `react` | Try a reaction on a local copy: `skald --snapshot /path/copy react love tmdb:123 --person alex` |
| `decide` | Ask the engine for a decision: `skald decide --list --remote` |
| `me forget` | Remove a selected local imported source: `skald --snapshot /path/copy me forget --source imdb --person alex` |

## Import history

| Command | What it does and an example |
|---|---|
| `connect` | Save your laptop API address and token privately: `skald connect https://tv.example.org` |
| `onboard` | Show export instructions and guide review: `skald onboard --sources` |
| `import` | Resolve, review and save an export on the laptop: `skald import imdb ratings.csv --person alex --remote` |
| `imports list` | List local imports: `skald --snapshot /path/copy imports list` |
| `imports undo` | Undo a selected local import: `skald --snapshot /path/copy imports undo IMPORT_ID` |
| `push imports` | Preview sending resolved titles: `skald push imports --remote` |

Plain import asks about unclear matches in a terminal and saves only on the laptop.
For imports, `--dry-run` shows counts only and saves nothing; `--yes` asks no questions, for scripts, and leaves unclear matches in the local review file.
Review the push preview, then run `skald push imports --remote --apply` to send resolved signals. The `--remote` route uses HTTP, without SSH.
With `--remote`, titles resolve on your server; no laptop TMDB key is needed, even for dry runs.
Use `--resolve-online` with laptop `TMDB_API` for lookups without the server.
Per-person tokens skip other members' titles in preview and apply; a household token sends everyone's.
The older `--live` route uses SSH and has no default host; set `SKALD_SSH_HOST` explicitly.

## Connect sources

| Command | What it does and an example |
|---|---|
| `setup` | Guide a household through setup: `skald setup` |
| `setup --status` | Show done, missing and one next command: `skald setup --status --json` |
| `setup alerts` | Opt into health checks with `--enable-health`, safe repairs with `--repair`, and alerts with `--telegram --token-stdin --chat-id ID` or `--webhook --url-stdin --webhook-style discord`. `--test` sends a test; `--quiet-hours 22-07 --timezone Europe/Berlin` defers alerts; `--digest` enables summaries. See [setup](SETUP.md) for defaults and private storage. |
| `setup household` | Set up fresh DATA and an existing AIOStreams login in one command with `--member`, `--aio-login` and `--source-path`. See [setup](SETUP.md) for private password input, addresses, rows and retry instructions. |
| `setup aiostreams` | Make a login with Skald and title details, or update one: `skald setup aiostreams --uuid <your uuid>` |
| `setup link` | Issue a private laptop token: `skald setup link --name laptop --person alex` |
| `setup prefs` | Set member rows: `skald setup prefs --person alex --discovery gems --years 1990- --exclude-movie-genres horror --exclude-series-genres reality,talk --hide documentaries,anime --focus-movie-genres horror,drama --focus-series-genres drama --focus-languages es --focus-countries ES`; `--hide` adds movies, series, documentaries, animation or anime; `--show anime` removes a hide; focus ranks without filtering; empty focus lists clear that group. `--kids-age 8` (3..17) limits all Skald rows and search for that child; `--kids-age off` clears it. Other addons and streams are not filtered; use the app's profile lock too. TMDB ratings can be missing (titles are hidden) or wrong. Reset with `skald setup prefs --person alex --clear`. Omitted flags keep other settings; `setup --status` shows each member. |
| `setup adopt` | Make a legacy household editable without changing its config: `skald setup adopt --yes` |
| `setup rows KEY,KEY,...` | Choose home rows in Skald's usual order: `skald setup rows episodes,foryou,top10,digital,toprated`. Top Rated movies and series are on by default; Latest Digital Releases and New to You (`newtoyou`) are opt-in. An explicit list keeps exactly those rows; search stays available. `skald setup rows all` restores default rows. See [setup](SETUP.md) for valid keys. Changes refresh AIOStreams rows. |
| `connectors test` | Probe a source without saving history: `skald connectors test jellyfin --json` |
| `connectors` | Inspect saved source health: `skald --snapshot /path/copy connectors` |
| `trakt login` | Authorize a configured Trakt app: `skald trakt login --person alex` |
| `login trakt` | The same device login: `skald login trakt --person alex` |

See [connector credentials and limits](CONNECTORS.md).

### Setup flags

| Flag | What it does |
|---|---|
| `--yes` | Use supplied values and defaults without prompting |
| `--json` | Print a JSON result with addon tokens, AIOStreams passwords and Stremio login URLs hidden |
| `--status` | Show completed steps, missing settings and one next command |
| `--check` | Fetch `<base>/<lang>/manifest.json` and say if your apps can reach it |
| `--key-stdin` | Read the source key from stdin; same as `--secret-stdin` |
| `--secret-stdin` | Read the secret (AIOStreams instance access key first, if protected) from stdin instead of a prompt |
| `--aio-url` | AIOStreams address reachable from Skald; saved value or `http://aiostreams:3000` inside the stack |
| `--uuid` | Existing AIOStreams login to update; password uses a private prompt |
| `--password-stdin` | Read the AIOStreams login password from stdin |
| `--member` | Make an AIOStreams login for NAME, together or all; default: household |
| `--members` | Comma-separated household member names, e.g. alex,sam |
| `--lang` | Language for titles, e.g. en-US |
| `--row-lang` | Language for row names, card labels and reasons, e.g. en-US; `same` follows `--lang`. Restart Skald to apply. |
| `--timezone` | Timezone for dates, e.g. Europe/Berlin |
| `--country` | Country for AI context, e.g. United States |
| `--base` | The address your watching apps use, e.g. https://skald.example.org |
| `--internal-base` | Skald address inside Docker for AIOStreams, e.g. http://skald:8080 |
| `--watch` | Watching app: stremio, aiostreams or jellyfin-app |
| `--disconnect` | Remove a member MDBList, Stremio or PMDB connection and private key: `skald setup --disconnect mdblist --person NAME` |
| `--connect` | Taste source: files, trakt, mdblist, stremio, pmdb, jellyfin, plex or aiostreams-db |
| `--name` | Name for the laptop token created by `setup link` |
| `--person` | Household member whose source or laptop token to use |
| `--source-url` | Address of the Jellyfin or Plex server |
| `--source-path` | Path to the AIOStreams database inside the engine |
| `--user-id` | Jellyfin user id to map to a member |
| `--auth-env` | Environment variable holding the source token or API key |
| `--client-id-env` | Environment variable holding the Trakt client id |
| `--scopes` | Comma-separated laptop permissions; default: read,decide,events |
| `--map` | Map `SOURCE_ID=MEMBER` or `SOURCE_ID=together`; repeat for each account; `SOURCE_ID=skip` ignores another household's AIOStreams profile |

Other command flags use `--person` to select a member, `--base` for the watching
address, and `--token-stdin` to read a laptop token privately. `addon-url --rotate-addon-token` replaces all addon links; reinstall them in your apps.
`onboard --yes --plan HASH` applies a reviewed plan; setup's `--yes` uses supplied
values and defaults. Fact correction is not implemented yet.


## Run the server

| Command | What it does and an example |
|---|---|
| Docker start | Build and run Skald: `docker compose up -d --build` |
| `addon-url` | Print installation links: `skald addon-url --base https://tv.example.org` |
| `addon-url --stremio` | Add one-click Stremio links (app and Stremio Web) for HTTPS addresses; `--person NAME` picks one member, `--qr` prints a QR code of its Stremio Web link: `skald addon-url --base https://tv.example.org --stremio --person alex --qr` |
| `doctor` | Check a local copy and import state: `skald --snapshot /path/copy doctor` |
| `doctor keys` | Check saved title identifiers: `skald --snapshot /path/copy doctor keys` |
| `backup list` | List DATA backups: `skald backup list --data /data` |
| `backup verify` | Check an archive: `skald backup verify archive.tar.gz` |
| `backup restore` | Preview restoring into an empty folder: `skald backup restore archive.tar.gz --to /path/empty --dry-run` |
| `eval` | Run offline checks: `skald --snapshot /path/copy eval` |

See [network setup](SETUP.md) and [upgrades](UPGRADE.md).

For members with few ratings, doctor suggests an interview or importing a Netflix
or IMDb export. Run `skald import --help` for the import path.

PublicMetaDB: run `skald setup --connect pmdb --person NAME` with a key from
Settings → API → Create. Use the private prompt or `--yes --key-stdin`.
`skald connectors test pmdb` reads each account without saving history.
See [PMDB](CONNECTORS.md#publicmetadb-pmdb) for signals and limits.

Stremio: run `skald setup --connect stremio --person alex`. Leave the key prompt blank,
then open the printed link (or scan its QR code) and sign in to Stremio. Scripts use `--yes --key-stdin`.
Run `skald connectors test stremio` to check it and `skald setup --status` for connections.
Disconnect with `skald setup --disconnect stremio --person alex`.
See [Stremio](CONNECTORS.md#stremio) for finding the key and sync limits.

MDBList: get a free key at mdblist.com/preferences, then run `skald setup --connect mdblist --person alex`.
Use the private prompt or `--secret-stdin`. Run `skald connectors test mdblist` to check it.
Restart the engine after setup; it syncs watched history and watchlists automatically.
Use the Plex webhook from MDBList Preferences or MDBList plugins for Emby/Jellyfin to record plays.
See [connectors](CONNECTORS.md#mdblist) for limits and disconnect instructions.

### Personal commands

`no horror` and `hide horror` exclude Horror from movies and series; `hide horror movies` changes movies only. Including a genre without a type clears it for both. `create a new catalog about psygoloy crime`, `add a row for crime`, `neue Reihe mit Horrorfilmen` and `erstelle einen Katalog über Psychologie und Krimi` preview an always-on shelf. Theme text, including typos, stays as typed. `best catalog of crime movies` remains a one-off search.

Shelves accept any weekday list and whole clock hours, for example `every monday,friday 19-22 comedy`. German phrases include `montags morgens`, `freitags abends`, `wochentags morgens` and `am Wochenende abends`, followed by a theme. An explicit start such as `at 20:00` runs until midnight. Schedules use the existing logical day from 05 to 29 (05:00 next morning). Household row commands include `rename For You to Ana Picks`, `hide Top 10`, `show Latest Digital Releases` and `put Top 10 above For You`. They change rows for everyone. Undo refuses to overwrite a later household row change.

Commander also answers `why these rows?`, `why does Because show this` and `why don't I see mood rows` from the home plan (`home_plan`). Ask for `more mood rows` (`home_more`), `fewer lane rows` (`home_fewer`) or `new picks in the cozy row` (`row_refresh`) to preview a household change. Confirm with the exact `yes NN` shown in the preview, then use `undo` to revert. `Refresh this row` asks which row when there is no row context. These operations use the same model decision path and support English, German and Serbian. Undo a Commander refresh inside Commander; the CLI refuses to restore worker stamps. A refresh undo refuses to overwrite a newer background build.

The family limits live in `household.home.family_caps`, for example `{"mood": 3, "lane": 3}`. Missing limits default to three on automatic Home, including when only row order or labels are configured. An explicit household row selection bypasses these limits. Hiding or showing rows from the default set keeps them. Plan counts describe the stored feeds under the current viewer's filters, including Family and active archived rows. Inspection uses cached metadata only and never enriches records, pins rows or records exposures. Metrics, when enabled, add shown and play counts from the last four weeks.

Search `every sunday morning a classic for us` or `weekday evenings something light for me` for a scheduled shelf in the household timezone (Sunday 05-12 or weekdays 18-24). Use `for MEMBER` for a named person, or `for us`, `for both`, `for both of us`, `for everyone` or `for together` for a Together shelf. `make me a row of heist films` creates an always-on shelf. `stop Classics` or `stop the sunday shelf` previews stopping an existing shelf with that name; unmatched names stay title searches; `undo` restores it. `show 6 shelves` or `fewer shelves` previews a slot count change (0..10). After shelf changes Skald asks AIOStreams to reload row names for logins made by `skald setup aiostreams`; Stremio installed with Skald's own link may need the link reinstalled to show new names. Full shelves ask you to stop one first. `my settings` or `settings` lists personal and Together shelves. `what do you know about NAME` answers for a named member. A member id or full label also works in `my settings for NAME`, `NAME settings`, `NAME's settings`, `settings NAME` or `settings for NAME`. `help` shows five search examples. `ideas` and `suggest` offer at most two habit suggestions; `yes NN` creates one and `no NN` dismisses it for 90 days. The exact `yes NN` code comes first in each preview or suggestion card name. Card names stay short (24 characters); open a card to read its details. Command cards appear once, in movie search results; paired series requests return no command cards. Shared links ask for a member. Children can manage their own shelves; fills keep their age filter, and they cannot change Together or other people's shelves.

`skald command --person ana "hide anime"` previews a change and prints its plan hash. Repeat the same text with `--yes` or `--plan HASH` to apply it. Use `skald command --person ana --undo` or type `undo` to revert the last command. `show my shelves` and `what are my settings` return an answer without a confirmation. Soft commands such as `less horror` or `more documentaries` move matching titles at most three places without banning them. `no movies longer than 2 hours` limits runtime; titles without runtime are hidden. `kids age 12 for ben` sets the existing child filter; an adult on a shared link can undo it. `I already watched Arrival` marks a verified title as seen; an ambiguous title needs its type. People focus keeps the actor or director name and ranks matching cached credits first.
Title reactions, genre exclusions, focus, discovery and years use the same writer as search. In search, type the exact “yes NN” code from the preview; plain “yes” changes nothing. Identical mutation requests within 60 seconds repeat their first result.
The API previews with `POST /api/v1/decide/parse_command`, body `{"in":{"query":"hide anime"},"person":"ana"}`. Apply within ten minutes with `POST /api/v1/command`, body `{"plan":"HASH"}`, or undo with `{"undo":true}`.
Bound tokens can change only their own person. Guest and child tokens cannot change settings.

`skald command --person ana "history"` lists settings changes with their age.

### Household people and controls

These examples print JSON for an agent or script. Changes preview first; repeat
the same command with `--plan HASH` from that preview, or `--yes`, to apply.
Read-only lists need no confirmation. In search, use the exact `yes NN` instead.
`--person ana` names the adult making the change; the positional name is the target.

| Action / Commander op | Search example | CLI example with JSON output |
|---|---|---|
| List people | "my settings" (`settings`) | `skald members list --json` |
| Add a member (`member_create`) | "Add my mother Eva" or "Add Eva" | `skald members add eva --member --label Eva --person ana --json` |
| Add a child (`member_create`) | "Add Kim, she is 12" | `skald members add kim --age 12 --person ana --json` |
| Ask a missing name or age (`ask`) | "Add my child"; reply with the name, then age | `skald command "Add my child" --person ana --json` |
| Update an age (`member_update`) | "Update Ben to age 13" | `skald members update ben --age 13 --person ana --json` |
| Update a name (`member_update`) | "rename Ana to Anja" | `skald members update ana --label Anja --person ana --json` |
| Add a guest for days (`member_create`) | "Add Oskar as a guest for 3 days" | `skald members add oskar --guest --days 3 --person ana --json` |
| Add a guest until a date (`member_create`) | "Add Oskar as a guest until 18 October" | `skald members add oskar --guest --until 2026-10-18 --person ana --json` |
| Change guest status (`member_update`) | "Make Oskar a full member" | `skald members update oskar --member --person ana --json` |
| Change a guest's end (`member_update`) | "Kim bleibt bis Sonntag" | `skald members update kim --until 2026-10-18 --person ana --json` |
| Clear a guest's end (`member_update`) | "Let Kim stay without an end date" | `skald members update kim --no-end --person ana --json` |
| Read a guest's end (`member_until`) | "When does Kim leave?" | `skald command "When does Kim leave?" --person ana --json` |
| Show an own Home row (`member_rows`) | "Give Kim her own row" | `skald members rows kim on --person ana --json` |
| Add a guest shelf (`shelf_add`) | "a catalog for Kim and me with heist movies for 3 days" | `skald shelf add "heist movies" --person ana --with-person kim --days 3 --json` |
| Leave (`member_remove`) | "Ben moved out" | `skald members remove ben --person ana --json` |
| Preview permanent erasure (`member_forget`) | "Forget Ben" (preview only) | `python -m skald members forget ben --person ana --json` |
| Away without an end (`member_away`) | "Ben is away for a while" | `skald members away ben --person ana --json` |
| Away for days (`member_away`) | "pause Ben for two weeks" | `skald members away ben --days 14 --person ana --json` |
| Away until a date (`member_away`) | "Ben is away until 1 November" | `skald members away ben --until 2026-11-01 --person ana --json` |
| Back (`member_back`) | "Ben is back" | `skald members back ben --person ana --json` |
| Read the AI budget (`ai_status`) | "how much AI credit is left?" | `skald ai budget --json` |
| Set the monthly budget (`ai_budget`) | "set the AI budget to 5 dollars a month" | `skald ai budget 5 --person ana --json` |
| List changes (`history`) | "what changed this week" | `skald changes --since 7d --person ana --json` |
| Undo by name (`undo`) | "undo the AI budget" | `skald changes undo "AI budget" --person ana --json` |
| Undo by id (`undo`) | "undo ID" | `skald changes undo ID --person ana --json` |
| Undo the last change (`undo`) | "undo" | `skald command --undo --person ana --json` |
| View Get Ready (`home_plan`) | "what should I watch before the new Invented Ember?" | `skald rows ready --person ana --json` |
| Hide Get Ready (`row_hide`) | "hide the get ready row" | `skald command "hide the get ready row" --person ana --json` |
| Show Get Ready (`row_show`) | "show the get ready row" | `skald command "show the get ready row" --person ana --json` |

The CLI has no `--role` flag: ages 3..17 set the child filter; ages 18..120
clear it. Search understands adult or child meaning. A child without an age
gets a question before anyone is added; a title search clears the pending question
and searches normally. To give a child age without a conversation in a script,
use `members add NAME --age N`. Guest/member status is separate from age.
Use `--guest` to switch a member to guest, `rows NAME off` to hide their own row,
and `shelf add ... --until YYYY-MM-DD` for a shelf with a last date.

Undo by name uses the change's name or identifying words; ids come from `changes`.
Ambiguous names ask for an id. Unrelated newer changes stay, including newer settings
when undoing an earlier people change. A conflicting newer value must be undone first.
Forgetting cannot be undone. Away and back target adults only. An adult can forget an adult, child or guest.

Manage household people with `skald members list --json`. It lists display names,
optional ages, member or guest status, own Home rows and shelf counts. The limit
is six active people, including guests; retired profiles do not count. Nothing
in this list contains connector secrets. Undo refuses to reactivate someone
if it would put the household above six active profiles.

`skald members add oskar --guest --person ana --json` previews a named guest.
Add `--age 12` for a child, `--label Oskar` for a display name, or `--own-row`
when they want a Home row. Guests have no own Home row by default. Repeat the
same command with `--yes` or `--plan HASH` to apply the preview.
Add `--days 3` or `--until 2026-10-18` for a temporary guest. Days must be
1..30, counted from today's date; the guest stays through the end of that
local day in the household timezone. An explicit date can be today or within
30 days. `members update kim --days 4 --person ana` previews a new end counted
from today; `--days 0` or `--no-end` clears it. Full members cannot have an end.
`members list --json` includes `guest_until` when set. Commander accepts
`Add Kim as a guest for 3 days`, `Kim bleibt bis Sonntag` and the read-only
question `When does Kim leave?`.

The running worker retires expired guests on its next minute tick, using the
usual removal journal. Their evidence stays saved and their active slot is freed.
`undo` or `bring Kim back` restores them and clears the elapsed end date, so
they stay back until you set a new end. Children and guests cannot make these changes.
`skald members update oskar --member --person ana` promotes a guest;
`--guest` changes them back. Neither change adds a row implicitly.
`skald members rows kim on --person ana --json` previews Kim's own Home row;
use `off` to hide it. Repeat with `--yes` or `--plan HASH` to apply, and use
`skald members --undo --person ana` to restore the previous row setting.

`skald members remove ben --person ana` previews their rows, shelves, likes,
hides, Together label and connector links. Apply it with `--yes` or `--plan HASH`.
A private config backup is saved first. Evidence, history, imports and shelves
stay saved, including clients and AIOStreams settings. Retired people cannot
authenticate or receive rows, and refresh skips their saved logins.
Use the separate forget command to erase evidence. Connector links keep their
original owner and stop syncing that person's inputs until removal is undone.
Ask another adult to remove you. The last adult cannot be removed, and children and guests
cannot change people. Use `skald members --undo --person ana` or
`skald command --undo --person ana` to restore the last change. Undo keeps
unrelated changes and refuses conflicting changes before restoring anything.

Erase runs only from the server command line. Search and the HTTP command API
show a preview with the command; they cannot apply erasure.
`python -m skald members forget ben --person ana --json` previews permanent erasure,
including children, guests and already removed people. It counts personal feedback, imports,
history, shelves and Row Lab ideas, and shared watches per remaining member.
Repeat with `--yes` or `--plan HASH`. Personal data and connector links are
deleted. Shared watches keep teaching remaining people, without the erased name.
Use `--shared drop` to remove those watches too; their titles then stay seen.
The choice is part of the plan hash. Other people's own ratings stay. Shared shelves with that person are
deleted. Forget cannot be undone.

The preview also counts the person's search records (`forget.counts.searches`),
decision records (`decisions`), daily job records (`daily`), dismissed suggestions
(`suggestions`), legacy saved wishes (`wishes`) and old legacy settings copies
(`settings_copies`). They are removed before the fresh backup.
The preview includes `forget.counts.backups`, the number of older backup files
that will be deleted. When it is above zero, text and search previews say:
"Older backups to delete: N. Skald then takes one fresh backup."
After a successful forget, Skald deletes every file in DATA/backups, including
daily, weekly, pre-migration and private settings copies, then takes one fresh
verified state backup. A failed forget keeps the old backups.
The JSON result includes `backups_deleted` and `fresh_backup` (the archive name,
or null if creating it failed). The deleted count includes the safety settings
copy made at the start of the forget. Text says:
"Older backups deleted: N; fresh backup: NAME."
If creating the fresh backup fails, the old copies stay deleted and text says:
"Older backups deleted: N. No backup exists until the next daily backup."

Skald also removes the person's saved AIOStreams login and prefix mapping. If either
existed, the preview and result say: "Ben's Odin login and its watch history still
exist in Odin and AIOStreams. Remove them there if Ben should lose access."

`python -m skald erase --household --json` previews erasing every person and all
household data, with counts and a plan code. Confirm with
`python -m skald erase --household --yes --plan CODE --confirm ERASE --json`.
Both confirmations are required. Without `--json`, a terminal can ask you to type
ERASE instead of `--confirm ERASE`. A wrong word or changed plan deletes nothing.
All DATA files are deleted by default, including credentials and every Skald backup.
Only addon and server remote tokens, `.skald-format`, the addresses `base`,
`internal_base` and `aio_url` in `setup.json`, stable server locks and an empty
`.skald-erased` generation marker stay. Existing addon links answer with empty rows; run
`skald setup household`, then restart Skald. Backups outside Skald's data folder are not
touched. Delete them yourself. Search "erase everything" and the HTTP API only preview.

`skald members away ben --person ana --json` previews a temporary absence.
Apply with `--yes` or `--plan HASH`. Their profile and link keep working and
their own picks keep learning, like a named guest. Their evidence and settings
do not affect other people, Together or household rows. Their Home row and
shelves stay saved. They still count toward the six-profile limit.
`skald members back ben --person ana --json` previews their return. Both away
and back support `members --undo --person ana`. Another adult must make these
changes; children and guests cannot. You cannot pause or forget yourself or
the last adult at home.

Add `--days 14` or `--until 2026-11-01` to `members away` for a dated absence.
Days must be 1..30 from today; until must be today or within 30 days. The person
stays away through that whole local day and returns the following day. The
preview shows the return date. Repeat away with a new date to shorten or extend
an absence. Without a date, away keeps its current indefinite behaviour.
The worker checks dates at startup and each cycle. Automatic returns are
journaled and undoable. Return and removal clear the date; their undo restores
it. An expired date restored by undo becomes due again on the next cycle;
change the away date to extend the absence. Forget clears it permanently.

Commander also accepts `Lena moved out`, `We broke up, remove Lena`,
`Eva passed away`, `Vergiss Lena`, `Zaboravi Lenu`, `pause Ben`,
`Ben ist eine Weile weg`, `Ben je odsutan neko vreme`, `Ben is back`,
`Ben ist zurück` and `Ben se vratio`. Dated examples include `Ben is away until
1 November`, `Ben ist bis Ende des Monats weg`, `Ben je odsutan do petka` and
`pause Ben for two weeks`. Each shows one preview before `yes NN`, except forget,
which shows a read-only preview and the server command to run.
Removal keeps a neutral description and shows `python -m skald members forget NAME --person ADULT`
to erase history too. Reasons for leaving are not stored.

`household.away`, `household.away_until` and `history-seen.json` are optional,
so FORMAT stays unchanged. Older validators retain the absence fields and
ignore the return date.
A 1.4.9 rollback ignores absence and treats the person as a full member again.
It also ignores the new seen-only record, so keep the current server for these
learning and seen guarantees.

Commander accepts these changes in English, German and Serbian, including
`Add my best friend Oskar as a guest`, `Entferne Ben` and `Ukloni Bena`.
Every child needs an age. `Add my kids Mia 9 and Leo` asks how old Leo is
before adding either child. Reply with the age, then confirm the complete preview.
You can interrupt a pending question with a title search.
Confirm the exact `yes NN` preview code. `Undo that` reverses the last change.
`Add a catalog for Oskar and me with heist movies` creates a shelf on the
requesting member's Home, with both people's hides, excluded genres, seen and
negative filters. Guests' reactions never shape household taste or the Together
label. Pronouns are used to understand speech and are never stored as profile fields.
For a guest with ratings, a shared shelf averages the host's and guest's member
scores equally. With no guest ratings it keeps the host's ranking. Shared shelves
between full members keep the host's ranking.

Commander also accepts `Make a catalog for Kim on my Home with invented heists
for 3 days` or `a catalog for Kim and me with invented heists until Sunday`.
The shelf belongs to the host's own scope and uses Kim as its second person.
Guest retirement hides it as usual; its own end removes it and records an undo.
Undoing that expiry restores the shelf without the elapsed end date.
Temporary shelves and shelves with a guest partner require an adult household member.
Guest and shelf end dates are optional fields, with no FORMAT change.
Version 1.4.9 loads them but ignores the end dates: the guest and shelf stay.

Data format stays unchanged: guests, ages, retired people and row ids and the optional
second person on a shelf are optional fields older servers ignore. Keep the
current server when using guest isolation and retirement. Retired identities
remain in the saved members list so older servers can load their client records.
Before rolling back to 1.4.9, copy `ops/rollback-1.4.9.patch` into that
source tree and run `patch -p1 < ops/rollback-1.4.9.patch`. It makes undo check saved
evidence for every person in a multi-person addition before removing anyone.
Rolling back to df66a3f makes saved retired people active again, removes guest
isolation and reactivates their scopes and tokens. It ignores retired row ids
and shared-shelf partner filters. Ages alone are ignored, while existing
`prefs.kids_age` still applies.
That release also refuses configs with more than six saved member ids. After
adding a replacement for a retired person, direct rollback is only possible
while the saved member list still has at most six ids. Removing another person
does not shorten that list; it keeps their evidence too.

`skald command --person ana "rename Ana to Anja"` previews a display name change.
On a shared watching link, a named adult can rename themselves without choosing Who's watching;
the normal `yes NN` code still applies. Age changes, adding members and child targets need the usual adult identity.
`skald command --person ana "rename the together row to Anja & Ben"` renames the Together label.
German `nenne gemeinsam um in Wir zwei` and Serbian `preimenuj zajednički red u Nas dvoje` work too.
Together changes need an identified adult. Confirm, restart Skald to apply member labels, and use `undo` to restore them.
Display names accept 1..40 characters without control characters. Row rename previews show the current label.
`undo no horror` reverses a matching change while keeping unrelated newer settings.
Scalar undo refuses if that value changed since. Plain `undo` remains LIFO.
`redo no horror` and `do again no horror` use the usual preview and confirmation.
`reset filters`, `reset shelves` and `reset everything to default` list what clears.
Repeat with `--plan HASH` or `--yes` to apply; one undo restores the reset.
Add `for us` to reset adult filters or Together shelves. Personal shelves stay intact.
Together filter resets need a shared link or an unbound CLI/API session.
Resets preserve ratings, learned taste, imports, viewing history and household rows.

This Week keeps new, worthwhile picks fixed from Monday at local midnight until the next Monday. Movies need a digital or streaming release in the last 42 days; series need a premiere or season start in the last 28 days. Charts alone do not qualify. Known calibrated matches need at least 70%; without calibration, only the leading fifth of raw taste scores qualifies (at least 3, at most 12 candidates). The row shows fewer cards when needed and hides below six. Watched or disliked titles drop out during the week. People changes rebuild it; other taste changes wait until next week.

The background job reuses cached releases. If it has no recent movie candidates, it can fetch one digital discovery page and check at most 12 movies, even with the Digital row disabled. It never looks up releases while serving the row. It attempts at most one weekly model call across languages and restarts, under the existing feed budget with label `week`; failures and people changes use taste order with factual reasons. Eight weeks of picks stay in `learn.json`; older versions ignore these extra keys. Ask Commander “show this week's picks”, “what was new last week?” or “show the last 3 weeks of saved picks”. These are read-only requests understood by the command model. German and Serbian work too.

`This Week for <people>` (`weekpicks`) adds personal picks from any year directly
after New This Week. It makes at most one extra call per local calendar week,
under the feed budget with label `weekpicks`. A failed call, no AI key or no budget
leaves this row empty for the week. Reasons use fixed translated text and an
optional loved-title connection. The two weekly rows never share a title.
`python -m skald rows week --weeks 2 --person ana --json` and Commander op
`week_picks` (`values: [2, 0]`, `person: ana`) keep their existing `weeks` field
and add `personal`, a list of weeks with personal picks in the same pick format.
Both histories keep eight weeks. Forgetting a person clears learned weekly history
through the existing cache cleanup. A week-only marker in `usage.json` keeps the
one-call limit after forgetting. These optional learning and config fields are
ignored by older servers; the data format version stays the same.
History keeps 180 days and at most 200 entries per scope, including undone entries.
Undated legacy entries stay and support plain undo; they cannot be targeted or redone.
Children can read their history and manage their own shelves through command previews.

`hide anime and cap movies at 90 minutes for Ana` previews both changes as one plan; confirming applies all or none, and one `undo` reverts the whole plan.
Right after a change, `no, I meant Ben` or `not horror, thriller` fixes it: a pending preview is replaced, an applied change is undone and the fixed one previewed.
Unclear requests such as `maybe hide some of those?` get one short question and change nothing.
`I wish there were more gentle mysteries` is noted in `wishes.jsonl` without changing settings.
`limit my Classics shelf to movies under 90 minutes` adds a filter to one existing shelf (`exclude movies` keeps only series); `Christmas movies every December` adds a shelf that shows only in those months.
`add a household member named Kim, age 12` and `update Ben to age 13` preview member changes; they apply on confirmation, and `undo` reverts them unless a new member already has data.
`something like Arrival`, `movies by Kim` and `something cozy tonight` run a search and change nothing.

Title commands work on one title and preview first like other changes.
`not tonight Arrival` (also `heute abend nicht`, `ne večeras`) hides it from your rows until 05:00 the next morning; `hide Arrival until 2026-12-24` or `blende Arrival bis zum 24.12.2026 aus` hides it until that day starts. Dates must be after today and within a year. Deferrals live in `title_fixes.json`, expire by themselves and do not hide the title from search.
`Arrival is not for me, too slow` marks it not for you; known reasons such as too dark, too slow, too violent, too silly or not my genre are kept with the rating, other wording stays only in the command history, also when the title is already marked not for you.
`I already watched Arrival`, `ich habe Arrival schon gesehen` and `već sam gledao Arrival` mark it seen.
`don't learn from my watching of Arrival` keeps that plain watch out of your taste and your share of the household taste; it still counts as watched, and your ratings of it still count. `Ben watched Arrival, not me` moves your watch of it to Ben, who can then fix it the same way. Both need a recorded watch of yours and change taste only, not household history.
`why is Arrival in my picks` explains the row without changing anything.
`are my best matches on my home?` reads reach for your profile, with the row ids and why missing matches were dropped. It changes nothing.
`the movie Calm` or `der Film Happy` searches for a title with that name.
Plain `undo` reverts a title command; `undo` with a phrase does not target them. Forgetting a person also removes their title fixes; a moved watch goes with the member it moved to.

## Shelves

`skald shelf add --person ana --when "sun 05-12" --name "Sunday Classics" "a classic film"` previews a wish and prints its plan hash. Repeat with `--yes` or `--plan HASH` to apply. Use `--together` instead of `--person` for shared slots.
`skald shelf list [--person ana]` lists wishes. `skald shelf stop --person ana NAME` stops one; a slot number also works. `skald shelf replace --person ana --slot 1 --yes "another wish"` replaces it.
Add `--days 3` or `--until 2026-10-18` to `shelf add` or `shelf replace`
for an end date. `--with-person kim` uses Kim's seen titles and hides too.
For example, `skald shelf add --person ana --with-person kim --days 3
--json "invented heists"` previews a temporary shelf on Ana's Home.
Repeat with `--yes` or `--plan HASH`; `skald command --undo --person ana --json`
restores the previous shelf state.
`skald shelf count --person ana 6` or `skald shelf count --together 3` changes the number of slots. These commands use local DATA_DIR. GET `/api/v1/shelves` lists a bound member's own wishes plus Together; POST accepts `{ "plan": "HASH" }` from a local preview. Child and guest tokens cannot use this API.

Lowering the shelf count keeps hidden wishes saved. Raise the count to show them again. Shelf filters use the same runtime bounds (1..600 minutes), year bounds (1..9999), two-letter language codes and TMDB genre names as personal preferences.

`python -m skald rows names --json` lists every row ID, its label key, the label shown
in the row language, its source (`fixed`, `template`, `ai`, or `custom`), and
missing translation keys. Because and feeling rows also include the seed ID
and title. JSON includes `lang` (titles), `row_lang` (labels) and
`language` (the preview language). `--lang de-DE` only previews another label language. It reads
only local config and cached row names, including hidden rows. Use `--snapshot DIR`
for a copied directory. It never builds rows or contacts a service.

Ask Commander “What are my rows called?”, “Wie heißen meine Reihen?” or
“Kako se zovu naši redovi?” for the same read-only list. Rename an existing row with
`python -m skald command "rename For You to Ana Picks" --person ana --json`.
Review the preview, then repeat it with `--plan HASH` to confirm. Commander also
previews row renames and waits for confirmation. Custom names use the existing
`household.row_labels` setting and take priority over translated or AI names.

Row names, card labels and reasons use optional `region.row_lang`, with English
fallback per key. Without it they follow `region.lang`, as before.
`python -m skald setup --lang de-DE --row-lang en-US` gives English labels with
German titles and plots. TMDB requests, artwork and install URLs keep using
`region.lang`. `python -m skald setup --row-lang same` removes the override.
Label languages need a bundled locale: English, German or Serbian. Serbian scripts
can be set with `sr-Latn-RS` or `sr-Cyrl-RS`. Restart Skald after changing the setting,
just as with `--lang`; `setup --status --json` shows both languages. Cached card
labels rebuild after restart. Current weekly picks use factual reasons in the new
language without another weekly model call.
Commander also understands “show my row names in English”, “Reihennamen auf Deutsch”
and “Prikaži nazive redova na engleskom”. Review and confirm the preview, then
restart Skald. Undo restores the previous setting and also needs a restart.
Keyword lanes get one naming call per lanes pass, batching only lanes without a
name in that language. Successful names stay in `learn.json` under each lane's
`titles` map. No AI, a failed response, or a spent feed cap keeps the old name.
Names are natural phrases of at most four words in the household language, with
no `&` double titles. Code checks the word count; the naming decision chooses the wording.
The call shares `AI_DAILY_FEED_CALLS` (40 by default); a stable set of named lanes
adds no daily calls. Names survive retirement and restarts. Existing cached card
reasons refresh on their next feed build. New episode and season details appear
in the card description. The optional cache keys keep DATA at FORMAT 5, so the
previous build can still load it without a migration.

Row Lab tries at most two new ideas and shows at most three lab rows after the
personal rows. It uses cached, unseen titles already known to Skald, excluding
Home picks. A household gets at most one proposal call on each local calendar
date, including failures. Failed calls leave Home working. Names and catalog IDs
stay with an idea when its picks are refilled; retired catalogs return no picks.
Each daily call can also refill up to three kept ideas. Commander receives current
and recent lab names within its row context limit; use the ID from `rows lab` for
an older idea.

Row metrics must be enabled for Row Lab. The optional `lab` configuration defaults
to `enabled: true`, `trial_showing_days: 10` and `kept_showing_days: 30`. Turning
metrics off pauses proposals, learning and lab rows. The optional private
`lab.json` file is backed up; older servers ignore it. DATA FORMAT stays 5.

A trial is kept after its first attributed play. Trials expire after ten distinct
showing dates without a play. Kept ideas expire after thirty showing dates since
the last credited play or manual keep. Fetching twice on one date counts once;
a date with no fetch does not count. A delayed credit can revive an automatic
retirement. Kept ideas appear only in their recorded play moments, or in any
moment while they have none. Calendar month restrictions also apply.

Moments use `region.tz`, with exclusive end times and late checked first: late
is 22:00 to 04:00, weekend is Friday 17:00 to Sunday 22:00, evening is Monday to
Thursday 17:00 to 22:00, and day is everything else.

Commander understands “what is Skald trying out?”, “which rows work on weekends?”,
“keep the Quiet Puzzles row”, “drop the Quiet Puzzles row” and “stop/start trying
new rows”, including German and Serbian. Row names resolve through the supplied
ID and name context. Ambiguous names ask for an ID. Controls use the usual preview,
exact `yes NN` confirmation and undo. These mechanics do not establish taste lift.
