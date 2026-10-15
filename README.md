# Skald

<p align="center">
  <a href="https://github.com/Sophanos/skald/releases"><img src="https://img.shields.io/github/v/release/Sophanos/skald" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License: AGPL-3.0"></a>
  <a href="https://github.com/Sophanos/skald/actions/workflows/ci.yml"><img src="https://github.com/Sophanos/skald/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

**Skald learns what each person in your home likes and fills your TV with rows picked for them.**

Up to six people get their own rows, plus Together rows for watching as a group.
You steer it from the search box on your TV: ask for a mood, ask why a title showed up, or tell it who really watched.

Self-hosted in one container. Works in Odin (Apple TV and Mac), Stremio and Jellyfin apps through AIOStreams; your stream addons still play the video.

<img src="docs/images/skald-odin.jpg" alt="Skald rows in Odin on a Mac for a test household" width="960">

## Install

### 1. Pick your setup

| You watch in | Install with | Learns from |
|---|---|---|
| Odin on Apple TV or Mac, Jellyfin apps | AIOStreams in Jellyfin mode | AIOStreams database, accounts, imports |
| Stremio via AIOStreams | Stremio link from `skald setup aiostreams` | Stremio library, accounts, imports |
| Stremio with Skald's link | Printed member or Together link | Stremio library, accounts, imports |

Tested by us: Odin on Apple TV and self-hosted Stremio via AIOStreams. Not tested with live services: Stremio-direct playback, Plex and the standalone Jellyfin connector (the Stremio library connector has synthetic tests).
Ready-to-run [Compose setups](examples/README.md) cover LAN apps, existing AIOStreams, HTTPS, a tunnel or a small box. See [watching](docs/WATCH.md).

### 2. Connect AI (optional)

