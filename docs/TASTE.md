# How Skald learns your taste

Skald keeps signals for each household member and turns them into a taste profile.
Rows and picks come from that profile; your corrections feed back into it.

## How it works

1. Connect accounts or import exports, mapped to household members. Skald merges their signals into taste profiles.
2. Skald builds rows from history, TMDB recommendations and metadata, and AniList anime schedules. Optional OpenRouter adds curation.
3. Install the printed member or Together link in your watching setup. Use your stream addons to play.

A new household's first row build takes several minutes. Rows fill in as they are built; later builds reuse the cache.
OpenRouter can choose AI picks and This Week picks, help choose mood shelves, and sort unclear import matches. Rows work without it.
A "Who's watching?" control attributes shared viewing to the right member.

## Sources

Accounts connect on the server; exports import on a linked laptop with Python 3.12.
The [source table](CONNECTORS.md#sources) lists what each source supplies, its exact command and its limits.
Imports and connector data enter rows on their next scheduled build. Clicks and opened title pages do not teach taste.

## Import from a laptop

Imports run on a laptop with Python 3.12 and a clone of this repo. First [make the laptop API reachable](SETUP.md#6-link-a-laptop-optional).

<img src="images/skald-import-preview.svg" alt="Skald import preview for a test household" width="800">

1. On the server: `docker compose -f compose.stack.example.yaml exec skald skald setup link --name laptop`.
   This token covers every member. Adding `--person ana` limits it to Ana; other members' titles are skipped when pushing.
2. On the laptop: `python3 -m skald connect http://127.0.0.1:8081 --token-stdin`. Use your own API address and paste the token.
3. Import: `python3 -m skald import imdb ratings.csv --person NAME --remote`.
   Your server resolves titles; the laptop needs no TMDB key. Skald asks about unclear matches in a terminal
   and saves the result only on the laptop. Without server lookups, use `--resolve-online` with `TMDB_API` on the laptop.
4. `python3 -m skald push imports --remote` previews the send. Add `--apply` to send resolved signals.

Raw exports stay on the laptop. See [import commands](CLI.md#import-history) for counting-only runs and scripts.

## What changes as you watch

| What happens | What Skald changes | How soon |
|---|---|---|
| History or ratings change | Rows are ordered by taste; cards can show a short reason. | Next build; daily or on taste change, episodes every 3h, upcoming every 6h. |
| Taste changes with OpenRouter enabled | For You and the AI rewatch row refresh from your taste; This Week picks stay for the week. | At least 1h between taste-only builds, or twice daily. |
| A title is shown without a play | It moves down tracked rows except Continue Watching, Upcoming and Catch Up; For You impressions can remove it from the AI pick pool. | After six days shown in the relevant row. |
| You play picks from a theme shelf | Theme selection favours shelves that lead to plays; current slots get a staying bonus. | Daily or on taste change; bonus lasts 7 days without plays, 14 with plays. |
| A new evening starts | OpenRouter can choose moods from weekday, season and recent watching, with a penalty for yesterday's moods. | Once per logical day, starting at 05:00; rule rotation is the fallback. |
| A mapped person watches, or you set Who's watching? | Plays credit the mapped person, otherwise the active session; Together ranking uses adult members. | When history is read; without a session, unmapped plays count as Together. |
| Learned memory goes without new evidence | Its influence fades; preferences explicitly told to Skald keep their strength. | Learned strength halves every 180 days. |
| You abandon or dislike titles | Negative signals lower taste fit; opening a title is only a hint. | On the next taste update and row build. |
| You change `skald setup prefs` | Discovery style, years and genre exclusions shape rows; child age limits also filter search. | Next row request; apps may cache briefly. |
| Two positive titles share a director or creator | A `More from` shelf can show their unseen work if enough suitable titles remain. | Next theme build; needs at least eight unseen candidates. |

AI picks and AI mood selection need OpenRouter; other changes above work without it, though some shelves use saved AI tags. Rows with too few suitable titles stay hidden from the app.

## Correct it

Type corrections in your app's search box. Each one previews first; type its `yes NN` code to apply and `undo` to revert.

- `not tonight Arrival` hides one title until tomorrow or a date.
- `I already watched Arrival` marks it seen.
- `Arrival is not for me, too slow` rates it down with a reason.
- `Ben watched Arrival, not me` moves a watch to the right person.
- `don't learn from my watching of Arrival` keeps a plain watch out of your taste.

See [search and commands](SEARCH.md) for more phrases and [personal settings](ROWS.md#personal-settings) for discovery style, years and hides.
