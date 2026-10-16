# Skald engine and CLI

Python 3.12+, standard library only. Run from the repository root:

```sh
export SKALD_SNAPSHOT=/path/to/snapshot
python3.12 -m skald taste show --person ana
python3.12 -m skald onboard
python3.12 -m skald doctor
python3.12 -m skald me --person ana
python3.12 -m skald taste why Paddington --person ana --json
python3.12 -m skald rows --row foryou --n 10
python3.12 -m skald react love Paddington --person ana
python3.12 -m skald react not_for_us tmdb:116149 --person ana --reason 'too slow'
python3.12 -m skald learn
python3.12 -m skald import trakt /local/export.json --person ben --yes --dry-run
python3.12 -m skald import netflix /local/ViewingActivity.csv --person ana --profile-map 'Local profile=ana' --yes
python3.12 -m skald imports list
python3.12 -m skald imports undo EVENT_ID
python3.12 -m skald eval --protocols p4 --variants current --json
python3.12 -m skald push imports
python3.12 -m skald push imports --apply
python3.12 -m skald --live rows
```

Every command accepts `--json`, before the command or after its final subcommand. Text uses compact tables and terminal-only color. `--snapshot DIR` overrides `$SKALD_SNAPSHOT`. `--state-dir DIR` overrides `~/.local/share/skald`; it must be outside the snapshot. Inspection, reactions, evaluation and dry runs leave the snapshot and local state unchanged. A dry run can make API calls only if `--resolve-online` is explicitly supplied.

## Facade contract

The CLI, a TV addon adapter and a future decision API can use this same boundary:

```python
from skald import Engine

with Engine(data_dir, server_path=None, offline=True) as engine:
    taste = engine.profile(person="ana")
    score = engine.score("tmdb:116149", person="ana")
    explanation = engine.explain("tmdb:116149")
    home = engine.rows(lang="de-DE")
    simulation = engine.react([{
        "key": "tmdb:116149", "kind": "movie", "title": "Paddington", "year": "2014",
        "action": "love", "who": "ana"
    }], person="ana")
    learning = engine.learn()
```

All methods return detached JSON-compatible dictionaries. Callers receive no module, model, vector, set, lock or mutable server state. `close()` is also available when a context manager is inconvenient. `person=None` means the household; individual models are Ben and Ana.

| Method | Result |
|---|---|
| `profile(person=None)` | Entries without raw history, source/signal counts, top experiences/themes/creators, affinities and anchors |
| `score(key, person=None)` | Score, reasons, facets/anchors, percentile and confidence |
| `calibration(person=None)` | Reference kind and size, score percentiles and cached row match bands |
| `explain(key)` | Existing server explanation, including per-person scores |
| `rows(lang="de-DE")` | Actual snapshot rows through `row_metas`, with names, ordered title keys and why lines |
| `react(events, person=None)` | Changed ranks per served row and in the top 50, facet deltas; never persists events |
| `ingest(events)` | Validated events persisted to the caller's `data_dir/imports.jsonl`, then the copied model is rebuilt; returns added events/count |
| `learn()` | `learn_stats`, row credit, recent hits, tired titles and credited keys; dates have day precision |

The loader copies only named engine data files to a temporary directory. It stubs TMDB, AniList and OpenRouter in offline mode and never starts the HTTP server or feed worker. It uses cached metadata for local title resolution. If `profile.json` is absent, it reconstructs the profile with `build_profile` from cached metadata and `history.json`/`trakt.json`; unresolved history is skipped by the existing server behavior. Stored snapshot profiles retain their snapshot weights and stop readings. After event merges, the common profile builder applies Skald's stop reading to unread entries before building genre weights and the model. The facade calls the existing scoring/model functions without changing their rules.

Match percentages rank a title against a deterministic sample of at most 800 known, unseen, non-negative titles in the local facet store, scored for the household, member or Together. The model records `cal_reference` as `known` or `trending`. With fewer than 200 local titles, an online build falls back to trending; offline builds cannot fetch that fallback. Percentile and confidence remain `null` when the reference is unavailable or the person's taste evidence is too new. The display remains 40..99 with midranks for ties, so 70 is the median. Reaction ranks compare taste-sorted versions of the same served pools, plus unconsumed cached titles. They measure model response; they do not predict new feed membership, curation, availability or row-builder behavior. The same post-event candidate universe is used before and after. Negative reasons are accepted and validated by the existing remote schema; their effects follow the existing model.

## Local onboarding

`onboard` is the first-run entry point. With no saved onboarding state, `skald` prints
`start with: skald onboard`. On a terminal, bare `onboard` offers Quick or Custom
setup, then asks who the exports belong to, using the existing members plus
`both`. Quick offers `~/Downloads` and scans only the named folder's immediate
filenames; every match needs confirmation before it is opened. Custom asks for
sources and paths. Explicit flags keep scripted runs unchanged. `--quick` selects
the folder scan directly. `onboard --sources` shows export steps, URLs and expected
waits; all six guides are marked unverified because this pass had no network access.

