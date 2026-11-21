# openajar.com

The landing page for openajar, served from a Cloudflare Worker with static assets only (no Worker
script). Everything the site serves is in `public/`:

| File | What it is |
|:--|:--|
| `public/index.html` | the page: HTML with its CSS inline, light and dark from the system setting |
| `public/main.js` | the copy buttons and the Take over demo (the page works without it) |
| `public/404.html` | what an unknown address shows, with a 404 status |
| `public/_redirects` | `/install.sh` is a 302 to `scripts/install.sh` on the public repository's main branch |
| `public/_headers` | security headers, cache times, and `text/plain` for the MCP Registry file |
| `public/.well-known/mcp-registry-auth` | proves the domain to the official MCP Registry |
| `public/.assetsignore` | files never uploaded (`.DS_Store`). `.well-known/` must stay out of it |
| `public/fonts/` | Geist and Geist Mono, Latin, self-hosted (SIL Open Font License, `OFL.txt`) |
| `public/img/` | the product pictures, from `assets/readme/` and the Dawn picture from `web/src/assets/dawn/` |
| `public/og.png` | the social image, 1200 by 630, made from `og/og.html` |

No external scripts, fonts or trackers, and no cookies. The install line on the page is
`curl -fsSL https://openajar.com/install.sh | sh`: curl's `-L` follows the redirect to the same
installer the README's raw GitHub line runs.

## Look at it locally

A plain static server shows the page (it ignores `_redirects` and `_headers`):

```sh
cd site/public && python3 -m http.server 8765    # http://127.0.0.1:8765/
```

`wrangler dev` serves it as Cloudflare will, redirect and headers included. It needs no account:

```sh
cd site && npx wrangler dev                      # http://127.0.0.1:8787/
curl -sI http://127.0.0.1:8787/install.sh        # 302 to raw.githubusercontent.com
curl -si http://127.0.0.1:8787/.well-known/mcp-registry-auth   # 200, text/plain
```

Check the page at 390 and 1440 pixels wide, in light and dark, before you deploy a change.

## Deploy

From this folder, signed in to the Cloudflare account that holds openajar.com:

```sh
cd site && npx wrangler deploy
```

`wrangler.jsonc` names the Worker `openajar-site` and serves it on the route `openajar.com/*`. The route
rides on the proxied DNS record the domain came with (a custom domain would need that record deleted
first, and the deploy sign-in cannot edit DNS). `www.openajar.com` has no DNS record yet: add a proxied
one in Cloudflare, then the route `www.openajar.com/*`. The page names `https://openajar.com/` as canonical.
`workers_dev` and preview URLs are off. `npx wrangler deploy --dry-run` checks the config without
uploading.

## The social image

`og/og.html` is the source of `public/og.png`. After changing it (or the pictures it uses), render it
again with the Google Chrome on your computer:

```sh
uv run --no-project --with playwright python site/og/render.py
```

## Rules

The page follows web/DESIGN.md (Dawn): its colours are Dawn's tokens, violet only for the agent, what
is pressed and the one primary button, the Dawn picture only behind the night band and pictures of
the screen. No em dash in anything a person reads. Pictures that show the product's old name stay
out: `assets/readme/setup.png` does, and the live view clip here is cropped to the screen for that reason.
