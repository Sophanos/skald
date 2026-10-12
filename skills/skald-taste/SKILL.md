---
name: skald-taste
description: Set up Skald for a household, import approved watch history, and read taste through the CLI or API.
metadata:
  requirements: Python 3.12; local DATA for setup; configured API for laptop imports
---

## Set up Skald for a household

Run `skald setup --status` where DATA lives. In Docker, use
`docker compose exec engine skald setup --status`.
Run the printed next command, then repeat the status check until setup is complete.
Ask the human only for secrets and choices such as names, language, watching app and source accounts.
For unattended steps use `--yes` and the printed flags. Supply secrets through the private
prompt, stdin, or a named environment variable. Never put them in arguments or conversation.
Use `skald setup link --name laptop --person MEMBER` to link a laptop.
Keep private addon links and tokens out of the conversation.

## Connect each person's sources

Run on the server where DATA lives; prefix with `docker compose exec engine` for
Stremio direct, or `docker compose -f compose.stack.example.yaml exec skald` for the stack.
Use configured member names. Keys go only through the private prompt or stdin,
never in arguments or conversation. Scripts add `--yes --secret-stdin`.

- Stremio: `skald setup --connect stremio --person MEMBER`. Paste an auth key
  privately, or leave it blank for private email and password prompts. `--key-stdin`
  reads one auth key line. Repeat for each person; earlier accounts stay connected.
- Trakt: `skald setup --connect trakt --person MEMBER`. Supply the access token,
  then client id privately; `--secret-stdin` reads those two lines. Repeat per person.
- MDBList: `skald setup --connect mdblist --person MEMBER`. `--secret-stdin`
  reads one key line. Repeat per person.
- Jellyfin: `skald setup --connect jellyfin --source-url http://jellyfin:8096`.
  Setup lists users and asks for member mappings; scripts can repeat `--map USER_ID=MEMBER`.
- Plex: `skald setup --connect plex --source-url http://plex:32400 --person MEMBER`.
  The token's account supplies this person's history.
- AIOStreams database: `skald setup --connect aiostreams-db --source-path /aio-data/db.sqlite`.
  Mount the database directory read-only, including WAL files; map listed profiles
  to members, or repeat `--map SOURCE_ID=MEMBER`.
- Check with `skald connectors test SOURCE` (`trakt` or `aiostreams_db` for those sources).
  Remove a member's Stremio or MDBList connection with
  `skald setup --disconnect stremio --person MEMBER` or
  `skald setup --disconnect mdblist --person MEMBER`. This removes its key but keeps history.
  Other sources have no setup disconnect command. Restart the engine after connector changes.

## Set up watching and adopt older households

- Run `skald setup aiostreams` for Odin and Jellyfin apps, or Stremio through
  AIOStreams. It prints private server/login details and a Stremio link.
  Update an existing login with `skald setup aiostreams --uuid UUID`.
  Passwords use the private prompt or `--password-stdin`; a protected instance's
  access key uses the private prompt or `--secret-stdin` and is read first.
- AIOStreams records Jellyfin-app viewing, not Stremio playback. Connect Stremio
  directly for its library signals. Stream addons still handle playback.
- If `skald setup --status` says the older household needs adoption, run
  `skald setup adopt --yes`, then check status again. It copies the existing
  config unchanged to make setup, laptop linking and imports available.

## Set personal row preferences

- `skald setup prefs --person MEMBER --discovery gems --years 1990- --exclude-movie-genres horror --exclude-series-genres reality,talk`
- Discovery is `balanced`, `mainstream` or `gems`. Years accept `1990-2010`,
  `1990-` or `--years=-2010`; genres use TMDB's English names, separately by type.
- Omitted flags keep other settings. `skald setup prefs --person MEMBER --clear`
  removes all preferences. Changes apply on the next row request; apps may cache.
- These settings shape rows, not search. Together respects members' genre
  exclusions and keeps its usual years and order.

## Read taste only when asked

Use only the person's authorized CLI or API. Read an approved local copy with
`skald --snapshot /path/approved-copy COMMAND`; never fetch private data unasked.

