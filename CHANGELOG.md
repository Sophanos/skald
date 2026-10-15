# Changelog

Every future release entry includes a **Data changes:** line stating the data
format, migrations, and any backup or rollback steps. Use “none” when the format
and layout stay the same.

## 1.6.1

Parents can follow a child's watching. A current adult (not a guest, not away) can read a child's plays (`python -m skald plays recent kim --person ana`, `plays why TITLE kim --person ana`), what shapes the child's picks (`python -m skald taste align kim --person ana`) and the child's watching days (`python -m skald plays around kim --person ana`). In search, ask "What did Kim watch this week?", "What does Kim like?" or "When is Kim around?". Watching days show the last day, how many of the last 7 and 28 days had watching, the usual weekdays once there are 3 full weeks of data, runs of 3 or more days in a row and gaps of 7 or more days. Skald shows dates and counts only and never says why. Only plays from the child's own Odin/AIOStreams login count: a shared or household login never does, and marking a season as watched is not a watching day. Children, guests and away adults still read only their own plays, and adults still cannot read each other's. Reading a child's likes cannot change them.
Forgetting a child removes their watching days, and the preview counts them. A forget with a cutoff date removes the days before it. Erasing the household removes the file.
Fix: over HTTPS, a bulk "mark as watched" at the edge of plays retention could lose its bulk mark and then count as real watching. The mark now stays as long as any of its plays are kept.
Timing tests no longer fail on slow machines, and CI no longer runs a second time for a release tag.

**Data changes:** none to the data format (FORMAT stays 5); no migration. New private file `watching-days.json` (children only, dates only, last 56 days). Rolling back to 1.6.0 needs no restore; 1.6.0 ignores the file but does not remove it when you forget a child, so delete it after a rollback if needed.

## 1.6.0

Forget keeps shared watches by default. When you erase a person, the watches they shared with others keep teaching the people who stay, with the erased person taken out of them. `--shared drop` removes those watches from everyone's taste as in 1.5.0; their titles stay seen. The preview counts shared watches per remaining person and now works without a TMDB connection.
Children and named guests can be erased too, also after they were removed. Erasing still runs only from the server command line (`python -m skald members forget kim --person ana`), and the person asking must be a current adult who is not away and is not erasing themselves.
Forget also removes the AIOStreams login Skald saved for the person and its login mapping. When Skald had either, the preview and the result say: "Ben's Odin login and its watch history still exist in Odin and AIOStreams. Remove them there if Ben should lose access." Skald does not delete accounts or history in Odin or AIOStreams.
New: erase the whole household with `python -m skald erase --household`. It first shows counts without changing anything. To apply, repeat with `--yes --plan CODE` and type ERASE when asked, or add `--confirm ERASE` (needed with `--json` or without a terminal). It erases every personal file in Skald's data folder, its credentials and all backups inside it, and keeps only the addon and phone-remote tokens, the format marker, the setup addresses and lock files. A running Skald stops serving and saving personal data once the erase finishes; addon links show empty rows until you run `skald setup household` and restart Skald. Search and the HTTP API cannot erase a household. Backups outside Skald's data folder, for example copies your host makes, are not touched. Delete them yourself.
Fix: a "Because you ..." row could show empty in Odin for up to a day after its weekly refresh. The refresh could switch the row between movies and series, and Odin keeps a row's kind for about a day. A row now keeps its kind and moves on to another title of the same kind; it changes kind only when nothing of its kind is left to show. `rows plan --json` and `rows why` show each Because row's kind and since when.
Fix: a refused command line action, for example a child asking to erase someone, said "could not read or write local data". It now says why it was refused.

**Data changes:** none to the data format (FORMAT stays 5); no migration. New files: `erase.lock`, and `.skald-erased` after a household erase. Rolling back to 1.5.0 needs no restore; 1.5.0 ignores both files. Erased data cannot be rolled back.

## 1.5.0

Odin plays teach each person separately. Skald reads what everyone plays in Odin from the mounted AIOStreams database, as before, or, when no database is mounted, over HTTPS with each person's own AIOStreams login (`skald setup aiostreams --member NAME`). Each login belongs to exactly one person; a login that belongs to no one or to two people is skipped and listed by its first eight characters (`skald connectors mappings`). A play is counted once, also when Skald switches between the two paths or when a burst of episodes arrives at once.
`skald plays` shows which path is in use and why, the last and next check (every 15 minutes by default; `plays every 5m` up to `6h`) and new plays per person. `plays mode auto` prefers the database and falls back to HTTPS (`db`, `https` and `off` also work). `plays person ben off` stops reading one person, `plays check` reads once now, and `plays compare` reads both paths and lists the differences without writing anything. In search, ask "is Skald seeing what I watch?", "what did I watch this week?" or "why did you count Dune as watched?"; `plays recent` and `plays why` answer the same in the CLI. Adults see their own plays plus Together; children, guests and away people see only their own. "Read my Odin watches now" checks within a minute, at most once every two minutes per household.
Over HTTPS, AIOStreams turns ratings into likes, leaves out dropped titles and returns at most 500 played items. A read that reaches that limit keeps the last good result instead of losing plays. `plays compare --json` shows where the two paths differ.
Erasing a person now runs only from the server command line. Skald has no passwords of its own: it knows who is asking from the link, the Odin profile or the "who's watching" choice on the phone page, so a child could pick an adult there and confirm "forget Ben" in search. Search still shows what would be erased and ends with the command to run, for example `python -m skald members forget ben --person ana`. Moving out, away, back, guests and undo work from search as before.
Forgetting a person also removes their plays switch and their plays from the plays cache.

