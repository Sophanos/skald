# Set up Skald

We recommend the [setup we run](WATCH.md): Odin on Apple TV connects to AIOStreams
in Jellyfin mode. AIOStreams reaches Skald inside Docker. Only AIOStreams needs HTTPS.
First run `mkdir -p data && sudo chown 65534 data && sudo chmod 700 data`.
Skald runs as user 65534 and keeps DATA private.
Use `compose.stack.example.yaml` and both example env files. Add TMDB and OpenRouter
keys in `.env`. Make `SECRET_KEY` with `openssl rand -hex 32` and set `BASE_URL` to
AIOStreams' public HTTPS address in `.env.aiostreams`. Keep both files private.
AIOStreams stores its database in its own named volume at `/app/data`.
Its database setting is `DATABASE_URI=sqlite://./data/db.sqlite`.
AIOStreams is a separate project with its own [docs and licence](https://github.com/Viren070/AIOStreams).
We reference its published image.

Run `docker compose -f compose.stack.example.yaml up -d --build`, then
`docker compose -f compose.stack.example.yaml exec skald skald setup`.
Then run `skald setup aiostreams`: it makes your login with Skald and title details, and prints the Odin server, user and password.
You can also supply `--internal-base http://skald:8080 --watch jellyfin-app`.
Commands below using `exec engine` describe the Stremio-direct alternative;
for the recommended stack, use `-f compose.stack.example.yaml exec skald` instead.


Run setup where your household data lives. In Docker:
`docker compose exec engine skald setup`.
The folder mounted at `/data` is called DATA below.
Start with the [README quickstart](../README.md).
Clone from `https://github.com/Sophanos/skald`.
Delete the `SKALD_MEMBERS`, `SKALD_LANG` and `SKALD_TZ` lines copied into `.env`;
setup asks for these household settings instead.

### Without Docker

Use Python 3.12; no packages need installing. `DATA_DIR` selects the household
folder and defaults to `/data`. Choose a folder you own, create it with mode 0700,
and export your TMDB key in the same shell before setup:

```sh
export DATA_DIR="$PWD/data"
mkdir -p "$DATA_DIR" && chmod 700 "$DATA_DIR"
export TMDB_API=YOUR_KEY
python -m skald setup
python server.py
```

Supply the real key privately so it does not enter shell history. For example,
Python's `getpass` can read it: `export TMDB_API="$(python -c 'import getpass; print(getpass.getpass("TMDB API key: "))')"`.
Stop and restart `python server.py` after changing settings.


## 1. Check the machine and keys

**What it does:** checks that DATA is private and writable, then makes one TMDB request.
**What you need:** DATA owned by the engine user (65534 in Compose), mode 0700,
and `TMDB_API=YOUR_KEY` in a private `.env` file (mode 0600).
Get a free key from TMDB's account settings → API.
After changing `.env`, run `docker compose up -d --force-recreate`.
**Worked when:** setup prints “DATA writable and private; TMDB key valid”.

OpenRouter is optional. Add `OPENROUTER_API_KEY=YOUR_KEY` for AI picks,
natural-language search, mood shelves and weekly picks. Rows work without it.

## 2. Add your household

**What it does:** saves one to six members, language, timezone and country.
**What you need:** short lowercase names such as `alex,sam`, at least two letters each;
a language such as `en-US`; a timezone such as `Europe/Berlin`.
**Worked when:** setup prints your names and settings and DATA contains `skald.json`.
It writes atomically. A rerun keeps existing settings and asks only for missing steps.
You can add a member. Setup refuses to remove or rename someone who has data.
The optional `row_prefix` in household config sets catalog ids, for example
`skald.foryou`. It defaults to `skald` and accepts 2–16 lowercase letters and
digits, starting with a letter. Existing households keep their prefix in
`DATA/legacy.json`; stored row ids are not rewritten.

Older households kept in a separate configuration file are read-only in setup.

For agents, every question has a flag. For example:

```sh
docker compose exec engine skald setup --yes --members alex,sam --lang en-US --timezone UTC --country 'United States' --base http://192.168.1.20:8080 --watch stremio --connect files
```

`--yes` never prompts. A missing value gets a one-line fix.
Use `--json` for a machine result. Addon tokens are replaced by `<addon-token>`;
AIOStreams passwords and Stremio login URLs are hidden too; run in text mode to get the private login and URLs. Laptop linking requires text mode too.

## 3. Choose the address

**What it does:** prints private member and Together addon URLs.
**What you need:** an address your watching app can reach.
Compose initially listens only on the host's `127.0.0.1:8080`.
For your LAN, change its port line to `"192.168.1.20:8080:8080"`, using your machine's LAN address,
then recreate the engine. Use that address with `--base`.
For apps outside your network, put an HTTPS proxy or tunnel in front of Skald.
**Worked when:** your app can open a printed manifest URL (the addon description).

`skald setup --base https://tv.example.org --check` explicitly fetches
`https://tv.example.org/en-US/manifest.json` using your chosen language.
A private addon can reject that path; use the printed private URL to check installation.
Setup reports the rejection and a fix. It does not silently send your addon token there.

For the recommended stack, proxy AIOStreams on port 3000. Skald's internal URL
already works between containers; it needs no public address for watching.

### Caddy

Example for Caddy running on the Docker host:

```caddyfile
aio.example.org {
    reverse_proxy 127.0.0.1:3000
}
```

Keep the paths and Host header unchanged. Do not enable request access logging.
Keep private URL paths out of proxy error logs too.
If Caddy is in Docker, share a network and use `aiostreams:3000` instead.
For Stremio direct, proxy Skald on `127.0.0.1:8080`; optional laptop API access uses 8081.

### Cloudflare Tunnel

Install and sign in to `cloudflared`, create a named tunnel and route your hostname to it.
In its config, use your own tunnel id and credential path:

```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: /path/to/tunnel.json
ingress:
  - hostname: aio.example.org
    service: http://127.0.0.1:3000
  - service: http_status:404
```

Run `cloudflared tunnel run YOUR_TUNNEL_ID` on the Docker host.
For Stremio direct, use your Skald hostname and port 8080.
For laptop API access, add a rule for your Skald hostname with `path: /api/v1/.*`
and `service: http://127.0.0.1:8081` before the addon rule.
Use a similar rule for `/taste/.*` if you use the private feedback page.
Keep tunnel credentials and request logs private.
These deployment examples should work, not yet tested by us.

## 4. Choose where you watch

**What it does:** prints the matching [watching recipe](WATCH.md).
**What you need:** `--watch stremio`, `--watch aiostreams` or `--watch jellyfin-app`.
**Worked when:** Skald rows appear in the player.
Run `skald setup aiostreams --uuid <your uuid>` to add Skald and missing title details to an existing login; it keeps your other settings and asks before saving.
Use `--aio-url` for the AIOStreams address; `--member NAME|together|all` makes member logins.
Passwords use a private prompt or `--password-stdin`. Protected instances ask for their access key (`--secret-stdin` reads it first).
New passwords are shown once and saved privately in DATA/credentials/aiostreams.json. Reruns use saved logins.
AIOStreams may need a restart after rows change.

## 5. Connect taste sources

**What it does:** saves a private credential, maps accounts to people, then runs a read-only test.
**What you need:** a source account or an export. See [each source](CONNECTORS.md).
**Worked when:** the connector test says reachable, auth OK and zero unmapped accounts.
Use `--connect files` for laptop import instructions.
Secrets go through a private prompt, `--secret-stdin`, or a named environment variable.
They never belong in command arguments. After changing connectors, restart the engine.

## 6. Link a laptop (optional)

**What it does:** creates a token and prints laptop commands. The token is shown once.
**What you need:** Python 3.12 and a Skald checkout on the laptop, plus API access. The API lets your laptop send imports to the engine.
Add `API_PORT=8081` to `.env` and a Compose port `"127.0.0.1:8081:8081"`.
Recreate the engine and forward `/api/v1/` through a separate Skald hostname to 8081.
The AIOStreams address serves AIOStreams, so it cannot accept Skald laptop imports.
**Worked when:** the laptop import preview can use the API.

```sh
docker compose exec engine skald setup link --name laptop --person alex --base https://tv.example.org
```

On the laptop, run the printed `python3 -m skald connect URL` and paste the token privately.
It stores the address and token in `~/.config/skald/`, with private permissions.
`SKALD_API` and `SKALD_TOKEN` still override them.
Local import state lives in `$XDG_DATA_HOME/skald`, or `~/.local/share/skald`.
`--state-dir` overrides that folder. Container imports also need a persistent, writable folder.
For shared imports, run the printed `export SKALD_MEMBERS=...` command.
For title lookups with `--resolve-online`, provide your TMDB key in the laptop environment
as `TMDB_API`, using a private prompt rather than typing it into shell history.
Preview with `push imports --remote`; send only after approval with `--apply`.
These commands use HTTP, without SSH.

## 7. Finish and check progress

**What it does:** prints a doctor summary and the next command.
**What you need:** follow that command on the Docker host when it is a Docker command.
**Worked when:** `docker compose exec engine skald setup --status` has no missing steps.
Restart the engine after setup so it loads the saved settings, then install the printed URL.
Allow time for the first rows to build. Title search works while rows are building.
Print links again with `skald addon-url --base YOUR_ADDRESS`.
Rotate a leaked addon link with `skald addon-url --rotate-addon-token --base YOUR_ADDRESS`.
That replaces every installed link. Reinstall them in your apps.
