# Search and commands

Use the search box in your watching app. **Search** finds titles. **Commands**, typed in the same box,
change settings, shelves or one title's place in your rows. Each command previews first; type its `yes NN` code to apply it and `undo` to revert.

## Search examples

| You type | Skald does |
|---|---|
| "Dune" | Plain TMDB title search, without AI. |
| "cozy mystery for a rainy evening" | AI picks ordered by the request, blended with your household's taste match. |
| "anime I haven't watched" | Drops titles already in your recorded history from AI results. |
| "sci-fi I've watched" | Selects matching titles from your own recorded history. |
| "hide anime for Ana" | Previews a setting change. The "yes NN" code comes first in the card name. Open the card for details; type the code to apply, "undo" to revert. |
| "hide anime and show 6 shelves"; "no, I meant Ben" | Several changes preview as one plan with one code and one undo. A correction right after a change fixes it; unclear requests get one short question. |
| "not tonight Arrival"; "I already watched Arrival"; "Arrival is not for me, too slow" | Hides one title until tomorrow or a date, marks it seen, or rates it down with a reason. `undo` reverts each. |
| "Ben watched Arrival, not me"; "why is Arrival in my picks"; "something like Arrival" | Moves a watch to the right person, explains a pick, or finds similar titles without changing settings. |
| "help"; "ideas", "suggest" | Help shows five search examples. Ideas offers at most two shelf suggestions; `yes NN` creates one, `no NN` dismisses it. |
| "what do you know about me", "what do you know about Ana", "my settings" | Shows an "About" card in plain sentences: likes and dislikes, shelves, top taste and recent commands. German "was weißt du über Ana" and Serbian "šta znaš o Ana" also work. |

## AI search and history

AI search needs OpenRouter; title search works without it.
Commands work in English, German and Serbian (Latin or Cyrillic) at the same time; there is no language switch.
Title searches ask TMDB in the language of your install link.
If AI fails, search falls back to TMDB title results.

For explicit watched or not-watched requests, member links use that member's history and Together links use current members' combined history.
Ordinary AI search uses household history and taste.
Watched searches use up to 300 recent titles with watch or rating evidence and omit disliked titles.

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