- Show rows with `rows --n 10`, a profile with `taste show --person MEMBER`,
  a title's fit with `taste why tmdb:123 --person MEMBER`, or saved memory with
  `memory --person MEMBER`. The API provides rows, profile and title explanations
  under the bound token's scope, as listed below.
- Only after a request, remove a selected local imported source with
  `me forget --source imdb --person MEMBER`, or an import with
  `me forget --import IMPORT_ID --person MEMBER`.
- `me forget --fact FACT_ID --person MEMBER` and
  `me correct --fact FACT_ID --person MEMBER` are placeholders in 1.1.0:
  they exit without changing memory. Explain this limit; never claim a correction succeeded.

## Import taste

Run `python3 -m skald onboard --sources` for export instructions.
Ask which exports to use. Read only files the person names. Use a configured member name
with `--person MEMBER`; use `both` for shared imports. Map Netflix profiles with
`--profile-map SOURCE_NAME=MEMBER`. Keep raw profile names out of the conversation.

Preview locally:

```sh
python3 -m skald --json onboard --person MEMBER --non-interactive --source imdb=/path/approved.csv --remote --dry-run
```

Repeat `--source NAME=PATH` for multiple exports. A dry-run writes nothing and makes no
paid API calls. Omit `--dry-run` to save the review and plan for an apply.
Show the person counts, unresolved titles and the plan hash. `review_file` is relative
to the state directory. With permission to send title metadata, add `--remote`.
Only titles, years, media types and external ids reach the server for lookup, even in dry runs.
`--remote` uses the server's TMDB key; the laptop needs no TMDB key.
`--resolve-online` uses a laptop TMDB key instead. Optional paid decisions also send public TMDB choices and use the server budget.

After a yes, use the same files and options plus `--yes --plan HASH`.
A changed plan needs a new preview and yes. Unresolved titles stay in private `review.json`.
Completed sources resume automatically. `--again` rechecks them without duplicating signals.
Use `onboard --status` to inspect progress. Undo only when requested:
`onboard --undo SOURCE --person MEMBER`.

The agent may run `push imports --remote --apply` after showing the person the
`push imports --remote` dry-run and getting a yes.
Never invent a title as evidence. Never read or print token files. Never copy export
contents into the conversation beyond titles the person asks about.

<!-- skald:decide-api:begin -->

## Skald decisions and taste API

A bound token sees its own person + together. Use only the person's authorized CLI or API.

### explain_pick (serving)
- inp: `{"key":"tmdb:N","kind":["movie","tv"],"person":["ana","ben","together","guest",null]}`
- out: `{"why":["str",null],"match":["int",null],"confidence":["Strong fit","Good fit","Wildcard",null],"rung":["seed","facet",null],"reasons":["str",3],"maturity":{"state":["too_new","learning","ready","guest"],"explicit":"int","need":"int"}}`
- CLI: `python3 -m skald decide explain_pick --in '{"key":"tmdb:900001","kind":"movie","person":null}'`
- API: `python3 -m skald decide explain_pick --remote --in '{"key":"tmdb:900001","kind":"movie","person":null}'`

### interview_next (serving)
- inp: `{"person":["ana","ben"]}`
- out: `{"v":"int","person":["ana","ben"],"maturity":{"state":["too_new","learning","ready","guest"],"explicit":"int","need":"int"},"progress":{"positives":"int","need":"int","negatives_with_reason":"int","need_neg":"int"},"done":"bool","done_reason":["ready","answers",null],"anchors":[{"key":"tmdb:N","kind":["movie","tv"],"title":"str","year":["str",""],"experience":["cozy_case","banter_duo","will_they","slow_romance","period_society","forbidden_love","found_family","mentor_bond","second_chance","tearjerker","grief_healing","heartwarming","coming_of_age","underdog","rivalry_training","epic_quest","magic_wonder","team_spectacle","hero_burden","dynasty_power","frontier_grit","political_intrigue","war_sacrifice","antihero","revenge","heist","whodunit","mind_bender","dread","survival","competence","courtroom","family_secrets","big_ideas","time_twist","dystopia","feel_good","dark_comedy","slice_of_life","true_story","passion_art","sports_glory","nostalgia"]},12]}`
- CLI: `python3 -m skald decide interview_next --in '{"person":"ana"}'`
- API: `python3 -m skald decide interview_next --remote --in '{"person":"ana"}'`

