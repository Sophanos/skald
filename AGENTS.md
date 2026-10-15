# Work on Skald

Skald builds personal recommendation rows and search for a household.
It learns from watch history and ratings; existing streaming addons handle playback.

## Start with the task

Read the code and its callers before editing. Make the smallest complete change.
Write a failing test first, then fix it and run the relevant checks.
Use Python 3.12 and its standard library only. No third-party dependencies, ever.
Run commands from the repository root:

- `python -m unittest discover -s tests` runs the offline suite.
- `docker build .` checks the container build when Docker and network are available.
- `python -m skald --help` lists commands; see `docs/CLI.md` for tasks.
- `python -m skald setup` starts household setup; see `docs/SETUP.md`.

A change is done when the suite, `python eval/profile_golden.py --fixture --check`,
`python eval/decisions.py`, `python eval/commands_eval.py --fixture` and `git diff --check` pass.
Tests use synthetic data in a temporary DATA folder. Stop every thread or pool a test starts
before that folder goes; `offline_server` in `eval/profile_golden.py` does this for the server.

Optional private snapshot tests use SKALD_SNAPSHOT and SKALD_SNAPSHOT_U. Set them to snapshot
directories only when authorized to read those inputs; unset, those tests skip.
SKALD_ROW_ORDER optionally selects a player order file; otherwise rows use the server order.
Never commit DATA, watch history, credentials, tokens or private snapshots.

## Repository map

- `server.py`: HTTP server, household config, scoring, rows and connector adapters.
- `skald/__init__.py`: package entry point. `skald/__main__.py`: CLI parser, dispatch and output.
- `skald/commands.py`: preview and apply one change, shared by search, CLI and API.
- `skald/members.py`: household config and row ids. `skald/leave.py` forgets a person;
  `skald/erase.py` erases the whole household.
- `skald/backup.py`: archive listing, verification, creation and restore.
- `skald/connectors.py`: source account discovery and read-only connector checks.
- `skald/engine.py`: copied-data facade for offline inspection and simulation.
- `skald/guide.py`: doctor and next actions. `skald/migrate.py`: FORMAT checks and migrations.
- `skald/i18n.py` and `skald/locales/`: fixed interface text in en, de, sr-latn and sr-cyrl.
- `skald/intake.py`: import review, resolution, plans and local state.
- `skald/boot.py`: container startup. `skald/live.py`: explicit SSH snapshot and import transport.
- `skald/recipes.py`: setup and watching text. `skald/remote.py`: linked HTTP API and import upload.
- `skald/setup.py`: household setup, private credentials and laptop linking.
- `skald/trakt.py`: Trakt device login. `skald/plays.py`: Odin watch state from AIOStreams.
- Home helpers: `skald/row_names.py`, `skald/rowplan.py`, `skald/lab.py`, `skald/ready.py`.
- `skald/ingest/`: one parser per export; `skald/ingest/__init__.py` holds the candidate model
  and source registry, `skald/ingest/resolve.py` matches titles.
- `eval/`: offline replay, decision checks and goldens; `eval/household/` diffs Home between builds.
- `tests/`: unittest checks and synthetic fixtures. `docs/`: public docs for users.
- `skills/`: shipped agent guidance for taste imports and the decision API.
- `compose.yaml` runs the engine directly; `compose.selfhost.example.yaml` and
  `compose.stack.example.yaml` are self-hosted and Skald plus AIOStreams examples.
- `Dockerfile`: Python 3.12 container running as an unprivileged user.

## Keep the project working

Build every row and catalog id with rid(); never use a literal prefix.
Never rewrite stored ids. A stored DATA shape change needs a numbered migration
in `skald/migrate.py`, a bump of FORMAT, and a migration test.
New optional fields or files that older servers ignore are not a shape change:
they need no migration and keep FORMAT; say so in the CHANGELOG data line.
Read secrets through getpass, stdin or mode 0600 files; never accept them in argv or print them.
The threat model is `docs/CONNECTORS.md`: one uid owns DATA and controls its files.
Keep that boundary; do not over-harden for unrelated threats.
Write docs in plain words a stranger can follow, without em-dashes. Keep help and docs in sync.
Use synthetic names in examples, tests and docs, never real household or contributor names.
Use short imperative commit subjects, such as "Explain setup flags".

Give every new user-facing feature an agent CLI command with `--json` and a search (Commander) op.
Route user text through the model op parser, its hints and eval cases; no regex or phrase lists.
Models pick titles and actions; new text users read comes from fixed strings in `skald/locales`.
Forget and erase must remove anything new stored about a person; add a test that proves it.
Do not widen who can read, change or erase another person's data; leave the adult filter alone.

Grow the evals with the feature. When you add or change a command, a search
phrase or anything a model parses, add cases in the same change:
`eval/commands_cases.jsonl` for commands, `eval/fixtures/decisions/<name>.jsonl`
for search routing, title resolution and picks. Cover English, German and
Serbian, typos, indirect wording and look-alikes that must not trigger it.
Mark cases for behaviour not built yet `"contract": "next"`; switch them to
`now` when it lands. A new op or op hint also needs SKILL.md regenerated and the
tests that pin op counts updated on purpose. Before a release, compare models with
`python eval/commands_eval.py --live --models A,B` and note accuracy and cost
in the eval README.

Version releases slowly. A patch release (1.4.1) carries fixes and small
additions that need no DATA migration and change no command, setting or default.
A minor release (1.5.0) is only for a FORMAT bump, a changed or removed command
or setting, or a changed default users will notice. Batch work into one release;
do not release per feature. Add each change to the next CHANGELOG entry.

Generated text has one source. Do not hand-edit its copies:

- `skald/recipes.py` supplies `docs/WATCH.md` through watch_doc().
- `python eval/decisions.py --skill` prints the decide-API block for
  `skills/skald-taste/SKILL.md`; add `--write` to regenerate it intentionally.
- `python eval/profile_golden.py --fixture --write` rewrites fixture goldens; only for an intended change.

## Where to look first

- Add a row: start with row definitions and serving in `server.py`, then
  `tests/test_rows_r2.py` and `tests/test_row_prefix.py`; use rid() throughout.
  Give it a plain everyday name in every locale. A renamed row keeps its old name
  working through the aliases in row_id() in `skald/commands.py`, with eval cases.
- Add a connector: start with adapters in `server.py`, discovery and probes in
  `skald/connectors.py`, setup in `skald/setup.py`, and `docs/CONNECTORS.md`.
- Add an import format: start with `skald/ingest/__init__.py` and a nearby parser;
  add its IMPORT_TRUST entry in `server.py` and synthetic cases in `tests/test_ingest.py`.
- Add a CLI command: start with parser() and dispatch in `skald/__main__.py`,
  reuse `skald/engine.py` or the existing task module, and update `docs/CLI.md`.