Default output shows one count line per source, offline-review hints, at most six
learned notes, and the available maturity/count information. Full rank moves and
facet deltas appear with `--verbose` or `--json`. Missing maturity is displayed as
an explicit-rating count; the interview command is only a suggested next step.
JSON results have `v:1` and ordered `next:[{id,say,cli}]` actions. Parser failures
identify the source and local basename, without echoing export rows. Unmapped
Netflix profile names appear only in the local error, never in saved state.

Re-running resumes sources and never wipes them. `onboard --reset --person P`
requires terminal confirmation or `--yes`, and uses existing source undo to remove
that person's onboard imports. Reset is a separate operation from a new import;
it retains undo records. Repeated source/import forget operations report zero
changes. Corrupt `onboard.json` or `review.json` stops onboarding with exit 2 and
`run: skald doctor` before any writes.

`doctor` runs read-only local checks for readable data, private state modes,
onboarding/review state, unpushed imports, remote configuration metadata, mixed
facet keys, offline calibration, member rating counts and pending memory proposals.
It never opens `remote.json`. `doctor --json` returns
`{v:1,ok,checksRun,findings:[{checkId,severity,message,fixHint}]}`; exit codes are
0 for clean, 1 for findings, and 2 for an internal failure. `doctor --fix` creates
a missing state directory and repairs its mode to 0700 and immediate regular files
to 0600. It never deletes state, undoes imports or changes snapshot files.
`doctor keys` keeps its existing behavior. Mixed-facet loader diagnostics appear
only through doctor or onboard `--verbose`. For these front-door commands,
`--live` uses a cached live snapshot unless `--remote` explicitly allows a pull;
doctor's opted-in reachability pull goes to a temporary directory.

`me --person P` shows a bounded personal card: owned facet anchors, source status,
memory cards, pending counts and local forget actions. Its v1 JSON is at most 4 KB;
`say` is one sentence of at most 140 characters. `me forget --source NAME` delegates
to onboard undo; `me forget --import ID` delegates to import undo and checks its
person. Fact changes are not implemented: `me forget|correct --fact ID` prints the
exact proposed memory command and exits 3 without writing.

```sh
python3 -m skald doctor --json
python3 -m skald doctor --fix
python3 -m skald me --person ana --json
python3 -m skald me --person ana forget --source letterboxd
python3 -m skald me --person ana forget --import EVENT_ID
python3 -m skald onboard --reset --person ana --yes
```

Parsers return `Candidate(title, year, kind, ids, signal, n, last_ts, src)`; a mapped `person` field is present for multi-profile Netflix exports. `last_ts` is an ISO day. They retain no URLs, device identifiers or raw Netflix profile names. Netflix profiles require explicit `--profile-map NAME=PERSON`; the fallback `--person` applies to sources without profiles. Use multiple file arguments, or a Letterboxd export directory. Only small synthetic exports live under `tests/fixtures/ingest`.

| Export signal | Imported action |
|---|---|
| Rating ≥8/10 or ≥4/5, or a like | `love` |
| Rating 6–7.5/10 or 3–3.75/5 | `like` |
| Rating ≤4/10 or ≤2/5 | `not_for_us` |
| Watched, including ratings between the specified bands | `seen` |
| Series episodes | `seen`, with episode count `n` retained |
| Watchlist, trailer/teaser/official clip, allowlisted browser visits | `interest` |

Chrome copies History (including available WAL files) to a temporary directory before SQLite access. SQL selects titles/counts/dates only for URLs passing the exact host/path allowlist. The temporary copies are deleted. Ordinary YouTube videos and other browsing are discarded.

Resolution uses exact TMDB ids, then IMDb `/find`, then search. `match_score` combines normalized title/aliases (72%), year (18%; ±1 accepted), kind (8%) and bounded popularity (2%). Automatic search acceptance requires ≥0.88 and a ≥0.08 margin over the second choice. Remakes and equally named series stay unresolved when evidence cannot separate them. English/romaji titles match when the provider supplies aliases; no translation service is implied.

The source registry discovers adapters in `skald/ingest`: each provides `SOURCE = {"hint": str, "dir": bool, "profiles": bool}` and `parse(path, **kw)`. Every candidate must have the adapter's bare name as `src`. An adapter without a server `IMPORT_TRUST` row is listed as unusable and refused before parsing. `utterance` and `agent:<client>` are reserved for later intake lanes.

