# Work on Skald

Skald builds personal recommendation rows and search for a household.
It learns from watch history and ratings; existing streaming addons handle playback.

## Start with the task

Read the code and its callers before editing. Make the smallest complete change.
Write a failing test first, then fix it and run the relevant checks.
Use Python 3.12 and its standard library only — no third-party dependencies, ever.
Run commands from the repository root:

- `python -m unittest discover -s tests` runs the offline suite.
- `docker build .` checks the container build when Docker and network are available.
- `python -m skald --help` lists commands; see `docs/CLI.md` for tasks.
- `python -m skald setup` starts household setup; see `docs/SETUP.md`.

Optional private snapshot tests use SKALD_SNAPSHOT and SKALD_SNAPSHOT_U.
Set them to snapshot directories only when authorized to read those inputs.
Those tests skip when the variables are unset.
SKALD_ROW_ORDER optionally selects a player order file; otherwise rows use the server order.
Never commit DATA, watch history, credentials, tokens or private snapshots.

## Repository map

- `server.py`: HTTP server, household config, scoring, rows and connector adapters.
- `skald/__init__.py`: package entry point.
- `skald/__main__.py`: CLI parser, dispatch and output.
- `skald/backup.py`: archive listing, verification, creation and restore.
- `skald/boot.py`: container startup and waiting for household setup.
- `skald/connectors.py`: source account discovery and read-only connector checks.
- `skald/engine.py`: copied-data facade for offline inspection and simulation.
- `skald/guide.py`: doctor, personal summaries and next actions.
- `skald/intake.py`: import review, resolution, plans and local state.
- `skald/live.py`: explicit SSH snapshot and import transport.
- `skald/members.py`: shared household config and row id helpers.
- `skald/migrate.py`: data format checks and numbered migrations.
- `skald/recipes.py`: shared setup and watching instructions.
- `skald/remote.py`: linked HTTP API and resolved import upload.
- `skald/setup.py`: household setup, private credentials and laptop linking.
- `skald/trakt.py`: Trakt device login.
- `skald/ingest/`: local export parsers and title resolution.
- `skald/ingest/__init__.py`: candidate model, parser discovery and source registry.
- `skald/ingest/chrome.py`: allowlisted browser history imports.
- `skald/ingest/imdb.py`: IMDb CSV imports.
- `skald/ingest/letterboxd.py`: Letterboxd export imports.
- `skald/ingest/netflix.py`: Netflix viewing history and profile mapping.
- `skald/ingest/plain.py`: one-title-per-line imports.
- `skald/ingest/resolve.py`: title matching and resolution.
- `skald/ingest/trakt.py`: Trakt JSON export imports.
- `skald/ingest/youtube.py`: YouTube Takeout history imports.
- `eval/`: offline replay, decision checks and synthetic fixture goldens.
- `tests/`: unittest checks and synthetic fixtures.
- `docs/`: public setup, commands, connectors, watching and upgrades.
- `skills/`: shipped agent guidance for taste imports and the decision API.
- `compose.yaml`: direct self-hosted engine deployment.
- `compose.selfhost.example.yaml`: example self-hosted deployment.
- `compose.stack.example.yaml`: example Skald and AIOStreams stack.
- `Dockerfile`: Python 3.12 container running as an unprivileged user.

## Keep the project working

Build every row and catalog id with rid(); never use a literal prefix.
Never rewrite stored ids. A stored DATA shape change needs a numbered migration
in `skald/migrate.py`, a bump of FORMAT, and a migration test.
Read secrets through getpass or stdin, or store them in mode 0600 files.
Never accept secrets in argv or print them.
The threat model is `docs/CONNECTORS.md`: one uid owns DATA and controls its files.
Keep that boundary; do not over-harden for unrelated threats.
Write docs in plain words a stranger can follow. Keep help and docs in sync.
Use short imperative commit subjects, such as "Explain setup flags".

Grow the evals with the feature. When you add or change a command, a search
phrase or anything a model parses, add cases in the same change:
`eval/commands_cases.jsonl` for commands, `eval/fixtures/decisions/<name>.jsonl`
for search routing, title resolution and picks. Cover English, German and
Serbian, typos, indirect wording and look-alikes that must not trigger it.
Mark cases for behaviour not built yet `"contract": "next"`; switch them to
`now` when it lands. Run `python eval/commands_eval.py --fixture` and
`python eval/decisions.py` offline. Before a release, compare models with
`python eval/commands_eval.py --live --models A,B` and note accuracy and cost
in `eval/README.md`.

Version releases slowly. A patch release (1.4.1) carries fixes and small
additions that need no DATA migration and change no command, setting or default.
A minor release (1.5.0) is only for a FORMAT bump, a changed or removed command
or setting, or a changed default users will notice. Batch work into one release;
do not release per feature. Add each change to the next CHANGELOG entry.

Generated text has one source. Do not hand-edit its copies:

- `skald/recipes.py` supplies `docs/WATCH.md` through watch_doc().
- `python eval/decisions.py --skill` prints the decide-API block for
  `skills/skald-taste/SKILL.md`; add `--write` to regenerate it intentionally.
- `python eval/profile_golden.py --fixture --check` checks fixture goldens.
  Use `python eval/profile_golden.py --fixture --write` only for an intended change.

## Where to look first

- Add a row: start with row definitions and serving in `server.py`, then
  `tests/test_rows_r2.py` and `tests/test_row_prefix.py`; use rid() throughout.
- Add a connector: start with adapters in `server.py`, discovery and probes in
  `skald/connectors.py`, setup in `skald/setup.py`, and `docs/CONNECTORS.md`.
- Add an import format: start with `skald/ingest/__init__.py` and a nearby parser;
  add its IMPORT_TRUST entry in `server.py` and synthetic cases in `tests/test_ingest.py`.
- Add a CLI command: start with parser() and dispatch in `skald/__main__.py`,
  reuse `skald/engine.py` or the existing task module, and update `docs/CLI.md`.
