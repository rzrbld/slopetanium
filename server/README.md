# Slopetanium community server

The service behind [slopetanium.pw](https://slopetanium.pw).

- **Slopetanium on handhelds** shares ports here, posts reviews and, when its owner agreed,
  counts installs.
- **The website** is rendered on the server and is read-only:
  - `/`: the landing page, Slopetanium's features. Its download button links to the newest
    release's zip, checked hourly.
  - `/ports` and `/p/{owner}/{name}`: the shared ports, with ratings and reviews.
  - `/about`: what is stored.
  - `/cover/{owner}/{name}`: a port's box art. It is fetched once from its `cover_url` (public
    https addresses only, images only, at most 6 MB) and served from here.

It is one static Go binary with an SQLite database. It uses the pure-Go `modernc.org/sqlite`,
so no C toolchain is needed.

## API

The handheld sends `X-Slop-Device: <hex secret>` on each write. The server keeps only a keyed
hash of that secret (HMAC-SHA256 with a salt that lives next to the database, never in it).

| Call | What |
| --- | --- |
| `GET /v1/ports?q=&sort=popular\|rating\|updated\|new\|name` | Listed ports: shared by someone, checked on GitHub |
| `GET /v1/ports/{owner}/{name}` | One port: config, reviews, installs by version / handheld / firmware |
| `POST /v1/ports` `{repo, title, description, cover_url, config}` | Share a port. GitHub must have a release with a `.zip`. |
| `DELETE /v1/ports/{owner}/{name}/share` | Stop sharing it. It stays listed while another handheld shares it. |
| `POST /v1/ports/{owner}/{name}/reviews` `{rating, text, version, device}` | This handheld's review, edited in place |
| `DELETE /v1/ports/{owner}/{name}/reviews` | Delete this handheld's review |
| `GET /v1/me`, `PUT /v1/me` `{nick}` | The name shown on reviews, this handheld's shares and reviews |
| `POST /v1/pulls` `{repo, tag, device, cfw}` | An install. Counted once per handheld and version, for listed ports only. |

**Limits**

- Rate limits are kept per IP address and per handheld, in memory only.
- Request bodies are capped at 32 KB.
- Text is cleaned: printable characters only, with length limits.
- A port's `config` keeps only the fields Slopetanium knows, and each one is checked.

**Game information (website only):** with `IGDB_CLIENT_ID` and `IGDB_CLIENT_SECRET` (a Twitch
app) in `/etc/slopetanium/community.env`, each listed port is matched to its game on IGDB:

- **Matching:** by name. The server tries the title's "(Game Name)", then the GOG title, then the
  title itself. An exact name wins; otherwise a name with a subtitle ("Vyruz" matches "Vyruz:
  Destruction of the Untel Empire").
- **What it adds:** tags (FPS, Racing, Multiplayer…), with a tag filter on `/ports`, and an
  "About the game" panel on port pages: summary, developer, publisher, year, modes and
  screenshots.
- **Pictures:** served from here through `/igdb/{size}/{id}.jpg`.
- **Refresh:** a match is checked again monthly.
- **Not for handhelds:** none of this goes into the API Slopetanium uses.

 every port's latest release, stars and download counts are fetched from GitHub
every 6 hours. Set `GITHUB_TOKEN` for a higher GitHub rate limit.

## What is stored

- No IP addresses, locations or accounts.
- Caddy's access log is discarded.
- `/about` on the site says the same in plain words.

## Running it

```bash
go test ./...
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o slopetanium-community .
./slopetanium-community serve -listen 127.0.0.1:8787 -db community.db -base https://slopetanium.pw
```

`deploy/` holds the production setup:

- `slopetanium-community.service`: systemd, a locked-down `slopetanium` user that can write
  only `/var/lib/slopetanium`.
- `Caddyfile`: HTTPS for slopetanium.pw. Caddy gets the Let's Encrypt certificates and renews
  them itself. www redirects to the bare domain.
- `slopetanium-backup.timer`: a consistent copy of the database every night, kept 14 days in
  `/var/lib/slopetanium/backups`.

To update the server, build the binary, copy it to `/opt/slopetanium/slopetanium-community`,
then run `systemctl restart slopetanium-community`.

## Moderation

```bash
sudo -u slopetanium /opt/slopetanium/slopetanium-community admin -db /var/lib/slopetanium/community.db <command>
```

| Command | What |
| --- | --- |
| `stats` | Counts of ports, handhelds, reviews and installs |
| `list-reviews owner/name` | A port's reviews, with ids and author hashes |
| `hide-review <id>` / `show-review <id>` | Hide or show one review |
| `hide-port owner/name` / `show-port owner/name` | Take a port off the list, or put it back |
| `ban <author>` / `unban <author>` | Hide a handheld's reviews and refuse its writes |
| `igdb owner/name <id\|auto\|none>` | Pin a port's IGDB game, match it again, or show none |
| `backup <file>` | A consistent copy of the database |
