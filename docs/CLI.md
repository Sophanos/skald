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
| `--disconnect` | Remove a member MDBList connection and private key: `skald setup --disconnect mdblist --person alex` |
| `--connect` | Taste source: files, trakt, mdblist, jellyfin, plex or aiostreams-db |
| `--name` | Name for the laptop token created by `setup link` |
| `--person` | Household member whose source or laptop token to use |
| `--source-url` | Address of the Jellyfin or Plex server |
| `--source-path` | Path to the AIOStreams database inside the engine |
| `--user-id` | Jellyfin user id to map to a member |
| `--auth-env` | Environment variable holding the source token or API key |
| `--client-id-env` | Environment variable holding the Trakt client id |
| `--scopes` | Comma-separated laptop permissions; default: read,decide,events |
| `--map` | Map `SOURCE_ID=MEMBER` or `SOURCE_ID=together`; repeat for each account |

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

MDBList: get a free key at mdblist.com/preferences, then run `skald setup --connect mdblist --person alex`.
Use the private prompt or `--secret-stdin`. Run `skald connectors test mdblist` to check it.
Restart the engine after setup; it syncs watched history and watchlists automatically.
Use the Plex webhook from MDBList Preferences or MDBList plugins for Emby/Jellyfin to record plays.
See [connectors](CONNECTORS.md#mdblist) for limits and disconnect instructions.
