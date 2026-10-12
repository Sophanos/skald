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