**Data changes:** none to the data format (FORMAT stays 5); no migration. New optional fields and files that 1.4.10 ignores: a `plays` object in `skald.json` (mode, interval, per-person switches) and a private `plays.json` cache. Rolling back to 1.4.10 needs no restore: plays are read from the database mount only, logins saved for HTTPS stay unused, and forget works from search again.

## 1.4.10

People: add anyone by name, by role or both ("Add my mother Vesna", "Add Vesna", "Add Kim, she is 12", "Meine Mutter Vesna wohnt jetzt bei uns", "Dodaj mamu Vesnu"). Skald asks only for what is missing: a name, or a child's age. A title search typed while it waits for the answer still works as a search.
Guests can stay for a set time: "Add Marko as a guest for 3 days" or "until Sunday" (1 to 30 days, through the last local day). After the visit they leave the active household and their history and ratings stay saved. A shared shelf can end with the visit too ("a catalog for Marko and me with heist movies for 3 days"). CLI: `members add marko --guest --days 3`, `members update marko --until 2026-10-18` or `--no-end`.
When someone leaves: "Ben moved out" removes him and keeps his data, and undo brings him back. "Ben is away until 1 November" (or "pause Ben") stops an adult's taste from shaping other people's picks while his profile, link and rows keep working; he comes back on his own the day after the last day, or sooner with "Ben is back". "Forget Ben" permanently erases an adult's personal data after a preview and the exact confirmation code, and cannot be undone. CLI: `members remove`, `members away ben --until 2026-11-01`, `members back`, `members forget`. What forget erases and keeps is listed in [Privacy](docs/PRIVACY.md#leaving-guests-and-forgetting).
Forget also clears the household's row-play learning (which rows get played), because those plays are not stored per person; it rebuilds as you watch. Together-watches with the forgotten person stay seen for the others but stop teaching their taste.
AI budget and change list: adults set the household's monthly AI budget from search ("set the AI budget to 5 dollars a month", "turn AI off this month") or with `skald ai budget 5 --person ana`. Without a saved budget nothing changes: `AI_MONTHLY_USD` when set, otherwise 3 USD. "What changed this week" or `skald changes --since 7d` lists saved changes; undo one by name or id (`skald changes undo ID`) and unrelated newer changes stay.
Get Ready rows: up to two rows of earlier saga films or unfinished seasons to catch up on before a release from seven days ago to 42 days ahead. Each viewer sees only their own reasons. `skald rows ready --person ana --json` explains the rows or why none qualifies.
Because rows keep to films or series. Odin (AIOStreams Jellyfin mode) remembers a row's kind for a day, so a Because row that switched between them showed up empty. A row now switches kind only after seven days, or sooner when all its saved picks would be filtered out.
Checked live on the production model: of 108 requests covering away dates, guests, wishes, undo and Get Ready, 107 picked the right action; one Serbian wish became a "more documentaries" preview instead.
Fix: a test pinned its clock after resetting usage, so two source tests failed on any checkout from 15 October 2026.

**Data changes:** none to the data format (FORMAT stays 5); no migration. New optional fields that older servers ignore: `household.ai_monthly_usd`, `household.guest_until`, `household.away`, `household.away_until`, shelf end dates, change ids, actors and times in the change journal, a kind marker on Because row slots, and `history-seen.json`. Rolling back to 1.4.9: it uses the environment AI budget and ignores the saved one, ignores guest and shelf end dates (guests and shelves stay), shows away people as present, and ignores `history-seen.json`. Forget and its erased data cannot be rolled back. Before a rollback, apply `ops/rollback-1.4.9.patch` to the 1.4.9 source (`patch -p1 < ops/rollback-1.4.9.patch`) so its undo checks every person a multi-person addition added.

## 1.4.9

