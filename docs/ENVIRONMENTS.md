# Environments: a computer in the cloud for each agent

An environment is a VM that the node rents for an agent. It has a terminal, a real browser whose
traffic leaves from the VM, and your repository in `/work/<name>`. The agent works there, commits
and pushes a branch. By default it stays: when it goes idle the whole machine is saved and the VM
deleted, and it comes back when an agent opens it again with the same title. A single-use one is
deleted when it is done, and one kept running stays on
([Keeping an environment](#keeping-an-environment)). You can watch its browser and its commands
live and take over.

This page is the user guide. The design, the decisions and the exact tool shapes are in
[design/environments.md](design/environments.md).

## Set it up

Everything here runs on the node, as the owner (`sudo openajar ...` on a node installed with
`deploy/install-node.sh`, or with `OPENAJAR_HOME` pointing at a dev node's home).

### Connect a repository

A public repository needs nothing: the VM clones it directly. A private one is connected once, by
you. An agent cannot do it, and never sees the token: `environment_open` with a repository the node
cannot read rents nothing and gives the agent a card instead (`status: "approval_pending"`, `kind:
"repo_connect"`). The agent hands you its `review_url`. In T3 Code it opens beside the chat (the
preview pane), and that is the in-chat way to connect it.

On the card, in Needs you:

- **Paste a token** (the default). It needs no admin rights on the repository, so a collaborator can
  connect one. A classic token needs the `repo` scope; a fine-grained one needs this repository with
  Contents: Read (Read and write, so agents can push). The button opens a small page the node serves
  itself, in a new tab, and you paste the token there: the web app never holds it. The node checks it
  with GitHub, then with `git ls-remote` the way the git gateway fetches, and keeps it in the vault for
  this one repository only. The page says connected, the card moves on by itself, and the agent's
  `approval_wait` returns, so it opens again. When GitHub says when the token expires, the page and
  the card show it. The page's link works once, for 15 minutes; a token that did not work can be
  pasted again on the same page until then.
- **Use a deploy key instead (repo admins)**: the card shows the node's public key and the
  repository's deploy keys page. An admin adds it with Allow write access, then you press "I added
  it, check".

The token goes only to GitHub, to git and into the vault: never into the chat, a tool result, an
error, the card, the web app, an approval, the action log or a log file. Only the owner signed in to
the panel can open the token page (a person present, with a fresh passkey check when you have a
passkey): no agent's token, grant or link can. Never paste a token into the chat. No tool takes one, because anything in a tool
argument is in the transcript, and the node cannot read a chat client's secret references (T3 Code's
`request_secret`) either.

What the token page says when a token does not work:

| It says | What to do |
|---|---|
| wrong, expired or revoked (GitHub's 401) | make a new token and paste it |
| this token cannot see the repository (404) | check you are a collaborator; a fine-grained token needs the right resource owner and this repository in its repository access |
| must be authorized for the organization's single sign-on | open the link it gives, authorize the token, paste it again |
| waits for the organization's approval | an organization admin approves it (Personal access tokens, Pending requests) |
| the organization does not allow this kind of token | make the kind it allows (often fine-grained), or ask an admin |
| GitHub accepts it, but git cannot read the code | give it Contents: Read (classic: the `repo` scope) |

At the node, the same in one command, and the card closes on its own within a minute:

```
openajar environments repo add github.com/me/shop --token   # a token, read from a prompt (no admin rights needed)
openajar environments repo add github.com/me/shop           # a deploy key: it prints the key for a repo admin to add
openajar environments repo list                             # what is connected, with which key or token
openajar environments repo remove github.com/me/shop        # disconnect it, and delete the key made for it
```

`repo add` is the two steps of section 3 in one: `openajar vault git key add --host github.com --repo
me/shop` (or `--token`) and `openajar vault git remote allow env github.com/me/shop`. Add the deploy
key it prints with write access, so agents can push. When a key or token stops working later (the
deploy key was removed, the token expired), the agent's next open gives the same card, to give a new
one (at the node: `repo add ... --token --replace`).

### 1. Where the VMs come from

**Docker** (no cloud account): each environment is an Ubuntu container with SSH on the node's own
Docker. Good for trying it, for CI and for a node at home. Put this in `$OPENAJAR_HOME/vms.toml`:

```toml
provider = "docker"
```

Then get its image once: `openajar environments setup --pull` downloads the prebuilt image for your
version of openajar (`ghcr.io/sabalys-ramunas/agentos-env:<version>`, a minute or two), and builds it
here when it cannot (10 to 15 minutes). `--build` always builds it here. `openajar setup --docker`
does the same for you. Get it again after an upgrade when setup says the image is older than openajar.
To never download it, set `docker_pull = ""` in `vms.toml`.

What a docker machine is kept from, and what it is not:

- Each machine has a docker network of its own, so machines never reach each other, and limits
  from `vms.toml`: `docker_cpus` (default 2), `docker_memory_gb` (4, no swap on top) and
  `docker_pids` (512 processes and threads). An agent that asks for a size (below) gets that size's
  CPUs and memory instead, never more than this computer has.
- It has only the capabilities a dev box and sshd need. A firewall inside it, set before sshd
  starts and out of reach after (the machine has no `NET_ADMIN`, root included), refuses new
  connections to private, shared and link-local addresses: the machine reaches the internet but
  not this computer and not your network. That matters most on Docker Desktop for Mac, which
  forwards `host.docker.internal` to the Mac's own loopback, where the node's git gateway and the
  other environments' browser tunnels listen. On Linux, the docker gateway is the computer itself.
  DNS (port 53) is the one exception.
- It still shares a kernel: with this computer on Linux, with Docker Desktop's Linux VM on a Mac
  (and that VM can read the folders you share with Docker, your home folder by default). A machine
  that breaks out of its container is on this computer. Use the docker provider for your own code;
  the cloud provider is the isolated one.
- The browser's tunnel to a machine is a port on this computer's loopback. On Linux the node
  serves it to its own user only. macOS cannot tell which account opened a local connection, so
  another account on the same Mac could use it.

`openajar environments setup` says the same.

**Hetzner Cloud**: make a project that holds nothing but these VMs (the token can delete any
server in it), make a read-write API token in it, and give it to the node:

```
openajar vm setup              # asks for the token, keeps it in $OPENAJAR_HOME/secrets/hetzner.token (0600)
openajar vm types              # what can be ordered today, and what each profile rents
```

The node rents the cheapest server type that fits, unless you or the agent set a size. The
location, your own profiles and the node's VM limits are in `vms.toml`
([design/vms.md](design/vms.md#settings-openajar_homevmstoml)).

**Morph Cloud**: machines that start from a snapshot in about a second, and that a save pauses with
their memory, so an environment comes back in seconds with its dev server still running. Make an
API key at cloud.morph.so and give it to the node:

```
openajar vm setup --provider morph    # asks for the key, keeps it in $OPENAJAR_HOME/secrets/morph.token (0600)
openajar vm image build dev           # the dev snapshot environments start from (a few minutes, once)
```

Setup also pins the host key of Morph's SSH gateway (it prints its fingerprint) and sets `provider =
"morph"` in `vms.toml`. What is different, said plainly:

- Prices are Morph's MCUs in USD, turned into EUR at `morph_eur_per_usd`: estimates, marked as such
  (about EUR 0.09 an hour for a small one). Set the rates in `vms.toml` to your plan.
- A machine reaches the node over Morph's SSH gateway, with its own key; there is no provider
  firewall, and no outbound rule can be set on Morph.
- There is no cloud-init: the node runs the environment's setup over SSH (the `dev` snapshot has it
  done already).
- Every machine has a time limit at Morph too (its own limit plus a margin), so one the node forgot
  stops billing by itself.

An agent asks for a size by name, and the open's `machine` says what it got and what each bigger
size would rent and cost (`machine.bigger`):

| `size` | At least | On Hetzner | On docker | On Morph |
|---|---|---|---|---|
| left out | the node's default | the cheapest type that fits (`profile` in `environments.toml`, else vms.toml's default) | `docker_cpus`, `docker_memory_gb` | `morph-small` |
| `small` | 2 CPU, 4 GB | the cheapest type in the location with at least that | 2 CPU, 4 GB | `morph-small`: 2 CPU, 4 GB, 32 GB disk |
| `medium` | 4 CPU, 8 GB | the same | 4 CPU, 8 GB | `morph-medium`: 4, 8, 64 |
| `large` | 8 CPU, 16 GB | the same | 8 CPU, 16 GB | `morph-large`: 8, 16, 128 |
| `xlarge` | 16 CPU, 32 GB | the same | 16 CPU, 32 GB, at most this computer's | `morph-xlarge`: 16, 32, 256 |

On Morph a machine of the `dev` snapshot's size (small, unless you built it bigger) starts warm in
about a second; another size boots cold from it, in seconds more.

It may also name a server type (`cpx42`) or one of your `vms.toml` profiles. The biggest an agent
may ask for is `max_size` (default `xlarge`; `openajar environments allow --max-size large`); past
it the open is refused with `environment_limit`.

### 2. The image

Without one, each VM starts from Ubuntu 24.04 and installs git, curl, build tools, Node 22 and
Python with uv on first boot, which takes a few minutes. A snapshot boots ready in under a minute:

```
openajar vm image build dev    # Hetzner: a snapshot with git, build tools, Python with uv, Node 22, Docker
```

The node uses the `dev` snapshot when it has one. The build applies the security updates of that day
(machines never update themselves mid-session, see [What is on the VM](#what-is-on-the-vm-and-what-is-not)),
so rebuild it now and then. The build runs the environment's own setup script
too, so a VM from the snapshot has the ready marker and pnpm, and its setup check is one SSH call. A
snapshot built before that (no marker) still works: each open waits for cloud-init in one call and
runs the setup, which skips apt when every package is there and installs pnpm.
Rebuild it to drop that. It also holds the browser for `browser = "vm"`
(below), as does the docker image: Xvfb, x11vnc, fonts and Camoufox at the node's own versions, about
3.3 GB more (Camoufox's build is 2.4 GB, most of it the fonts of the systems it can look like) and
about two minutes more to build. And the desktop (below): openbox, a terminal, a file manager, fonts
and TigerVNC, about 90 MB more.

### Where the browser runs

`browser` in `$OPENAJAR_HOME/environments.toml` says where each new environment's browser runs:

```toml
browser = "node"   # the default: on the node, with its traffic through the VM
browser = "vm"     # on the VM itself: for a node with no browser of its own, such as openajar on a Mac
```

With `"vm"` the browser, its screen and its live view run on the VM, bound to the VM's loopback only.
The node starts them over the SSH connection it already holds and drives the browser through that
connection. Every browser tool works the same way, `browser_screenshot(proof=..., width=...)`
included (proofs stay on the node), and so do the live view and take over. On the VM, `localhost` is
the VM, so no proxy is needed. Each time it opens it is a new browser process, but the same device:
its first launch draws a fingerprint (user agent, screen, GPU, fonts and the rest), the node
reads it back from the running browser and keeps it with the environment's data
(`environments/<id>/fingerprint.json` in the node's home, never on the VM), and every later launch
gets exactly that one. A save carries it, so a resumed environment, in any chat and on a new machine,
is the same device; a new environment draws its own. What a pin never holds follows each launch: the
timezone and WebRTC address come from the browser proxy's exit (with no proxy, from the machine's own
place, below), the languages (and the speech
voices drawn for them) from the launch's locale, and the screen and window from the launch when it
asks for another size (full screen or the desktop's window). When the VM's
Camoufox is a newer Firefox than the pin says, only the pin's Firefox version moves with it (the old
pin is kept beside it), as a real Firefox updates itself. A VM booted from an image without the
browser installs it while the environment starts (a few minutes), and so does one whose browser is at
other versions than `browser-requirements.txt` pins (an older image). `ui_check` still runs
studio's browser on the node, through the tunnel. An environment keeps the setting it opened with.

Either way the browser starts while the environment comes up, beside the clone, and keeps starting
after the open returns: the first `browser_navigate` finds it running (or waits for the rest of its
start) instead of starting it then. A start that fails is only logged, and the first browser call
starts it again.

#### The desktop

With `"vm"` the VM's screen is a desktop, not a bare browser: a wallpaper (a sunrise over foggy ridges), a
top bar, the browser as a window with its own title bar, and a dock at the bottom with Browser, Terminal
and Files, plus every other app while it has a window. The person watching the live view sees what the
agent does, and after take over uses it as a computer. Its look is the openajar design ("Dawn"): light
window frames, glass icons, and one violet for the agent.

**The screen belongs to the environment, not to the browser.** It starts in the background as soon as the
environment is ready (`environment_open` does not wait for it) and stays until the environment closes. The
live view (`links.browser`) shows the desktop from then on: before the agent first uses the browser, and
after the browser lane goes idle and closes (the node's `idle_seconds`). The browser window comes when the
agent first uses the browser and goes when its lane closes; the wallpaper, the dock, the agent's terminal and
every window a person opened stay.

- **The top bar** shows the openajar mark and the environment's title on the left, what the agent is doing
  in the middle ("Claude Code is using the browser", "is running a command", "is reading files", "is
  editing files", "has control" when it is between steps, "is stopped", or "You have control"), and the
  date and the time on the right, at the openajar computer's own time. The node tells it who has control
  and which tool the agent calls; the agent's name is the one the live view uses. A long title or status is
  cut with an ellipsis (the agent's name first, so the verb stays). It is drawn again only when its words
  change and once a minute, and it reserves its height, so a maximized window sits between it and the dock.

- **The browser** is a window with an even margin of wallpaper around it. It draws its own title bar, as a
  browser does on a desktop: its tabs, with minimize and maximize beside them. Drag the tab strip to move
  it, double-click it or press maximize to fill the screen (a maximized window stops above the dock), drag
  its edge to resize it. It has no close button, and Alt+F4 minimizes it (the dock's Browser brings it back),
  so a click never ends the agent's browser, its pages or its sign-ins. Ctrl+Q does nothing.
- **The dock's Browser when no browser runs** opens it: the dock asks the node, which opens the
  environment's browser lane as whoever has control (the person who took over, else the agent), the same
  as the agent's first browser call would. "Opening the browser" shows over the dock until the window comes.
  The ask comes from the machine, which is the agent's, so it never passes a stop or a freeze: nothing
  opens while the agent is stopped there or the lane is frozen. The node acts on nothing else the machine
  sends, and at most once every 10 seconds.
- **The agent's terminal**: the window titled with the agent's name ("Claude Code · commands") starts with
  "Read only. The commands Claude Code runs show here." and shows each `terminal_exec` as the node reads it:
  the command, its output (stderr in red), then its exit code and time. A job
  (`terminal_job_start`) shows one line with its id. It only shows: a command's result, timing, timeout and
  cancellation are the same with it or without it, and a command shows at most 256 KB of its output
  there (the rest is counted, and the agent still gets all of it).
- **Terminal and Files** in the dock are the person's own: a shell in `/work` and a file manager on it.
- **The window the agent uses comes to the front**: the browser when it drives the browser, its
  terminal when it runs a command. Only when that changes, and never while a person has taken over
  the browser: windows do not move under a person's hands.
- **A GUI program** the agent starts (`terminal_exec` or a job) opens on this screen: commands get
  `DISPLAY` and the screen's cookie while the desktop runs and the agent has the screen (a command started
  while a person has control gets neither; a program started before keeps what it had), and its window gets a place in the dock, its own icon on the same
  glass tile as the others. GTK programs, those and the ones a person starts, use the desktop's light theme
  (`GTK_THEME=Adwaita`); terminals keep a dark body. The browser keeps its own look, which is part of what a
  page sees.
- **The agent's pointer**: the live view draws a large violet pointer with the agent's name where the agent
  acts in the browser (where it moves, clicks, scrolls and types), since the node drives the page without
  moving the screen's own pointer. The node knows the point in the page it aims at; the desktop's helper
  tells the node where the browser window is on the screen (read from X, never from what the browser tells
  pages), and the node sends the live view only the point on the screen. It glides between points, fades
  after a few quiet seconds, and hides while a person has control and while the agent works in its
  terminal. The screen tools (`screen_act`) move the screen's real pointer and tell the same labelled
  pointer where they act, in screen pixels (typing and keys where the pointer is), so a person sees the
  agent at work in a desktop app too. It adds no time to a tool call. The web app's environment page draws the same pointer over its
  picture: the node sends it there as a `pointer` event on the panel's event stream, only while that page
  shows the live view, never kept for a replay, and only to those who may watch that screen (the owner, and
  members whose role watches). The machine's numbers are checked (whole numbers inside the
  screen) before they place anything.

**This is the agent's computer.** While a person has control, no openajar tool shows the agent the screen
or types on it, and its commands are not handed the screen. That is a rule of the tools, not a wall: the
agent's commands run as root on that machine, and root can read the screen's cookie and socket, the
browser's profile and every file, so an agent that set out to could watch the screen or read what a
person types there. Sign in on that screen only to accounts the agent is meant to use. The take over
keeps the agent's own tools out of the person's way; it does not keep a hostile agent out of its own
machine. [THREAT-MODEL.md](THREAT-MODEL.md) ("An environment's desktop") says the same.

`desktop = false` in `environments.toml` turns it off: the browser fills the screen as before.

```toml
browser = "vm"
desktop = true     # the default
```

The wallpaper is ours (`src/openajar/environments/desktop/wallpaper.svg`, and the same picture made ahead at
1920x1080, `wallpaper-1920x1080.jpg`, so a new machine does not spend a second drawing it). Put your own at
`$OPENAJAR_HOME/desktop/wallpaper.svg`, `.png` or `.jpg` (at most 8 MB): an SVG is drawn at the screen's
size on the machine, a picture is fitted to it. It is read each time a screen starts. The wallpaper, the
icons and the openajar mark are original work for openajar.

The screen is TigerVNC's Xvnc, one process that is the X server and the VNC server, on a Unix socket only
(no TCP listener), behind the same folder only root can open and the same cookie as before. A machine
without it (an image from before the desktop) uses Xvfb and x11vnc as before. What it costs, measured on
the docker provider (2 CPU): the desktop starts in 0.3 to 0.6 s; idle it uses no CPU (over a minute:
openbox 0.0 %, the helper 0.03 % for its clock, Xvnc 0.0 % alone and 0.02 % with a viewer connected);
openbox and the helper take about 60 MB; Terminal and Files about 30 MB each while open. While a page
scrolls, Xvnc sends about 29 updates a second to the live view where Xvfb with x11vnc sent 16, for the
same CPU; the top bar and the dock change nothing there (a client on the machine's own socket counted 52
to 53 updates a second with the helper running and with it stopped). The x11vnc fallback runs with `-defer 5 -wait 5
-nowf` (updates gathered and polled every 5 ms instead of 20, and a moved window shown whole).

What a page reads with the desktop: the screen is 1920x1080 as before; the window is 1776x890, and as the
browser draws its own title bar, `outerWidth`/`outerHeight` are 10 px larger than the window (the edge GTK
draws round it with no compositor) and the page area is about 35 px taller than under a separate title bar.
These are a real window's sizes, inside the screen. `screenX`/`screenY` and `screen.availHeight` are
Camoufox's own pick at each launch, as before. Nothing a page reads is pinned by these numbers: the
fingerprint pins the screen and the window size the browser is asked for.

Where it comes from: `desktop.sh`, beside `browser.sh`, in the docker image (its label
`org.agentos.desktop`), in the `dev` snapshot, and on a machine without it (or with an older version)
when its environment opens, in the background (about a minute). A screen that starts before that is
done waits up to a minute for it; if the desktop cannot be installed or started, the screen shows the
browser alone and the node's log says why. A browser that opened on the bare screen while the desktop
still installed keeps running: the desktop starts under it as soon as the install is done (the screen is
not started again), and frames it maximized; the next time the browser opens it is a window as usual.

**A screen that dies comes back.** The node asks each environment's screen once a pass (its regular check
of environments, every `reap_interval_s`, a minute by default). If Xvnc died, the screen starts again;
if only the desktop's parts died (openbox, the helper, or x11vnc on a machine without Xvnc), they start
again on the running screen and every window on it stays. The live view retries on its own and shows the
screen again without a reload. A browser that still runs is never under a screen that is started afresh:
under it only what is missing starts. Nothing is checked while the screen is being started, the browser
is opening or the desktop installs, and the check uses the screen's own lock, so it never races them. The check,
like every command the node runs on the machine for the screen, is no use of the machine: it never
keeps an environment from closing or saving for idleness.

#### The agent's screen tools

The browser tools read a page by its elements. For everything on the desktop that is not a web page (a
calculator, an editor, a GUI the agent built or installed), the agent loads `tools_more("screen")`:

- `screen_snapshot(on, window=None, image=true, proof=None)`: a PNG of the screen (or of one window's
  area of it), the open windows (id, title, app, kind, place and size, which is in front, which are
  minimized) and the pointer. The picture is at most 1280 px on its longest side, so a 1920x1080 screen
  comes as 1280x720 with `scale` 1.5: a point in the picture times the scale is the screen pixel the
  actions take. One window comes at its real size when it fits. The picture is in full colour when it fits
  in 640 KB, else in 256 colours (the wallpaper's gradients are what make it large; an app's text and flat
  colours survive), else smaller: MCP clients refuse a message over 1 MB, and base64 makes a PNG a third
  larger. `image=false` gives only the windows and the pointer. `proof="..."` keeps the picture as proof, as
  `browser_screenshot(proof=...)` does.
- `screen_act(on, action, ...)`: click, double_click, right_click, move, drag (to_x, to_y), scroll
  (direction, amount), type (text), key ("ctrl+s", "alt+F4", "ctrl+a Delete") and focus (a window by
  its id, also from minimized). It answers the window in front after it, the windows that opened or
  closed, and the pointer; type and key also say which window had the keyboard (`keys_to`), and with
  `window` bring that window to the front first.
- `screen_open(on, app=None, file=None)`: starts one of the desktop's apps (a `.desktop` entry by its
  name or id, or a program on the PATH), detached and on the screen, waits up to 8 s for its window and
  gives it the keyboard; without `app` it lists them. The environment's browser starts in the background
  when the environment opens, and its window may come up after the app: it takes no focus by itself
  (openbox's rule for it, and the desk helper puts the app back in front during the browser's start), so
  the app keeps the keyboard until the agent uses the browser, a person clicks it, or the dock's Browser
  is pressed. The agent installs an app first with `terminal_exec` (`apt-get install -y
  galculator`). A GUI started with `terminal_exec` works too, but its window ties the command's output pipes
  unless it is sent to the background with its output redirected; `screen_open` needs neither.

Input is real X input through XTEST, from `desktop/screen_ctl.py` on the machine (python3-xlib, which
desktop.sh already installs: no new package), paced as the browser's hands are (`openajar.node.human`):
a look of 200 to 600 ms before the hand moves, a glide that bows a little to one side, slow at both ends
and now and then a little past a far target (about 250 ms for a short reach, 700 ms across the screen), a
pause before the press, a press held 50 to 230 ms; keys held about 85 ms with about 150 ms between key
downs, Shift held round capitals, the odd letter pressed before the last is up. Text types at about 5
characters a second, at most 600 characters a call. A character the keyboard has no key for (ą, €, an
emoji) gets a spare key mapped for it while it is typed. Every key and button an action pressed is let go
when it ends, also when it fails or runs out of time.

The person comes first: while a person has control of the environment (its browser lane), every screen
tool is refused with `human_in_control` (or `agent_paused`), `screen_snapshot` included, as every
browser tool is then (a person may be typing a password during a hand-over), and the agent waits with
`browser_wait_handback`. An action already running when a person takes over (or stops the agent) is stopped
on the machine: the node hears the change, ends the action's command, and the script on the machine
stops at its next pause between keys or steps, lets go of every key and button, and says how many
characters it typed. A person gets the keyboard and the pointer back within about a second (the node's
signal over the environment's SSH connection plus at most a few hundred milliseconds), and the agent's
error says how much was done. Long text also goes in parts of 40 characters with control read before
each. Each call is in the audit trail (what is typed included, as `browser_act`'s) and
in `environment()`'s last calls (without the text). The top bar says "is using the desktop" meanwhile.

Measured on the docker provider (2 CPU) with the default wallpaper: a snapshot takes 0.2 to 0.25 s on
the machine and 0.45 s for the whole MCP call (the full colour picture is about 860 KB, so it goes in
256 colours, about 100 KB); `image=false` takes 0.26 s; a click, glide and press included, about 1.4 to
1.8 s; 80 characters of text about 13 to 15 s. Each call is one command over the environment's SSH connection:
nothing new runs or listens on the machine between calls.

What the agent cannot do with them yet: see the pointer drawn in the picture (its place is in
`pointer`), read a window's text other than by the picture (no accessibility tree), or hold a key or a
button across calls. A video proof (`browser_record`) records the whole screen, so with the desktop on it
shows the desktop's windows too, not only the browser's page.

A VM with the browser and one page open uses about 1 GB of memory (measured on the docker provider).
The smallest size (`small`, 2 CPU and 4 GB, a Hetzner cx23) leaves room for a typical dev server; a
heavy build next to a big dev server wants `size="medium"`.

### 3. Git: keys and declared repositories

The VM never holds a git key or your token. Clone, fetch and push go from the VM through an SSH
tunnel to the node's git gateway, which uses the credential in the vault, only for the repositories
you declared for the `env` workspace ([GIT.md](GIT.md)). The VM holds one gateway token, in its git
config: it fetches and pushes the repository the environment was opened with and nothing else (not
the other declared ones), works only through that VM's own tunnel, and is revoked when the
environment closes. An environment with no declared repository gets no token at all.

```
openajar vault git key add --token --host github.com --repo 'me/*'   # a fine-grained token, read from a prompt
openajar vault git remote allow env 'github.com/me/*'                # environments may clone and push these
```

Or a deploy key per repository: `openajar vault git key add --host github.com --repo me/shop` prints the
public key to add on GitHub, with write access if agents should push. A push from an environment
never asks you, the default branch included. Refused instead (`push_refused`: the agent pushes a
new branch and opens a pull request): a force push or a delete of the default branch (or of a
branch you protect with `--protect`), a force push or a delete of any branch the environment did
not create itself (a colleague's branch that was there before), and moving or deleting a tag. Its
own branches (the ones it pushed first) it may rewrite and delete. A push from anywhere else, an account
browser or a brain, still asks you for the default branch (`--allow-default-branch` on `remote allow`
turns that off for a repository). A public repository that is not declared is cloned directly on
the VM, read only.

How the clone travels. A private repository comes through the node's mirror, so its pack crosses the
upload of the computer openajar runs on: the VM clones it blobless (every commit and tree, and only the
files the checkout needs; git fetches any other file through the gateway when a command needs it). A
241 MiB repository is about 57 MiB that way. A declared repository the VM can read with no credential
(a public one) is cloned and fetched straight from GitHub, at the cloud's own speed, and only its
pushes go through the gateway, with the rules above. Either way the node starts fetching the mirror
when the open starts, so a cold mirror fills while the VM boots.

### 4. The limits

Nothing in an environment asks you (the owner's decision of 2026-11-13): not opening one, not its
commands or its browser, not a push, also after the agent read a web page. Opening one costs
money, so the node's limits bound it instead. They hold from the start, without any command:

| Limit | Default | Changed with |
|---|---|---|
| open at once | 3 | `openajar environments allow --max-live N` |
| the longest life, `extend` included | 8 hours | `openajar environments allow --max-hours H` |
| booked in a month | EUR 50 | `openajar environments allow --monthly-eur E` |
| the biggest machine (`size`) | `xlarge` (16 CPU, 32 GB) | `openajar environments allow --max-size large` |
| machines kept warm for the next open | 0 | `openajar environments allow --warm N` |

```
openajar environments allow --max-live 2 --max-hours 4 --monthly-eur 20
```

Past a limit the open (or an extend) is refused with `environment_limit`, whose message names the
limit and the command that raises it: the agent closes one, asks for fewer hours, or tells you.
What the agent read never changes this. The budget counts what open environments have booked: each
one to its time limit, not only what it has cost so far. The node's own limits in `vms.toml`
(`max_live`, `max_ttl_minutes`, `monthly_budget_eur`) hold too, and the tighter one wins.
`allow` grants nothing; it only writes the limits to `environments.toml`.

How environments stay is set in the same file: `default_keep` (`save`, the default, or `none`),
`allow_keep_live` (off until you turn it on) and `max_live_kept`. `openajar setup` asks the first
one, and turns the second on only when you choose it. See
[Keeping an environment](#keeping-an-environment).

### Network

What a machine may reach is `network` in `environments.toml`, the node's default for every new
environment:

| `network` | The machine reaches |
|---|---|
| `full` (the default) | the internet, as before (not this computer or your network: see step 1) |
| `limited` | package registries and code hosts (the list below), the repository's own git host, your `allow_hosts`, the hosts of [bound secrets](#secrets-for-the-app), and the git gateway |
| `none` | the git gateway and the hosts of bound secrets, nothing else |

```
openajar environments allow --network limited                # the node's default
openajar environments allow --host api.example.com           # one more host for limited (repeat --host)
openajar environments allow --host .example.com              # a domain and every host under it
```

`allow_hosts` takes a host (`api.example.com`), a domain with its hosts (`.example.com` or
`*.example.com`), and a port when it is not 80 or 443 (`api.example.com:8443`). An agent may pass
`network` to `environment_open` only to narrow it (full, then limited, then none). Asking for a wider
one is refused with `network_not_allowed` and the command that widens it: nothing an agent reads, and
nothing in a repository, widens it. A machine keeps the network it opened with.

`limited` reaches, on ports 80 and 443 (`openajar.environments.network.PRESET` is the exact list):
npm, yarn, nodejs.org and JSR; PyPI and its files; crates.io and rustup; the Go module proxy and its
checksum database; RubyGems, Packagist, NuGet; Maven Central and Gradle; Ubuntu's archive (and its
country mirrors), security and ports hosts, and Hetzner's mirror; Docker Hub and ghcr.io; GitHub's
code, API, release and raw hosts; Playwright's browser downloads. It is narrower than the "common
dependencies" lists of other agents: no google.com, no microsoft.com, no cloud consoles.

How it holds. The agent is root on its machine, so the network is closed outside it:

- On Hetzner, a closed machine gets the node's second firewall (`openajar-<node>-<home>-closed`)
  instead of its usual one: SSH and ping in from the node as before, and one outbound rule (ping to
  the node), which makes Hetzner drop every other packet the machine sends. The firewall is stateful,
  so the node's SSH connection keeps working. It needs the `dev` image: a machine closed from its
  first boot cannot install its tools (`network_not_allowed` says so). Two kinds of traffic Hetzner
  never filters, whatever the firewall says: its metadata service, answered on the host (it holds
  this machine's own first-boot config, nothing the machine does not already have), and DNS to
  Hetzner's own resolvers. That second one is a known limit: root on a closed Hetzner machine can
  still send DNS queries, a slow way to leak data out by name lookups. Use docker where that matters.
- On docker, the entrypoint refuses every new connection out but loopback, DNS included, before
  sshd starts, with no NET_ADMIN left after. Docker's own resolver (127.0.0.11) is refused too, and
  the machine is started with `--dns 0.0.0.0` so that resolver has nowhere to forward: no name
  resolves on the machine. The docker provider copies its own entrypoint in for this, so an image
  pulled before this works too.
- Another provider that cannot close a machine refuses `limited` and `none` (`network_not_allowed`)
  before it rents anything.

The machine's way out is the node's proxy, reached through the machine's SSH connection as
`127.0.0.1:3130` there (like the git gateway on 3129). The node resolves every name itself and
refuses private and local addresses. On the machine, every process gets `HTTP_PROXY`, `HTTPS_PROXY`
(and the lower case ones), `NO_PROXY=localhost,127.0.0.1,::1` and `NODE_USE_ENV_PROXY=1` (so Node's own
`fetch` takes the proxy too, Node 22.21 and later) from `/etc/environment`, apt has a proxy file, git and
the package managers follow the variables, and Docker's daemon gets a systemd drop-in (restarted only
when it runs). A program that ignores the proxy variables reaches nothing.

What it refuses, it says and keeps. A request to a host that is not on the list gets a 403 whose body
says `<host> is not on this environment's network list. The owner can allow it: openajar environments
allow --host <host>`, and a refused CONNECT says it in its status line too (Python and Node's `https`
print it; curl shows only "response 403"). The result of the `terminal_exec` or `terminal_job_read` whose command
was refused names it: `network: {blocked_now: [hosts], fix}`. Nobody is asked. `environment()` and the
web app's environment page list each
refused host with a count and when it was last refused (`network.blocked`), and
`environment_pull_request` adds a "Blocked during this work" section to the pull request when there
were any.

The browser. With `browser = "node"`, a closed environment's browser uses a proxy on the node that
splits: `localhost` and the machine's loopback go through the machine's tunnel (your dev server, as
before), and every other host goes out from the node through the same list. Sites then see the
node's address, not the machine's. With `browser = "vm"`, the machine's browser uses the proxy on
`127.0.0.1:3130` there, and `localhost` stays the machine's own.

### The browser's proxy

A browser that goes out from a cloud machine's address is stopped by many sites. Give the
environments' browsers a static residential proxy (an HTTP proxy) instead:

```
openajar proxy add lt1 host:port:user:pass     # with openajar local it joins the environments' list
openajar proxy test lt1                        # its exit IP, country and timezone
openajar proxy list                            # "environments" marks the ones environments use
openajar proxy remove lt1
```

With openajar local, `proxy add` puts the label in `browser_proxies` in `environments.toml`. On a server
node, where proxies also serve account lanes, it does that only with `--environments`
(`--no-environments` takes a label out). The command writes `environments.toml` again, as
`openajar environments allow` does, so comments in that file are not kept. It applies to environments
opened from now on:

```toml
browser_proxies = ["lt1", "lt2", "lt3"]   # the pool; empty (the default): no proxy
```

Which one. A new environment takes the proxy its title (else its repository) had the last time, when no
open environment uses that one; otherwise the proxy the fewest open environments use (ties: the list's
order). It keeps it while it is open: closing and opening its browser, and a restart of the node, keep
the label and so the address. A save keeps the label and the exit IP the browser had, so a resume (in
any chat, a paused machine's too) goes out from the same address the sites its sign-ins know have
seen. When that label is no longer in the list or in `proxies.json`, the resume picks again and its
`missing` says so; when the label is there but its proxy now exits from another IP (`openajar proxy add
<label> ... --yes` pointed it at another host), `missing` says that too. The label is the identity:
`proxy add --yes` and `proxy remove` name the open environments and the saves that use it, since they
then reach their sites from a new address. An agent never chooses a proxy (there is no `environment_open`
argument for it) and sees only `network.browser_proxy` in `environment()`: the label, the exit IP and
its country, never the proxy's host, port or credentials.

Only the browser uses it. The credentials stay on the node: for an environment with a proxy, the node
runs a forwarder of the browser's own that chains to your proxy, and the machine's SSH connection
carries it to `127.0.0.1:3132` there. The browser gets only Firefox settings that point at it, with
`localhost` kept direct (your dev server), WebRTC only through the proxy, HTTP/3 and DNS prefetch off.
apt, npm, git and the shell keep their own way out (direct on a `full` network, `3130` on a closed one).
On a `full` network the forwarder lets the browser reach any public host on any port, never a private
or local address; on a closed one it keeps to the network's list. Plain `http://` pages go through a
tunnel of the proxy too (a CONNECT to the site's port 80), so your credentials are never in a request
whose address and headers a page chose. A proxy that refuses a tunnel to port 80 cannot load plain
`http://` pages (the browser shows a proxy error); `https://` is not affected. One exception on a closed network: a host of a
[bound secret](#secrets-for-the-app) is answered by the node's secret swap, so the browser's requests to
it leave from the node's own address, not from the proxy.

It fails closed. Before each launch the node asks ipinfo.io through the proxy. When the proxy cannot
be reached or refuses its credentials, the browser does not start, and the error names the label and
says to run `openajar proxy test <label>`. Once it runs, Firefox never falls back from its proxy and
the forwarder answers 502 when your proxy refuses (without your proxy's address in the answer): the
browser never goes out from the machine's own address. After `openajar proxy remove`, an environment
that is open keeps its proxy until it closes or the node restarts; after a restart its browser does not
start until the environment is closed and opened again.

Where it is. The same check gives the exit's country, timezone and location, and on every launch the
browser claims what a browser at that address would, nothing of the datacenter's:

- Its clock: the exit's timezone in `Intl` and `Date`, with that zone's offset and summer time.
- Its position: none, as an untouched Firefox at that address. The Permissions API says `prompt`, and a
  page that asks for the position waits, as for a person who has not answered the prompt. Camoufox's own
  way, the exit's city given without a prompt and `granted`, reads as a bot to a site never visited before
  (the red team's `engine.geo_permission`), and with the position given it answers at once even when the
  permission says `prompt` (measured), so the browser gets no position at all. `openajar proxy test`
  still names the place the exit is near.
- WebRTC: the exit's IP; the machine's own addresses are hidden behind mDNS names, and ICE goes only
  through the proxy.
- Its languages: `navigator.languages`, the `Accept-Language` header (with Firefox's own q values),
  `Intl`'s default locale (how numbers and dates are written) and the browser's own language (a form's
  messages), the same in pages, frames and workers.
- Its keyboard: the machine's screen gets the layout of that language (Lithuanian, German, Polish...), and a
  letter the US layout lacks (ą, ß, é) is typed on its key through the X server, as a person there types it,
  not inserted with no key press. English keeps the US layout; a language with no layout here, or a
  machine that cannot (no libXtst), types those letters as text, as before.

The language follows `browser_language` in `environments.toml`:

```toml
browser_language = "exit"      # the default: the exit country's language, then English
# browser_language = "en-US"   # a fixed language, wherever the exit is ("de-DE, de, en" for several)
```

With `"exit"`, a Lithuanian exit gives `lt-LT, lt, en-US, en` (Intl writes `1 234 567,891`), a Japanese one
`ja-JP, ja, en-US, en`, a Brazilian one `pt-BR, pt, en-US, en`: the language first, then English, as
Firefox's own builds for those languages and most browsers set up there look. The language is the
official one most people in the country speak (CLDR's tables, the ones Camoufox's GeoIP draws from, but
always the main one: a draw by speaker share gives a Lithuanian address Russian about one time in three). Where
English is an official language a good part of the people use (India, Nigeria, the Philippines), it is
English (`en-IN, en`). Canada, Belgium and Switzerland follow the region: Quebec, Wallonia and Brussels,
and Romandy are French, Ticino Italian. A fixed `"en-US"` suits an agent that should read sites in
English: an English browser at a Lithuanian address is common and no reason to block by itself, but it
does not match the place, and sites answer in the language the browser asks for. `openajar proxy test` shows what the browser claims through each proxy:

```
lt1  exit 203.0.113.7  Vilnius, LT  Europe/Vilnius
     browser: Europe/Vilnius, lt-LT, lt, en-US, en (exit near 54.69,25.28)
     second source: geojs.io agrees (Vilnius, LT)
```

The last line is a second geolocation source's view of the same exit. Sites place an address with one
database or another, so when the two put it in different countries the line says so plainly: some sites then
treat the address as the other country's (one test proxy was in Lithuania for ipinfo.io and in Hong Kong for
other databases, and TikTok refused it as Hong Kong's). It also says when the second request left from
another address (a proxy that rotates).

When ipinfo.io's answer leaves the country or the timezone out (or names a zone that does not exist),
the node asks a second service (geojs.io) through the same proxy, and keeps its answer for that exit IP
for some hours. When neither gives it, the timezone is the country's (where it has several, the zone
nearest to where the exit is). With no country at all, the clock is left to the browser, and the
language stays the lane's.

With openajar local the browser claims Linux, what the machine is. Its screen's work area is the whole
screen, as Firefox itself reports it on Linux whatever panels a desktop has (an untouched Firefox on the
machine's desktop, with its top bar and dock, reads the same).

Cloudflare's check. Cloudflare's "Just a moment..." page and its Turnstile widget often let the browser
through by themselves after some seconds, with no click. So `browser_navigate`, and a `browser_act` that
lands on such a page, wait up to 20 seconds for that, as a person waits, before they report the page's
signal. A widget that has passed (its token is in its form field, and it shows "Success!") is not a
challenge, so it no longer stops the browser. Whether the check passes by itself depends mostly on the
address: on a real machine an untouched Firefox, stock Camoufox and this browser got the same answers on
every page tried, from the datacenter's address and from a home one. A check that still wants a person
after the wait (the "Verify you are human" checkbox) holds the browser for the human as before.

With no proxy the browser goes out from its machine's own address, and claims that machine's place: the
timezone of where ipinfo.io places the machine's public address (asked from this computer once per
machine), and that address for WebRTC. When ipinfo.io does not answer, the timezone is the one of the
server's location (Hetzner's `nbg1` and `fsn1` are Europe/Berlin, `hel1` Europe/Helsinki, `ash`
America/New_York, `hil` America/Los_Angeles, `sin` Asia/Singapore). When neither is known (a docker
machine on this computer), the browser starts as before, its clock UTC, and the node's log says so. The
launch never waits more than a few seconds for this and never fails for it. Its languages stay `en-US`
whatever `browser_language` says, and it gets no position, as with a proxy.

A check the agent can click. In any browser, an environment's or an account's, a site's "are you human" checkbox
(Cloudflare's, hCaptcha's, reCAPTCHA's "I'm not a robot") shows at the top of the snapshot as a checkbox with
a ref, and the browser is not held for it:

```
- checkbox "Verify you are human (Cloudflare check)" [ref=c1] [cursor=pointer]
- text: a site's check on this browser. Click c1 once (browser_act); an image puzzle after it is for your human
```

The check sits in a frame of another site, often in a closed shadow root, where the page's outline has no
node, and on Cloudflare's own challenge page the frame is out of Playwright's reach altogether: its pointer
events never arrive there. So openajar finds the checkbox the way a person does, in a picture of the check,
and `browser_act(ref="c1", action="click")` clicks it with real input on the machine's screen (the keyboard
helper's X hand): a glide to just beside the check, a short move onto the box, a press held as a hand holds
it. A browser without the machine's hand (an account's browser on the node) clicks with the browser's own
pointer, which reaches the widget frames but may not reach Cloudflare's full-page check; then the browser goes to
the human as before. After the click it waits for the vendor's answer and says it: the check passed, an image
puzzle appeared (the owner's solving service through `browser_challenge`, else the human), or it did not pass
(do not click again in a loop). A click that did not get past the check holds the browser for the human, unless
what it left is a puzzle the owner's solver could take. A check still at work by itself ("Verifying...") shows a
line that says to wait instead of a ref. When a solving service the owner set up could take the check (below),
the line names `browser_challenge` instead.

It covers `browser = "vm"` only. An environment whose browser runs on the node (`browser = "node"`)
exits through its machine as before: that browser's traffic is the tunnel's SOCKS proxy, which does
not chain to another proxy, so `proxy add` says so on such a node.

#### The owner's solving service

For a widget a click does not pass (Cloudflare Turnstile, reCAPTCHA v2, reCAPTCHA Enterprise v2, hCaptcha on an
ordinary page), the
owner can plug in a captcha-solving service of their own. It is off until they add one. openajar ships no key
and shares none. The service, and what it costs, are the owner's choice.

```
openajar solver add main 2captcha                                  # prompts for the key
openajar solver add mine webhook --url https://solve.example.net/token
openajar solver add mine command --command "python3 /path/solve.py"
openajar solver list             # each service, and this month's solves with the spend they reported
openajar solver test main        # the balance (free); --solve buys one token for Cloudflare's test widget
openajar solver log              # every request: when, which browser, site, widget, service, result, cost
openajar solver remove main      # the entry and its key; the log stays
```

- **Services.** Built in: `capsolver`, `2captcha` and `anticaptcha`, each by its documented task API (createTask,
  then getTaskResult; getBalance for `test`), with the proxyless task types only. All three take Turnstile,
  reCAPTCHA v2 and reCAPTCHA Enterprise v2 (the checkbox: CapSolver's `ReCaptchaV2EnterpriseTaskProxyLess`,
  2Captcha's and Anti-Captcha's `RecaptchaV2EnterpriseTaskProxyless`, with the widget's `s` as
  `enterprisePayload` when the page has one). reCAPTCHA is Enterprise when the page loads
  `recaptcha/enterprise.js` or its frame is `/recaptcha/enterprise/anchor`. None of them lists hCaptcha any more (their documentation, read 2026-11-25), so hCaptcha goes
  only to a hook. `--url` points a built-in at another address with the same API. `test --solve` asks for a token
  for Cloudflare's test key that forces the interactive check (`3x00000000000000000000FF`) on 2Captcha's demo
  page; 2Captcha refuses the always-pass key.
- **Hooks.** `command` runs a command line on this computer, with no shell. `webhook` POSTs to an https URL
  (http only to this computer itself), and follows no redirect. Both get the job as JSON:
  `{"kind": "turnstile" | "recaptcha_v2" | "recaptcha_v2_enterprise" | "hcaptcha", "site_key": "...",
  "page_url": "https://host/path", "action": ..., "cdata": ..., "invisible": false, "s": ...}` (`s`: reCAPTCHA
  Enterprise's, when the page has one), and answer `{"token": "...", "cost": 0.002}` (`cost` in US
  dollars, optional) or `{"error": "why"}`. The key, when there is one, is the command's environment variable
  `OPENAJAR_SOLVER_KEY` (never its command line), or the webhook's `Authorization: Bearer` header.
- **The log.** `solver/solves.jsonl` in the node's home gets one line after each request: the result, and the
  cost the service reported (2Captcha and Anti-Captcha report one, CapSolver does not). It is only a log: a line
  that cannot be written or read is a warning in the node's log, and nothing waits for it.

The agent uses it through one tool, `browser_challenge(on)` in the `browser` toolset (`tools_more("browser")`,
or `tools_call`). Its steps, in order, each only when the one before did not get past the check:

1. **wait**: up to 20 s for Cloudflare's check, 5 s for the others, for it to pass by itself;
2. **click**: one click on its checkbox, as above, unless one was made on that page in the last minute;
3. **solver**: the widget's site key and kind are read from the page's own document (its element's
   `data-sitekey`, else the address of the vendor's frame directly in the page), the first of the owner's
   services that takes that kind is asked for a token, and, when the page is still at the address the token was
   asked for, the token goes where the widget puts it (`cf-turnstile-response`, `g-recaptcha-response`,
   `h-captcha-response`), then the page's own callback is called when the page named one (`data-callback`, or
   reCAPTCHA's client table). The site checks the token when the form is sent. reCAPTCHA leaves an image puzzle
   that the click opened over the page, where it blocks the next click: openajar clicks once on the puzzle's own
   transparent overlay, at a point where that overlay is what the page's hit test answers, which closes it as a
   click outside it does. When there is no such point, or it stays open, nothing else is clicked and the step
   says so. hCaptcha's challenge is left as it is;
4. **human**: nothing got it past, so the browser is held for the human, and the agent waits with
   `browser_wait_handback`. A call that ends early on a page with a check (its 240 s deadline, a cancel, an
   error) holds the browser the same way.

The result's `ended_by` is `nothing`, `wait`, `click`, `solver` or `human`, and `steps` says what each step
did, the service and its cost included. An image puzzle in an environment's browser is not held at once when a
service of the owner's could take it (a widget is found in the page's own document and a service takes its
kind), so the agent can call the tool, and the snapshot says so. Otherwise it is held for you at once, as without
a solver.

It works the same in every browser, an environment's or an account's. What it does not send, because no token
from a service works there:

- **Cloudflare's full-page check** ("Just a moment..."): its answer is a cookie tied to the browser and its
  exit address, so a service would need the proxy's credentials, and openajar never sends a proxy or its
  credentials anywhere. It is recognised by its signal and by its own markers (`#challenge-form`,
  `#challenge-stage`, the orchestrate script).
- **A widget inside another site's frame.** Its key would go under this page's address, and its token could not
  reach this page.
- **A widget it finds no site key for.**

What the service learns: the page's scheme, host and path (never its query or fragment), the widget's site
key, and for Turnstile its action and cData. The request goes from the node (with openajar local, this
computer), not from the machine. The key is stored in `secrets/solvers.json` (600) and is in no tool result,
log, trace or the solve log. [THREAT-MODEL.md](THREAT-MODEL.md#the-owners-solving-service) has the rest.

#### This computer's own connection

With openajar local, the browser can also go out through the computer openajar runs on, with no proxy:

```
openajar proxy this-computer          # on: prints the address this computer exits from now, and its country
openajar proxy test this-computer     # the same check a launch makes
openajar proxy this-computer --off    # off (as `openajar proxy remove this-computer`)
```

It puts the reserved label `this-computer` in `browser_proxies`. It needs no entry in `proxies.json`, and
`proxy add this-computer ...` is refused. It can stand alone or sit in the list beside proxies; the pick,
the save and the resume treat it like any label, and `environment()` shows it as
`network.browser_proxy` with the label `this-computer`, the exit IP and its country.

The browser's forwarder for it has no upstream: it connects to the sites itself, from this computer, so
the browser's traffic goes machine, SSH tunnel, this computer, site. Before each launch the node asks
ipinfo.io straight from this computer (with the same second source as for a proxy), and the browser
claims that exit's timezone, languages and WebRTC address. The check asks over IPv4 first, and the
forwarder connects over the family the check answered over, so a dual stack home shows sites one
address; it uses the other family only for a name that has none in this one. When the check fails
(this computer offline or asleep) the browser does not start.

From this computer the forwarder reaches public addresses only: never a local name, a private, loopback,
link-local or CGNAT address (also inside an IPv4-mapped or NAT64 IPv6), this computer's own public
addresses and the household's IPv6 `/56`, or the exit address itself (the router seen from inside), and
never port 25. Names are looked up on this computer once per connection, and the connection goes to the
address that was checked. A closed network's list still applies first, and an entry in it that names a
private address stays refused. [THREAT-MODEL.md](THREAT-MODEL.md#this-computer-as-the-exit-this-computer)
has the details.

What it costs:

- Every page travels through your home connection twice: down to this computer, then up to the machine.
  Your upload speed is the limit, and video is heavy.
- It works only while this computer is on and online. openajar local saves the environments when the
  computer sleeps, so this matters for a computer left open.
- The address is your household's. What an agent does on a site counts for your home: a site that bans the
  agent bans your home address.
- Every environment that takes it shares the one address. For several agents at once, or accounts that must
  not be linked, a static residential proxy per environment is still the way.
- When the laptop changes network or a VPN is on, the address changes. The browser's timezone and language
  follow it at the next launch, and a resume's `missing` says the address moved.

On a server node it works too, but the server's own address is a datacenter's, which sites stop as they
stop the machine's, so `proxy this-computer` says it gains nothing there.

### Secrets for the app

An app often needs an API key for its tests or its dev server (a payment API's test key, say). Bind
it once at the node, and the machine never holds it:

```
openajar environments secret add STRIPE_SECRET_KEY --host api.stripe.com                 # every environment
openajar environments secret add STRIPE_SECRET_KEY --host api.stripe.com --repo github.com/me/shop
openajar environments secret list                                                        # names and hosts only
openajar environments secret remove STRIPE_SECRET_KEY [--repo github.com/me/shop]
```

The value is read from a prompt (or stdin with `--stdin`), never the command line, and kept in the
vault as an API key bound to those hosts and to environments (`env/*`). Storing it is the
permission: nothing asks when it is used. Only you bind one: no tool takes a binding, so an agent
cannot bind a variable to a host of its choosing. A binding for a repository wins over one for every
environment. New environments get it; open ones when they open again.

On the machine, `STRIPE_SECRET_KEY` holds a placeholder, `agentos-secret-<random>` (another one in
each environment), in `/etc/environment`. A request to the bound host reaches the node, which ends
its TLS with a certificate from the environment's own CA, puts the real value in place of the
placeholder, and sends the request on to the real host over TLS with full certificate checks, to an
address it resolved itself. `environment()` lists the bound secrets by name (`network.secrets`),
never a value.

- In `limited` and `none`, the bound hosts are on the list, and their HTTPS through the proxy is
  ended by the node instead of passed through blind.
- In `full`, the machine's `/etc/hosts` sends the bound hosts to `127.0.10.1`, where a small relay
  (a systemd service on a cloud VM, a process on docker) passes the connection to the node through
  the SSH connection (`127.0.0.1:3131`). If the agent breaks the relay, its requests fail; the value
  still never reaches the machine.
- The CA: one per environment, made on the node; its key never leaves the node and is deleted when the
  environment closes. The machine trusts its certificate: the system store, and
  `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` in `/etc/environment`.

What is covered, exactly:

- The value goes in only when the TLS server name and the request's `Host` header are both a host the
  secret is bound to, and only into the request's headers (a `Basic` credential's user or password
  included) and its URL's query. Every request on a kept-alive connection gets it.
- Not into a body (a key in JSON or a form is sent as the placeholder), not over plain HTTP, and
  never to another host: a placeholder sent anywhere else goes out as it is and is listed under
  `network.blocked`.
- HTTP/1.1 only (the node offers `http/1.1` alone, and clients fall back to it). HTTP/3 (QUIC) is not
  carried; clients fall back to TCP. A client that pins the API's certificate refuses the
  environment's CA, so its requests fail. WebSockets to a bound host are not carried.
- The response comes back as it is: an API that echoes your key in a response hands it to the
  machine. The browser does not trust the environment's CA: opening a bound host in it shows a
  certificate error (it is meant for the app's API calls).
- Warm machines (a pool the node keeps ready, when it has one) are `full` machines: an open with
  `limited` or `none` rents its own closed machine. Secrets are set up on any machine as it opens.

**Which secrets a placeholder can carry.** The node puts the value in on the way out, so a
placeholder works only for a key the app sends to an API:

| Works | Does not work, and why |
| --- | --- |
| an API key or token in a header (`Authorization: Bearer ...`, `X-Api-Key: ...`) | a value the app uses on the machine itself: a webhook signing secret (Stripe's `whsec_...`), a session, cookie or JWT secret, an encryption key, a private key, AWS's secret access key (it signs each request on the machine). The machine computes with the placeholder and gets a wrong answer |
| a Basic credential's user or password (the client builds the header) | a value sent in a request body: an OAuth refresh token or client secret in a token request's form, a key in a JSON body |
| a key in the URL's query (`?key=...`) | a database, cache, queue or mail server's password (Postgres, Redis, SMTP...): not HTTPS |
| over HTTPS, HTTP/1.1, to the bound host | a secret inside a URL (`https://user:pass@host`, a Slack webhook URL), plain HTTP, WebSockets, a client that pins the API's certificate, another host |

For one that does not work, give the machine a value of its own: a test account's key, a fresh
random session secret, a database on the machine with a dev password.

### Env files

An app's local `.env` files hold a few secrets and many settings (URLs, flags, ports, names). One
command puts them on an environment's machine, from the repository's folder on your computer:

```
openajar environments env push e7k2 apps/api/.env apps/web/.env.local --dry-run   # the table only
openajar environments env push e7k2 apps/api/.env apps/web/.env.local             # the table, then it asks
openajar environments env push e7k2 --plain DATABASE_URL --host ZOHO_API_KEY=www.zohoapis.eu --yes
```

`e7k2` is the environment's id (`openajar environments list`) or its title. With no files it pushes
`env_files` from the repository's `.openajar/environment.toml`, else `./.env`. It reads each file
(dotenv syntax: quotes, `export`, comments, empty values), sorts each key into plain or secret, and
shows a table: the key, its class, and what happens. A plain value is shown cut short; a secret's
value never is.

- **Plain** (a URL with no password, a flag, a number, a path, a few short words): copied as written.
- **A secret a placeholder can carry** (an API key or token): bound to its host in the vault for
  this repository's environments, exactly as `secret add --repo` binds it. The host comes from a known
  API (`STRIPE_`, `sk_live_...`, `OPENAI_`...), from a URL among the file's settings with the same
  prefix (`DATAFORSEO_PASSWORD` next to `DATAFORSEO_API_URL=https://api.dataforseo.com/v3`), or from
  `--host KEY=HOST`. Without one it is not sent. One bound already is reused, not bound again.
- **Any other secret** (the table's "not supported as a placeholder", with the reason above): not
  sent. The table says what you can do.

It errs toward secret: a key whose name says secret (`KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `DSN`,
`PRIVATE`...), whose value has a secret's shape (a known key prefix, a JWT, a URL with a password, a
long random string), or whose value it is not sure about (a UUID, letters and digits mixed) is a
secret. A setting about a secret is plain when its value is plain (`AUTH_URL`, `JWT_EXPIRES_IN=7d`).
Per key, with patterns: `--plain KEY` copies it as it is (still never shown), `--secret KEY` treats it
as a secret, `--host KEY=HOST` binds it to HOST, `--skip KEY` leaves it out.

The machine's file goes to the same path under the repository's folder (`/work/shop/apps/api/.env`),
replacing what was there (mode 0600), over the environment's SSH. It holds the plain values,
`KEY=${KEY}` for each bound secret (it reads the placeholder from the machine's environment: most
dotenv loaders keep a variable the environment already has, and those that expand `${...}` read it
from there; a resumed machine gets new placeholders, and this line follows them), and a comment naming each secret not sent. No comment or bad line of your file is copied.
A secret bound by this push reaches an open machine when the environment opens again (close it and
open the same title); until then it reads as empty. The environment's last calls get one line per
file, with the counts and the names of the secrets not sent, never a value.

It is a command you run (or approve once when an agent proposes it): a repository's `env_files`
only names files, and nothing on the node or in an open reads your computer's files. Asking happens
on a terminal; without one it needs `--yes`, after the table. `--json` gives the table as data (plain
values included, secret values never).

### Signing in

An environment's browser is a plain browser with tabs, and some work needs an account: a dashboard to
check, a staging site, a service's settings. Bind the login once at the node, and the agent signs in
whenever it needs to, without asking anyone and without ever seeing the password:

```
openajar environments login add github.com --user me@example.com --title "Shop fix"      # that computer
openajar environments login add https://staging.example.com/login --repo github.com/me/shop
openajar environments login add example.com --user me@example.com --all --totp           # every one, with codes
openajar environments login add github.com --entry "github work" --title "Shop fix"       # one already in the vault
openajar environments login list                                                          # names and scopes only
openajar environments login remove github.com [--title "Shop fix"]
openajar environments login "Shop fix" github.com                                         # sign it in now
```

- **The binding.** The password (and with `--totp` the TOTP seed) is read from a hidden prompt, or
  from stdin with `--stdin` (the password on the first line, the seed on the second), never from the
  command line. It is an ordinary vault login bound to environments (`env/*`), with the computers it
  covers. `--title`: the computer an agent has with that title now (open or saved), and only that
  agent's (the grant family that opened it): it follows the computer across saves and resumes, where
  the id changes, and another agent that picks the same title gets nothing. So bind it once the
  computer exists: with no computer of that title yet, or several agents' computers of it, `add` says
  so (name one by its id then: `--title e7k2`). `--repo`: every environment of that repository,
  whichever agent opened it. `--all`: every environment. `add` again with another scope adds it;
  `remove` takes one scope off (or all), and a login `add` stored leaves the vault with its last one.
  A login is named after its site; `--name` tells two logins of one site apart. Only you bind one: no
  tool takes a binding, and the vault seals its bindings with its key, so an agent can never add or
  widen one. Storing it is the permission: signing in never asks.
- **The agent.** `environment_open` and `environment()` list the logins the computer may use, by name
  and site (`logins`; never a secret, and not the username, which the vault scrubs from everything an
  agent reads). `login(on=...)` signs in: the node opens the login's page when the browser is not on
  it, finds the form by its structure, checks the vault's rules (the login is bound to this computer
  and to this site, over https or on the machine's own `localhost`, the field is a password field)
  and types the username and password with human rhythm, then the TOTP code when the login has a
  seed. When a site logs the browser out, the agent calls `login` again. A CAPTCHA, a device approval
  or a code sent by email or text is the person's: the result says `needs`, and you finish it in the
  live view (`links.browser`).
- **You, again.** `openajar environments login "<title>" <site>` fills the login in that open
  environment's browser now, for when the agent is not running, and says what happened: signed in, or
  what needs you (with the live view's link). A saved environment comes back first with
  `openajar environments resume "<title>"`.
- **Where the password goes.** It is typed where the browser runs: with `browser = "vm"` (a laptop)
  into the VM's browser, over the connection the node already drives, so it passes through that
  browser's process; with `browser = "node"` it stays on the node. The session cookies the sign-in
  makes stay in that browser and in its saves (encrypted with `encrypt_saves`), so a resumed
  environment is still signed in. Never in a command line, a log, a trace, a tool result or a replay
  caption, and no replay frame is taken while a fill runs. A frame or a video can show the page
  after it, and the username as typed (the password field shows dots). Anything running as root on
  that machine could read what the browser holds: bind only what the computer's work needs
  ([THREAT-MODEL.md](THREAT-MODEL.md#logins-in-an-environments-browser)).
- **The grant.** The `environments` special of `openajar connect` includes typing a bound login on
  environments. An agent connected before this change gets `special: this grant does not include
  secret`: run `openajar connect` again.

### 5. Check it

```
openajar environments setup
```

It checks the provider (Docker running, or the Hetzner token), the image and the git
declarations, and says what is missing and the command that fixes it.

## Use it from an agent

Connect your agent first (`openajar connect`, see the [README](../README.md#connect-claude-code-codex-and-t3-code)).
The MCP intro tells every agent it can have an environment, and the `openajar-environment` skill
(installed by `openajar connect` for Claude Code) teaches it the loop. You only have to ask:

> Work on this in openajar: fix the failing checkout test, check the page on a phone, push a branch.

The loop: open, read and edit files, run the tests, start the dev server and look at it in the
browser, keep a screenshot as proof and give you its link, push a branch and open the pull
request (you get its link), close (the VM is deleted).

What the agent does:

| Call | What it does |
|---|---|
| `environment_open(title, repo, ref, size, hours, keep, signins, network)` | rents the VM, clones `repo` into `/work/<name>`, opens the browser; returns the workdir and the browser's live view link for you. With `repo` and no `title` it finds that repository's one open or saved environment (several: `environment_choice`, with each title to pick from; none: a new one named after the repository) |
| `file_read(path, offset, limit)`, `file_edit(path, old, new)`, `file_write(path, content)` | the repository's files on the VM, with line numbers (`file_read(paths=[...])` reads several in one call); an edit replaces exact text that occurs once (a relative path starts in the repository) |
| `terminal_exec(cmd, cwd, max_bytes)`, `terminal_job_start`, `terminal_job_read(until_exit, until_port)`, `terminal_job_cancel` | commands and dev servers on the VM (`on`: the handle your environment_open gave; the job tools take it too and check it names the job's machine, though the job id alone is enough); in every agent's tool list. A command's output comes back within 16 KB by default (the middle is cut, with the full sizes). `shell_open` and the rest are `tools_more("terminals")`. See [Waiting and memory](#waiting-and-memory) |
| `browser_navigate(url)`, `browser_snapshot`, `browser_act` | the environment's browser, a desktop window; `localhost` is the VM |
| `browser_screenshot(proof="checkout after the fix", full_page, width)` | keeps the shot as proof (see below); `width=390` shows the phone layout |
| `ui_check(url="http://localhost:3000")` | studio's page checks (`tools_more("studio")`), on the VM's dev server; while the environment cannot be reached they fail with `environment_not_ready` and never check the node instead |
| `environment()` | state, time left, cost so far, listening ports (a shared one with its `link`), git (branch, ahead, dirty, `pushed`: each branch at its commit on the remote now, `deleted`: branches it deleted, `compare`: GitHub's form for the pushed branch, `pull_requests`), proofs, the agent's `note`, the last calls (a command's `exit_code`, a refusal's `error`), and always the agent's `open` and `saved` environments by title |
| `environment_pull_request(title, body, base, draft)` | opens the pull request for a branch this environment pushed, with the node's GitHub token, and returns its `url` (see below) |
| `environment_port(port)`, `environment_port(ports=[...], same_numbers=true)` | a link on your own computer to a server on the VM, or several with the VM's own port numbers (see [Ports](#ports)) |
| `environment_checkpoint(note)`, `environment_rollback(checkpoint)` | a cheap undo with git (see [Checkpoints](#checkpoints)) |
| `environment_close(discard, final, note, forget)` | refuses with `unsaved_work` while any repository under `/work` has work that is not on a remote (changed or new files, commits on any branch, stashes); then deletes the VM, its terminal and its browser; `machine` says `deleted` only once the provider has no such server (`deleting` while the node still deletes it). `saves` says what became of the saves; `note` and `forget` are in [Keeping an environment](#keeping-an-environment) |

The agent's default tool list is this loop and nothing else: `environment_open`, `environment`,
`environment_save`, `environment_close`, `environment_upload`, `environment_download`, the terminal
(`terminal_exec`, `terminal_job_start`, `terminal_job_read`, `terminal_job_cancel`), the files (`file_read`,
`file_write`, `file_edit`), the browser (`browser_navigate`, `browser_snapshot`, `browser_act`,
`browser_wait`, `browser_screenshot`, `browser_tabs`, `browser_dialog`, `browser_wait_handback`,
`browser_record`), `environment_pull_request`, `environment_checkpoint`, `environment_rollback`,
`environment_port`, `login` and `approval_wait`, plus `tools_more` and `tools_call`. The rest come in
groups the agent loads with `tools_more(group)`: `accounts` (your own signed-in account browsers on a server
node), `browser` (a browser's downloads, scrolling), `terminals` (terminals and services the
node keeps, raw VMs, and `environment_forget`), `studio` and more ([TOOLS.md](TOOLS.md)). The names 0.4.0
renamed (`lane_exec` is `terminal_exec`, `lane_file_read` is `file_read`, and so on) were removed in 0.5.0, and
so were the three merged into another (`browser_tab` is `browser_tabs(select=...)`, `browser_dialog_answer` is
`browser_dialog(accept=...)`, `approval_status` is `approval_wait(wait_s=0)`). A call by an old name fails, and
its error names the tool to call instead ([TOOLS.md](TOOLS.md), "Removed tool names"). A wait is `wait_s` on
every tool in the list.

Work that is only on your computer (changes not committed yet, or a project with no repository the
node can clone) gets there two ways. The agent commits it to a branch, pushes it, and opens with
`ref=<branch>`; or, without a commit, it uploads its checkout:

1. `environment_upload(dest)` gives a signed link on
   the node's own address (for an agent on your Mac, `http://localhost:7400` through its tunnel),
   good once and for 15 minutes, and two commands.
2. The agent runs them in its own shell, in the folder with its code. In a git checkout `pack`
   takes the tracked and new files and leaves out what git ignores; in a plain folder it takes all
   of it but `node_modules`, `.env`, `.git`, `.venv` and `__pycache__`. The archive goes to the temp
   folder, never into the folder it packs. `send` puts it on the node with curl, so the bytes never
   go through tool calls. An archive with no files is refused:

   ```
   A="${TMPDIR:-/tmp}/openajar-work.tgz"; if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
     git ls-files -co --exclude-standard -z | COPYFILE_DISABLE=1 tar --null -T - -czf "$A"
   else COPYFILE_DISABLE=1 tar --exclude node_modules --exclude .env ... -czf "$A" .; fi
   curl --fail-with-body -T "${TMPDIR:-/tmp}/openajar-work.tgz" "<url>"
   ```

3. The node checks every file in the archive, then extracts it into the workdir (`/work/<name>`,
   or `/work` for an environment opened without a repository; `dest` may name a folder under
   `/work`). Files in it replace the same paths, and nothing else is deleted. The reply lists the
   files; the upload is in the audit log and in `environment()`'s last calls.

The checks: at most 512 MB sent and 4 GB unpacked, 200,000 files, and no absolute path, no `..`,
no link that leaves the folder, no device or fifo (Python's tarfile data filter, and more). One bad
file refuses the whole archive before the VM sees any of it. The link ends when the environment
closes or the node restarts, and a used, expired or changed one gets a 403. For a few small files
without a shell of its own, the agent still has `terminal_upload` and a job's sync.

One chat holds a live machine (the owner's rule of 2026-11-16: "only one agent chat should have
access to the same machine at the same time, even if it is the same project"). `environment_open`
with a new title opens a new environment, even while others of the same agent are open (the node's
limits still hold), and gives the calling chat the machine's handle, `on` (its id, a dot and 16
random letters and digits). The node keeps only the handle's sha256, so no record, listing or log
shows it. Only calls that carry it reach the machine: the agent keeps the `on` from its
`environment_open` and passes it on every call.

- A client that sends no MCP session id (MCP 2026-07-28, as Claude Code and T3 Code use it, and
  stdio) looks like every other chat of the agent. A terminal, browser or environment call without
  `on` is refused with `which_environment`, even with one machine open, and so is a title or an id
  in `on`: another chat's machine must never be reached by default.
- A client that sends a session id (`mcp-session-id`, the older protocols) holds its machines by the
  session too: its calls without `on` go to the one it opened last, and its own id or `env/<id>`
  works as `on`.

The title of a live machine another chat holds is `environment_in_use`, with the fix: open your own
with another title, like "<title> 2". Once the holder has made no call for `take_over_minutes` (10,
in `environments.toml`), `environment_open(title, take_over=true)` gives the machine to the new chat
with a fresh handle (`opened: "taken_over"`), and says the earlier chat's work is on it. The old
handle stops working (`no_environment`, saying it was taken over), and the move counts as a call, so
the earlier chat cannot take it straight back. Without `take_over` it is refused, so taking a machine
is always deliberate. A machine kept live and let go (`environment_close` with keep "live") is
nobody's: its title gets it back at once, with a new handle. The repository may be left out; the
same title with another repository is `environment_already_open`, whose fix is another title. What
an attach could not give as asked (keep, hours, size, ref) is in `differs`, with the values it has in
`applied`. With the title of a saved one it resumes it into a new machine leased to the resumer
(`opened: "resumed"`); of two chats resuming at once, one wins and the other gets
`environment_in_use`.

`environment()` and every listing show a live machine the caller does not hold without its handle
or id: its title, state and `use` ("in use by another chat", "quiet for N min" with the take-over
call, or "let go"). `environment(on=<handle>)` shows that machine in full. `terminal_jobs` without
`on` lists no environment's jobs over MCP. Studio's checks take `on` for a page on the machine
(`ui_check(url="http://localhost:3000", on=...)`). Every result of a call that went to an
environment carries `environment: {id, title}`. Calls with `on="workspace/name"` go to that account
browser, as before: account browsers and environments live side by side.

An environment belongs to the agent that opened it: its grant, and the grants narrowed from it.
Other people's agents cannot
run commands there, drive its browser, read it or close it, whatever their grant says; to them the
environment does not exist. You see and close every one from the web app and
the CLI; a member sees and closes the ones their own agents opened.

Nothing in the environment waits for a person, even after the agent read a web page: not opening
it, not its commands, not its browser (a click that publishes, an upload), not a push. The VM holds
no account and no key, its browser only the logins you bound to it ([Signing in](#signing-in)), and
its one token pushes only its own repository. What stays are refusals, never questions: the limits above, a push that would
rewrite or delete the default branch or a branch the environment did not create, a repository the
token does not reach. Reading a page in the
environment's browser still marks the agent as having read untrusted text, so what it does next on
your account browsers (a post on `personal/x`, say) asks you as before.

### Waiting and memory

- **Waiting for a dev server.** `terminal_job_read(job, until_port=3000, wait_s=120)` returns as soon
  as something listens on port 3000 on the VM (`listening: true`; a server on `0.0.0.0`,
  `127.0.0.1` or `::1` counts), or when the job ends first (`listening: false`, its exit code, and
  stdout and stderr are the end of its output), or at `wait_s` (at most 120; call again to wait
  longer). The waiting is one command on the VM that reads its listening sockets
  (`/proc/net/tcp`), not a loop of calls, and the call sends progress while it waits. No `sleep`
  loops in `terminal_exec`.
- **Waiting for a job to end.** `terminal_job_read(job, until_exit=true, wait_s=120)` returns when
  the job ends. When more is left after your offset than `max_bytes` (16 KB by default), stdout and
  stderr are each its start and its end, cut at line ends, as `terminal_exec` keeps them, with a marker
  between them (`[openajar: 6452946 bytes skipped here, of 6468895 in all; read them with
  stdout_offset=3992]`) and the same words in `note`. `next_stdout_offset` is then the end; a read
  at the offset the marker names pages through what was skipped. A read without `until_exit`, and
  one with `base64_output`, always gives the bytes right after your offset.
- **Out of memory.** When the VM's kernel kills a process for memory, `terminal_job_read` and
  `terminal_exec` say so: `oom` lists each kill since the job or command started (`pid`, `name`,
  `rss_mb`, `at`, `machine_mb`: the machine's memory, and `in_job`: whether it was one of this job's
  processes, known from the job's own login session on the VM; `null` when the VM cannot tell),
  `oom_killed` is true for a kill of its own, and `note` says it in words ("The kernel killed pid
  4242 (node, 4.1 GB) of this job at 06:52:02 UTC: out of memory. The job runs on without it and
  may hang: ..."). When the killed process used half the machine's memory or more, the note names
  the machine's memory and the next size ("This machine has 3.7 GB of memory, and python3 used 3.6
  GB when it was killed: run less at once, or push your work and open a bigger machine,
  environment_open(title=..., size="medium")"); resuming a saved title keeps the size it was saved
  with. A job whose child was killed often keeps running and prints nothing; a read that waits
  (`until_exit`, `until_port`, or for more output) returns at once when such a kill appears. The
  node reads the VM's kernel log (`dmesg`) with one SSH call: at most every 10 seconds while a job
  is read, when a read's new output has the shell's "Killed" line, and once when a job or command
  ends with a sign of a kill: an exit code that is not 0, a "Killed" line, or an exit code 137 in
  its output (`child exit=137`, `exit status 137`, `SIGKILL`). That covers the common case of a
  parent that printed its child's 137 and exited 0. For an ended job this happens before the read
  that reports the end returns. A clean end costs no call. Every machine has swap (see [What is on
  the VM](#what-is-on-the-vm-and-what-is-not)), so a heavy build slows down before it runs out.
- **Jobs outlive a restart of openajar.** A `terminal_job_start` job runs on the VM on its own (its own
  session, `setsid`), with its stdout and stderr in files there (`/tmp/agentos-wl/<id>.out` and
  `.err`), and the node follows those files over SSH. A restart or an upgrade of the openajar service,
  a laptop that sleeps, or a dropped connection no longer ends a dev server: when the service starts
  again it finds each job still running on its machine and follows it again, and `terminal_job_read` goes
  on with the same job id and offsets. `terminal_job_cancel`, the job's timeout (also one that passed
  while the service was down) and closing the environment still end its whole process tree. If the
  machine restarted meanwhile, the job is `lost` and its `note` says so. A job whose node never comes
  back ends a minute past its timeout by itself.
- **`pkill -f` killed its own shell.** `pkill -f vite` matches the shell that runs it, whose
  command line holds the pattern too, so the shell dies before the rest of the command runs (exit
  255 over SSH, or 143, 137, a signal), even after it printed something. When a `terminal_exec` or an
  ended job exits that way and its command holds `pkill`, `pgrep` or `killall` with `-f` or
  `--full`, its `note` says the shell running it was killed and why: use `pkill -f '[v]ite'` (the
  brackets keep the pattern from matching itself), kill a pid, or `terminal_job_cancel` for a job.

## Watch and take over

`environment_open` returns `links.browser`, and agents are told to give it to you: **the
browser's live view**, served by the node itself, the environment's browser as it is now. Take
control to click or type yourself, then hand it back, from that link alone: the environment's
browser holds no account, so no sign-in is needed (an account browser's link still needs your panel
sign-in for that, [THREAT-MODEL.md](THREAT-MODEL.md#viewer-links)). On a server node an environment
whose desktop is on needs that sign-in too: its dock opens a root terminal on the machine
([THREAT-MODEL.md](THREAT-MODEL.md#an-environments-desktop)). The agent waits until you hand
it back, watching it and your input count as use, so the reaper does not close it under you, and the trail records
what you did as `link holder`. The link is on the node's own address as the agent reaches it (for an
agent on your Mac, `http://localhost:7400` through its tunnel to the node), so it opens on that computer. In T3 Code the agent opens it in the preview pane
beside the chat (`preview_open`), so you watch the browser while you talk to the agent.

**Record a clip** with the button next to full screen (or **R**): it saves what you see as a video on
your computer, with the agent's pointer in it, made in your browser and never uploaded
([LOCAL.md](LOCAL.md#the-live-view)).

How long a link works: it opens until its expiry (`links.browser_expires_at`, 15 minutes after it was
made by default, `view_token_seconds` in node.toml), and a page that is open renews itself until 30
minutes after its link was made. `environment()` hands out the same link on every call until shortly
before it expires, then a new one; a link given earlier keeps working until its own expiry
(`links.browser_note` says this to the agent). Replacing the viewer key ends them all.

### Proof of the work

`browser_screenshot(proof="checkout page after the fix")` keeps that screenshot as proof. It is
stored on the node with the environment (`$OPENAJAR_HOME/environments/<id>/proofs/`), recorded in the
audit log, and kept for 30 days after the environment closes (at most 30 for one environment). The
agent gets a link to it on the node's own address, good for 15 minutes (`environment()` and
`environment_close` list every proof with a fresh one), which it gives you or saves with
`curl -o proof.png "<url>"`. The web app's environment page shows them too
(`GET /api/orgs/{org}/environments/{id}` lists `proofs`, and each `src` is its PNG).

A proof can also be a **video**. The agent calls `browser_record` (in its list):

```
browser_record(action="start", proof="guest checkout, end to end")
browser_navigate(...), browser_act(...)          # the flow, as it would do it anyway
browser_record(action="stop")                    # or stop with the title: proof="..."
```

ffmpeg records the real screen of the environment's browser (the X display it runs on: the VM's for
`browser = "vm"`, the node's display of that browser for `browser = "node"`) in a process of its own, so
the agent's calls never wait for it, at 10 frames a second, at most 1280 pixels wide, H.264 in an
MP4 (small: on a docker machine, 41 seconds of filling a form and clicking took 73 KB, and its
GIF 84 KB). Stopping makes the MP4 and a short GIF preview (sped
up to at most 15 seconds, at most 8 MB), brings both to the node over the environment's SSH
connection and keeps them as one proof, in the same folder, with the same links, retention and
node-wide cap (1 GB of proofs, videos counted). A recording stops by itself and keeps what it has
after `record_max_s` (default 180 seconds), when the environment is saved or closes, and when the
browser goes away. One runs at a time; while it runs, `environment()` shows `recording` (its title,
`started_at`, `stops_at`).

The docker image and the `dev` image have ffmpeg. A machine from an older image installs it on its
first recording (apt, through the network's proxy when it is `limited`: about a minute), and the
result says so (`installed`); a machine whose network is `none` cannot, and the call fails with
`recording_failed` and the fix. With `browser = "node"`, the node needs ffmpeg
(`deploy/install-node.sh` installs it; `openajar doctor` warns when it is missing).

A video proof as `environment()`, `environment_close` and the web API list it:

```json
{"id": "p7k2m9qx4ta", "kind": "video", "title": "guest checkout, end to end",
 "taken_at": "2026-11-18T10:02:11Z", "page_url": null, "width": 1280, "height": 720,
 "duration_s": 27.4, "bytes": 1843210, "ended": "stopped",
 "gif": {"width": 640, "height": 360, "bytes": 2210447},
 "url": "http://localhost:7400/view/env/e8x2k4m9q-browser/proof/p7k2m9qx4ta.mp4?t=...",
 "gif_url": "http://localhost:7400/view/env/e8x2k4m9q-browser/proof/p7k2m9qx4ta.gif?t=...",
 "expires_at": "2026-11-18T10:17:11Z"}
```

`kind` is `"screenshot"` or `"video"`. `ended` says what stopped it: `stopped` (the agent), `limit`
(its length), `ended` (the browser went away), `save` or `close`. The links serve the MP4 with range
requests, so a player seeks. In the web API the MP4 is `src` and the preview `gif_src`
(`/api/orgs/{org}/environments/{id}/proofs/<id>.mp4` and `.gif`, behind the app's sign-in).

### Replay

Proofs are what the agent chose to keep. The **replay** is what happened in the environment's
browser between them, and it stays after the VM is gone: the node keeps a timeline of small frames
(JPEG pictures of the browser's window, 1280 pixels wide when Pillow is installed, about 15 KB each)
with what led to each one:

- after each agent call that can change the page (`browser_navigate`, `browser_act`,
  `browser_scroll`, `browser_upload`, `browser_dialog` answering, `browser_tabs` switching, `browser_wait`), with
  the call and the same short summary environment() lists for it (`click e12`, the url), scrubbed
  as the audit log is (`cause: agent`);
- every 3 seconds while you have control in the live view (`person`), and while the page goes on
  changing after an agent call (`change`: an animation, a dev server rebuilding), until two looks in
  a row find no change (at most 20 after one call);
- when the environment is saved (`save`) and when it closes (`close`), its last state.

A frame that shows nothing new is not kept. Taking one never gets in the agent's way: the node takes
it only while no call is on the browser and none waits for it, takes no place on it (no call waits
behind a frame), and drops it when a call starts while it is taken. It is a screenshot only: the
pointer, the focus, the scroll and the tabs stay as they are. On a docker machine with the browser on
the VM a frame took about 60 ms (the browser's JPEG is about 30 KB at 1920 pixels; shrinking it on
the node about 15 ms).

The frames, an index and the calls made on the environment are in
`$OPENAJAR_HOME/environments/<id>/replay/`. One environment keeps at most 1,500 frames and 150 MB
(past that its oldest `change` frames go first, then its oldest others, never the save and close
ones), every replay on the node together at most 1 GB (the oldest frames of closed environments go
first; while only open ones hold it, new frames are skipped). The reaper removes a replay
`replay_days` after its environment closed. In `environments.toml`:

| Setting | Default | |
|---|---|---|
| `replay` | `true` | keep a replay of each environment's browser |
| `replay_days` | `14` | days a closed environment's replay is kept |

It is on by default because an environment's browser holds no account of yours but the logins you
bound to it ([Signing in](#signing-in)), and no frame is taken while one is typed. A frame still shows
whatever the page showed, a secret your app printed or a signed-in page for example, so it is treated like a proof ([THREAT-MODEL.md](THREAT-MODEL.md#viewer-links)):
only you read it, an account browser is never pictured, and the agent sees only a count
(`environment()` gives `replay: {frames, since, bytes}`).

To look at one after the VM is gone:

```sh
openajar environments replay e7k2m9p3x4q --out ./replay    # frames/, timeline.json and index.html
```

`index.html` is a page that steps through the frames (left and right arrows, space plays them), with
each caption and the calls in between. It needs no network. The web API gives the same timeline,
to you only (a member's role and an agent's grant get `denied`):

- `GET /api/orgs/{org}/environments/{id}/replay?after=<at>&limit=500` answers `environment` (id,
  title, state, browser, created_at, closed_at), `replay` (enabled, frames, since, until, bytes,
  kept_until), `links_expire_at`, `timeline` and `next`. `timeline` is the frames and the calls by
  time: a frame is `{kind: "frame", id, at, cause, tool, summary, page_url, page_title, width, height,
  bytes, url, src}`, a call `{kind: "call", at, tool, summary, ok, error, exit_code, lane}`. `url` is
  the frame on the node's viewer, signed like a proof's link (15 minutes; replacing the viewer key
  revokes it), `src` the app's own route for it. Pass `next` as `after` for the next page (it is the last item's time and a count, `<at>|<n>`, so no item that shares a millisecond with a page's edge is lost).
- `GET /api/orgs/{org}/environments/{id}/replay/{frame}.jpg` is a frame (its `src`).

### Who the commits are by

With a GitHub login saved (`openajar setup github`), an environment's git commits as you: your GitHub
display name and your GitHub noreply address, `<id>+<login>@users.noreply.github.com`, so the commits
link to your account without showing your email. openajar asks GitHub for the account's number once,
with the saved login, and keeps it in `git/login.json`; only the name and the address reach the VM,
never a token. Without a login (or when GitHub cannot be asked), commits are by the agent: its name and
`agent@openajar.invalid`. An agent may still set its own with `git config` on the machine.

### Pull requests

After `git push origin <branch>`, `environment_pull_request(title, body=None, base=None,
draft=False)` opens the pull request on GitHub and returns its `url`, which the agent gives you.
The VM has no `gh` and no credential: the node opens it through GitHub's API with the token the
git gateway pushes with (a token declared for the repository or its owner first, else the GitHub
login of openajar local, `openajar setup github`; a deploy key cannot open one). It works only for
a branch this environment created on the remote, into `base` or the repository's default branch;
if one is open for that branch already, it returns that one. It never merges, closes or changes
anything else, and the token never leaves the node. With no token it fails with
`pull_request_failed`, and the agent gives you `git.compare` instead: GitHub's own form for the
branch. The proofs then stay on the node with their links.

The pull request shows the proof. Every proof of the environment (or the ones named with
`proofs=["<id or title>", ...]`) goes on the repository's `agentos-proofs` branch, an orphan branch
the node makes and adds to through GitHub's API with the same token (files at `<environment
id>/<proof id>.png`, `.mp4`, `.gif`), so it is never in the pull request's diff and shares no history
with your code. The body gets a **Proof** section: each title, a screenshot or a recording's GIF
shown inline, and the recording's MP4 as a link (GitHub plays a video in a body only when a person
uploaded it by hand). The links are `https://github.com/<owner>/<repo>/blob/agentos-proofs/<path>?raw=true`,
which open for anyone who can see the repository. GitHub's limits hold: a file of 100 MB or more stays
on the node (the result lists it in `proofs.too_large`), and an image over 10 MB is a link. If GitHub
refuses the files (a protected branch, a token that may not write), the pull request still opens and
`proofs.why` says why.

Calling `environment_pull_request` again for an open pull request (more proof since) updates only
openajar's own sections of its body, between their markers (`<!-- agentos:proofs:begin -->` and
`<!-- agentos:proofs:end -->`; the network's "Blocked during this work" the same way): the agent's
text and anyone else's stay as they are. `environment_pull_request(proofs=false)` leaves the proofs
out, and `pr_proofs = false` in `environments.toml` does so by default.

A proof pushed to the repository is visible to everyone who can see the repository (for a public
one, the world), and it shows whatever the page showed. Never keep a proof of a page with a secret
on it. See [THREAT-MODEL.md](THREAT-MODEL.md).

### Ports

`environment()` lists the ports listening on the machine, for example the dev server on port 3000.
`environment_port(3000)` shares one with you, as E2B and Daytona give a preview link: the node
listens on a free `127.0.0.1` port and sends each connection through the environment's own SSH
connection (its SOCKS proxy) to that port on the VM, so no new SSH session, key or firewall rule is
needed, and only the node's own user may use it. It closes with the environment.

- On openajar local the node is your computer: `url` is `http://127.0.0.1:<p>/`, open it as is.
- On a server node the port is on the server: run the `tunnel` line it gives
  (`ssh -N -L <p>:127.0.0.1:<p> <your node>`, the same as for the live view), then open `url`.

An app of several servers (a page on 5173 whose code calls its API at `localhost:3000`) needs them
together and under the same numbers, or the page calls your own computer's port 3000:
`environment_port(ports=[5173, 3000], same_numbers=true)` gives `forwards` (each with `url`, which says
`localhost`, the origin the app knows from the VM) and, on a server node, one `tunnel` line with an `-L`
for each. A number already taken here (another program, another environment's link) is never shared:
that port gets a free one, `same: false`, and `why` says that calls to `localhost:<port>` will not reach
the VM. Free numbers are not the default: on a laptop they would hold your usual dev ports while an
environment is open, and a server node holds every environment's links. A one-port call lists the other
ports the VM listens on (`also_listening`), which a page may call too.

The web app's Ports card shows the link (and the ssh line on a server node) for each shared port;
the others say "On the machine only".

From the CLI:

```
openajar environments list           # the open ones (alias: openajar envs); --all: closed and failed too
openajar environments show e7k2      # one in full: machine, cost, ports, git, last calls
openajar environments close e7k2     # --discard to close with unsaved work
openajar environments resume "Black Friday page"   # a save, by title or save id: held by no chat
```

The web app's Computers page (its Environments tab) shows the same, with each one's live browser, commands and git on
its own page ([web/README.md](../web/README.md) says how to build and open it). Without a build, the
live view links, the CLI and the terminal dashboard (`openajar open --terminal`) show the same.

## Prepared machines

A repository can say how its machine is set up, in a small file of its own,
`.openajar/environment.toml`. With it, an open installs the dependencies and starts the app by
itself, and from the second open on it starts from a **prepared machine**: an image of a machine on
which the setup already ran, as Codex cloud, Claude Code's cloud environments, Daytona and E2B do.

```toml
# .openajar/environment.toml
setup = "pnpm install --frozen-lockfile"     # once, on a fresh machine: its result is the prepared machine
refresh = "pnpm install --prefer-offline"    # on each open from a prepared machine, after the checkout
start = "pnpm dev --host 0.0.0.0"            # the dev server, started as a job on each open
port = 3000                                  # where it listens (needed with start)
ready_timeout_s = 120                        # how long the open waits for the port (default 120)
inputs = ["pnpm-lock.yaml"]                  # what makes a prepared machine stale (default: the lockfiles)
env_files = ["apps/api/.env"]                # local env files `openajar environments env push` sends (Env files, above)
```

Every key is optional, but `start` needs `port`, and `refresh` needs `setup`. A key openajar does not
know is an error that names it; the open goes on without the file and says why. Without `inputs`,
the lockfiles and manifests the repository has are used: `package-lock.json`, `pnpm-lock.yaml`,
`yarn.lock`, `bun.lock`, `bun.lockb`, `uv.lock`, `poetry.lock`, `requirements*.txt`,
`Pipfile.lock`, `Gemfile.lock`, `go.sum`, `Cargo.lock` and `composer.lock`. The commands run with
bash in the repository's folder, as root, with `CI=true`.

**Where it is read.** On the node, before anything is rented: from the node's own copy of a
connected repository (the git gateway's mirror), else from GitHub's public files for a public one
(no credential), at the `ref` the agent asked for. When the node cannot read it there, it is read in
the checkout once the clone is done. No file: nothing changes, the open works as it always did.
Agents propose the file in their branch when a repository has none; you merge it. An agent never
needs it merged to use it on its own branch: an open of that branch reads it there.

**What an open does with it.**

1. A ready prepared machine for this repository, its `setup`, `refresh` and input files, and the
   image the node would boot: the machine boots from it. The checkout it holds is brought to the
   asked ref (a `git fetch` and a checkout, not a clone), then `refresh` runs.
2. None yet: the machine boots as before, and `setup` runs on it as part of the open, so the agent
   gets its dependencies either way. The node then builds the prepared machine for next time in the
   background, on a machine of its own, **only within the node's limits** (below). A failed setup
   never fails the open: the result says what failed, with the end of its log, and no build starts.
3. With `start` and `port`: the command runs as a job, exactly as `terminal_job_start` does (it shows in
   jobs, a save records it as a service, and a resume starts it again), and the open waits until the
   port listens, up to `ready_timeout_s`. A port that does not listen in time is not a failure
   either: `app.why` says what happened, with the job's last output and the ports that listen instead.

`environment_open` and `environment()` say what happened:

- `prepared.used`: true when it booted from one, with `from` (the commit it was built at, and its
  age), `checkout`, `refresh` and `setup_s` (how long setup took on the build, and what it saved).
  Else `why` (no file, one being built, the last build failed with its log, no room for a build),
  `setup` (`ok`, `seconds`, the end of its output on a failure) and `build` (started, or why not).
- `app`: the command, the port, `listening`, its `job_id`, and `link`, the port's link as
  `environment_port` gives it (and `tunnel` on a server node). When it did not listen: `why`,
  `log_tail` and `listening_instead`.
- `environment_app_restart()` (in `tools_more("terminals")`) stops the app's job with its processes (and
  any running copy of the same command), waits for the port to close, and runs `start` again from the
  file as it is in the checkout now. It returns `app` as the open does, with `stopped`.

On Docker, a first open of a small test repository took 25 s (20 of them its setup) and started
the build; the second open, from the prepared machine, was ready in under 5 s with the app
listening; with a warm machine, about 2 s.

**The build.** It never runs on an agent's machine. The node rents a build machine from the base
image (it counts as a machine against `--max-live` and books an hour or more against the month's
budget; with no room it is skipped, and the open's `prepared.build` says so), clones the repository
(through the git gateway for a connected one, straight from the host for a public one), runs
`setup`, then removes what must never be in an image: the gateway's git config and its token (the
token is revoked before the snapshot, and the node checks that no byte of it is left on the disk:
if the setup wrote it somewhere, no snapshot is taken), git credentials, shell history, the login
and host keys (each new machine gets its own), the machine id and swap. Then it takes the snapshot
(`docker commit` on Docker) and deletes the build machine. Caches stay: they make `refresh` fast.

A build is the grant's that started it: an agent's open builds under that agent's own grant, and
`openajar environments prepare` under the owner's. Its gateway token is that grant's and only fetches
(no push, whatever the repository's declaration allows). If the grant is revoked or frozen while the
build runs, the build stops within seconds: its machine is deleted and no snapshot is taken.

**What makes it stale.** The prepared machine's key is a hash of the repository, the base image,
`setup`, `refresh`, each input file's content (by its git blob id), openajar's own setup version and
the ref. Change one and the next open runs setup again and builds a new one; `start` and `port` are
not part of it. The ref: an open with no `ref` (the default branch) boots the repository's one
shared build, from the default branch. An open of any other branch, tag or commit boots only a build
of that same ref, because that build ran that ref's scripts (whoever pushed the branch chose them).
So an agent's branch never ends up in the machine of an open of `main`, and reopening a branch keeps
its speedup.

**Keeping few.** A newer build of a repository and ref replaces the older one; one no open used for
`prepared_days` (default 14) is deleted; at most `prepared_max` (default 5) are kept on the node,
the least recently used going first. The reaper does it, as it does for saves. A snapshot is billed
like a save's (`snapshot_eur_per_gb_month`); on Docker it costs nothing. `prepared_builds = false` in
`environments.toml` turns builds off.

```
openajar environments prepare github.com/me/shop [--ref <ref>] [--force]  # build it now, with its steps
openajar environments prepared                                           # each one, its age and cost; the warm pool
openajar environments prepared forget github.com/me/shop                 # delete them
```

`prepare` asks agentosd to build it (the build needs its git gateway) and follows the build's steps
until it is ready or failed; stopping the command leaves the build running.

## Warm pool

A warm machine is booted before anyone asks for it, so an open that matches it skips the rent and
the boot. It is **off by default**: nothing bills until an agent opens a machine.

```
openajar environments allow --warm 1
```

A running node takes it up at once. What is warmed: a machine booted from the image the newest open
used (a prepared machine's, or the base image), with the default size and location, up to SSH-ready,
its setup checked, apt ready and swap on. Nothing that belongs to a chat or a repository: no tunnel,
no token, no clone.

- **When.** When a chat connects (a new MCP session of a grant that may open machines), and after
  each open, to fill the pool again.
- **Limits.** A warm machine counts against the machines at once and books one hour against the
  month's budget. It always yields to an open: when the node is full and the open does not match it,
  the warm machine is deleted first.
- **How long.** An unclaimed warm machine is deleted after `warm_minutes` (default 50: Hetzner
  bills each started hour, so it goes before its first hour ends). On Docker it costs nothing, so a
  longer time is fine.
- **The claim.** An open whose image, server type and location match takes the warm machine: its VM
  record becomes the environment's (lane, purpose, creator, time limit), and the provider's labels
  follow where the provider can change them. The environment names the machine first, so a crash in
  between leaves it deleted by the environment's failed bring-up or by its time limit, never billed
  for ever. `prepared.warm` in the result says it came from the pool.

`openajar environments prepared` and the node's report show the pool: how many are warm, since when,
and what they cost so far.

## Checkpoints

`environment_save` saves the whole machine, which takes minutes on Hetzner. For a cheap undo inside
an environment ("checkpoint, try the big refactor, roll back if the tests explode") the agent has two
tools in its list:

| Call | What it does |
|---|---|
| `environment_checkpoint(note)` | for every repository under `/work` (the ones a save finds), the work as it is now: tracked and new files git does not ignore, written with a temporary index and kept as a commit on top of HEAD under `refs/agentos/checkpoints/<n>`. The agent's index, stash and branches are not touched. Seconds, no provider call, nothing billed. Returns `checkpoint` (the number), `repos` (each one's `head`, `branch` and `files_changed` from HEAD), `compare` (`git diff refs/agentos/checkpoints/<n>`, to run on the machine) and `rollback` |
| `environment_rollback(checkpoint)` | first checkpoints the work as it is (`undo`, so the rollback can be undone), then puts each repository back: the branch it was on at the checkpoint's HEAD, the index at that HEAD, the work tree to the checkpoint's files, with new files git does not ignore removed. Returns per repository `files_restored`, `files_removed`, `branch`, `head`, `was` (the branch and head before) and `commits_undone` (the undo checkpoint still has them) |

Files git ignores (`node_modules`, `.env`, build output) are never in a checkpoint and a rollback
leaves them as they are: after a dependency change, run the install again. A rollback is refused,
changing nothing, while a repository is in the middle of a merge, rebase, cherry-pick or revert
(`rollback_refused`, with the abort command), and for a checkpoint no repository has
(`checkpoint_not_found`). Files in `/work` outside any repository are not in a checkpoint; a folder
with no repository is `checkpoint_failed` (git init one, or use `environment_save`). `git diff
refs/agentos/checkpoints/<n>` compares the tracked files; a file that is new and still untracked shows
there as deleted, and `git status` lists it.

About 20 are kept per environment: a new one deletes the oldest refs. `environment()` lists them as
`checkpoints` (`id`, `note`, `at`, `auto`: the rollback an automatic one was made before, and per
repository `dir`, `head` and `files`), read by the same command that reads git, so it costs no call.
The web app's environment page has each one with what changed since, and a diff view of one against
now (docs/API.md).

Checkpoints never leave the machine. They are git refs on its disk: the git gateway refuses a push
of `refs/agentos/*` (`push_refused`), and a save's file part carries only branches and stashes. So a
snapshot save or a paused one (Morph) keeps them, and a resume from the files alone does not.

## Keeping an environment

An environment has a `keep` mode, chosen by the agent when it opens it (`environment_open(...,
keep=...)`), else `default_keep` in `environments.toml`:

| Mode | In the web app | What happens | While nobody uses it |
|---|---|---|---|
| `save` (the default) | Saves when idle | on idle, close, its time limit or laptop sleep, the whole machine is saved (a disk snapshot plus the files) and the VM deleted; autosave every 15 minutes | the snapshot, about EUR 0.012 per GB a month (cents) |
| `none` | Single use | work, push, close; the VM is deleted | nothing |
| `live` | Kept running | the VM stays on; deleted only on a final close or by you | every hour, about EUR 7.80 a month for a `cx23` |

The skill and the MCP intro tell the agent to keep the default, `save`, and to choose single use
(`keep="none"`) only for a one-off fix, and `live` only when you ask for an always-on machine. To
make single use the default again, set `default_keep = "none"` (or choose it in `openajar setup
limits`). `live` is refused (`keep_live_not_allowed`) until you allow it:

```
openajar environments allow --keep-live
```

**On Morph, a save pauses the machine.** Morph can pause a machine with its memory, so there a
save that ends the machine (idle, close, the time limit, a laptop that slept past it, and the
rescue save of a single-use one) pauses it instead of taking a snapshot and deleting it. The file
part is made as always. The save says `paused` (in `environment_close`'s `saved` and in the web
app's Saved) and what pausing costs a month (Morph bills only the storage: memory and disk, about
EUR 0.23 a month for a small machine, an estimate). `environment_open` with the title resumes the
same machine in seconds: the processes never stopped, so the dev server is still serving,
`resumed.from` is `paused`, and `resumed.services` says each one is running when its port listens
(nothing is started again). The tunnels, a new gateway token (the old one was revoked when it
closed) and the browser with its sign-ins are made again. If Morph no longer has the paused machine,
the resume falls back to the files on a fresh machine, and `resumed.missing` says so. Forget,
`keep_last` and `saved_days` delete the paused machine with its save. `environment_save` (which
keeps working) still takes a snapshot, and `pause_saves = false` in `environments.toml` turns
pausing off.

**Coming back.** `environment_open` with the same title, from any chat, recreates the VM from the
save, and the job continues: the tools, the Docker volumes, `node_modules`, the browser's sign-ins
and the dev server come back. `environment_open(repo=...)` without a title finds the one
environment of that repository. Its result has `resumed`: where it came from (`snapshot`, or
`files` when only the files were saved), the branch and head, the files and commits restored, a
fresh `expires_at`, `signins`, what is `missing`, and the note the last agent left. The note is the
agent's own text: the next agent reads it as data, never as instructions. To bring one back, say
to your agent: "continue the shop in openajar".

**The services.** Each `terminal_job_start` command that ran at the save (with the port its command
names) is started again, and so is each server that listened on the machine at the save from a
folder under `/work`, though the node no longer knew it as a job (a node that restarted while a dev
server ran has lost its job; a server started over your own ssh). The save reads those from the
machine itself (its listening TCP ports, `ss -ltnp`, and each one's process, folder and parents
from `/proc`): the command is the one that was started (`npm run dev` in `/work/shop`, not the
`node .../vite` it runs), and nothing that was not listening is recorded. The node waits up to 45
seconds in all for those ports to listen.
`resumed.services` says for each one `running` and `listening`; one that did not come back has
`why` and a ready `restart` call (`terminal_job_start(cmd="pnpm dev --port 5321", cwd="/work/shop")`),
and when none came back `running_jobs_lost` lists them, so the agent never trips over a dev server
that quietly vanished.

**Swap and process memory.** A save never holds process memory. zram swap is memory, so it is not
on the disk at all. A swapfile (a kernel without zram) is switched off, deleted and its blocks
trimmed (`fstrim`) before each snapshot, and made again when the machine works on after it, so a
save holds neither what was swapped out nor gigabytes of swap. A machine from a save makes its swap
at boot like a new one.

**Sign-ins.** A save of a kept environment holds its browser's storage state (cookies and each
site's local storage), read from the environment's own browser only, never an account browser
or the vault. It is `browser.json` in the save's files, sealed with the saves key like the rest;
the manifest has only the cookie count. On resume the browser opens with it: on the VM its new
context starts with that state (and every later start of that browser gets the newest one, so a
browser closed while idle keeps its sign-ins); on the node the cookies are added and each site's
local storage is set where it has none. `environment_open(signins=false)` opts one environment
out (its saves remember it); `save_signins = false` in `environments.toml` turns it off for the
node.

**Stopping and finishing.** One call each:

- Stopping for the day: `environment_close(note="what is next")` on a kept one saves with that
  note (it comes back in `resumed.note` and `environment()`), then deletes the VM. Its result has
  `saved` and `pause`, what the wait costs and how long a resume takes ("saved: about EUR 0.02 a
  month, resumes in about a minute"). `environment_save(note=...)` saves without closing, before
  a risky step.
- On one kept running, `environment_close()` only lets go of it, unless `final=true`.
- Done for good: `environment_close(forget=true)` deletes the VM and every save of its title (with
  unpushed work only together with `discard=true`).
- `environment_close(discard=true)` makes no save, and its `saves` says so: `made` is null and
  `left` lists the earlier saves that stay.

**Saved, in the web app.** Under Computers, on the Environments tab, Saved lists each save: the title, repository and
branch, when it was saved, the snapshot's size and what it costs a month, and the agent's note
(shown as text). A save the node deletes soon says so. **Forget** (the owner alone) deletes the
snapshot and the files, after asking; an agent that opens that title again starts on a fresh
machine. Agents forget finished work themselves with `environment_forget(title)`.
`environment()` always lists the agent's `open` and `saved` ones.

**You resume one yourself.** Without a chat: **Resume** on a save in the web app's Saved list, `openajar
environments resume <title or save id>` on the node, or the Resume button on the saved environment's
live-view link. It resumes the save as `environment_open` with its title does (a disk snapshot, or on
Morph the paused machine, in seconds), within the same limits (`environment_limit` and
`environment_full` name the fix), and no chat holds it, as a kept machine its chat let go: you watch
it in its live view at once (the CLI and the API give the link), and the next agent that opens the
title takes it with its `resumed` record (`opened: "resumed"`). Its idle rule and time limit are any
open environment's: while you watch it stays, and unused and unwatched it is saved again after
`idle_minutes`. An agent's calls with the `on` of the closed one are refused with when it was saved
and how to come back (`environment_open(title="...")`); openajar never resumes one on an agent's behalf.

**A saved environment's live-view link.** Once it is saved and its machine closed, the agent's
`links.browser` shows a small page instead of an error: to you, signed in to the panel in that
browser (or with your own link), its title, when it was saved, the agent's note (as text) and Resume,
which then shows the live view once it is ready; to anyone else only that it is saved. A link to one
that is open again (you or a chat resumed it) says so, and gives you its live view.

**When a save fails.** `save_failed`: the VM is not deleted, so nothing is lost; the agent tries
once more, then tells you. `restore_failed`: the save is kept; tell the agent to try again later or
open a fresh one under another title. `save_limit`: forget saves you no longer need.

## Costs and the reaper

- The price is the provider's hourly price for the server type, shown in the audit log's record
  of the open, on the environment page and in `environment()`. Hetzner bills each started hour. Docker costs nothing.
- `cost_eur` so far is started hours times the hourly price. The month's total counts against
  `--monthly-eur` and the node's `monthly_budget_eur`.
- Every environment has a time limit (`hours` on open, at most `--max-hours`, 8 by default). The
  environment page extends it, up to the same limit.
- **The reaper** closes an environment that the agent forgot: idle (no call, no running command,
  nobody watching it and no input from you in its live view; in local mode also no use of the
  agent's own `ssh openajar-<id>`, docs/LOCAL.md) for `idle_minutes` (default 60), or past its time
  limit. Watching is a live view of its browser open (the node's link or the web app's) or a
  connection through an `environment_port` link (a dev server's page open on your computer): while
  one is open the environment is in use, and the idle time counts from when the last one closed. It
  is noted on the record at most once a minute, and `environment()` and the web app say `watched`
  (since when, and how many). The web app polling the environment's page is not watching and never
  counts. One that saves when idle is saved first; one kept running is left alone. A single-use one
  with unsaved work is never deleted for being idle: it waits to its time limit, gets a rescue
  save, and you get a notification. Unsaved work is anything in a repository under `/work` (the cloned
  one or any other) that is not on a remote: changed or new files, commits on any local branch
  that no remote has, and stashes. `environment_close` refuses for the same reasons.
- A daemon restart never deletes environments; the reaper picks up where it was. A server the node
  made but no longer knows about is deleted as an orphan.
- **A link that drops** (the SSH connection that carries an environment's tunnels: the browser, its live
  view, the git gateway and the network's ports) comes back by itself. The drop is noticed at once (its
  ssh ends), and the node first lets the VM firewall follow this computer's address when it moved (local
  mode: docs/LOCAL.md "The VM firewall"), then starts the tunnel again, with tries after 3, 10, 20 and 30
  seconds and then on the reaper's pass each minute. The dead connection is stopped first, so this
  computer's ports and sockets are free for the new one, which takes the same ports. When the machine
  still holds a reverse port of the old connection (its sshd has not seen it end, as after this
  computer's address changed), the node ends the sshd sessions that listen on its own reverse ports there
  (3129 to 3132) and tries once more. A browser that ran is started again with the link, live view
  included, with the sign-ins it last kept; open tabs do not come back. Meanwhile a call that needs the
  machine runs nothing and gets `environment_not_ready` (what happened, and that the node is on it), and
  `environment()` waits up to a minute for the link and shows `link`: `down` (since when, why, the tries)
  or, for ten minutes after, `back`.
- **A machine that stops** (its container killed, its server powered off or deleted outside openajar, its
  host lost) is found once and said everywhere. A command whose ssh cannot reach the machine, the reaper
  finding its tunnel down, `environment()` finding git on it silent, and `environment_open` with its
  title each ask the provider (`docker inspect`, Hetzner's API), twice, two seconds apart. Only the
  provider decides: one failed ssh call is no reason, a network blip or a busy machine looks the same,
  and a provider that does not answer decides nothing. A machine it says runs is not asked about again
  for ten seconds. These checks never run on the machine, so they are never the agent's use of it.
  From then on the environment shows `state: "gone"` and `machine_gone` (when, why, the next step);
  every call that needs the machine fails with `machine_gone`, saying whether a save exists to resume
  from; a job that ran on it ends `lost` with the same words; `environment_close` ends it without a
  save (there is nothing to save from) and names the saves left; and `environment_open` with the same
  title ends it and starts a new machine on that title, from the newest save when there is one, so the
  agent needs no second title. One nobody comes back to is ended after `idle_minutes`, and you get a
  notification when it is found.

## What is on the VM, and what is not

On it:

- Ubuntu 24.04 with git, curl, build tools, Node 22 with npm and pnpm (through corepack, with
  `COREPACK_ENABLE_DOWNLOAD_PROMPT=0`, so a repository's `packageManager` version downloads without a
  question), and Python with uv (or the `dev` snapshot, which also has Docker). A snapshot made
  before pnpm was set up gets it when an environment comes up.
- `/work/<name>`: the repository, on the branch or `ref` asked for.
- Swap, made on every boot (`machine.swap` in `environment()` says what it got): the machine's
  memory, at most 8 GB, compressed in memory (zram) where the kernel has the zram module, as
  Ubuntu's does. A kernel without it gets a swapfile, `/agentos.swap`, of at most a quarter of the
  free disk (none when that is under 1 GB) with swappiness 10, so it is used under pressure only.
  Nothing goes in `/etc/fstab` or the image. A container (the docker provider) gets none.
- No updates in the middle of a session. Ubuntu's unattended-upgrades and its apt timers are off
  (`/etc/apt/apt.conf.d/99agentos-updates`), and needrestart only lists what it would restart
  (`/etc/needrestart/conf.d/90-agentos.conf`). Why: on 2026-11-16 a VM's unattended-upgrades ran while
  an agent worked, reloaded and re-executed systemd, and restarted networking, DNS, the clock and the
  journal under it (and the reloads made systemd print an old OOM kill again, which read as a new one).
  The security updates are applied instead where nobody works yet: when a fresh Ubuntu VM is set up
  (before the machine is handed over; on a fresh VM Ubuntu's own run held the package lock at that
  moment anyway) and when the `dev` snapshot is built, so a machine from the snapshot is as patched as
  the day it was built: rebuild it now and then (`openajar vm image build dev`, monthly is a good
  rhythm). A `keep: "live"` machine, which lives for weeks, gets them at a quiet moment: no call and no
  job for 30 minutes (`upgrade_quiet_minutes` in `environments.toml`), at most once a day, restarting
  no service. `machine.updates` in `environment()` and the panel says when they were applied, that they
  wait (a machine that was never quiet for a week, say because a dev server always runs), and when a
  new kernel waits for a restart. A save waits for an upgrade that runs before it snapshots the disk.
  Machines of a single session (`keep: "none"`) and machines back from a save run what they were built
  with.
- A git config that sends declared hosts to the gateway (`127.0.0.1:3129` on the VM is the node's
  gateway, through the VM's SSH connection).
- With a closed network (`limited`, `none`): the proxy variables in `/etc/environment`, an apt proxy
  file (`/etc/apt/apt.conf.d/95agentos-proxy`) and Docker's drop-in. With bound secrets: their
  placeholders in `/etc/environment`, the environment CA's certificate (never its key), the bound
  hosts in `/etc/hosts` and the relay (`/usr/local/lib/agentos-env/secret-relay.py`). See
  [Network](#network).
- Whatever the agent installs. It is the agent's machine, by its title.
- In local mode only: the public half of the environment's own ssh key in root's authorized_keys,
  restricted to a terminal and commands (no forwarding), so `ssh openajar-<id>` works from the agent's
  shell (docs/LOCAL.md). A server node gives none: its agent is on another computer.

Not on it:

- openajar itself, a node token, a grant, the vault, any key or token, any account.
- The browser, with the default `browser = "node"`. It runs on the node, with its traffic carried
  through the VM (a SOCKS tunnel over SSH, DNS included), so `localhost` is the VM and sites see the
  VM's address. With `browser = "vm"` it runs on the VM (above). Either way it has no account and no
  saved login but the logins you bound to it ([Signing in](#signing-in)), whose sessions stay in it and
  its saves; it is deleted with the environment. Its screen is 1920x1080, so pages show their desktop
  layout: on the node its window fills it, on the VM's desktop it is a 1776x890 window (an account
  browser's window is the size its identity drew).

## Limits

- One environment per title for an agent. The node's `max_live` and budget cap how many are open
  in all, and `max_live_kept` how many are kept running.
- Nothing reaches the VM from the internet except SSH from the node (Hetzner: a firewall per node).
  Its ports are seen through the environment's browser (the live view).
- A client that sends no `mcp-session-id` (MCP 2026-07-28, stdio) is told apart only by the
  titles its chats use: two chats that use one title share that computer, and with several open
  their calls name one with `on`. `environment()`'s calls show which client made each.
- Files go in through git, `environment_upload` (a tarball of a local checkout, at most 512 MB), or
  `terminal_upload` and a job's `sync` (above); they come out through git, or
  `environment_download(path)` (a one-use link to a file or folder in `/work`).
- The node checks where a push goes (declared repository, protected branch), not what the commits
  contain. Review a branch before you merge it.
- On the dev node (`openajar dev up`), account browsers are simulated. The environment's machine,
  terminal, git and studio's checks are real.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| `environment_limit` | `max_live`, the longest life or the month's budget is reached; the message says which | close one (`openajar environments list`), or raise that limit with `openajar environments allow` |
| `network_not_allowed` | the agent asked for a wider network than the node's, the provider cannot close a machine, `none` cannot clone an undeclared repository, or a closed Hetzner machine has no `dev` image | the message names the command: `openajar environments allow --network ...`, `openajar environments repo add ...` or `openajar vm image build dev` |
| a 403 that says a host "is not on this environment's network list" | the environment's network is `limited` or `none`, and that host is not allowed | `openajar environments allow --host <host>`; new environments get it |
| a bound secret's API answers 401 | the request carried the placeholder: a body, plain HTTP, or another host than the bound one (`network.blocked` says which) | send the key in a header or the query, over HTTPS, to the bound host |
| `environment_failed` | the VM did not boot or answer SSH in time; it was deleted | `openajar environments setup`; on Hetzner, `openajar vm types` (sold out in the location?) |
| `state: "starting"` for minutes | first boot without the `dev` image installs the tools | wait, or build the image (step 2) |
| `no_environment` | a call without `on` in a chat with no environment | the agent calls `environment_open(title=...)` first, or names an account browser with `on` |
| `which_environment` | a call without the machine's handle: no `on` from a client with no session id, or a title or an id in `on` | the agent passes the `on` its environment_open gave, or opens its own |
| `environment_already_open` | the title names an open environment with another repository | the agent picks another title |
| `environment_in_use` | another chat of the agent holds the live machine with this title | the agent opens its own with another title; `take_over=true` once the holder has been quiet for `take_over_minutes` |
| `environment_not_ready` | the environment is starting, or its link is down (a restart, a dropped connection, this computer's address moved) and the node is bringing it back | wait a minute; `openajar environments show ID`, and `openajar logs --level warn --since 10m` if it does not come back |
| `machine_gone`, or `state: "gone"` | the machine stopped for good: the provider says its container or server is off or no longer exists ("Costs and the reaper") | the agent opens the same title again and gets a new machine; if it happens often, `docker ps -a` or the Hetzner console, and `openajar logs --level warn --since 30m` |
| `unsaved_work` on close | uncommitted changes or commits not pushed; "mode change only" names files whose content is as committed (a `chmod +x`) | commit and push, or close with `discard=true` if the work may go |
| `approval_pending` with `kind: "repo_connect"` | the repository is private and not connected, or its key or token was refused; nothing was rented | open the card's `review_url` and paste a token (see "Connect a repository"), or at the node `openajar environments repo add github.com/me/shop --token` |
| the token page says it did not work | the token did not work, and nothing was kept; the page says why | do what the page says (table in "Connect a repository"), then paste it again on the same page |
| `repo_not_connected` | a node without approvals, or the clone on the machine still failed (its machine was deleted) | `openajar environments repo add github.com/me/shop` (the message names the repository) |
| a push is refused | the repository is not declared for `env`, or it is fetch only | `openajar environments repo add github.com/me/shop` |
| a push is refused with `push_refused` | it would force-push or delete the default branch (or a protected one), force-push or delete a branch the environment did not create, or move a tag | push a new branch and open a pull request |
| `localhost:3000` does not load in the browser | nothing listens on that port on the VM | `environment()` lists the ports that listen; start the dev server with `terminal_job_start` |
| `studio_blank_page` on a dev server | nothing rendered within the wait (15 s on a dev server) | a slow app: `ui_check(..., wait_for="main h1")`; else the app failed to render (the message quotes its console) |
| a proof link answers "not valid or has expired" | the link is 15 minutes old | `environment()` lists the proof with a fresh link; the web app's page shows it too |

Every error code and its fix is in [ERRORS.md](ERRORS.md).
