# Search and commands

Use the search box in your watching app. **Search** finds titles. **Commands**, typed in the same box,
change settings, shelves or one title's place in your rows. Changes preview first; type the `yes NN` code to apply and `undo` to revert. Forgetting a person is permanent.

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

## People and household controls

Keep household picks in step with who lives or visits with you. An adult can add people
by relationship or just a name: "Add my mother Vesna", "Add Vesna" or "Add Kim, she is 12".
"Meine Mutter Vesna wohnt jetzt bei uns" and "Dodaj mamu Vesnu" work too.
Moving in means a member; a short visit means a guest. You can also say which you want.
Guests do not teach household taste. Up to six people can be active, including children and guests.

If a name is missing, Skald asks what to call the person. If a child has no age,
it asks one short question, such as "How old is Leo?". Reply "7", then confirm the preview.
When adding several children, nobody is added until all needed ages are given.
A title search such as "Find the film Arrival" still searches during a pending question
and clears that question. You can start the addition again later.

| You type | What happens after confirmation |
|---|---|
| "Add Kim as a guest for 3 days" | Adds a guest through the end of the date three days from today. |
| "Kim bleibt bis Sonntag" | Sets an existing guest's last day. |
| "When does Kim leave?" | Reads the guest's end date, without confirmation. |
| "Give Kim her own row" | Gives the guest a Home row; guests have none by default. |
| "a catalog for Kim and me with heist movies for 3 days" | Adds a shelf on your Home for the visit, using both people's filters. |
| "Ben moved out"; "We broke up, remove Ben"; "Ben passed away" | Removes Ben from the active household, keeping his data. Undo restores him. The reason is not saved. |
| "Forget Ben"; "Vergiss Ben"; "Zaboravi Bena" | Shows a read-only erasure preview, even after removal, with the command to run on the server. No confirmation code. |
| "Ben is away for a while" | Keeps his profile working but pauses his influence on others' picks until he returns. |
| "Ben is away until 1 November"; "pause Ben for two weeks" | Does the same through the last local day, then returns him automatically. |
| "Ben is back" | Ends an adult's absence early. |
| "set the AI budget to 5 dollars a month" | Sets the household's monthly paid AI limit in USD, within the server's ceiling. |
| "what changed this week" | Lists the last seven days of visible changes, with ids and undone status. No confirmation needed. |
| "undo the AI budget"; "undo ID" | Previews reverting a named change or an id from the list, keeping unrelated newer changes. |

Guest visits, dated absences and temporary shelves accept 1 to 30 days, or a date
from today through 30 days ahead, in the household timezone. The worker ends them
after that whole day. Expired guests keep their saved history; "bring Kim back"
undoes the removal and clears the old end date. Use a new date for another visit.
Away and back accept adult targets only. Forget also accepts children and guests. Another adult must make
the change; you cannot pause or forget yourself or the last adult at home.
Erase runs only from the server command line:
`python -m skald members forget ben --person ana`. Preview there, then repeat with
`--yes` or `--plan HASH`. Search and the HTTP command API cannot apply erasure.
Shared watches keep teaching whoever stays, without the erased name. Add
`--shared drop` on the CLI to remove those watches too; the preview counts them
per remaining person. If Skald had a saved Odin login or mapping, it removes
that copy and says: "Ben's Odin login and its watch history still exist in Odin
and AIOStreams. Remove them there if Ben should lose access."

"Erase everything" or "delete all our data" shows a read-only household preview,
including counts of Skald backups and saved logins. On the server, run
`python -m skald erase --household`, then repeat with `--yes --plan CODE` and
type ERASE at the terminal. Without a terminal or with `--json`, also supply
`--confirm ERASE`. Addon tokens, the format marker, setup addresses, server locks
and an empty generation marker stay. Existing addon links answer with empty rows;
run setup again and restart Skald. Backups outside Skald's data folder must be deleted separately.

The budget stays set until changed again. "turn AI off this month" sets it to 0;
it does not reset next month. Other currencies keep the number and show USD in the preview.
Adults can read and undo household changes; children and guests see only their own
changes and can undo only permitted changes. Undo asks for an id if a name is ambiguous,
and refuses if a later change to the same setting must be undone first.
See [data erasure and backups](PRIVACY.md#leaving-guests-and-forgetting) before forgetting anyone.

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