Fix for 1.4.8: rows kept building only until Watch It Again saved its saga list; after that For You, the feeling rows and the personal rows stopped refreshing. They rebuild again.
Your best matches now show on Home. 1.4.8 added the best unseen titles Skald already knows to For You and the personal rows, but most of them have a poster without a background picture, and rows put fully illustrated cards first, so they landed at the end of the row where Home never shows them. They now take turns with the other picks after the first five cards, in personal rows, For You and the feeling rows. The limits on dark titles, franchises and repeats stay as they were. `python -m skald rows reach --top 20 --json` shows how many reach Home.
Row Lab: once a day, when row metrics are on, one AI call suggests up to two new rows, each with a plain name and 8 to 20 titles Skald already knows. A trial row that gets watched stays; one that is not played within ten days of showing goes away. Kept rows show in the parts of the week they were watched in. `python -m skald rows lab --json` and `rows moments --json` read it offline; Commander and the CLI keep, retire, pause and resume ideas ("keep the heist row", "stop the row lab"). Together ideas follow both people's hides.
Row names in one language, titles in another: `python -m skald setup household --row-lang en` (or `de`, `sr`) sets the language of row names and card labels while titles and descriptions keep the TMDB language; `--row-lang same` goes back. Commander: "show row names in English".
People: add a friend as a guest ("Add my best friend Marko as a guest", `python -m skald members add marko --guest --person ana`). A guest gets their own picks and filters, but their reactions never change the household's taste, the Together row or anyone else's picks, and guests cannot change the household's rows, settings or other people. Skald knows who is asking by the Odin profile or Skald link in use: give a guest their own Odin profile (`python -m skald setup aiostreams --member marko` makes their login; `python -m skald addon-url` lists their own Skald link for Stremio). On someone else's profile a guest acts as that person, and undo still works. Remove someone after a preview of what changes ("Entferne Ben", `members remove ben`); their history stays saved, a private copy of the settings is kept first, and undo brings them back. `members rows kim on` turns a person's own Home row on or off. "A catalog for Marko and me with heist movies" builds one shelf that follows both people's hides, excluded genres and seen titles. Up to six active people; removed people do not count. Children and guests cannot change people.
The stack example (`compose.stack.example.yaml`) now mounts the AIOStreams database into Skald read-only, so Odin plays teach Skald on the main setup path, not only in the Odin Jellyfin example. After your first play in Odin, connect it once: README step 7 (`skald setup --connect aiostreams-db --source-path /aio-data/db.sqlite`, then restart Skald). Existing stacks: add `- aiostreams-data:/aio-data:ro` under the Skald volumes.
Hiding a row from the default Home now removes only that row. Before, it also switched on studio rows and extra documentary, mood and lane rows; showing it again brings back exactly the default Home. Home shows at most four mood rows and five lane rows (`rows prefer`, "more mood rows"). Settings are checked when you change them, and settings saved by older versions keep loading. Lowering the shelf count keeps hidden wishes; raise it to show them again. Shelf filters use the same limits as personal preferences (runtime 1 to 600 minutes, years 1 to 9999). An invalid AI limit in the environment (`AI_DAILY_*_CALLS`, `AI_MONTHLY_USD`, `API_LANE_SHARE`) now names the setting at startup.

**Data changes:** none to the data format (FORMAT stays 5); no migration. New optional files and fields that older servers ignore: `lab.json` for Row Lab ideas, `region.row_lang` in the household settings, `household.rows_from_default` (set when a hide or show turns the default Home into a row list), `household.guests`, `household.ages`, `household.retired` and `household.retired_rows`, and an optional second person on a shelf (`with_person`). Rolling back to 1.4.8 needs no restore if you have not changed people. If you have: 1.4.8 makes removed people active again with their logins, treats guests as full members, ignores shared-shelf partners, and refuses a settings file with more than six saved people. In that last case restore the private copy saved before the change (`backups/pre-people-*.json`) as `skald.json`.

## 1.4.8

