# openajar on your computer

`openajar local` runs a small openajar service on your laptop (macOS or Linux). It serves MCP to the
agents on that laptop (Claude Code, Codex). When an agent opens an environment, openajar rents a
cloud VM for it (Hetzner, Morph, or a Docker container). By default it stays: when it goes idle it
is saved and its VM deleted, and it comes back when an agent opens it again with the same title
([ENVIRONMENTS.md](ENVIRONMENTS.md#keeping-an-environment)). A single-use one is deleted when the
agent closes it or its time runs out.
Nothing costs money while no environment is open, except saves (cents a month) and any VM you
chose to keep running.

The agent itself runs on the laptop too. So the laptop must be awake while a VM works: no agent
works while the lid is closed in this version.

`openajar setup` walks you through all of it. This page is what happens underneath, and how to do
it by hand.

## What runs

- MCP at `http://127.0.0.1:7410/mcp`, and the web app at `http://127.0.0.1:7410/`.
- Grants and approvals, the action log, the vault, the git gateway.
- VMs and environments, with their limits and the reaper that deletes them.

It listens on 127.0.0.1 only. There are no account browser lanes on a laptop (those need a Linux
server: see `openajar connect`). An environment has a terminal, your repo and a real browser.

## The environment's browser

The browser runs on the environment's VM, not on your laptop: Camoufox on the VM's own screen, a
small desktop with a dock (Browser, Terminal, Files) where you also see the agent's commands as it runs
them ([ENVIRONMENTS.md](ENVIRONMENTS.md#the-desktop); `desktop = false` turns it off). openajar on your
laptop drives it over the SSH connection it already holds for the terminal,
so the browser tools work as on a server (`browser_navigate`, `browser_snapshot`, `browser_act`,
`browser_wait`, `browser_screenshot` with `proof`, `width` and `full_page`). In it, `localhost` is the
VM. The agent gives you `links.browser`, the live view, at `http://localhost:7410/view/...`: watch
it, take over, and hand it back.

Nothing browser-related is installed on your laptop except Playwright's client (the installer's
`openajar[local]`). No browser is downloaded here, and no Xvfb is needed.

Where the browser comes from on the VM:

- **Docker**: the machine image has it (`openajar setup --docker` downloads it once, or `openajar
  environments setup --pull`). An image from before the desktop still works: each machine adds the
  desktop when it opens (a minute), until you download or build it again.
- **Hetzner with the `dev` snapshot**: it is in the snapshot, so each environment's browser starts
  at once. `openajar setup vms` offers to build it, or run
  `OPENAJAR_HOME=~/.openajar openajar vm image build dev`. It takes a few minutes and costs a cent or
  two, and the snapshot (about 4 GB) is billed as Hetzner snapshot storage while you keep it.
- **Hetzner without a snapshot**: each environment installs the browser when it opens. That adds 2
  to 4 minutes to every `environment_open`.

`environments.toml` has `browser = "vm"` here, also when it does not say so. `browser = "node"` (a
server's setting) stops the service with a message, because a laptop cannot run that browser.

### The live view

The link opens the environment's screen (its desktop, from the moment the environment is ready until
it closes, with or without the browser) as a card on a dark stage, with nothing above it: the screen's
own top bar already says the environment's title and what the agent is doing. The card is sized to the
screen's own shape (any size, any aspect) and as large as the window allows. Under it one pill says who
has control, with the agent's violet dot and its name when the agent's MCP client said one ("Claude Code
has control"), and one button: **Take over**, or **Hand back** once you have it. While the agent works
in its browser, a large violet pointer with its name shows where it clicks, types and scrolls. While you
have control the card has a white ring, the pill says since when, and the copy and paste buttons sit in
it. Everything else is one step away in **More** (the three dots): the screen's name, its state and
signal and where websites see the browser coming from, Stop agent, Agent activity, the theme, keyboard
shortcuts, and how long the link works. **F** (or the square button) is full screen; the pill stays under
the card. A view-only link says so and has no Take over button. On a phone the pill sits at the bottom, at the
thumb, a tap on the screen is a click once you have control, and the keyboard button in the pill opens
the phone's keyboard: what you type goes to the screen.

**Recording a clip.** The round button with the red dot, next to full screen (or **R** while you watch),
records what you see: press it again to stop, and the clip lands in your downloads, named after the
environment and the time ("Launch checklist 2026-11-20 at 23.01.54.mp4"). While it records, the button
shows a red dot and how long it has run; it stops by itself after three minutes and says so. The clip is
made in your browser from the picture the page already shows, with the agent's pointer drawn in, and is
never uploaded: nothing new is sent to or asked of openajar, and a view-only link records too (only what
that link already shows). It is framed for posting: 1920 by 1080, the screen as a rounded card on the dark
stage with a small openajar mark under it, 30 frames a second, MP4 (H.264) where the browser can record it
(Chrome does) and WebM elsewhere. The button is not there in a browser that cannot record.

Measured on the same local link: a 27 second clip of an agent ticking items, typing and running a
command is 4.3 MB (about 1.3 Mbit/s); a screen that never stops moving takes about 2.9 Mbit/s, so a
three minute clip stays under 70 MB. While it records the page still draws 60 frames a second (the
slowest frame 16.8 ms, as without it), the stream still updates as often (about 78 draws a second either
way), and the clip's frames come every 33 ms. Only while it records does the page compose a frame of its
own, and it stops the moment you press stop. A tab in the background keeps recording but its picture
holds still until you come back (browsers pause drawing there).

The web app's environment page shows the same pointer over its live view, with the agent's name.

Speed, measured on a local link (a Docker environment, 1920 by 1080, a page scrolling at 60 frames
a second): about 19 frames a second reach the page at about 23 Mbit/s, a key typed shows in about
40 ms and a Page Down in about 105 ms. The page draws frames as they come (noVNC, Tight with JPEG at
quality 6, zlib 2) and does no layout work per frame; the pointer is drawn locally. Quality and
compression levels from 2 to 9 moved this by a few frames at most: the limit is x11vnc's encoding on
the machine, not the page.

## Signing in

The browser on the VM is a plain browser with tabs that the agent uses when it needs to. When the work
needs an account (a dashboard, a staging site), give the computer a login from your vault, here on your
laptop. The agent then signs in by itself whenever it needs to, and never sees the password:

```
openajar environments login add github.com --user me@example.com --title "Shop fix"   # hidden prompt
openajar environments login list
openajar environments login "Shop fix" github.com                                      # signed out? sign it in again now
```

`--title` follows that title across saves and resumes, `--repo` covers a repository's environments,
`--all` every one; `--totp` stores the TOTP seed too, so codes fill as well. The agent's `login` tool
opens the site's login page and fills it; `environment()` lists the logins the computer may use. A
CAPTCHA, a device approval or a code by email or text is yours, in the live view.

The password is typed into the VM's browser over the connection this laptop already drives, so it
passes through that browser's process, and the session cookies stay in that browser and in its saves
(encrypted). Anything running as root on the VM could read what the browser holds, so bind only what
that computer's work needs. All of it is in [ENVIRONMENTS.md](ENVIRONMENTS.md#signing-in).

## Your own shell: ssh openajar-<id>

In local mode an agent can also use its own shell on the VM: `ssh openajar-<id>`, `scp`, `rsync` and
git over ssh, the way it works on the laptop. `environment_open` and `environment()` give the commands in `ssh` (`host`, `command`, `examples`).

- When an environment is ready, openajar makes a key for it and writes a host entry to
  `~/.openajar/ssh/config`: `Host openajar-<id>`, the VM's address, user root, that key with
  `IdentitiesOnly yes`, and the VM's host key pinned (`StrictHostKeyChecking yes` against
  `~/.openajar/ssh/<id>/known_hosts`; openajar made that host key and gave it to the VM, so the first
  connection is already checked). The entry and the key go when the environment closes, fails, or
  is saved and its VM deleted. A resume writes a new one.
- `openajar setup` asks once to put `Include ~/.openajar/ssh/config` at the top of `~/.ssh/config`
  (it makes the file, 0600, if there is none, and leaves the rest as it is). Then plain
  `ssh openajar-<id>` works. `openajar setup ssh` asks again. Without that line the command is
  `ssh -F ~/.openajar/ssh/config openajar-<id>`, and the results say so.
- The key is the environment's own, not the node's. On the VM it may open a terminal and run
  commands, and nothing else: no port forwarding either way, no ssh agent (your own keys never
  reach the VM), no X11. Use `environment_port` to show a dev server. `ssh -L` is ignored without a
  word (the entry has `ClearAllForwardings yes`, so a forward in your own `Host *` is not tried
  either), so the entry carries a comment, and the `ssh` block of `environment_open` and
  `environment()` has `port_forwards`, which says forwards are off and gives the environment_port
  call that makes a local link.
- The VM firewall lets in your laptop's address (below), which is where both openajar and your
  shell connect from, so nothing more is opened. With Docker, SSH is on 127.0.0.1.
- A machine is idle when nothing used it for `idle_minutes`. An ssh session running a command, an
  interactive one typed in lately, or one that ended within that time counts as use, so an agent
  working only over ssh keeps its machine.
- **Not in the call log.** openajar logs every call made through its tools. What runs over the
  agent's own ssh is not in that log: openajar sees that a session ran, not what it ran. This does
  not widen what the agent can reach. The VM holds no key or token of yours: its one credential is
  the git gateway token for its own repository, which works only through that VM's own tunnel and
  ends when the environment closes. The agent could already read it with `terminal_exec`.

A server node (`openajar connect`) gives no `ssh`: its agent runs on another computer and works
through the tools only.

## Setup, one step at a time

`openajar setup` runs every step. Run one again to change one thing:

| Step | What it does |
|---|---|
| `openajar setup vms` | where VMs come from: a Hetzner token (checked at once), a location, a default size and the `dev` snapshot, or Docker and its image, or a machine of your own (`openajar environments machine add NAME user@host` brings one in: [ENVIRONMENTS.md](ENVIRONMENTS.md#1-where-the-vms-come-from)) |
| `openajar setup github` | your `gh` login, `gh auth login`, a pasted token, or none; and whether environments may use any repository the login reaches |
| `openajar setup limits` | the monthly cap (10 EUR by default here), hours per VM, VMs at once |
| `openajar setup agents` | starts the service, connects Claude Code and Codex, adds the skills |
| `openajar setup web` | the web app at `http://127.0.0.1:7410/`: adds the copy built for your version (yes by default), updates one from another version, or removes it |
| `openajar setup ssh` | adds `Include ~/.openajar/ssh/config` at the top of `~/.ssh/config`, so `ssh openajar-<id>` works |
| `openajar setup check` | each piece, in plain words |
| `openajar setup test-vm` | opens a VM, runs `uname -a`, closes it (about 0.01 EUR on Hetzner) |

For scripts: `openajar setup --yes` takes every default and asks nothing, with
`--hetzner-token-file FILE`, `--github-token-file FILE`, `--docker` or `--no-web`. A test VM never
opens without a yes.

## The web app

The web app is optional. Setup offers it, and `openajar web` changes it later:

| Command | What it does |
|---|---|
| `openajar web install` | downloads `openajar-web-<version>.tar.gz` from the GitHub release of the openajar you have, checks it against GitHub's own digest of it on that release (which must be immutable; `OPENAJAR_WEB_SHA256` pins one for a mirror), and unpacks it into `~/.openajar/web/app` (no sudo) |
| `openajar web update` | the same, only when it is out of date, and never after a no or a `remove` |
| `openajar web remove` | takes it away; updates leave it away until `openajar web install` |
| `openajar web status` | what `/` serves now, and whether it matches your openajar |

Running the installer again updates openajar, restarts a running local service so it runs the new
version (`openajar local restart`, same home and port; it says what that interrupted), and then updates the web app to the same version (setup's web step
does it; without setup, the installer runs `openajar web update`, and `OPENAJAR_NO_WEB=1` skips it). A
copy from another version shows as "to do" in `openajar setup check` and as a warning in
`openajar doctor`.

Where `/` comes from, first match wins: `OPENAJAR_PANEL_DIR`, then a checkout's own build
(`web/dist`, [web/README.md](../web/README.md)), then the installed copy. The service looks on every
request, so an install or a remove shows on the next reload. It serves those files as they are, from
inside that folder only: an archive with a link, an absolute path or a `..` in it is refused before
anything is unpacked. The app holds no secret: it is static files that talk to the service's API with your
browser's session.

Without network, or for a release made before the web app was published, setup says so and goes on;
the service, MCP and the live view links work without it.

## By hand, step by step

```sh
openajar local start              # sets up ~/.openajar, starts the service, connects Claude Code and Codex
OPENAJAR_HOME=~/.openajar openajar vm setup        # your Hetzner token (or the docker provider)
openajar local status             # running? which port? which environments are open?
```

Then start a new Claude Code or Codex session and ask: "open an openajar environment and run
uname -a".

`openajar local start`:

1. makes `~/.openajar` (chmod 700) with `node.toml` and a vault key file in `secrets/`;
2. picks port 7410, or the next free one if 7410 is taken, and remembers it in `node.toml` (it
   never takes 7400, which `openajar connect` tunnels use, or 7450, which `openajar dev up` uses);
3. installs and loads the service: a launchd agent (`~/Library/LaunchAgents/dev.openajar.local.plist`)
   on macOS, a systemd user unit (`~/.config/systemd/user/openajar-local.service`) on Linux. It
   runs `openajar local serve`, starts at login, and starts again if it stops. On macOS it is an
   Interactive job: launchd throttles a Background one (efficiency cores, slow disk and network),
   and every git and ssh it starts with it (a 13 MB clone took 212 s instead of 2 s). A node still
   running from an older plist logs a warning, and the next `openajar local start` loads it again;
4. issues the agent grant `openajar connect` gives by default (environments and memory_read: the
   agent may open its own environment and run commands there, and nowhere else), stores its
   token in the macOS Keychain (Linux: a chmod 600 file in `~/.config/openajar/tokens/`), and
   registers `openajar-local` with Claude Code (user scope) and Codex, and copies the skills. No
   token goes into their config: they read it through `~/.config/openajar/bin/openajar-mcp-headers`.

Run it again at any time: it keeps what works.

## Commands

| Command | What it does |
| --- | --- |
| `openajar local start` | Set up, start the service, connect Claude Code and Codex. `--port`, `--no-register`. |
| `openajar local stop` | Stop it. It stays stopped, also after a restart, until `openajar local start`. |
| `openajar local restart` | Start the running service again, so it runs the openajar installed now (same home, same port). The installer does this after an update; open environments stay open and their jobs keep running, but each one's browser closes and opens again on the next browser call. |
| `openajar local status` | Running or not, the port, open environments, what it closed after a sleep. `--json`. |
| `openajar local logs` | The end of the log (`~/.openajar/logs/local.log`). `-f` keeps printing. |
| `openajar local serve` | The service itself, in the foreground. Ctrl-C stops it. When it starts it brings the skills openajar installed up to this openajar, only for a home `start` registered (`--skills`, `--no-skills` to say). |

Every command takes `--home` for another folder than `~/.openajar`. Another home is a second node with
a service of its own (`dev.openajar.local.<8 letters>`, `openajar-local-<8 letters>.service`), so
`start` and `stop` with it never replace or stop the one in `~/.openajar`. Only a home `start` (or
`openajar setup agents`) registered writes into Claude Code's and Codex's folders: `start
--no-register` marks it so (`clients-registered` in the home), and its `serve` leaves the skills as they
are. A test or a builder's node: `openajar local start --home <scratch> --no-register`, or `serve --home
<scratch>` alone.

## GitHub: any repository, or one by one

With a GitHub login saved, setup asks once: "Let environments clone and push any repository this
GitHub login can reach?" Yes (the default here) means an agent can open an environment on any
repository the login reaches, with nothing more to connect. No means you connect repositories one
by one (`openajar environments repo add github.com/<owner>/<name>`), as on a server.

Either way the push rules hold: no force push or delete of a branch the environment did not
create, and no tags. The setting is `~/.openajar/git/login.json` (`any_repo`).

With `gh`, openajar stores the token `gh auth token` gives and does not call `gh` again. When gh
signs in again or the token changes, run `openajar setup github` again.

## Where your tokens are

They stay on your laptop, in `~/.openajar`: the Hetzner token in `secrets/hetzner.token` (only you
can read it), and the GitHub token sealed in the openajar vault (`vault.db`, its key in
`secrets/vault.key`). A VM never holds either. A push from the VM goes through openajar's git gateway
with a token for that one repository, which ends when the environment closes.

## The VM firewall

A VM lets SSH in only from your laptop. Your laptop's public address changes when you move to
another network, and sometimes while you sit still (a hotspot, a VPN, your provider), so openajar
reads it again (IPv4, and IPv6 when you have it) every time an environment opens, after every wake,
when a tunnel to a VM drops, and once a minute while VMs run. When it moved, the firewall lets in the
new address and drops the old one in the same step, and the environments' links come back by
themselves, their browsers too. A move needs two lookup services to agree. If it cannot find the
address (you are offline), the environment does not open and says so. Set `ssh_from` in
`~/.openajar/vms.toml` to fixed addresses if you would rather. docs/THREAT-MODEL.md says how the
lookup is bounded.

## Sleep

When the laptop is about to sleep, openajar acts on each open environment by its mode:

- **Saves when idle** (`keep="save"`, the default): it is saved (the disk snapshot and the files)
  and its VM deleted. Nothing bills while the laptop sleeps but the save. When you are back, say
  "continue <title> in openajar" in any chat, and it comes back.
- **Single use** (`keep="none"`): one with nothing to lose is closed. One with unsaved work gets a
  rescue save (its files and commits), and you are told; an agent that opens the same title gets
  that work back.
- **Kept running** (`keep="live"`): it stays on and keeps billing, as you chose.

A sleep that comes too fast for this (the lid closed on a battery about to die) leaves the VMs
running and billing, and nothing on the laptop can delete them. When it wakes, or when the service
starts again, openajar at once deals with every environment past its time limit the same way, and
`openajar local status` says what it closed or saved. `openajar local stop` leaves open environments
running: nothing closes them until the next start.

## Remove it

```sh
openajar local stop
openajar connect --remove local   # the Claude Code and Codex entries, the token
rm -rf ~/.openajar                # everything else, the vault too
```
