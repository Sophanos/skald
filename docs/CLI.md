# Commands by task

In Docker, prefix server commands with `docker compose exec engine`.
On a laptop, use `python3 -m skald` instead of `skald` (Python 3.12).
Replace `alex` with your configured member name.
For local inspection, use `--snapshot /path/to/copy` before the command.
The copy is read-only. Imports are saved separately in `$XDG_DATA_HOME/skald`,
or `~/.local/share/skald`. Change that with `--state-dir PATH`.
`--json` prints machine results. Use `skald COMMAND --help` for flags.

`skald ai status --json` reads saved OpenRouter credit, the last check and failure,
Skald spending today and this month, and daily calls used per group. It works
offline and writes nothing. Add `--refresh` to check the key once now, using
`OPENROUTER_API_KEY` from the environment. `setup --status` shows a credit line.
Ask Commander “how much AI credit is left?” to read the same saved numbers.

## See your taste

| Command | What it does and an example |
|---|---|
| `me` | Show what is known about you, including your taste archetype: `skald --snapshot /path/copy me --person alex` |
| `taste show` | Show your taste profile: `skald --snapshot /path/copy taste show --person alex` |
| `taste align` | Read the titles and facets shaping your picks, with correction previews: `skald --snapshot /path/copy taste align --person alex --limit 10 --json`. Omit `--person` for the household. Writes nothing. |
| `taste why` | Explain a title's fit: `skald --snapshot /path/copy taste why tmdb:123 --person alex` |
| `taste report` | Summarize recent learning: `skald --snapshot /path/copy taste report --since 7` |
| `taste calibration` | Inspect the reference, score percentiles and cached row match bands without fetching or writing: `skald taste calibration --person alex --json` |
| `explain` | Show scoring details, unseen known rank per person, Home row presence, feeling rows sourced from the tagged catalog and the household taste archetype: `skald --snapshot /path/copy explain tmdb:123 --json`. A served Top Rated pick includes its rating, relative taste match and row position; use `--person ana` for that member. |
| `rows` | Preview rows: `skald --snapshot /path/copy rows --n 10` |
| `rows pool [--json]` | Read today's seed choices and last background pool counts: `skald --snapshot /path/copy rows pool --json`; missing offline endpoints are reported, and counts carry their build time. |
| `rows reach [--person NAME] [--top N]` | Check how many best unseen known matches appear on each profile's Home, with row ids and the first drop reason for missing titles: `skald --snapshot /path/copy rows reach --top 20 --json`. Omit `--person` for the household and every member. Reads stored data offline; it does not source or rebuild feeds. `--top` defaults to 20 and accepts 1..10000. |
| `rows plan` | Explain every registered home row, including hidden rows: `skald rows plan --person household --lang en-US --json`. Top Rated cards include type, position, TMDB rating, relative taste match (0..100) and blend score. Feeling rows include `candidate_sources`: catalog and recommendation counts before vetting or balancing, and `missing_cards` skipped because stored artwork is missing. Themed vetting examines the first 40 cards. Counts appear after a rebuild. Themed rows include `season.months` (month numbers, `"any"`, or `null` when unknown) and `season.hidden` for the household's local date. Reads local `DATA_DIR` or `--snapshot`; `--person` also accepts `together`, a member or `guest`. |
| `rows why ROW` | Explain one row and its kept and dropped cards: `skald rows why mood1 --person ana --json`. Use `skald rows why toprated --person ana --json` to list ranked movies and series with rating and taste. Commander can read the same list through its Home plan operation, or hide and show the row with `hide top rated` and `show top rated series`. Both types share the `toprated` control. Uses the same read-only build as `rows plan`. |
| `rows prefer` | Preview a household mood or lane limit change: `skald rows prefer --theme mood --more` or `--theme lane --fewer`. Defaults are three each; limits are 0..8. Apply with `--plan HASH` from the preview or `--yes`. Preview and apply refuse a reduction below six populated rows, or the current count if already lower, and never allow zero. Changes use the existing atomic `skald.json` write; the server reloads them on its next row request or worker pass. An explicit household `rows` list bypasses these limits. |
| `rows refresh ROW` | Preview refreshing one row: `skald rows refresh mood1`. Apply with `--plan HASH` or `--yes`; the CLI atomically queues row ids in `DATA_DIR/row_refresh.json`, without writing feeds or calling upstream services. The worker clears those rows' stamps and deletes the request at the start of its next feed pass. Older servers ignore the request. A grouped mood or lane build can also rebuild its siblings. Queued CLI refreshes have no undo. |
| `rows week [--weeks N] [--person NAME] [--json]` | Read saved weekly picks offline, including on an older snapshot. Defaults to the latest saved week; `--weeks 1..8` includes that week and earlier calendar weeks. For example: `skald --snapshot /path/copy rows week --weeks 4 --person ana --json`. |
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

Stremio: run `skald setup --connect stremio --person alex`. Paste the auth key privately,
or leave it blank for private email and password prompts. Scripts use `--yes --key-stdin`.
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