Row names are plain and localized: "Continue Watching" / "Weiterschauen", "Picks for Ana" / "Tipps für Ana", "More Like Your Favorites" / "Mehr wie deine Favoriten", "New This Week" / "Neu diese Woche". `python -m skald rows names --json` lists what each row is called and where the name comes from; Commander answers "what are my rows called?".
New: "Best Movies" / "Die besten Filme" and "Best Series" / "Die besten Serien" (Top Rated), on by default when you have not picked your rows. They read ten pages of TMDB's best-rated movies and five of series (series need 100 votes, movies 200), drop what you have seen or rejected, and order the rest per viewer by 65% taste match and 35% rating. `rows why toprated --person NAME --json` lists the ranked titles with rating and taste match; Commander hides or shows them ("hide best series"). A household that lists its rows keeps exactly its choice.
Holiday and seasonal rows show only in their season. When a themed row is built, the AI also says which months it fits ("any" for most); a Christmas row stays hidden in October. `rows plan --json` shows each row's months and whether it is hidden today.
New: "New This Week" / "Neu diese Woche". Once a week Skald picks up to three new and worthwhile releases per person and for Together, from titles that really came out in the last weeks and that match their taste, and keeps that week's picks fixed. Cards read "98 % Übereinstimmung · Neu seit 9. Okt" / "98% match · New since 9 Oct"; German dates everywhere now carry the day dot ("9. Okt"). `python -m skald rows week --weeks N --json` reads up to eight past weeks offline; Commander answers "what was new last week?".
AI credit alarm: the worker checks the OpenRouter key's remaining credit once a day and warns when it falls below 20% of the limit or below $1. `python -m skald ai status --json` shows saved credit, today's and this month's spending, and calls per group, offline; `--refresh` checks once now. `setup --status` and Commander ("how much AI credit is left?") show the same numbers.
The taste model finishes building when an embedding call fails, keeping the vectors it already has.
Search still answers when the AI cannot: out of credit, refused, rate-limited, timed out or not set up. It shows matches from your taste and says why at the top: "AI search is unavailable right now (credit used up). Showing matches from your taste instead." / "Die KI-Suche ist gerade nicht verfügbar (Guthaben aufgebraucht). Stattdessen zeigen wir Treffer passend zu deinem Geschmack." A slow AI picker gets a backup after half a second, so an answer arrives within about 7 seconds even when the AI hangs.
Search is faster. Results are served as soon as 15 good picks are ready while the rest keep arriving, and repeat searches reuse saved title lookups (at most 5,000 entries, about 10 MB, in `.cache/search-resolve.sqlite3`). On a copy of a real household, 16 searches took a median 3.8 seconds instead of 5.9. `python -m skald search --explain-timing --json` shows each stage of the last 15 searches, offline.
Home reaches further into what Skald already knows. Titles you have not seen yet that match your taste best, from everything Skald has already looked up, now feed For You and the other personal rows, taking turns per member and mixed with your favorites' matches and trending titles. `python -m skald rows reach --top 20 --json` shows, per profile, how many of the best unseen matches made it onto Home, in which row, and why the others did not; Commander answers "are my best matches on my home?".
Mood shelves and the feeling row now draw from every title Skald has already tagged, not only from titles similar to your favorites. A title that strongly carries the feeling and matches your taste can show up even when no favorite points to it; the two sources take turns. No extra AI calls. `rows plan --json` shows per feeling row how many titles came from each source; `explain` and Commander ("why is this in the row?") say when a title came from the catalog.

**Data changes:** none to the data format (FORMAT stays 5); no migration. New optional fields and files that older servers ignore: season months on themed-row decisions, weekly picks in the learning file, credit fields in `usage.json` with a `usage.lock` file beside it, and a title-lookup cache in `.cache/search-resolve.sqlite3` that is safe to delete. Rolling back to 1.4.7 needs no restore.

## 1.4.7

The home shows fewer, honest rows. Themed rows (moods, lanes, Because, Same Feeling, documentaries, studios) show only their own picks and hide when they have too few, instead of padding with unrelated titles. For You puts its own picks first and only tops up to 12. On a copy of a real household each profile went from 28 rows to 18, and the share of borrowed cards fell from 39% to 4%.
Superhero titles are capped at two per row, except in a row that is about that universe, and one franchise appears in at most two rows. History and chart rows (Continue Watching, Catch Up, Watch Again, Coming Soon, Top 10) are exempt.
A member's own row ("For Ana") shows only on the household, Together and that member's profile, not on another member's profile.
Without a `rows` setting the home starts with a smaller default set: studio rows, Digital, Top Rated, New to You and all but one daily documentary row are off, and at most three mood and three lane rows show. Explicit `rows`, `row_order` and `row_labels` settings work as before.
Lane cards no longer show a bare keyword as their reason.
Themed rows ask the AI once a day to check their own candidates and add a few that fit, then order them by the household's taste, instead of asking the AI for each row's titles. At most eight themed rows are checked per build and twelve per day, counted across restarts. Past that, lane rows show their first 40 candidates ordered by taste, unchecked, and mood rows use their tag list. Reality, news and talk shows stay out of themed rows.
Marking many titles as watched at once (a library backfill) no longer counts as recent watching: those titles no longer pick Because seeds or For You seeds. For You also seeds from long-time favourites.
Searches for a feeling or theme ("something cozy", "anime I have watched") ask the AI for a wider list and order it by the searcher's own taste. In a blind test on a real household this list won more picks than the old one. The old picker runs alongside as a fallback, so a slow or failed wide answer still returns results. Watched titles and localized titles of the reference film are handled correctly.
Search answers within seven seconds. Routing, picks, year rules, title lookups and child checks share one deadline, so a slow AI answer switches to the fallback picks in time instead of taking up to nine seconds.
The home can show up to three "Because you watched" rows. Each starts from a different title the person really watched recently or loved, and the three starting titles must differ in feel, so the rows do not repeat each other. They rotate daily. Titles from a bulk mark or a player backfill start a row only when nothing else can.
New: ask "what shapes my picks?" (also in German and Serbian), or run `python -m skald taste align --person NAME --json`. Skald lists the titles and themes behind each person's picks. It flags titles that count only through a bulk mark, have no watch date, outweigh everything else, are disputed between members, come only from a shared account, or still pick rows after being ignored. Each flag comes with the matching correction preview. Nothing changes until you confirm.
Row metrics can now tell which rows earn their space: `python -m skald metrics learning --json` compares plays per showing for each row against the household's average, using only weeks old enough to judge. Each play counts once, however often the title was shown. Row metrics stay off unless `metrics.enabled` is set.
For You draws from a wider pool. It starts from up to 16 titles each person recently watched or loved, at most one per franchise and spread across different feels, and asks TMDB for both recommendations and similar titles. Each build fetches details for at most 50 new candidates per pool, best first; the rest fill in over later builds. `python -m skald rows pool --json` shows which titles seeded the pool and why others were skipped.
Match percentages now compare a title with up to 800 unseen titles Skald already knows, instead of this week's trending titles. Before, nearly every curated card showed 90% or more; now about a quarter do. Titles blocked for adults or children never enter the comparison. Coming Soon and member picks admit titles from 65% when that comparison is available. `python -m skald taste calibration --json` shows the comparison set and the spread of percentages.
New: `python -m skald rows plan --json` explains every home row, including hidden ones, and `rows why ROW --json` lists the cards a row kept and dropped, and why. `rows prefer --theme mood --more` (or `lane`, `--fewer`) changes how many mood and lane rows show, with a preview, and never drops the home below six filled rows. `rows refresh ROW` asks the server to rebuild one row on its next pass. Commander understands the same requests: "why these rows?", "more mood rows", "new picks in the cozy row".
A full home build now takes about seven minutes instead of about two, mostly waiting on the AI checks and TMDB. It runs in the background and the previous rows are served meanwhile.
Self-hosters: give the container 512 MiB. A full build peaked at 173 MiB on a real household copy, which leaves little headroom under 256 MiB.