### rank_titles (serving)
- inp: `{"choices":[{"key":"tmdb:N","kind":["movie","tv"]},25],"person":["ana","ben","together","guest",null]}`
- out: `{"ranked":[{"key":"tmdb:N","kind":["movie","tv"]},25],"pick":[{"key":"tmdb:N","kind":["movie","tv"]},null],"why":["str",null],"match":["int",null]}`
- CLI: `python3 -m skald decide rank_titles --in '{"choices":[{"key":"tmdb:900005","kind":"movie"},{"key":"tmdb:900001","kind":"movie"}],"person":"ben"}'`
- API: `python3 -m skald decide rank_titles --remote --in '{"choices":[{"key":"tmdb:900005","kind":"movie"},{"key":"tmdb:900001","kind":"movie"}],"person":"ben"}'`

### resolve_title (LLM)
- inp: `{"title":"str","year":["int",null],"kind":["movie","tv",null],"choices":[{"key":"tmdb:N","kind":["movie","tv"],"title":"str","aliases":["str",5],"year":["int",null],"pop":["float",null]},8]}`
- out: `{"pick":[{"key":"tmdb:N","kind":["movie","tv"]},null]}`
- CLI: `python3 -m skald decide resolve_title --in '{"title":"Synthetic text 0","year":0,"kind":"movie","choices":[{"key":"tmdb:900001","kind":"movie","title":"Synthetic text 0","aliases":["Synthetic text 0","Synthetic text 1"],"year":0,"pop":0.0},{"key":"tmdb:900002","kind":"tv","title":"Synthetic text 1","aliases":["Synthetic text 2","Synthetic text 3"],"year":null,"pop":null}]}'`
- API: `python3 -m skald decide resolve_title --remote --in '{"title":"Synthetic text 0","year":0,"kind":"movie","choices":[{"key":"tmdb:900001","kind":"movie","title":"Synthetic text 0","aliases":["Synthetic text 0","Synthetic text 1"],"year":0,"pop":0.0},{"key":"tmdb:900002","kind":"tv","title":"Synthetic text 1","aliases":["Synthetic text 2","Synthetic text 3"],"year":null,"pop":null}]}'`

### route_query (LLM)
- inp: `{"query":"str"}`
- out: `{"choice":["title","discover","request",null],"confidence":["float",null]}`
- CLI: `python3 -m skald decide route_query --in '{"query":"Synthetic text 0"}'`
- API: `python3 -m skald decide route_query --remote --in '{"query":"Synthetic text 0"}'`

- `/api/v1/connectors`: GET → read; query []; tail null; example `{}`
- `/api/v1/decide`: GET → read; query []; tail null; example `{}`
- `/api/v1/decide/*`: POST → decide; query []; tail "custom"; example `{"tail":"route_query","body":{"in":{"query":"synthetic request"},"no_llm":true}}`
- `/api/v1/events`: GET → events, POST → events; query []; tail null; example `{"body":{"stream":"imports","events":[]}}`
- `/api/v1/interview/next`: GET → read; query ["person"]; tail null; example `{"query":{"person":"ana"}}`
- `/api/v1/metrics`: GET → read; query ["days"]; tail null; example `{}`
- `/api/v1/profile`: GET → read; query ["person"]; tail null; example `{}`
- `/api/v1/resolve`: POST → read; query []; tail null; example `{"body":{"items":[]}}`
- `/api/v1/routes`: GET → read; query []; tail null; example `{}`
- `/api/v1/rows`: GET → read; query ["lang","person","row"]; tail null; example `{}`
- `/api/v1/session`: GET → read, POST → events; query []; tail null; example `{"body":{"who":"both"}}`
- `/api/v1/taste`: GET → read; query ["days","person"]; tail null; example `{}`
- `/api/v1/titles/*`: GET → read; query ["kind","person"]; tail "tmdb:[0-9]{1,9}"; example `{"tail":"tmdb:900001","query":{"kind":"movie"}}`

<!-- skald:decide-api:end -->