Without AI, home rows, title search and short commands such as "hide anime" work. An [OpenRouter](https://openrouter.ai) key adds:

- **Search that understands requests**, like "anime I haven't watched" or "cozy mystery for a rainy evening", checked against your history.
- **Picks:** ✨ For You and ✨ This Week, plus tonight's mood shelves.
- **Experience tags** on titles, which fill `Picks for <member>`, `Same Feeling as <title>`, mood shelves and Fresh Finds.
- **Free wording** for commands and wish shelves, and sorting of unclear import matches.

Cost is capped: AI calls stop at 3 US dollars a month by default (`AI_MONTHLY_USD`), with daily limits per task.

### 3. Quickstart

This is the setup we run: Skald and AIOStreams together in Docker. You need Docker with Compose and a free [TMDB API key](https://www.themoviedb.org/settings/api).

```text
Odin or Stremio ──HTTPS──▶ AIOStreams ──inside Docker──▶ Skald
```

**Only AIOStreams needs a public HTTPS address.** Skald has no public port. In this stack AIOStreams listens on `127.0.0.1:3000`; point your HTTPS reverse proxy or tunnel there.

Other cases have their own [Compose setup](examples/README.md):

| Your situation | Use |
|---|---|
| You already run AIOStreams | [existing-aiostreams](examples/README.md#existing-aiostreams): joins Skald to your AIOStreams Docker network |
| Home network only, no HTTPS | [odin-jellyfin](examples/README.md#odin-jellyfin): AIOStreams on `http://YOUR-LAN-IP:3000` |
| Stremio without AIOStreams | [stremio](examples/README.md#stremio), [reverse-proxy](examples/README.md#reverse-proxy) or [tunnel](examples/README.md#tunnel) |

**1. Get the code and make the private data folder**

```sh
git clone https://github.com/Sophanos/skald skald && cd skald
mkdir -p data && sudo chown 65534 data && sudo chmod 700 data
```

**2. Make the private env files** (the last line writes a fresh AIOStreams secret)

```sh
cp .env.example .env
sed '/^SECRET_KEY=/d' .env.aiostreams.example > .env.aiostreams
chmod 600 .env .env.aiostreams
openssl rand -hex 32 | sed 's/^/SECRET_KEY=/' >> .env.aiostreams
```

**3. Add your keys and address**

```sh
${EDITOR:-vi} .env .env.aiostreams
```

| File | Line | Value |
|---|---|---|
| `.env` | `TMDB_API=` | your TMDB key |
| `.env` | `OPENROUTER_API_KEY=` | your OpenRouter key (optional) |
| `.env` | `SKALD_MEMBERS`, `SKALD_LANG`, `SKALD_TZ` | delete these lines; setup asks for them |
| `.env.aiostreams` | `BASE_URL=` | AIOStreams' public HTTPS address, like `https://aio.example.org` |

**4. Start**

```sh
docker compose -f compose.stack.example.yaml up -d --build
```

The empty Skald container waits for setup.

**5. Set up your household and your watching login**

```sh
docker compose -f compose.stack.example.yaml exec skald skald setup
docker compose -f compose.stack.example.yaml exec skald skald setup aiostreams
```

The second command makes your AIOStreams login with Skald and TMDB title details. Add the printed server, user and password in Odin, or install the printed Stremio link. Then configure your stream addons in AIOStreams and [connect your taste sources](#sources). [AIOStreams](https://github.com/Viren070/AIOStreams) is a separate project with its own docs and licence. We use its published image.

**6. Wait for the first rows** (several minutes)

```sh
docker compose -f compose.stack.example.yaml exec skald skald setup --status
```

**7. Learn from Odin plays** (after your first play in Odin)

```sh
docker compose -f compose.stack.example.yaml exec skald skald setup --connect aiostreams-db --source-path /aio-data/db.sqlite
docker compose -f compose.stack.example.yaml restart skald
```

**Stremio only, without AIOStreams:** make and edit only `.env` as above, then run the commands below and install the printed link. Skald then listens on `127.0.0.1:8080`; the stremio, reverse-proxy and tunnel setups above give it a LAN or HTTPS address.

```sh
docker compose up -d --build
docker compose exec engine skald setup --watch stremio
```

[Setup and network configuration](docs/SETUP.md#after-the-quickstart) covers the rest.

## How Skald learns

```text
watch · rate · import · commands
               ▼
     signals for each member
               ▼
         taste profile ─────▶ rows and picks
               ▲                    │
               └─── corrections ◀───┘
  (not tonight, already watched, not for me, undo)
```

Each member has their own taste profile; Together ranking uses the adult members. Corrections are typed in search and previewed first.
[How Skald learns](docs/TASTE.md) covers sources, timings and corrections.

## Sources

```text
┌─ Accounts, on the server ─┐   ┌─ Exports, on a laptop ───────┐
│ Stremio · Trakt           │   │ Trakt zip · Netflix · IMDb   │
│ MDBList · PublicMetaDB    │   │ Letterboxd · YouTube Takeout │
│ Jellyfin · Plex           │   │ Chrome history               │
│ AIOStreams database       │   │ Plain text list              │
└─────────────┬─────────────┘   └───────────────┬──────────────┘
              └────────────────┬────────────────┘
                               ▼
            Skald ─▶ rows for each member + Together
```

Connect accounts on the server with `skald setup --connect SOURCE`. Import exports on a [linked laptop](docs/TASTE.md#import-from-a-laptop) with Python 3.12:

```sh
python3 -m skald connect http://127.0.0.1:8081 --token-stdin
python3 -m skald import imdb ratings.csv --person NAME --remote
python3 -m skald push imports --remote
python3 -m skald push imports --remote --apply
```

The import resolves titles on your server, asks about unclear matches and saves only on the laptop. `push imports` previews the send; `--apply` sends resolved signals. With a `--person` token, other members' titles are skipped.
See [all sources, commands and limits](docs/CONNECTORS.md#sources).

## Rows

<img src="docs/images/skald-home.jpg" alt="Odin home showing Skald rows for a test household" width="960">

- **Picked for you**, built from taste: ✨ For You, ✨ New This Week, `Picks for <member>`, `Because You Love <title>`, `Same Feeling as <title>`, `From <maker>`, Watch It Again, Coming Soon, mood, theme and wish shelves, Fresh Finds.
- **Catalogs**, plain lists filtered by your history: Continue Watching, Catch Up, Top 10, Anime This Week, `Anime · <season> <year>`, Latest Digital Releases, Best Movies, Best Series.

✨ rows need OpenRouter. Each member and Together get their own rows; each member can choose discovery style, release years and unwanted genres.
Get Ready brings earlier saga films or unfinished seasons together before a new release. Each viewer sees only their own reasons for catching up. Screenshots show a test household. See [all rows and settings](docs/ROWS.md).

## Talk to it from the search box

No extra app needed. The search field in Odin or Stremio finds titles ("Dune" searches as usual), teaches Skald what you like and changes your rows, in English, German and Serbian at the same time. ✨ marks what needs OpenRouter; short fixed phrases such as "hide anime" work without a key, free wording needs one.

<img src="docs/images/skald-search.jpg" alt="Odin search for anime I haven't watched, showing Skald AI results for a test household" width="960">

| Find | Skald |
|---|---|
| "cozy mystery for a rainy evening" ✨ | Picks for tonight that fit your taste. |
| "anime I haven't watched" ✨ | Searches around your own history. |
| "why is Arrival in my picks" | Explains the pick. Changes nothing. |
| "what do you know about me" | Shows your taste, shelves and settings. |

| Teach it | Skald |
|---|---|
| "I loved Dark", "I hated Dune" | Likes or dislikes the title. |
| "I already watched Arrival" | Marks it seen. |
| "Arrival is not for me, too slow" | Rates it down and keeps the reason. |
| "Skip Arrival tonight" | Hides it for tonight. Your taste stays the same. |
| "Ben watched Arrival, not me" | Moves your watch, and the taste, to Ben. |

| Change your rows | Skald |
|---|---|
| "hide anime for Ana" | Changes Ana's rows. |
| "make me a row of heist films" ✨ | Adds a heist shelf. "for us" makes it a Together shelf. |
| "stop the sunday shelf" | Stops a shelf. |
| "rename For You to Ana Picks" | Renames the row. |

| People and family | Skald |
|---|---|
| "Add my mother Vesna" | Adds a member. A name alone works too. |
| "My girlfriend Lena is moving in" | Adds Lena. Also in German and Serbian. |
| "Add Kim, she is 12" | Adds a child. Kim's rows follow her age. |
| "Add Marko as a guest for 3 days" | A visitor who does not change household taste. |
| "Ben is away until 1 November" | Pauses Ben's influence on household picks. |
| "What did Kim watch this week?" | A parent reads a child's plays. |
| "When is Kim around?" | A parent sees a child's watching days. |

**Every change is previewed.** A card whose name starts with a `yes NN` code shows what would change. Type the code within ten minutes to apply it, `undo` to take it back. Read-only questions such as "why is Arrival in my picks" change nothing and need no code.
"what changed this week" lists the last seven days with ids. "undo the AI budget" or "undo ID" reverts one change and keeps newer, unrelated ones. Right after a change, "no, I meant Ben" fixes it.
More: [everything you can type](docs/SEARCH.md), [how corrections teach](docs/TASTE.md#correct-it), [commands by task](docs/CLI.md).

## People in your household

- **Up to six active people**, children and guests included. Each has their own rows, and so does Together, which ranks by the adults. Guest visits and shelves end after the last local day; saved guest history stays.
- **Odin plays count for the right person.** A play goes to the member whose login played; otherwise to Who's watching, and without one to Together. `skald plays` shows new plays per person and `skald plays person ben off` stops reading one ([Odin plays](docs/CLI.md#odin-plays)).
- **Child reads** are for current adults only. Guests and away adults cannot ask, and adults cannot read each other's plays. Watching days come only from the child's own Odin/AIOStreams login, keep 56 days of dates and hold no titles or times. Shared logins never count. Skald shows dates and counts without guessing where the child is. [Privacy](docs/PRIVACY.md#childrens-watching-days).

## Leaving and erasing

- **"Ben moved out"** removes him from the household and keeps his data. `undo` restores him.
- **"Forget Ben"** and **"erase everything"** only preview in search and show the server command. Erasing runs only from the server command line and cannot be undone.
- **One person:** `python -m skald members forget ben --person ana`. An adult can erase an adult, child or guest.
- **Whole household:** preview with `python -m skald erase --household --json`, confirm the matching plan, then type ERASE. Scripts and `--json` require `--confirm ERASE`; see the [CLI confirmations](docs/CLI.md#household-people-and-controls).

<details>
<summary>The fine print</summary>

- **One person:** shared watches keep teaching whoever stays, without Ben's name; add `--shared drop` to remove those watches too. If Skald saved Ben's Odin login or mapping, it removes that copy and reminds you to remove access in Odin and AIOStreams.
- **Household:** deletes personal data, saved logins and all backups inside Skald's DATA folder. Keeps addon tokens, the format marker, setup addresses and server lock files, along with an empty erase marker. Existing addon links answer with empty rows; run setup again and restart Skald.
- **Backups outside** Skald's data folder are not touched. Delete them yourself. More in [Privacy](docs/PRIVACY.md#leaving-guests-and-forgetting).

</details>

## Your data

Household history and taste are stored on your machine. [How Skald handles your data](docs/PRIVACY.md) lists every request that leaves it.
Keep addon links, tokens, data and backups private. Anyone with the household addon link can choose any member's rows.

## Docs

[Setup](docs/SETUP.md) · [Watching](docs/WATCH.md) · [Your data](docs/PRIVACY.md) · [How Skald learns](docs/TASTE.md) · [Rows](docs/ROWS.md) · [Search](docs/SEARCH.md) · [Connectors](docs/CONNECTORS.md) · [Backups and upgrades](docs/UPGRADE.md) · [Commands](docs/CLI.md)

## License

[GNU AGPL-3.0](LICENSE). This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB.