**Data changes:** none to the data format (FORMAT stays 5); no migration. Row metrics files may carry optional play hashes and restored-cohort provenance, and credits are capped at 10,000 per cohort. A new `row_refresh.json` request file is ignored by older servers. Rolling back to 1.4.6 needs no restore.

## 1.4.6

Mood searches without a movie or series type fill the series row again.
Person searches no longer lead with talk-show appearances. Recurring series roles and directing work come before short guest spots.
Skald now learns from each person's whole history, not mostly from whoever watched most.
The taste report shows how much it has learned from each adult's titles.
Watching the same title together no longer counts it twice as support for a taste theme.
Search now sees each person's whole watched history. Imported titles are no longer suggested as new, and "watched" searches can find them.
Searches in one person's profile use that person's taste, not the household's.
Connecting one account no longer runs the whole setup: `skald setup --connect stremio --person NAME` asks only for that account's key.
When PublicMetaDB blocks a server with its bot check, Skald says so instead of reporting a rejected key, and keeps the last good history.

**Data changes:** none. No migration or restore is needed to roll back.

## 1.4.5

Search stays fast when you search several times in a row. The AI picker now starts while the router is still deciding, also after a previous search, and movies and series each get their own shorter answer. The router gives up after 2.5 seconds. Before, a second search could miss the deadline and leave the movie row empty.
Exact titles win over follow-ups: "Dark" after another search shows the series. A title that exists only as a series no longer fills the movie row with unrelated picks. Exact person names such as "Tom Hanks" show that person's acting and directing credits, in time even when the credits lookup is slow.
Typing no longer starts AI searches for half-typed words. A broken picker reply falls back to title results and can be retried.
History, help, settings and the other exact controls work right after a search.
Adults can rename themselves from a shared link and rename the Together row, with the usual preview, yes-code and undo. Rename previews show row names, not ids.
Stremio loves and likes now sync. Skald asked Stremio's likes service the wrong way before. If likes are unavailable, the library still syncs and earlier likes stay.
Search logs record router, picker and title lookup times, one line per search. `python eval/search_battery.py` runs a fixed, ordered search session against a live Skald, both rows at once like a player, and flags empty rows, filler, slow answers and missed deadlines.
The test suite no longer reads a private file that the public repository leaves out; that made the 1.4.4 CI run fail.

**Data changes:** none to the data format. Lines in searches.jsonl may carry a new optional `timing` field. The Together label uses the existing household labels setting (`household.labels.pair`). Rolling back needs no restore.

## 1.4.4