```sh
python3 -m skald --json onboard --sources
python3 -m skald --json onboard --person ben --non-interactive --source imdb=/named/ratings.csv --source imdb=/named/watchlist.csv
# Inspect counts and review.json, then use the returned plan:
python3 -m skald --json onboard --person ben --non-interactive --source imdb=/named/ratings.csv --source imdb=/named/watchlist.csv --yes --plan H
python3 -m skald --json onboard --status --person ben
python3 -m skald --json onboard --undo imdb --person ben
```

`onboard` previews per-source signals, resolution methods and review counts. Its JSON also retains the top eight facet changes and row moves. `--person both` simulates the household. Several files for one source share a hash over their sorted contents. JSON results and state use hashes; text prints local basenames. On a terminal, the wizard can collect source paths, ask before sending ambiguous titles, review matches inline (`accept / skip / tmdb:N / later`), show the preview, and ask before writing. All prompts go to stderr; `--json` still allows terminal prompts. `--non-interactive` disables them.

A non-interactive run without `--yes` saves a private resolve cache, review and plan without adding events. Applying requires `--yes --plan H`, reuses only the cache, and refuses any change to the person, file contents or accepted events. `--dry-run` writes nothing and makes no remote resolution call. Each applied source is checkpointed separately in `onboard.json`; unchanged applied sources are skipped unless `--again`. Repeated signals add zero events and preserve the local log bytes. Per-source undo removes only ids recorded for that person's source and retains them in `undone.json`.

Default resolution uses offline snapshot metadata and cached matches. `--resolve-online` opts into TMDB search using `TMDB_API`; it never calls a paid model from the Mac. `--remote` opts into the server's `resolve_title` decision API using `SKALD_API` and the configured token. Only title/year/kind and public choices are sent. The API registry is checked first; missing or unavailable resolution refuses the run. Gates and failures leave review items for a later retry. Receipts stay in the private cache and onboarding state. Batches contain at most 25 items and 60,000 encoded bytes, reserve $.02 each, and stop when the remaining `--max-ai-usd` cannot cover another reservation. A zero allowance skips both preflight and resolution calls.

Cache files are atomic, mode 0600 in a 0700 state directory. Unresolved cached choices are retried when a remote provider is present; real picks and explicit none answers are final. Review paths in results are relative to the state directory. Raw exports, URLs, profile names and API tokens stay out of reports, caches and events. The [skald-taste skill](../skills/skald-taste/SKILL.md) guides an agent through named exports and confirmed plans.

`onboard` writes only local imports and never pushes. Its push action is `skald push imports --apply --remote`, using the existing resolved-events API. Unpushed counts compare local ids with the snapshot; the existing push API has no local acknowledgement ledger, so a later pull is needed to clear that finding.

Persisted event fields are exactly `t,id,key,kind,title,year,who,action,src,conf,n`; `t` is a UTC day and `id` is generated locally. Exact repeated signals are skipped. A batch keeps the strongest signal and greatest depth for each resolved `(key,who,src)`, so watch-history records cannot bury ratings from the same export. Changed signals append to the log; the latest tuple is active. Undo removes that event and restores any previous signal for the tuple. Local undo ids are retained separately so a later live pull or push cannot resurrect them. Writes replace the file atomically with mode 600; batches validate before writing. At 20,000 events, further additions fail instead of deleting old history.

`build_profile` folds imports before the remote merge. Weights are action weight × confidence × source trust × decay. Trakt/Letterboxd/IMDb trust is 1.0, Netflix/plain 0.8, Chrome/YouTube 0.4. Loved/liked/seen/negative weights are 3/2/1/−3. Interest starts at 0.2 before trust/confidence, is capped at 20 active entries per person and decays to zero after 120 days. Other imports use the existing 30/120-day decay bands. Imports can raise a weaker implicit signal for the relevant person, while preserving real TV plays, told seeds and remote opinions. They do not become shared-TV learning credits. The import log and day-rounded weights invalidate the household taste hash.

## Live backend

`--live` requires an explicit `SKALD_SSH_HOST` and executes the fixed snapshot command through SSH, pulls named engine files into `~/.local/share/skald/live`, validates the tar allowlist and runs the engine offline. It never requests `hydra.json`, `.env`, `remote.json` or token files. Tar links, traversal, duplicate members and oversized members are rejected. The snapshot needs cached embeddings/facets and either a profile or watch history.

`push imports --remote` performs no SSH; it previews the linked HTTP API send, and `--apply` sends resolved events. The older route without `--remote` uses SSH and requires an explicit `SKALD_SSH_HOST`. `push imports --apply` reads the remote import log, validates it, preserves existing remote events, applies local undo ids and uploads the merged resolved events. Its fixed SSH script backs up the previous log, writes with `tee` as uid 65534, sets mode 600 and atomically replaces the log. No `docker cp` or raw export upload is used. Live commands and provider adapters are tested with stubs; this work does not deploy anything.

Run tests with Python 3.12:

```sh
python3.12 -m unittest discover -s tests -v
```
