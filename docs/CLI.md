# Commands by task

In Docker, prefix server commands with `docker compose exec engine`.
On a laptop, use `python3 -m skald` instead of `skald` (Python 3.12).
Replace `alex` with your configured member name.
For local inspection, use `--snapshot /path/to/copy` before the command.
The copy is read-only. Imports are saved separately in `$XDG_DATA_HOME/skald`,
or `~/.local/share/skald`. Change that with `--state-dir PATH`.
`--json` prints machine results. Use `skald COMMAND --help` for flags.

## See your taste

| Command | What it does and an example |
|---|---|
| `me` | Show what is known about you: `skald --snapshot /path/copy me --person alex` |
| `taste show` | Show your taste profile: `skald --snapshot /path/copy taste show --person alex` |
| `taste why` | Explain a title's fit: `skald --snapshot /path/copy taste why tmdb:123 --person alex` |
| `taste report` | Summarize recent learning: `skald --snapshot /path/copy taste report --since 7` |
| `explain` | Show scoring details: `skald --snapshot /path/copy explain tmdb:123` |
| `rows` | Preview rows: `skald --snapshot /path/copy rows --n 10` |
| `learn` | Inspect viewing signals: `skald --snapshot /path/copy learn` |
| `suggest` | Read your weekly habits: `skald suggest --person alex`. Use `--dismiss ID` to hide a theme for 90 days in private DATA_DIR. Listing writes nothing; a read-only copy also works with `--snapshot`. |
| `memory` | Inspect saved preferences: `skald --snapshot /path/copy memory` |
| `metrics` | Show recent activity: `skald --snapshot /path/copy metrics --days 7` |

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
| `import` | Preview an export: `skald import imdb ratings.csv --person alex --remote --dry-run` |
| `imports list` | List local imports: `skald --snapshot /path/copy imports list` |
| `imports undo` | Undo a selected local import: `skald --snapshot /path/copy imports undo IMPORT_ID` |
| `push imports` | Preview sending resolved titles: `skald push imports --remote` |

Review the preview, then add `--apply` after approval. The `--remote` route uses HTTP, without SSH.
With `--remote`, titles resolve on your server; no laptop TMDB key is needed, even for dry runs.
Use `--resolve-online` with laptop `TMDB_API` for lookups without the server.
Per-person tokens skip other members' titles in preview and apply; a household token sends everyone's.
The older `--live` route uses SSH and has no default host; set `SKALD_SSH_HOST` explicitly.

## Connect sources

| Command | What it does and an example |
|---|---|
| `setup` | Guide a household through setup: `skald setup` |
| `setup --status` | Show done, missing and one next command: `skald setup --status --json` |
| `setup aiostreams` | Make a login with Skald and title details, or update one: `skald setup aiostreams --uuid <your uuid>` |
| `setup link` | Issue a private laptop token: `skald setup link --name laptop --person alex` |
| `setup prefs` | Set member rows: `skald setup prefs --person alex --discovery gems --years 1990- --exclude-movie-genres horror --exclude-series-genres reality,talk --hide documentaries,anime --focus-movie-genres horror,drama --focus-series-genres drama --focus-languages es --focus-countries ES`; `--hide` adds movies, series, documentaries, animation or anime; `--show anime` removes a hide; focus ranks without filtering; empty focus lists clear that group. `--kids-age 8` (3..17) limits all Skald rows and search for that child; `--kids-age off` clears it. Other addons and streams are not filtered; use the app's profile lock too. TMDB ratings can be missing (titles are hidden) or wrong. Reset with `skald setup prefs --person alex --clear`. Omitted flags keep other settings; `setup --status` shows each member. |
| `setup adopt` | Make a legacy household editable without changing its config: `skald setup adopt --yes` |
| `setup rows KEY,KEY,...` | Choose home rows in Skald's usual order: `skald setup rows episodes,foryou,top10,digital,toprated`. The two movie lists are opt-in; search stays available. `skald setup rows all` restores default rows. See [setup](SETUP.md) for valid keys. Changes refresh AIOStreams rows. |
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
| `--disconnect` | Remove a member MDBList or Stremio connection and private key: `skald setup --disconnect mdblist --person alex` |
| `--connect` | Taste source: files, trakt, mdblist, stremio, jellyfin, plex or aiostreams-db |
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

Search `every sunday morning a classic for us` or `weekday evenings something light for me` for a scheduled shelf in the household timezone (Sunday 05-12 or weekdays 18-24). Use `for MEMBER` for a named person, or `for us`, `for both`, `for both of us`, `for everyone` or `for together` for a Together shelf. `make me a row of heist films` creates an always-on shelf. `stop Classics` or `stop the sunday shelf` previews stopping an existing shelf with that name; unmatched names stay title searches; `undo` restores it. `show 6 shelves` or `fewer shelves` previews a slot count change (0..10). After shelf changes Skald asks AIOStreams to reload row names for logins made by `skald setup aiostreams`; Stremio installed with Skald's own link may need the link reinstalled to show new names. Full shelves ask you to stop one first. `my settings` or `settings` lists personal and Together shelves. A member id or full label also works in `my settings for NAME`, `NAME settings`, `NAME's settings`, `settings NAME` or `settings for NAME`. `help` shows five search examples. `ideas` and `suggest` offer at most two habit suggestions; `yes NN` creates one and `no NN` dismisses it for 90 days. The exact `yes NN` code comes first in each preview or suggestion card name. Open a card to read its details. Command cards appear once, in movie search results; paired series requests return no command cards. Shared links ask for a member. Children can manage their own shelves; fills keep their age filter, and they cannot change Together or other people's shelves.

`skald command --person ana "hide anime"` previews a change and prints its plan hash. Repeat the same text with `--yes` or `--plan HASH` to apply it. Use `skald command --person ana --undo` to revert the last command.
Title reactions, genre exclusions, focus, discovery and years use the same writer as search. In search, type the exact “yes NN” code from the preview; plain “yes” changes nothing. Identical mutation requests within 60 seconds repeat their first result.
The API previews with `POST /api/v1/decide/parse_command`, body `{"in":{"query":"hide anime"},"person":"ana"}`. Apply within ten minutes with `POST /api/v1/command`, body `{"plan":"HASH"}`, or undo with `{"undo":true}`.
Bound tokens can change only their own person. Guest and child tokens cannot change settings.

## Shelves

`skald shelf add --person ana --when "sun 05-12" --name "Sunday Classics" "a classic film"` previews a wish and prints its plan hash. Repeat with `--yes` or `--plan HASH` to apply. Use `--together` instead of `--person` for shared slots.
`skald shelf list [--person ana]` lists wishes. `skald shelf stop --person ana NAME` stops one; a slot number also works. `skald shelf replace --person ana --slot 1 --yes "another wish"` replaces it.
`skald shelf count --person ana 6` or `skald shelf count --together 3` changes the number of slots. These commands use local DATA_DIR. GET `/api/v1/shelves` lists a bound member's own wishes plus Together; POST accepts `{ "plan": "HASH" }` from a local preview. Child and guest tokens cannot use this API.