You can now talk to your search results. After a search, type a reply in the same box and Skald treats it as a follow-up: "more like the second one", "but less violent", "also some series", "weniger albern". The decision model decides whether a reply refines the last search, acts on it, or starts a new one; no fixed phrases. Works in English, German, Serbian, Spanish and Portuguese.
"I like these, save them as my taste" and "save this as a row" act on the titles you were just shown. Both show a preview with the titles and need the usual yes-code; guests and kids get the same limits as for likes and shelves.
Skald remembers the last search per viewer for 30 minutes, in memory only.
A slow AI answer is no longer thrown away when search gives up waiting; searching again shows it at once instead of paying for a new call.
Search answers are no longer cached by the player, so follow-ups always reach Skald.
New follow-up cases are in the router test set, and `python eval/route_compare.py` compares decision models and prompts on them (speed, cost, accuracy per question and language).

**Data changes:** none. Search context stays in memory; saved taste and shelves use existing storage. Rolling back needs no restore.

## 1.4.3

Search now understands what you ask instead of matching fixed phrases. A decision model reads every search and answers three questions: is it a title, a wish, a genre filter or a command; should it show only titles you have not watched, only ones you have, or any; and do you want the widely acclaimed best or picks for your taste. It works with typos and missing apostrophes in English, German, Serbian, Spanish and Portuguese.
"Dark" on its own still finds the series; "dark movies" now gives dark films picked for your taste instead of "Not in your library".
"Animes I havent watched" and "something new to me" leave out everything in your watch history, and a row that falls under 8 titles tops up with unseen recommendations.
Search rows show at most 15 titles, and the AI picks now see your watched titles so they suggest fewer of them.
The router uses Perplexity's decision model, which scored best in a live test of five decision models (94% on 173 cases, about $0.015 per thousand searches). Set SKALD_ROUTE_MODEL to try another.
The router test cases live in eval/fixtures/decisions/route_query_live.jsonl; `python eval/decisions.py --type route_query --live` scores them.

**Data changes:** none. The data format stays 5 with no migration; rolling back needs no restore.

## 1.4.2

For You keeps this week's trending titles out unless they fit your taste as well as your usual recommendations, and shows at most two of them.
Latest Digital Releases and Top Rated Movies now hide titles you have seen or rejected and fill back up to 20 from later pages.
Latest Digital Releases skips old films that only came out again digitally.
Cards no longer show a cut-off reason such as "For your.".
After a restart, Skald builds the taste model right after the first row pass instead of waiting for the next scheduled rebuild.
Command answers in search keep short card names that players do not cut off; the full text is on the card's detail page.
Ask "what do you know about Ana" (also German and Serbian) for a plain-language summary of a member's settings, shelves and taste.
The README is shorter and starts with install; details moved to new guides on rows, search and how Skald learns.

**Data changes:** none. The data format stays 5 with no migration; rolling back needs no restore.

## 1.4.1

Connect a PublicMetaDB (PMDB) account per person. Skald reads watch history, watchlist, resume points and dropped shows with each person's private key, never writes to PMDB, and includes PMDB in backups, forgetting and `skald connectors test pmdb`.
Six Compose examples cover addons on a home network, Odin/Jellyfin, an existing AIOStreams, Caddy HTTPS, Cloudflare Tunnel and small boxes, each with a private env template and setup commands.
Titles in Greek, Cyrillic, Chinese and other scripts now show when TMDB has a translation in your language, instead of being replaced with the English title.
Row names and notes now come from one bundled English string table, ready for translations. What you see does not change.
Title fingerprints are built from the title details Skald already keeps, without asking TMDB again, and a switch to a new embedding model no longer stalls when a few titles can never be embedded.
Commands now come from one table that lists each command's fields, checks, preview and undo. Existing commands behave as before.
Several changes in one sentence preview as one plan, apply together or not at all, and undo together.
Say "no, I meant Ben" or "not horror, thriller" right after a change to fix it.
Unclear requests get one short question instead of a guess.
Wishes such as "I wish there were more gentle mysteries" are noted in a new optional wishes.jsonl and change no settings.
Shelves can take their own filters, and seasonal shelves can show only in chosen months, kept in a new optional shelf_rules.json.
Household members can be added or updated by command, with preview and undo.
"Something like Arrival", "movies by Kim" and "something cozy tonight" search without changing settings.
Say "not tonight Arrival" or "hide Arrival until 2026-12-24" to hide one title for a while; it comes back by itself and is kept in a new optional title_fixes.json that backups include.
Say "Arrival is not for me, too slow" to rate a title down with a reason, and "I already watched Arrival" in English, German or Serbian to mark it seen.
Say "don't learn from my watching of Arrival" or "Ben watched Arrival, not me" to fix what your taste learns from one watch; plain undo reverts it and forgetting a person removes these fixes.
Ask "why is Arrival in my picks" for a short explanation, or "the movie Calm" to search a title whose name looks like a mood.
Each member and the household now get a short taste archetype, a name and one line such as "Twisty puzzle chaser", shown in `skald me` and `skald explain`. A small model names it at most once a week from the taste Skald already learned, and Skald uses its own wording when no model is set up or the answer is unusable. Archetypes are kept in a new optional archetypes.json that backups include and forgetting removes.
The new opt-in New to You row, turned on with `skald setup rows` or by saying "add the New to You row", picks titles for one member or Together each day that keep what they love but try a kind of story they have not watched yet. Saying not for us to a title from that row counts only lightly against their taste.

