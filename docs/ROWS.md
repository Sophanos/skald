# Rows

Each member and Together get their own rows. Rows appear when they have enough suitable titles.
Names use your household language and follow the current picks.

## Two kinds of rows

**Picked for you** rows are built from taste: For You, New This Week, `Picks for <member>`, `Because You Love <title>`,
`Same Feeling as <title>`, `More from <maker>`, Watch It Again, Coming Soon, mood shelves, theme shelves,
wish shelves, Fresh Finds and Family Picks.

**Catalogs** are plain lists filtered by your history: Continue Watching and Catch Up
(your own shows and collections), Top 10, the two Anime rows, Latest Digital Releases and Best Movies / Best Series (Top Rated).
Documentary and studio shelves start from a catalog and are ranked by household taste.

Experience shelves need saved title tags; OpenRouter creates these tags. Without a key, ordinary rows still build.
[How Skald learns](TASTE.md) explains when rows change.

## All rows

| Row title | What it brings | Needs OpenRouter? |
|---|---|---|
| Continue Watching | Next episodes from shows you watch. | No |
| ✨ For You | AI picks from a pool based on your taste. | Yes |
| ✨ New This Week | Weekly picks for each member and Together. | Yes |
| `Picks for <member>` | Recommendations based on that person's positive history. | No curation call; uses saved experience tags |
| Coming Soon | Upcoming films and shows. | No |
| Family Picks (only when a child is set up) | Titles safe for every child, ordered by adult taste. | No |
| Catch Up | Shows and film collections you can return to. | No |
| Watch It Again | Favourites and familiar titles to revisit. | No |
| `Because You Love <title>` | Similar picks from a rotating favourite; also "Like", "Watched" or "Watch". | No |
| `Same Feeling as <title>` | Similar experiences from a different world. | No curation call; uses saved experience tags |
| `More from <maker>` | Unseen work by a director or creator you like. | No |
| Our Picks | Fallback name for shelves built around your themes; names change with the theme. | No |
| Cozy Case Night, Big Ideas Night and other mood shelves | Four shelves chosen for the evening. | Uses saved tags; AI mood selection is optional |
| Top 10 Movies This Week / Top 10 Series This Week | Weekly trending titles. | No |
| Wonder of Our World / Cosmic Questions / Lives That Changed History / Big Questions | Documentary shelves. | No; AI can add weekly picks |
| HBO / Paramount+ | Studio and network shelves ranked by taste. | No |
| Anime This Week | Airing anime. | No |
| `Anime · <season> <year>` | Next season's anime. | No |
| Fresh Finds (opt-in) | Daily picks for one member or Together that keep a loved kind of story and add one they have not tried. Turn on with `skald setup rows` or "add the New to You row". | Uses saved experience tags |
| Latest Digital Releases (opt-in) | Movies released digitally in your country in the last 60 days. | No |
| Best Movies, Best Series (Top Rated, on by default) | TMDB ratings (movies 200 votes, series 100), unseen titles in your taste order. | No |
| Find your taste | Starter titles to rate on the private taste page. | No |

"More Like Your Favorites", "Same Feeling, New World" and "Tonight's Mood" are fallback names while shelves build. "Our Picks" is also the member API's row label.
Wish shelves are rows you ask for in search; see [wish shelves](SEARCH.md#wish-shelves).
Choose rows with `skald setup rows`; see [setup](SETUP.md#2-add-your-household) for valid keys.

## Personal settings

Run on the server. Prefix commands with `docker compose -f compose.stack.example.yaml exec skald` for the stack, or `docker compose exec engine` for Stremio direct:

```sh
skald setup prefs --person NAME --discovery gems --years 1990- --hide documentaries,anime --focus-movie-genres horror,drama --focus-languages es --focus-countries ES
skald setup prefs --person NAME --clear
```

Choose `balanced` (default), `mainstream` (popular titles), or `gems` (high ratings with lower popularity). Years accept `1990-2010`, `1990-` or `--years=-2010`. Use `--hide` to add movies, series, documentaries, animation or anime; `--show anime` removes a hide. Animation hides all cartoons; anime hides Japanese animation. Focus genres, languages and countries rank personal rows without filtering; genre names are TMDB's English names.

Changes apply on the next row request; apps may cache rows briefly. Started titles in Continue Watching skip hides, genre exclusions and years. Together respects genre and category hides except movies/series, and keeps its years and order.

Set `skald setup prefs --person ben --kids-age 8` (ages 3..17; `--kids-age off` clears). Child age limits filter all Skald rows and search using household-country ratings, then US ratings. Other addons and streams are not filtered; also use the app's profile lock. TMDB ratings can be missing (titles are hidden) or wrong. [All settings](SETUP.md#2-add-your-household).

## Taste archetypes and reasons

Each member and the household get a short taste archetype, such as "Twisty puzzle chaser", shown in `skald me` and `skald explain`. It describes taste and does not change picks.
Cards can show a short reason; the explanation API exposes a match score when there is enough history.
