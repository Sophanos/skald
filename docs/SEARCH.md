# Search and commands

Use the search box in your watching app. **Search** finds titles. **Commands**, typed in the same box,
change settings, shelves or one title's place in your rows. Each command previews first; type its `yes NN` code to apply it and `undo` to revert.

After a search, keep talking in the same box: "more like this", "also some series",
"but less violent", "something like this but not quite so slow" or "more like the second one".
Skald combines your reply with the last search and the titles shown. This works in English,
German, Serbian, Spanish and Portuguese, including typos. A fresh search starts a new wish.
Each viewer link remembers the last search for 30 minutes in memory; restarting clears it.
Skipped keystrokes and unfinished picker waits keep the previous context. Late responses from
an older search cannot replace a newer turn. Search responses use `no-store` so your app sends
follow-ups and retries to the server; the server still shares cached model answers.

"I like these, save them as my taste" previews loving every shown title for you.
"Save this as a row" previews a shelf named after the last search, with those titles pinned first.
Both need the exact `yes NN` code. Guests cannot save either; children can save only their own
shelves and cannot change taste. Taste saves use the existing title feedback history, so type
`undo` once per title to revert them. Saved shelves keep the normal age and preference filters.
An older routed save cannot replace a newer preview. Result previews include the title keys
in their confirmation identity; use the code from the current card when the results change.
Their seed titles use the existing feed cache; clearing that cache lets the shelf refill from its theme.

## Search examples

| You type | Skald does |
|---|---|
| "Dune" | Plain TMDB title search when the exact title is found. |
| "Tom Hanks" | His acting and directing credits, most popular first. Exact titles come first. |
| "cozy mystery for a rainy evening" | AI picks ordered by the request, blended with your household's taste match. |
| "anime I haven't watched" | Drops titles already in your recorded history from AI results. |
| "Animes I havent watched" | The same unseen search, even without the apostrophe. |
| "sci-fi I've watched" | Selects matching titles from your own recorded history. |
| "best films of all time" | Uses broad consensus, without a household taste rerank. |
| "hide anime for Ana" | Previews a setting change. The "yes NN" code comes first in the card name. Open the card for details; type the code to apply, "undo" to revert. |
| "hide anime and show 6 shelves"; "no, I meant Ben" | Several changes preview as one plan with one code and one undo. A correction right after a change fixes it; unclear requests get one short question. |
| "not tonight Arrival"; "I already watched Arrival"; "Arrival is not for me, too slow" | Hides one title until tomorrow or a date, marks it seen, or rates it down with a reason. `undo` reverts each. |
| "Ben watched Arrival, not me"; "why is Arrival in my picks"; "something like Arrival" | Moves a watch to the right person, explains a pick, or finds similar titles without changing settings. |
| "help"; "ideas", "suggest" | Help shows five search examples. Ideas offers at most two shelf suggestions; `yes NN` creates one, `no NN` dismisses it. |
| "what do you know about me", "what do you know about Ana", "my settings" | Shows an "About" card in plain sentences: likes and dislikes, shelves, top taste and recent commands. German "was weißt du über Ana" and Serbian "šta znaš o Ana" also work. |
| "rename Ana to Anja" | Previews an adult's own display name change, including on a shared link. Confirm with its `yes NN` code. |
| "rename the together row to Anja & Ben"; "nenne gemeinsam um in Wir zwei" | Previews the Together label for an identified adult. Confirm with `yes NN`, then restart Skald. `undo` restores the old label. |

## AI search and history