**Data changes:** none. The data format stays 5 with no migration. The new optional files wishes.jsonl, shelf_rules.json, title_fixes.json and archetypes.json appear only once used, and older versions ignore them, so rolling back needs no restore.

## 1.4.0

Skald checks its own health on a schedule, repairs a small set of known faults by itself and can alert you by Telegram, ntfy, Gotify or Discord; `skald health` shows the saved checks and the last repair.
Optional local row metrics count which rows lead to plays, per person and week; they are off by default and `skald metrics` reads them.
Settings changes made in plain words keep a history: `history`, targeted `undo`, `redo` and a confirmed `reset` work from search and `skald command`.
Optional history minimisation folds old evidence into monthly summaries, and `skald history --forget-before DATE` forgets evidence before a date, including derived copies; both are off by default and preview before writing.
Search understands more command phrasings, with a cheaper fallback model when the main one fails.
Shelves get fixed slots, and AIOStreams re-syncs rows after shelf changes.
Household setup, row building speed and several setup fixes improve.
Embeddings default to Gemini Embedding 2; set `SKALD_EMBED_MODEL` to choose another.
Detailed `/health` is no longer treated as local when a proxy forwards the request, a malformed metrics file can no longer break rows, and Discord alerts count as sent only once Discord confirms them.
A household evaluation harness under `eval/household` compares row changes on frozen copies of a household's data.

**Data changes:** DATA format goes from 1 to 5 (2 household settings, 3 row metrics store, 4 settings history metadata, 5 empty minimisation summary store); all additive, run at engine startup after Skald makes and verifies a backup of household state.
Version 1.3.0 refuses format 5 data. To roll back, stop the engine, restore the DATA backup made before the upgrade and `.env`, then start 1.3.0; swapping the release alone is not enough.

## 1.3.0

Each member can set a kids age with `skald setup prefs --kids-age N|off`; their rows, search and details then show only titles certified for that age, and unknown or adult titles stay hidden.
Households with a child get a Family row picked for the youngest child's age; Together rows learn from adult plays only.
Shelves are standing rows with optional weekday and time windows: up to 10 per person and for Together (4 and 2 by default), managed with `skald shelf` or from search.
Search can change settings in plain words; each change shows a preview card and applies only after its exact "yes NN" code, with undo and a help card.
`skald suggest` and search cards offer shelves based on repeated searches and attributed plays.
Command cards in Odin lead with their code and a readable name and open to a details page.
After a shelf or row change Skald asks AIOStreams to reload rows at once for logins made by `skald setup aiostreams`.
`skald setup rows KEY,...` chooses which Home rows show, in Skald's usual order; two opt-in movie rows, Latest Digital Releases and Top Rated Movies, are new, and `skald setup rows all` restores the defaults.
In a shared AIOStreams database, `--map PROFILE=skip` ignores another household's profile.
`skald import trakt` reads the Trakt export zip directly, and Trakt favorites count as loved.
`skald setup --connect` now says when Skald needs a restart to start reading, and status shows a pending restart even while other steps remain.
`skald setup aiostreams --uuid` replaces a custom Skald preset that points at another engine instead of adding a second one.

**Data changes:** no format bump or migration (DATA format stays 1); adds optional `kids_age` and `shelves` member preferences and `household.shelves` and `household.rows` in `skald.json`, new private `shelves.json` and `commands.json`, and a person field in `searches.jsonl`.
Version 1.2.0 ignores the household keys but rejects `kids_age` or `shelves` member preferences; clear them with `skald setup prefs --person NAME --clear` before rolling back. Child links are not age-filtered on 1.2.0.

## 1.2.0

Each member can hide movies, series, documentaries, animation or anime with `skald setup prefs --hide`, `--show` and `--clear`.
Hiding movies or series affects only that member's own rows; documentaries, animation and anime stay hidden in Together if any member hides them.
Each member can focus on genres, languages and countries; focus moves matching titles up in their own rows and never removes others.
Next episodes ignore these settings; search stays unfiltered.
AI prompts no longer assume two members are a couple or use a gendered default, and the household note now reaches the This Week and mood picks.
The README explains what changes as you watch, and the agent skill covers connecting sources, AIOStreams, adopt and personal settings.

**Data changes:** no format bump or migration; adds optional `hide` and `focus` member preferences in `skald.json` and an origin country field in cached title facts, filled in small batches only when a member focuses on countries.
Version 1.1.0 rejects configs with `hide` or `focus` set; clear them with `skald setup prefs --person NAME --clear` before rolling back.