The family limits live in `household.home.family_caps`, for example `{"mood": 3, "lane": 3}`. Missing limits default to three, including when only row order or labels are configured. Plan counts describe the stored feeds under the current viewer's filters, including Family and active archived rows. Inspection uses cached metadata only and never enriches records, pins rows or records exposures. Metrics, when enabled, add shown and play counts from the last four weeks.

Search `every sunday morning a classic for us` or `weekday evenings something light for me` for a scheduled shelf in the household timezone (Sunday 05-12 or weekdays 18-24). Use `for MEMBER` for a named person, or `for us`, `for both`, `for both of us`, `for everyone` or `for together` for a Together shelf. `make me a row of heist films` creates an always-on shelf. `stop Classics` or `stop the sunday shelf` previews stopping an existing shelf with that name; unmatched names stay title searches; `undo` restores it. `show 6 shelves` or `fewer shelves` previews a slot count change (0..10). After shelf changes Skald asks AIOStreams to reload row names for logins made by `skald setup aiostreams`; Stremio installed with Skald's own link may need the link reinstalled to show new names. Full shelves ask you to stop one first. `my settings` or `settings` lists personal and Together shelves. `what do you know about NAME` answers for a named member. A member id or full label also works in `my settings for NAME`, `NAME settings`, `NAME's settings`, `settings NAME` or `settings for NAME`. `help` shows five search examples. `ideas` and `suggest` offer at most two habit suggestions; `yes NN` creates one and `no NN` dismisses it for 90 days. The exact `yes NN` code comes first in each preview or suggestion card name. Card names stay short (24 characters); open a card to read its details. Command cards appear once, in movie search results; paired series requests return no command cards. Shared links ask for a member. Children can manage their own shelves; fills keep their age filter, and they cannot change Together or other people's shelves.

`skald command --person ana "hide anime"` previews a change and prints its plan hash. Repeat the same text with `--yes` or `--plan HASH` to apply it. Use `skald command --person ana --undo` or type `undo` to revert the last command. `show my shelves` and `what are my settings` return an answer without a confirmation. Soft commands such as `less horror` or `more documentaries` move matching titles at most three places without banning them. `no movies longer than 2 hours` limits runtime; titles without runtime are hidden. `kids age 12 for ben` sets the existing child filter; an adult on a shared link can undo it. `I already watched Arrival` marks a verified title as seen; an ambiguous title needs its type. People focus keeps the actor or director name and ranks matching cached credits first.
Title reactions, genre exclusions, focus, discovery and years use the same writer as search. In search, type the exact “yes NN” code from the preview; plain “yes” changes nothing. Identical mutation requests within 60 seconds repeat their first result.
The API previews with `POST /api/v1/decide/parse_command`, body `{"in":{"query":"hide anime"},"person":"ana"}`. Apply within ten minutes with `POST /api/v1/command`, body `{"plan":"HASH"}`, or undo with `{"undo":true}`.
Bound tokens can change only their own person. Guest and child tokens cannot change settings.

`skald command --person ana "history"` lists settings changes with their age.

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
History keeps 180 days and at most 200 entries per scope, including undone entries.
Undated legacy entries stay and support plain undo; they cannot be targeted or redone.
Children can read their history and manage their own shelves through command previews.

`hide anime and cap movies at 90 minutes for Ana` previews both changes as one plan; confirming applies all or none, and one `undo` reverts the whole plan.
Right after a change, `no, I meant Ben` or `not horror, thriller` fixes it: a pending preview is replaced, an applied change is undone and the fixed one previewed.
Unclear requests such as `maybe hide some of those?` get one short question and change nothing.
`I wish there were more gentle mysteries` is noted in `wishes.jsonl` without changing settings.
`limit my Classics shelf to movies under 90 minutes` adds a filter to one existing shelf (`exclude movies` keeps only series); `Christmas movies every December` adds a shelf that shows only in those months.
`add a household member named Kim, age 12` and `update Ben to age 13` preview member changes; they apply after a restart, and `undo` reverts them unless a new member already has data.
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
`skald shelf count --person ana 6` or `skald shelf count --together 3` changes the number of slots. These commands use local DATA_DIR. GET `/api/v1/shelves` lists a bound member's own wishes plus Together; POST accepts `{ "plan": "HASH" }` from a local preview. Child and guest tokens cannot use this API.

`python -m skald rows names --json` lists every row ID, its label key, the label shown
in the household language, its source (`fixed`, `template`, `ai`, or `custom`), and
missing translation keys. `--lang de-DE` previews another UI language. It reads
only local config and cached row names, including hidden rows. Use `--snapshot DIR`
for a copied directory. It never builds rows or contacts a service.

Ask Commander “What are my rows called?”, “Wie heißen meine Reihen?” or
“Kako se zovu naši redovi?” for the same read-only list. Rename an existing row with
`python -m skald command "rename For You to Ana Picks" --person ana --json`.
Review the preview, then repeat it with `--plan HASH` to confirm. Commander also
previews row renames and waits for confirmation. Custom names use the existing
`household.row_labels` setting and take priority over translated or AI names.

Fixed row names and card labels use `region.lang`, with English fallback per key.
Serbian scripts can be set with `sr-Latn-RS` or `sr-Cyrl-RS`.
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