AI search needs OpenRouter; title search works without it.
Commands work in English, German and Serbian (Latin or Cyrillic) at the same time; there is no language switch.
Title searches ask TMDB in the language of your install link.
Perplexity's decider (`perplexity/pplx-decider-v1.1-27b`) routes free-text searches and decides
whether you want unseen, watched or any titles, and a consensus list or personal taste.
`SKALD_ROUTE_MODEL` overrides the router model. The router has a 2.5-second timeout.
The Luna picker runs alongside it, including when a previous search exists, with the full request,
a taste hint used only for personal requests, and up to 1,200 watched or rated titles.
Dated titles come first, most recent first; titles without dates follow by preference weight, highest first.
This limit applies only to the prompt. Watched and unseen result filters use the full viewer history.
Movie and series catalogs each ask for up to 18 lines of their own type, with an 850-token limit.
If the router refines the previous wish, a new picker uses the combined wish and the raw-query answer is dropped.
Commands and actions on previous results cancel the unused picker.
Mood requests such as "dark movies" ask for picks; "Dark" alone asks for a title.
An exact title wins even when the router calls it a refinement. If a routed title has TMDB hits only
in the other catalog type, this row stays empty. Only when both types have no hits does the picker answer.
An exact match on the top TMDB person's name uses their acting and directing credits for that type,
most popular first, with exact title matches first and the same adult and child filters. Names ignore
case, accents and extra spaces. A request such as "filme mit Cillian Murphy" goes to the picker.
If the router fails, picks use ordinary taste ordering and put watched titles last.
If the picker fails, search falls back to TMDB title results.
Nonempty picker text with no valid title lines is a failed answer and can be retried;
an intentional empty reply keeps that catalog empty. Slow person lookups stop at the search deadline
and leave available title results in place. Newer typing cancels AI work for older prefixes after title lookup.
If it is still running at the search deadline, repeat the search to use its finished answer without another picker call.

All AI searches use the linked member's history and learned taste; Together uses current members' combined history.
Together and unscoped requests use household taste. A member without learned taste uses the household taste.
Unseen searches omit watched and disliked titles. Watched searches resolve picks from history and omit disliked titles.
Each AI row has at most 15 titles. An unseen row with fewer than eight picks adds TMDB recommendations
of its kept picks, after them, while keeping the same history and age filters.
Equivalent queries share one AI answer per catalog type for 24 hours within the same viewer link when there is no previous search context.
Follow-up cache keys include that context and expire with its 30-minute lifetime.
Case, punctuation and apostrophes do not change that cache key.
Exact controls such as `history`, `verlauf`, `settings`, `help`, `undo`, `redo`, `ideas` and `suggest`
work the same before and after a search.

AI searches add a `timing` object to their `searches.jsonl` record. Paired movie and series requests
and cached retries within 30 seconds share one line. It keeps the first request's timing and row count.
The existing `t`, `q` and optional `p` fields stay the same.
Timing contains no result titles:

| Field | Meaning |
|---|---|
| `router_ms` | Router elapsed milliseconds, or `"timeout"` if the plan did not finish. |
| `router_source` | Decision source, such as `llm`, `cache` or `fallback`. |
| `picker_ms` | Picker elapsed milliseconds, `"cached"` for a reused answer, `"timeout"` when its wait timed out, or `0` when unused. |
| `resolve_ms` | Milliseconds spent looking up and preparing TMDB or history results, excluding router and picker waits. |
| `channel` | Final `picker`, `tmdb` or `command` channel. |
| `rows` | Served count for this response, for example `{"movie":15}` or `{"series":0}`. Command cards count as served results. |

`POST /api/v1/decide/route_query` takes `{"in":{"query":"Animes I havent watched"}}`.
Its answer includes `choice`, `confidence`, `watched` (`unseen`, `seen`, `any`) and
`list` (`best`, `taste`). The two new fields are optional and default to `any` and `taste`.
The route choices remain `title`, `command`, `discover` and `request`; a failed decision has a null route.
Optional `last` and `shown` inputs provide conversation context; `shown` accepts a list of title names.
With context, the answer adds `previous`: `new`, `refine` or `act`.

## Commands

Command cards appear once, in movie search results. Type the exact `yes NN` code to apply a preview.
The full list of phrases, undo, history and resets is in [change settings from search](SETUP.md#change-settings-from-search).
Title reactions and the CLI versions are in [personal commands](CLI.md#personal-commands).

## Wish shelves

Each member has four wish shelf slots by default; Together has two.
Search `every sunday morning a classic for us`, `weekday evenings something light for me` or `make me a row of heist films`.
Use `for MEMBER` for a named person. Confirm with `yes NN`; full shelves ask you to stop one first.
`stop Classics` or `stop the sunday shelf` previews stopping an existing shelf; unmatched names stay title searches; `undo` restores it.
`show 6 shelves` or `fewer shelves` changes slots (0..10). Schedules use the household timezone.
After shelf changes Skald asks AIOStreams to reload row names for logins made by `skald setup aiostreams`;
Stremio installed with Skald's own link may need the link reinstalled to show new names.
Children manage only their own shelves, with their age filter.
See [shelves](SETUP.md#shelves) for schedules and [shelf CLI commands](CLI.md#shelves) for local management.