## 1.1.0

AI search can exclude watched titles or find matches in your own history, including member and Together links.
Connect Stremio per person to learn from watched titles, library interests and loved/liked titles.
Set each member's discovery style, release years and excluded movie or series genres with `skald setup prefs`.
Together rows respect everyone's genre exclusions; settings do not filter search.
Taste lanes can show More from a director or series creator behind at least two titles a member liked or finished.
Together views show these rows only when every member qualifies.
A director or creator row needs 8 unseen titles, so a typical filmography is enough.
The README now opens with what Skald does at home and in search, with pictures from a test household.

**Data changes:** no format bump or migration; adds private Stremio keys and per-person history files, like MDBList, and optional member preferences in `skald.json`.
Version 1.0.9 can read configs with preferences set, but ignores them.

## 1.0.9

`skald setup adopt --yes` makes an older household editable while preserving its config and catalog ids.

**Data changes:** no format change or migration; adoption adds private `skald.json`
and `.skald-household`. Remove both to roll back to the retained `legacy.json`.

## 1.0.8

Connect MDBList per person to read watched history and watchlists with a free API key.
Setup shows each connection and can remove its private key.

**Data changes:** none. MDBList keys are stored privately in DATA like Trakt credentials.

## 1.0.7

A second `skald import` no longer erases titles still waiting for review from an earlier import.

**Data changes:** none.

## 1.0.6

Large YouTube, Chrome and Trakt exports use less memory when imported in the container.
Piped imports are recognised as new files when their contents change.
Piped and Chrome imports briefly use free disk space in the data folder for a temporary copy.

**Data changes:** none.

## 1.0.5

`skald import youtube` now reads the Takeout watch history as HTML, which is
what Google Takeout gives you by default, as well as JSON. You no longer need to
redo the export just to pick JSON.

**Data changes:** none.

## 1.0.4

Rows that come back no longer land at the bottom in AIOStreams. Once you have
reordered or hidden a row on the AIOStreams page, run `skald setup aiostreams`
again: it puts every Skald row back in Skald's order, in the place Skald's rows
already hold. Rows of other addons stay where they are, and rows you hid stay
hidden. Nothing is saved when nothing changed.

`skald setup --status` asks for a restart only while the running engine still
has older settings. A household made before `skald setup` now gets a status
that shows what it can see (TMDB key present, ratings per member) instead of
an error. `skald doctor` suggests importing a Netflix or IMDb export next to
the interview for new members.

**Data changes:** one small file, `.skald-startup`, records which settings the
engine loaded at start. No migration. Going back to 1.0.3 ignores it.

## 1.0.3

Importing on a laptop with `--remote` now matches titles through your own Skald
server, so the laptop needs no TMDB key. Only titles, years and ids are sent,
and `--dry-run` works the same way. `--resolve-online` still works without the
server.

A laptop token made for one member now pushes that member's titles and says
how many it skipped for others, instead of failing the whole push. A refused
push says why.

Watch Again only shows titles you have watched or rated. A new household no
longer gets a row of suggestions it never watched.

**Data changes:** none. New API routes for title matching and token checks.
Update the laptop and the server together.

## 1.0.2

`skald setup aiostreams` creates an AIOStreams login with Skald and title details
from The Movie Database addon (details only). It prints the Odin / Jellyfin app
login. Existing AIOStreams users can add Skald to their login with `--uuid`;
nothing else in that login changes. Skald's rows stay in Skald's order.

Home only lists rows that have something to show. A row appears once it has
enough titles. Rows no longer share a name in Jellyfin apps.

`.env.aiostreams.example` includes optional `MANIFEST_CACHE_TTL` so new rows
show up within minutes.

**Data changes:** New optional file `DATA/credentials/aiostreams.json`, with
permissions 0600, written only by `skald setup aiostreams`. DATA format unchanged.
No migration.

## 1.0.1

Watch Again no longer repeats a title already shown higher up on the home screen.
The recommended setup now creates a private DATA folder before starting Skald.

**Data changes:** none.

## 1.0.0 — first public release

First public release of Skald: household setup, taste-based shelves, connectors,
and the local CLI.

**Data changes:** Format 1 is the existing DATA layout. Skald records the format
and release in `.skald-format`; households without it count as format 1. Future
upgrades migrate automatically after a verified backup. Older releases refuse
newer data formats; restore the pre-upgrade backup to roll back.

Row ids use the household’s optional `row_prefix`, defaulting to `skald`. Existing
households supply their prefix through `legacy.json`; stored row ids stay intact.

Household setup supports private addon links and a laptop API connection.
History imports preserve member ownership and repeated events. Backups verify
archives before restoring, and offline tools check copied data formats before
loading them. CLI errors explain the next step without exposing credentials.
