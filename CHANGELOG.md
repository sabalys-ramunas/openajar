# Changelog

All notable changes to openajar are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version may break things).

## [Unreleased]

### Added

- Your own captcha-solving service for environments' browsers, off until you add one: `openajar solver add
  <name> <provider> [key] --cap <USD a month> --sites <hosts>`, with `solver list`, `solver test`,
  `solver remove` and `solver log`. Built in: CapSolver, 2Captcha and Anti-Captcha by their documented task
  APIs (Turnstile and reCAPTCHA v2, proxyless task types only); a `command` or `webhook` hook plugs in any
  other service or your own script, hCaptcha included. The key stays in `secrets/solvers.json` (600) on this
  computer and is in no tool result, log, trace or record. openajar ships no key and shares none. A solving
  service may be against a site's terms; the choice and the cost are yours.
- `browser_challenge` (toolset `browser`): one explicit call that gets an environment's browser past a site's
  check in this order: wait for it to pass by itself, the browser's own click, your solver, then you. When
  nothing got it past, or the call ends early on a page with a check (its deadline, a cancel, an error), the
  browser is held for you and the agent waits with `browser_wait_handback`. `ended_by` says which step ended
  it and `steps` what each did. It refuses an account's browser (`check_for_human`). Its solver step never runs
  for Cloudflare's full-page check, for reCAPTCHA Enterprise, for a widget inside another site's frame, for a
  site off your list or past your month's cap, and it refuses a page it recognises as a sign-up, login or
  payment page (checked again before the token goes in). The cap holds through a reservation written to the
  record before anything is sent, and the record fails closed: one that cannot be read or written sends
  nothing. Every try, refused ones too, is in the record with its cost.

### Changed

- In an environment's browser, when a service of yours could take the check now (it covers the site, takes the
  widget, the page passes the guards and the cap has room), the snapshot's line for it names
  `browser_challenge`, and an hCaptcha or reCAPTCHA image puzzle is not held at once, so the agent can call it.
  Otherwise nothing changes.

## [0.6.1] - 2026-11-25

This version adds one reserved label for `browser_proxies` (`this-computer`) and changes what an agent
sees on a site's "are you human" check in an environment's browser: the checkbox now has a ref the agent
may click once. An account's browser is held for any challenge, as before.

### Added

- An environment's browser can exit through this computer's own connection, with no proxy:
  `openajar proxy this-computer` puts the reserved label `this-computer` in `browser_proxies` and prints the
  address this computer goes out from now. With openajar local that is your home address, free, with a real
  household's reputation. It stands alone or takes turns with proxies, and a save and a resume keep it like
  any label. The node's forwarder connects to the sites itself and reaches public addresses only: never your
  home network, your router or this computer's own services (local names, private, loopback, link-local and
  CGNAT addresses, also inside IPv4-mapped or NAT64 IPv6, this computer's own public addresses and the
  household's IPv6 prefix), and never port 25. Before each launch the node asks ipinfo.io straight from this
  computer, over IPv4 first, and the forwarder keeps to that address family, so sites see the address the
  browser claims. When the check fails the browser does not start. `openajar proxy test this-computer` makes
  the same check, `openajar proxy this-computer --off` (or `proxy remove this-computer`) turns it off, and
  `proxy add this-computer` is refused. The README says what it costs: your upload speed, a computer that has
  to be on, and one address that every environment shares and that counts for your household.
- In an environment's browser, a site's "are you human" checkbox (Cloudflare's, hCaptcha's, reCAPTCHA's) shows
  at the top of the snapshot as a checkbox with a ref like `c1`, and `browser_act` clicks it once with real
  input on the machine's screen, then says what came of it: passed, an image puzzle for your human (never
  solved), or not passed. The checkbox sits in another site's frame, often in a closed shadow root, so the
  outline had no node for it, and on Cloudflare's challenge page Playwright's own clicks never reach it.
  openajar finds it in a picture of the check. The browser is not held for such a check; an image puzzle
  holds it for you as before, and an account's browser is held for any challenge, as before.
- The keyboard helper on an environment's machine starts with every browser, an English one too, and is also
  the hand for that click.

### Changed

- `openajar proxy test` also asks a second geolocation source (geojs.io, which the launch's check already
  uses) and says plainly when it places the exit in another country than ipinfo.io does, since sites use
  either kind of database and may then treat the address as the other country's. It also says when the
  second request left from another address (a rotating proxy).
- On Cloudflare's check ("Just a moment...", "Verifying..."), `browser_navigate` and a `browser_act` that
  lands on it wait up to 20 seconds for the check to let the browser through by itself, as a person waits,
  before they report the page's signal. Read at once, a check that was about to pass held the browser for a
  person with nothing left to do (Glassdoor's cleared by itself in about 15 seconds on a residential address).
- A Turnstile, hCaptcha or reCAPTCHA widget that has passed (its token is in its form field, and it shows
  "Success!") is no longer a challenge. A form with a widget that passed by itself held the browser for a person.
- An environment's browser that claims Linux reports its whole screen as the work area, as Firefox itself
  does on Linux. Some pins claimed 1032 or 1050 px of a 1080 screen, which no Firefox on Linux shows.

### Fixed

- With openajar local, the VM firewall follows this computer's address while it is awake, not only at start
  and after a sleep. When the address moved (a hotspot, a VPN, the provider's own NAT: three times in an hour
  in one test), every environment's link went down for good, since the firewall still named the old address.
  Now a tunnel that drops is the signal: the node looks up the address, moves the firewall to it (the old one
  goes in the same step), and brings the tunnel and the browser back, with tries after 3, 10, 20 and 30
  seconds. A look once a minute while VMs run catches a move nothing noticed. A move needs a second lookup
  service not to disagree, and an answer that is a private or reserved address is never used. During the
  gap a call runs nothing and gets `environment_not_ready` saying the link is coming back, and
  `environment()` waits for it and shows `link`.
- An environment's browser on its machine with no proxy claims the machine's own place: the timezone of
  where ipinfo.io places the machine's address (else of the server's Hetzner location) and that address for
  WebRTC, every launch, never pinned. It claimed UTC from a datacenter in Germany, which fingerprint pages
  flag ("privacy settings"). Its languages stay `en-US`. When the place cannot be learned, the browser
  starts as before and the log says so.
- A click on an element something covers now says what covers it, so the agent can close it in one step:
  the covering element's ref (or that of its closest ancestor with one), else the dialog or fixed bar it
  sits in with the refs of the buttons in it, from the last snapshot the agent saw, as in
  `another element covers the target: dialog "Cookie notice", which has button "Accept" [ref=e5]`. The
  error's `details` carry the same as `covered_by`. Before, the message only guessed "a banner, dialog or
  sticky header?", and cookie boxes, sign-in promos and location choosers stopped agents on 5 sites in one
  test.
- A tunnel to an environment's machine that dropped now comes back. The node started a new one on the
  dead one's ports while the dead one still held them, so every try failed with "the tunnel's port ... is
  taken" and the environment's browser stayed down. The dead tunnel is now stopped first (its ports, its
  sockets, the live view's included, and any of its ssh still running), and the new one takes the same
  ports and carries the same forwards, the browser proxy's `127.0.0.1:3132` included. When the machine
  still holds a reverse port of the old connection, the node ends that session there and tries once more.

## [0.6.0] - 2026-11-24

This version adds settings (`browser_proxies` and `browser_language` in `environments.toml`) and changes
what an environment's browser on its machine shows a site. Nothing changes for a browser with no proxy
except that it is now one device from launch to launch.

### Added

- An environment's browser can exit through your own static residential proxy, so sites see a home
  address instead of a datacenter's. `openajar proxy add lt1 host:port:user:pass` puts it in
  `browser_proxies` in `environments.toml` (with openajar local by default, on a server node with
  `--environments`), and each environment opened from then on takes the proxy the fewest open ones use. A
  save keeps it, so a resume keeps its address. The credentials stay on this computer: a forwarder here
  chains to the proxy and reaches the machine's browser as `127.0.0.1:3132`; apt, npm and the shell keep
  their own way out. Before each launch the node checks the proxy, and the browser does not start without
  it. The browser's timezone, languages and WebRTC address follow the exit. `environment()` shows
  `network.browser_proxy` (label, exit IP, country). Browsers that run on the node (`browser = "node"`) are
  not covered yet.
- An environment's browser on its machine is the same device every time it opens: the fingerprint its first
  launch draws is read back from the running browser, kept on the node with the environment's data, and given
  to every later launch, so a site sees the same cookies and address come from the same computer. A save
  carries it, so a resume (in any chat, on a new machine) is that device too; a new environment draws its own.
  The timezone, WebRTC address and languages still follow each launch (the proxy's exit), and nothing
  of the proxy is in the pin. A newer Firefox on the machine moves only the pin's version. A machine whose browser stack is at other
  versions than openajar pins (an older image) gets it installed again when the environment comes up.
- A proxied browser claims the exit's place on every launch: its timezone, WebRTC's address and its
  languages. It hands out no position, and its geolocation permission is `prompt`, as in an untouched Firefox. With `browser_language = "exit"` in
  `environments.toml` (the default) `navigator.languages`, the `Accept-Language` header (with Firefox's own
  q values), Intl's number and date formats and the browser's own language are the exit country's, then
  English (`lt-LT, lt, en-US, en` at a Lithuanian address); `browser_language = "en-US"` keeps a fixed
  language. A thin answer from ipinfo.io is filled from a second service through the proxy, else from the
  country, never from the datacenter. `openajar proxy test` prints what the browser claims through each proxy.
- `openajar proxy remove <label>` and `openajar proxy test [label]` (exit IP, country and timezone through
  each proxy; exits 1 when one fails). `openajar proxy list` marks the proxies environments use.
- A new environment with the title or repository of an earlier one takes the proxy that one had, when no
  open environment uses it, so a computer comes back from the same address without a save too. A save keeps
  the exit IP, and a resume says so in `missing` when the address has moved since.
- Letters a US keyboard does not have (ą, ė, ö, ñ and the like) reach a page on the machine's browser as
  real key presses: a keyboard helper on the machine, with the layout of the browser's language. They
  arrived as text with no key event before.
- README: what the browser is (Camoufox), how to give it a static residential proxy and what one costs,
  and the checks and sites it was run against on a real machine, with the ones it does not pass.

### Changed

- Typing, clicks and wheel notches reach a browser on a far machine with the timing the plan drew. Each
  key, button and wheel call is a round trip, and a key planned for 98 ms was held 204 ms as the page saw
  it on a machine 36 ms away. A browser beside the node (a lane on a server node) is timed as before.
- The browser on an environment's machine is hardened the way a node's own lanes are, at the launch that
  draws its device: a GPU name whose limits the machine's software renderer really has, the storage quota
  most Firefox users show, the machine's own core count, and on the desktop a window that says where it
  really is. Its geolocation permission is `prompt` and it hands out no position, proxied or not.
- A proxied browser makes no request of its own past the proxy: no fallback to a direct connection, no
  connectivity or captive-portal check, no DNS over HTTPS, no telemetry. Plain `http://` pages go through a
  tunnel of the proxy (a CONNECT to port 80), so the credentials never ride in a request a page chose, and
  the forwarder's errors no longer name the proxy's address.

- The browser on an environment's machine claims Linux when the node names no OS (openajar local), instead
  of a random OS each launch.

## [0.5.4] - 2026-11-22

From the second field report of an agent's run in an environment (2026-11-21).

### Added

- `openajar environments env push <id> [files]`: an app's local env files on an environment's machine. Plain
  settings are copied as written, API keys are bound to their host as placeholders (the machine's file gets
  `KEY=${KEY}`), and other secrets are never sent. One table shows each key, its class and what happens before
  you approve once; `--dry-run` shows only the table. `.openajar/environment.toml` takes `env_files = [...]`,
  the files it sends by default. docs/ENVIRONMENTS.md lists which secrets a placeholder can carry (headers and
  the query over HTTPS to the bound host) and which it cannot (body values, values used on the machine such as
  webhook signing secrets, database passwords, secrets inside a URL).
- `environment_port(ports=[5173, 3000], same_numbers=true)` forwards several ports with the VM's own numbers,
  so a page that calls its API on another localhost port reaches the VM. A number taken on this computer moves
  to a free port and `why` says what that breaks. A one-port forward lists the VM's other ports
  (`also_listening`).
- `environment_app_restart` (in the terminals toolset) stops the app's job and its processes and runs `start`
  again from `.openajar/environment.toml` as it is now, reporting `app` as the open does.
- `terminal_job_read` reads the end of a log with a negative offset: `stdout_offset=-4000` is the last 4000
  bytes (at most `max_bytes`), from a line's start.

### Changed

- `terminal_job_read(until_port=...)` that runs out while the job still runs no longer hides a crashed child
  process (turbo stayed up after its API died): stdout and stderr are the start and the end of what it printed
  since your offsets, and the note says the job still runs, whether it printed anything during the wait, and
  where to look. A process the kernel killed for memory ends the wait at once and is named.
- `terminal_exec` and jobs run under bash when the machine has it (else sh), so bash syntax such as
  `${v:0:15}`, arrays and `[[ ]]` works. The node's own scripts keep sh.
- `terminal_job_read`'s text output leaves out the colour and cursor codes dev servers print (`base64_output`
  keeps the raw bytes).
- A size means any server type with that much CPU and memory: when one family has no room, the open tries cx,
  cpx and ccx types, cheapest first, within the monthly budget, and `machine.fell_back` says what the machine
  costs next to the plan. An approval's price line names the most it can cost when only a dearer type has room.
- `openajar call` and `openajar tools` are in the top-level help, the environment skill and the README: any
  shell reaches the tools under the agents' grant when a session has not loaded them.
- `environment_close`'s unsaved-work refusal says "mode change only" for a file whose only change is its mode
  (a `chmod +x`), and says plainly when that is all there is.

### Fixed

- `openajar connect` registers the node in every Claude Code profile on the computer (`~/.claude.json`, the
  folder `CLAUDE_CONFIG_DIR` names, and each `~/.claude*/` that holds a `.claude.json`), copies the skills into
  each, and names the profiles it wrote. An entry from before the rename whose address on this computer no
  longer answers is replaced too. It says that open Claude Code sessions need a restart to see the tools.
  `openajar status --check` names a profile whose entry is missing or points at an address that does not
  answer, with the command that fixes it.
- A capacity error never suggests a size or type that just had no room: it names a smaller size that is
  available now, or one not tried yet.
- A server type the location does not sell (cpx41 in nbg1) is refused before Hetzner is asked, with where it
  is sold and what is sold here with as much.
- `bigger` skips a type that just had no room, and says when an entry costs less an hour than the running
  machine.

## [0.5.3] - 2026-11-21

### Added

- openajar.com: the project's website (site/, a Cloudflare Worker with static assets), with the short install line
  `curl -fsSL https://openajar.com/install.sh | sh` (a redirect to scripts/install.sh on main).

- `openajar mcp`: openajar as a stdio MCP server, for any MCP client that starts a command (Cursor, Claude
  Desktop, the MCP marketplaces). It bridges to openajar on this computer, as `openajar mcp-bridge local`
  does for Codex, and starts the local service first when it is stopped. On a computer where openajar was
  never set up, it still answers: its instructions and its one tool tell the agent what to run (the install
  line, `openajar setup`, and that VMs need a Hetzner Cloud token or Docker), and it says the same on stderr.
- openajar is ready for three registries: PyPI (`uvx openajar mcp`; the sdist now carries only the package),
  npm (`npx -y openajar`, a dependency-free launcher in integrations/npm that runs the installed CLI, or
  explains what to install when there is none) and the MCP Registry as `com.openajar/openajar` (server.json).
  glama.json names the maintainer for glama.ai. The publish workflow puts each release on all three by
  trusted publishing, after scripts/check_release_versions.py checks that every copy has the tag's version.

### Changed

- The unit tests no longer pass or fail on the runner's speed: they wait for what they expect for up to
  10 seconds instead of a fixed sleep, a speed check scales its bound on the macOS runners
  (OPENAJAR_TEST_SLOW in CI), and the store lock check fails only on a loop really stuck on the lock.
  docs/DEVELOPING.md says how a new test waits and bounds a speed check.

## [0.5.2] - 2026-11-21

### Fixed

- The web app names an agent the way the desktop does: Claude Code, not claude-code 2.1.328. One rule on the
  node and in the web app (openajar.core.agent_names) reads the grant and the MCP client as the product's name,
  with the computer as words and the raw values under Technical details.
- Updating with the install line restarts a running local service, so it runs the new version on the same
  home and port (`openajar local restart`, launchd or systemd). Before, the old service kept running the old
  code until it was stopped and started by hand. It says so in one line, and when environments are open, what
  the restart interrupted: their machines and jobs keep running, but an open browser there opens again on the
  next browser call, and a recording, a tool call in flight or an unused upload or download link ends.
- A stopped machine's page no longer shows the Network level (Full, Limited, None), which did nothing there:
  one line says what it was.
- The browser fingerprint probe gives `navigator.mediaDevices.enumerateDevices()` 20 seconds instead of 8. On a
  computer with no sound server (a CI runner image) its first look at the audio devices waited past 8 seconds
  and the fingerprint test failed; the values it reads are the same.

## [0.5.1] - 2026-11-21

An agent's environment whose machine dies is now noticed and replaced, typing on its desktop lands in the
app the agent opened, and the web app has a simpler navigation and the rest of its screens in the Dawn design.

### Added

- **Four more tasks in the eval suite**, ported from evals/ (t12 to t15: the machine dies in the middle of a
  build, a file from the person's folder up to the machine and back, a report that takes seconds and a confirm
  dialog, a price past a catalogue's first page), a harness step that runs beside the agent (t12's
  `docker kill`), the person's own folder for a task, and a paid run on the person's own `claude` sign-in. docs/EVALS.md has the baselines and
  what 0.5.1 changed, and `bench/evals.jsonl` keeps the runs.
- **The web app shows an environment whose machine stopped.** It leads the Open list and Home's Working now
  with a red Machine stopped chip on the dimmed desktop; its page says when it stopped and why, and that the
  agent starts a new machine when you say "continue <title> in openajar", and Close is what is left (Extend
  goes). The list's environments carry `machine_gone` (when, why) for it.

### Changed

- The environment skill, the server's instructions and the open's `next` say the defaults in so many
  words: when the task is done and nobody asked to keep the machine open, `environment_close()` is the
  last call, whatever the `keep`; files are read and changed with the file tools, never `cat` or `sed`
  in a command; and `machine_gone` means opening the same title again.
- `screen_act` with `type` or `key` says where the keys went (`keys_to`, the window that had the keyboard),
  and takes `window` to bring that window to the front first.
- **A simpler navigation in the web app: 13 places become 10.** Home, Needs you, Computers and What happened
  come first; then Your business (Accounts, Jobs that repeat, Brand), For developers (Runs and checks, Site APIs,
  each hidden until first used) and Settings. Computers holds Environments and Machines as two tabs, Jobs that
  repeat holds Routines and Email, Runs and checks holds Runs and Checks, and History is now called What
  happened. Every old address still opens the same page, and the phone's tab bar is Home, Needs you, Computers,
  What happened and More.
- **A setting that saves on its own now says what changed in a toast with Undo** ("Quiet hours turned on"),
  instead of Saved in the card's footer. Undo puts the old value back the same way it was saved; it never loosens
  what an agent may do without asking, and what cannot be undone keeps its confirm dialog.
- **Accounts and one account in the Dawn design.** Each workspace is a heading with one banner for what needs a
  look, and its accounts are cards with the site's tile, the handle, a state chip, what it did last and Ask me
  first. An account's page opens on the night band with the site's tile, its state and the live screen as a
  stage with the control pill, and glass panels for who is using it, what happened and whether it is safe. The
  workspace page, the login queue, the vault, Workers, Add an AI service (one page) and Proof follow the same
  design, and an address with no such account says so in plain words.
- **The detail pages in the Dawn design:** one run, one check, one machine, one routine, a new routine, Email,
  one Site API, memory and standing approvals each open with their kind's tile, the title, the state as a chip
  and one line of facts, and keep their skeletons in the page's shape while they load.
- **The phone's More opens a sheet from the bottom** with the places as tiles in the sidebar's groups and the
  machines' line and the organisation at its end, above the tab bar, which stays in reach.
- What an agent did on an environment's desktop reads as what its hands did (typed, pressed keys, brought a
  window to the front), and a `screen_act` type or key with `window` names that window in the call's line.

### Fixed

- **An app opened on the desktop keeps the keyboard.** The environment's browser starts in the background
  when it opens, and its first window could come up after an app the agent had just opened with
  `screen_open` (LXTerminal, say) and take the focus, so the agent's `screen_act` typing went into the
  browser. A browser window that comes up now takes no focus by itself: the app that had it stays in front
  until the agent uses the browser, a person clicks it, or the dock's Browser is pressed, and a browser on a
  desktop where nothing has focus still gets it. `screen_open` gives the app's window the keyboard before it
  returns.

- **A machine that stops is noticed, and said once, everywhere.** When an environment's container or server
  stops for good (killed, powered off, deleted outside openajar), the node finds out from the provider
  (`docker inspect`, Hetzner's API, asked twice; never from one failed ssh call, which a network blip also
  gives) and then: every call that needs the machine fails with the new error `machine_gone`, whose
  message says when it stopped, why, and whether a save exists to resume from; `environment()` shows
  `state: "gone"` and `machine_gone`; a job on it ends `lost` with "the job ended with its machine", not
  "could not reach the machine over ssh to start it"; `terminal_exec` no longer answers ok with ssh's exit
  255; `environment_close` ends it without asking for `discard=true`; and `environment_open` with the same
  title no longer hands back the dead machine as `attached`, `ready`: it ends it and starts a new one on that
  title (from the newest save when there is one), with `replaced` saying what it replaced. The checks never
  run on the machine, so they never count as the agent's use of it.

## [0.5.0] - 2026-11-21

The agent's computer has a desktop you can watch, a pointer that shows what the agent does, and a button
that turns it into a clip you can share. The web app has a new design, agents can work desktop apps that are
not web pages, and the product has a new name: openajar. Everything 0.4.0 brought stays, under the same
tools and settings.

### Changed

<!-- legacy-name: begin (the old names below are history: scripts/rename_to_openajar.py keeps them) -->
- **agentos is now openajar** (a door left ajar: the agent works in its own room, and the door is open for
  you to look in and step in). The package, the command, the MCP servers (`openajar-local`), the skills
  (`openajar-environment`, `openajar-lanes`, `openajar-offload`, `openajar-studio`), the variables
  (`OPENAJAR_HOME`, ...) and the docs carry the new name. What you set up keeps working: the `agentos`
  command runs openajar and says so on stderr, every `AGENTOS_*` variable is read when its `OPENAJAR_*` one
  is not set, and an existing `~/.agentos` and `~/.config/agentos` are used where they are, with their
  environments, saves, keys and grants. `openajar local start` replaces the old `dev.agentos.local` service
  on the same home and port, and registering replaces the `agentos-local` entries in Claude Code and Codex
  and the old skills. Environments, saves, the machine image (`agentos-env`, used without a rebuild),
  backups, links, cookies and tokens made before still work: docs/LEGACY-NAMES.md lists every name that
  stays, and why. To upgrade, run the installer again; it replaces the `agentos` tool with `openajar` and
  leaves your files where they are. From 0.4.0 that is all: its grant, web app (updated to 0.5.0's),
  checkpoints, replays, prepared machines and network settings carry over, and a repository's
  `.agentos/environment.toml` is still read (`.openajar/environment.toml` first). From 0.3.0 the agent
  grant is replaced by one with `secret` on `env/*` lanes, so `login` works with no `agentos connect`.
<!-- legacy-name: end -->
- **The web app's new design, Dawn.** The night sidebar with the sunrise at its foot and the organisation
  switch on glass, one violet for the agent, what is selected and the primary button, every button a pill,
  larger type, and the Dawn picture behind Home's window, sign-in and first run. Home opens on a window with
  one sentence and what needs you on glass cards; Environments shows each open one as a picture of its
  screen; one environment opens on a night band with its live screen and one pill for who has control.
  Every screen follows (needs you, runs, checks, machines, history, routines, settings and their forms,
  dialogs and the command palette), and web/DESIGN.md is its rule book. 0.4.0's environment screens are
  drawn in it: a closed environment's replay in its screen's place on the night band, Live and Replay so far
  on an open one, how it opened, checkpoints, the network and its refused hosts, Resume on a save, who is
  watching, and the prepared machines. Dawn's values come through 0.4.0's generated theme and pass its
  design rules.
- **The live view page.** The screen is a card, as large as the window allows, with nothing above it. One
  pill under it says who has control (the agent's violet dot and its name, or you, with a white ring round
  the card and since when) and has one button, Take over or Hand back. The screen's
  details, Stop agent, the activity list and the shortcuts are under More. On a phone the pill sits at the
  thumb and a keyboard button types on the screen.
- **A new mark.** The openajar mark is "Window a" everywhere: the README, the web app's logo, favicon and app
  icons, and the Mac companion's app and menu bar.
- **Commands start in the repository.** `terminal_exec` on an environment's machine starts in the repository's
  folder, as the file tools do, and a relative `cwd` starts there too. A command that runs out of time says
  so with the next step (`timeout_s` up to 200, or `terminal_job_start`), without the stray `setsid` line.
- **Every argument of the everyday tools says what it is** (`terminal_exec`'s `cmd`, `file_edit`'s `old`
  and `new`, `terminal_job_read`'s offsets and waits, and the rest), within 0.4.0's budget for the
  default list (7,141 of 7,150 tokens).

### Added

- **A desktop on every machine.** From the moment an environment is ready until it closes, its screen is
  a small desktop in the Dawn look: a top bar with the environment's title and what the agent is doing, a
  dock with Browser, Terminal and Files, a terminal window where the agent's commands show as it runs them,
  and the browser as a window that comes and goes on it. Apps the agent starts open there too.
  `desktop = false` in `environments.toml` turns it off. A new machine's desktop is up in about half a
  second.
- **Screen tools** for desktop apps that are not web pages: `screen_snapshot` (a picture of the screen and
  its windows), `screen_act` (click, drag, scroll, type and keys, paced like a person's hand) and
  `screen_open` (start an app), in the `screen` toolset. A take over stops the agent's input on the machine
  within about a second and lets go of every key and button, and `screen_snapshot` is refused while a person
  has control, as the browser tools are.
- **The agent's pointer.** A large violet pointer with the agent's name ("Claude Code") shows where the
  agent clicks, types and scrolls, in its browser and with the screen tools, in the live view and on the web
  app's environment page. It glides from point to point, fades after a few quiet seconds, and hides while
  you have control. It adds no time to the agent's work.
- **Record a clip.** A record button next to full screen in the live view (or **R**) saves what you see
  as a video on your computer: the screen as a rounded card on a dark stage, the agent's pointer in it, a
  small mark under it, 1920 by 1080 at 30 frames a second, MP4 where the browser can record it. It is made
  in your browser and never uploaded, shows the time it has run, and stops by itself after three minutes.
  View-only links have it too.
- **Still pictures of each screen.** The environments list and Home show a small, real picture of each
  desktop, taken only while someone looks at them, at most once a minute, never while a person has control
  and never for a machine that sleeps. Taking one does not count as the agent using the machine.
- **Screen recovery.** A screen that dies comes back at the node's next check (within about a minute) and
  the live view follows by itself: when only the desktop's parts died (its helper, the window manager)
  every window stays; when the display server died, the screen starts again empty. A desktop that
  finishes installing late starts under the running browser.
- **The eval suite grows** (`make eval`, docs/EVALS.md): `make eval-smoke` (`run.py smoke`) holds the error
  catalog, the skill and the everyday tool list to budgets with no node or model, in CI too; a model's
  repeats are compared with Fisher's test (real, likely or the same); and four tasks: probes for what
  testers found, a build longer than the default wait, a checkout form with every kind of field read back
  by the shop, and a command typed into the desktop's Terminal with the screen tools.

### Removed

- **The old tool and argument names 0.4.0 announced are gone.** `lane_exec`, `lanes_list`, `files_put` and
  every other name 0.4.0 renamed, the three merged tools (`browser_tab`, `browser_dialog_answer`,
  `approval_status`), and the old argument names (`wait_ms` on `terminal_job_read`, `timeout_s` where it
  became `wait_s`) no longer run. A call by one fails as an unknown tool or argument, and the error names
  what to call instead ("lane_exec is terminal_exec", "browser_tab is browser_tabs(select=...)", "wait_ms is
  wait_s, in seconds"), so an agent on old instructions recovers in one step. docs/TOOLS.md, "Removed tool
  names", has the table. A job no longer says where it runs as `lane` beside `terminal`. The CLI still
  calls a node from before 0.4.0 by the old names.

### Fixed

- **What the commands window shows is the agent's real record.** A command's output can no longer clear the
  window or draw a fake command or exit line, also when an escape sequence is split across reads.
- **On a server node a control link alone no longer drives a screen with a desktop.** The dock opens a root
  terminal there, so a control link needs the owner's panel session in the same browser or an owner link. In
  local mode the live view listens on 127.0.0.1 only, as before.
- **Said plainly:** the agent's commands run as root on its own machine, so root can read that screen and
  what a person types there. The take over keeps the agent's tools out of your way; it does not keep a
  hostile agent out of its own machine. The live view says so under Take over, and docs/THREAT-MODEL.md has
  a section on the desktop. Sign in on that screen only to accounts the agent is meant to use.
- An environment with a desktop still closes, or saves, when it is idle: the node's own screen checks and
  still pictures are not counted as use.
- The dock's Browser never opens the browser past a stop or a freeze; after a node restart the desktop's top
  bar and dock know the browser at once; a resumed save never signals a process from an old pid file; a
  desktop installed late keeps apt's package lists for the agent's own `apt-get install`; closing an
  environment whose machine does not answer waits once, not twice; a burst of pointer moves never pushes a
  control change out of the live view's events.
- A second home, or one started with `--no-register`, never writes into your Claude Code, Codex, skills or
  launchd setup.
- `browser_act` can choose an option of a dropdown in a form again, and type a line that ends in Enter: the
  form's check before each key no longer reads the field the act itself fills as the page changing the form.
- A second home's `openajar local start` and `stop` never touch the person's own service, nor the one an
  install from before the rename left.
- The installer no longer copies a checkout's `web/node_modules` into the install.
<!-- legacy-name: begin (the entry's name before the rename) -->
- **Upgrading removes Claude Code's old `agentos-<name>` entry** even when an `openajar-<name>` entry is there
  already (from an earlier run). It was kept, and failed with 401 at every session start once the new
  registration revoked its grant. `claude mcp get` does not show an entry's helper, so the old entry is
  known by the helper in ~/.claude.json or by a URL that points at the node.
<!-- legacy-name: end -->
- **A saved environment with a desktop goes idle again.** The node's own checks of the machine (its screen,
  the agent's ssh, stills, updates) no longer count as its use; two of them at once put back each other's
  time, so such an environment was never saved for idleness.
- **A desktop's helper that dies comes back.** Starting again on a running screen it freed the old wallpaper
  with a call Ubuntu's python3-xlib does not have, and the screen was left with the browser alone.
- The desktop's top bar says what the agent does by the tool names of 0.4.0 (it knew only the old ones, so it
  never said "is running a command"), and the screen tools' messages name `browser_wait_handback`.
- Upgrading keeps what 0.4.0 left on machines and in repositories: a resumed save's network block in
  `/etc/environment`, an open pull request's proof and network sections, and checkpoint refs keep their
  names, so nothing is doubled or lost.

## [0.4.0] - 2026-11-19

### Added

- `web/scripts/codemod-legacy-tokens.mjs` moves the web app's pre-Mono class names (`bg-bg`, `text-13`) to
  the Mono ones, and has run on all of `web/src`: no old name is left, and the design check refuses one.
  Two type roles join the system for the sizes no role had, `text-heading` (20/28) and `text-headline`
  (32/40). `web/scripts/visual-compare.mjs` photographs the catalog and the main screens on the mock and
  compares two runs pixel by pixel. The catalog's screenshot baselines are no longer in git: each machine
  records its own on the first run.
- The web app's design system for agents: `web/DESIGN-SYSTEM.md` (a screen in five steps, the tokens, the
  component groups, the checks), an index per group in `web/design-system/`, and `web/AGENTS.md` and
  `web/CLAUDE.md`. The catalog at `/$org/design` (dev only) draws every component by group, and agentos's
  own composites from the mock; the e2e suite photographs it in both themes at 1440 and 390.
- Design rules in `pnpm test` (`pnpm design:check`): no raw colour, arbitrary design value, class that is
  not a token, unknown `var()`, design value in `style`, hand-drawn component or replaced word in a screen,
  and a catalog entry for every component. What was there is counted in an allowlist that only shrinks.
- The Tailwind theme is generated from the token script (`web/tokens/theme.py`), and the app's own colours
  (board wires, Site API fields, the night band, the inverse button's edges) are tokens with light and dark values.

- **An environment's agent signs in to accounts, with logins from your vault.** `agentos environments
  login add <site> [--user NAME] [--totp] --title "<title>" | --repo <repo> | --all` binds a vault login
  to computers (the password, and a TOTP seed, from a hidden prompt or `--stdin`, never the command
  line; `--entry` binds a login already in the vault; `list`, `remove`). A binding by title follows the
  title across saves and resumes. The agent's `login(on=...)` opens the site's login page when the
  browser is elsewhere and fills it with the vault's own checks, again whenever a site logs it out, and
  never asks anyone; `environment_open` and `environment()` list the computer's `logins` by name and
  site. `agentos environments login "<title>" <site>` signs it in now from the node and says what needs
  you (a CAPTCHA, a code by email or text) with the live view's link. With `browser = "vm"` the value is
  typed into the VM's browser, whose session cookies stay in it and in its saves (encrypted): see
  docs/THREAT-MODEL.md. No replay frame is taken while a fill runs. The local node now serves `login`,
  `vault_fill` and `vault_list`, and the `environments` special includes `secret` on `env/*` lanes: an
  agent connected before needs `agentos connect` again.
- **The one-line install includes the web app, as an option.** Each release carries the web app
  prebuilt (`agentos-web-<version>.tar.gz` and its `.sha256`, from the new `web-release` workflow and
  `scripts/web_asset.py`, or `make web-asset` locally). `agentos setup` gains a web step that offers it
  (yes by default, `--no-web` skips it): it downloads the copy for the installed version, refuses one
  whose sha256 differs or whose archive holds a link, an absolute path or a `..`, and unpacks it into
  `~/.agentos/web/app` without sudo. `agentos web install|update|remove|status` change it later.
  Running the installer again updates the web app to the same version (without setup, the installer
  runs `agentos web update`; `AGENTOS_NO_WEB=1` skips it), and a copy from another version shows in
  `agentos setup check` and `agentos doctor`. The service serves a checkout's own `web/dist` first and
  the installed copy second, looked up on each request, so an install shows on the next reload.

- **What the evals found, fixed.** A lane's name passed as `on` (`<id>-browser`, the open's old
  `lanes.browser`) reaches the machine for the chat whose session holds it, and anyone else hears it is a
  lane's name and to pass the `on`; the open's result names no lane any more. `lane_exec` adds a one-line
  `hint` naming `lane_file_read(path=...)` when a command only read a file (nothing on any other command).
  A command refused a host on a `limited` network says so in its own result (`network.blocked_now` with the
  owner's command), and the CONNECT 403's status line carries the same words. `NODE_USE_ENV_PROXY=1` joins
  the proxy variables, so Node's `fetch` gets the 403 instead of ENOTFOUND. The environment's browser starts
  during the open, beside the clone (a close cancels a start nobody used), so the first `browser_navigate`
  no longer waits 10 to 15 s for it. A `browser = "vm"` environment on a `limited` network is no longer
  quarantined by the exit IP self-check (an environment's tunnel is not read), so a video proof works there.
  The evals gain `t07_video_proof`, the `first_navigate_s` metric and a wrong turn for a lane's name as `on`.

- **An environment's page in the web app tells what happened, on the real node.** A closed one leads
  with its replay (frame by frame, the agent's calls, a person's take-overs and the proofs on one track
  that fits a phone), then its proofs and pull request; an open one with its live browser. How it opened
  is one line that unfolds to each step and the work it skipped. Checkpoints are drawn as a line of work
  with the rollback on a side line, and each opens what changed since it, file by file. The owner resumes
  a save from the app (a refusal by the node's limits stays in the dialog with the fix) and watches it
  wake. Prepared machines has a page of its own with the warm pool's worst month. A video proof shows its
  preview and opens a player.
- **The app sees how an environment came up, and what the node's own machines cost.** The detail has
  `bringup`: each step of the bring-up with when it began and its seconds (the ones running while it
  starts), and the work it skipped (a prepared machine's setup and the seconds saved, a warm machine's rent
  and boot). `GET /api/orgs/{org}/environments/prepared` gives the owner the prepared machines, the warm pool
  with its hourly price and the most it can cost a month, and this month's opens by where they started. The
  saves name the machine they wake on, its price and their services; the detail adds the replay summary and
  the resume (`resumed`, `resumed_by`, `owner_woken`, `resumed_at`).
- **Checkpoints and rollback inside an environment.** `environment_checkpoint(note)` keeps every
  repository under `/work` as it is (tracked and new files git does not ignore) as a git commit under
  `refs/agentos/checkpoints/<n>`, made with a temporary index: seconds, no provider call, and the
  agent's index, stash and branches untouched. `environment_rollback(checkpoint)` checkpoints the work
  first (so it can be undone), then puts the branch back at the checkpoint's HEAD and the files as they
  were, removing new ones; ignored files (`node_modules`, `.env`) stay. Refused mid merge or rebase.
  About 20 are kept; `environment()` lists them, and the web API gives what changed since each one and
  one checkpoint's patch against now. They never leave the machine: the git gateway refuses a push of
  `refs/agentos/*`, and a files-only resume does not keep them. In `tools_more("terminals")`.
- **Watching keeps an environment open.** A live view of its browser, or a connection through an
  `environment_port` link, counts as use while it is open, so the reaper never saves or closes it under
  you; `environment()` and the web API say `watched`. The web app polling the page no longer counts.
- **You resume a saved environment without a chat.** `agentos environments resume <title or save id>`,
  Resume in the web app's Saved list, or the Resume button on the saved environment's live-view link
  (which now shows the title, when it was saved and the note instead of an error). It resumes as
  `environment_open` does, within the node's limits, held by no chat: you watch it at once, and the next
  agent that opens its title takes it (`opened: "resumed"`). An agent's call with the handle of a saved
  one says when it was saved and how to come back.
- **Video proofs.** `browser_record(action="start")`, the flow, then `browser_record(action="stop",
  proof="guest checkout, end to end")` records the real screen of an environment's browser with
  ffmpeg (10 frames a second, at most 1280 pixels wide), in a process of its own that never gets in
  the agent's way, and keeps an MP4 and a short GIF preview as one proof beside the screenshots: the
  same links (served with range requests), retention and node-wide cap. A recording stops by itself
  after `record_max_s` (default 180 s), on save and close, and when the browser goes away. The docker
  and `dev` images and `deploy/install-node.sh` have ffmpeg; an older machine installs it on its
  first recording, and `agentos doctor` warns on a node without it. The tool is in the `files`
  toolset (`tools_more("browser_record")`).
- **The pull request shows the proof.** `environment_pull_request` puts the environment's proofs on
  the repository's `agentos-proofs` branch (an orphan branch made through GitHub's Git Data API with
  the same token, never in the diff) and adds a Proof section to the body: each screenshot or GIF
  inline, each recording's MP4 as a link. Called again for an open pull request, it updates only
  agentos's own sections between their markers. `proofs=false` (or `pr_proofs = false` in
  `environments.toml`) leaves them out, and `proofs=[...]` names some. A proof in the repository is
  visible to everyone who can see the repository.

- **A network policy per environment.** `network` in `environments.toml` is `full` (the default),
  `limited` (package registries, code hosts, your `allow_hosts` and the hosts of bound secrets) or
  `none` (the git gateway alone). It is held outside the machine: a closed Hetzner firewall, or the
  docker entrypoint's closed wall. The machine's way out is the node's allowlist proxy, over its SSH
  connection; refused hosts get a 403 that names the fix, and are listed in `environment()`, the web
  API and the pull request. `agentos environments allow --network limited --host api.example.com`
  sets it; an agent may only narrow it on `environment_open(network=...)`.
- **Secrets for the app, never on the VM.** `agentos environments secret add STRIPE_SECRET_KEY --host
  api.stripe.com [--repo ...]` binds a vault API key to environments. The machine holds a
  placeholder, and the node's TLS swap puts the value in for the bound host only (headers and query,
  HTTP/1.1). `list` and `remove` too.
- **Morph Cloud as a VM provider.** `agentos vm setup --provider morph` takes a Morph API key from
  your own account (hidden, checked, kept 0600), pins the host key of Morph's SSH gateway and makes
  Morph the provider; `agentos setup` offers it next to Hetzner and Docker. Machines start from a
  snapshot in about a second, sign in through Morph's gateway with each machine's own key (the API
  key is never on a command line), and carry a Morph time limit as a backstop. Sizes are agentos's
  (`morph-small` to `morph-xlarge`), and prices are estimates from Morph's MCU rates in `vms.toml`.
- **On Morph, a save pauses the machine.** A kept environment that goes idle, is closed or reaches
  its time limit is paused with its memory instead of snapshotted and deleted, and
  `environment_open` with its title resumes the same machine in seconds, its dev server still
  running (`resumed.from` is `paused`). Forget and retention delete the paused machine.
  `pause_saves = false` in `environments.toml` turns it off.
- Providers declare what they can do (firewall, snapshot, pause, network policy, cloud-init), and a
  machine without cloud-init gets its setup from the node at once.
- `scripts/morph_live_check.py` checks the Morph provider against a real account for a few cents.
- **An environment opens in seconds, with the app already running.** A repository says how its
  machine is set up in `.agentos/environment.toml` (`setup`, `refresh`, `start`, `port`). The node
  reads it before it rents anything; the first open runs `setup` on the agent's machine and starts
  the dev server as a job, and the node builds a **prepared machine** (an image with the setup
  already run) in the background, on a machine of its own and within its limits. The next opens boot
  from it, fetch the asked ref, run `refresh` and return once the app's port listens.
  `environment_open` and `environment()` say what happened (`prepared`, `app` with its link); a setup
  that fails or an app that does not listen never fails the open, and says why with the end of its
  log. Builds remove every credential before the snapshot and check that no byte of the git
  gateway's token is left. A newer build replaces the older one, unused ones go after
  `prepared_days`, and at most `prepared_max` are kept. On Docker a first open of a test repository
  took 25 s (20 s of setup), a prepared one under 5 s.
- `agentos environments prepare <repo>` builds one now and shows its steps; `agentos environments
  prepared` lists them with their monthly cost; `prepared forget <repo>` deletes them.
- **A warm pool**, off by default: `agentos environments allow --warm 1` keeps a machine booted for
  the next open (from the image the newest open used), filled when a chat connects and after each
  open. It counts against the node's limits, always yields to an open, and an unclaimed one is
  deleted after `warm_minutes` (50, inside Hetzner's first billed hour). An open that matches takes
  it and is ready in about 2 s on Docker.
- **A replay of each environment's browser, kept after the VM is gone.** The node keeps small JPEG
  frames of the environment's own browser: after each agent call that can change the page (with the
  call and its summary), every 3 seconds while you have control or the page keeps changing, and at
  save and close. It never waits for or acts on the browser while a call uses it, and never pictures
  an account lane. `agentos environments replay <id> --out DIR` writes the frames and an
  `index.html` that steps through them with their captions; `GET /api/orgs/{org}/environments/{id}/replay`
  gives the owner the frames (with signed links) and the calls by time. Kept 14 days after the close
  (`replay_days`), within a cap per environment and on the node; `replay = false` in
  `environments.toml` turns it off. An agent sees only `environment()`'s `replay` count.
- **An eval of the agent's experience in environments.** `make eval` (or `uv run python
  tests/env_eval/run.py`) runs six tasks an agent really does (fix and open a pull request, a dev
  server with a proof, continue tomorrow, cold, prepared and warm opens, a refused host on a limited
  network, a checkpoint and rollback) on a docker node of its own with local repositories and a
  stand-in for GitHub, and measures finished, time to a ready machine, calls, errors, wrong turns and
  the tokens of tool output read, from a recording MCP proxy every agent goes through. Scripted
  `perfect` and `careless` agents prove the checkers for free; `--agent claude --model haiku` (and
  `codex`, or any `--agent-cmd`) runs a real model under a cost guard (`--max-usd`). Each run goes to
  `bench/evals.jsonl` and is compared with the one before; `--ref <commit>` runs another commit's
  agentos under the same harness for an A/B. [docs/EVALS.md](docs/EVALS.md).

### Fixed

- **Security: the fixes of the independent review of 2026-11-19.**
  - A closed (`limited` or `none`) docker machine has no DNS: Docker's own resolver (127.0.0.11) was on
    loopback and forwarded any name out. The entrypoint refuses it, and the machine starts with
    `--dns 0.0.0.0`. On Hetzner, DNS to Hetzner's own resolvers is a known limit (its firewall never
    filters it): docs/THREAT-MODEL.md.
  - A prepared machine's build runs under the grant that started it (an agent's open: the agent's grant,
    never the owner's), its gateway token only fetches, and a revoked or frozen grant stops the build.
  - A prepared machine built from a branch, tag or commit boots only opens of that same ref; the shared
    build is the default branch's.
  - The app secrets swap puts no value in a request whose target is a full URL naming another host.
  - A login bound with `--title` names the one agent's computer that has that title when you bind it,
    never another agent's computer that picks the same title. Bind it once the computer exists (`--title
    <id>` when several agents have one). A title binding made before this covers nothing: bind it again.
  - `POST /api/environments/login`, `/resume` and `/prepare` need the owner in person (the bare node
    token is refused); `agentos environments login`, `resume` and `prepare` still work, with the CLI's
    one-time presence code.
  - `agentos web install` trusts only GitHub's own digest of the archive on an immutable release
    (`AGENTOS_WEB_SHA256` pins one for a mirror), and the release workflow never replaces a file. Releases
    are now made as drafts and published after the web app is attached (docs/DEVELOPING.md).
  - A proof's file planted on `agentos-proofs` under its name is replaced, not shown as the proof.
  - The prepared build's scrub keeps the SSH keys on a provider that signs in with them (Morph).
  - Paging the replay loses no item that shares a millisecond with a page's edge.
- **A call's log lines no longer carry an environment's handle.** The `on` an agent passes is a
  secret its chat holds; the node's log context now names the environment's lane (`env/<id>`) instead.
- **`agentos setup --docker` works from the one-line install.** The machine image's Dockerfile and
  entrypoint now ship inside the package, so the image builds without a checkout of agentos.
- **Studio reads a font size scale named by number.** `font.13` in a tokens file and `--font-13` in CSS
  are font sizes now (Tailwind mode read them as font families), and a border width equal to a token is
  on the system even when a utility wrote it as a literal (Tailwind's `border` is 1px).

### Changed

- **The tools say terminal, file and browser, never lane, and the default list is your agent's
  computer.** An agent starts with the environment loop (30 tools with `tools_more` and `tools_call`,
  about 8,000 tokens, down from 41 and 10,150): open, status, save, close, upload and download, the
  terminal (`terminal_exec`, `terminal_job_start`, `terminal_job_read`, `terminal_job_cancel`), its files
  (`file_read`, `file_write`, `file_edit`), the browser with `browser_record`, `environment_pull_request`,
  `environment_checkpoint` and `environment_rollback`, `environment_port`, `login` and `approval_wait`.
  The owner's signed-in account browsers moved to a new `accounts` toolset (`browsers_list`,
  `browser_open`, `browser_claim`, `browser_view` and the rest; the agentos-lanes skill loads it first,
  and a controller lists it by default). The `files` toolset is now `browser` (the old name still
  works in `mcp_toolsets`, URLs and `--toolsets`), and `environment_forget` is in `terminals`. Every
  rename is in docs/TOOLS.md, "Renamed tools": `lane_exec` is `terminal_exec`, `lane_file_read` is
  `file_read`, `lane_wait_control` is `browser_wait_handback`, `lanes_list` is `browsers_list`, `lane_claim`
  is `browser_claim`, `files_put` is `browser_file_put`, `env_create` is `terminal_create`, `lane_do` is
  `browser_task`, and so on. Waiting is `wait_s` on the renamed tools (`terminal_job_read` took `wait_ms`,
  `browser_wait_handback` `timeout_s`). Descriptions, results and errors use the new words; the shared
  browser tools no longer carry the account rules (posting pace, Ask me first), which are in the
  account tools and the agentos-lanes skill; an error's fix names the toolset of a tool that is not in
  the default list.
- **The default list says less and means the same: about 7,000 tokens, down from 8,200.** Three tools
  merged into another where two competed for one intent: `browser_tabs(select=<index>)` switches tabs
  (was `browser_tab`), `browser_dialog` reads the waiting dialog and answers it with `accept`
  (`browser_dialog_answer` was the answer; `browser_dialog` is now in the default list), and
  `approval_wait(wait_s=0)` reads an approval now, or without an id lists yours still waiting (was
  `approval_status`). Waiting is `wait_s` on `browser_wait` and `approval_wait` too, like every other tool
  in the list (`timeout_s` is a command's own limit). The descriptions lost what an agent does not need
  to pick and call a tool: `on` on the default list's tools names no account browser, no "Effect:"
  lines (the MCP annotations carry it), no "Errors:" lists (every error carries its own fix line) and no
  lists of the fields a result shows anyway. A snapshot's header names an account browser as `browser:`
  and an environment's browser not at all (it said `lane: env/<id>-browser`, which is not an `on`), and a
  job says `terminal` where it runs; `agentos run --list` shows it as TERMINAL.
- **`file_read` reads several files in one call**: `paths=["package.json", "src/*.js"]`, each with its
  lines, within the same 40,000 characters. Agents read a small repository with a `cat` loop in the
  terminal because it took one path.
- **`environment_close` says `keep` is chosen at `environment_open`**, and `keep` passed to the close is
  refused with what to do instead (push, or `environment_save`, then close).
- **The environments eval's t06 counts a change that passes the tests**: a model that makes the risky
  change so the tests pass, after a checkpoint, finishes it too, as one that rolls back after a failure.
- **An environment's tools no longer say "Effect: read"** where they delete or change things (close,
  forget, rollback); the MCP annotations say what a tool does.

- **An environment saves when idle by default.** agentos is a computer for your agent that stays, so
  `default_keep` is now `save`: an `environment_open` that names no `keep` is saved (disk and files, or
  paused on Morph) when it idles, closes or the laptop sleeps, and its title brings it back in any chat.
  Single use is one argument away (`keep="none"`, which the skill and the MCP intro teach for a one-off
  fix only), and `default_keep = "none"` in `environments.toml` makes it the node's default again (an
  `environments.toml` that already names it keeps it). `agentos setup` offers saves when idle first, as
  the recommended choice, and `--yes` takes it.
- **The README and docs/VISION.md say what agentos is**: a computer for your agent, with a browser, a
  terminal and a disk that stay, on machines in your own account, signed in with logins from your vault
  that the model never sees. The FAQ compares it with hosted agent computers and with temporary
  sandboxes. The skill and both MCP intros name the login tool. docs/THREAT-MODEL.md says what a save
  holds and never holds.
- **The Docker image downloads instead of building.** Setup pulls the prebuilt
  `ghcr.io/sabalys-ramunas/agentos-env:<version>` (amd64 and arm64, published by each release) in a
  minute or two, and builds it here only when it cannot (10 to 15 minutes). `agentos environments
  setup --pull` does the same; `--build` still builds it here; `docker_pull = ""` in `vms.toml` never
  downloads.

### Deprecated

- **The old tool names work for one release.** `lane_exec`, `lane_file_read`, `lanes_list`, `files_put`
  and every other renamed tool (docs/TOOLS.md, "Renamed tools") are still callable, directly or through
  `tools_call`, and `tools_more` finds a tool by its old name, but they are not listed, and each result
  carries a `renamed` line ("lane_exec is now terminal_exec"). The same for `wait_ms` on
  `terminal_job_read` and `timeout_s` on `browser_wait_handback`. Calls by an old name reach the new
  tool, so grants, freezes and the call trail name the new one. The old names and argument names are
  removed in the next minor release. `agentos run` and the other CLI commands call a node from before the
  rename by the old names.
- **The merged tools' old names work for one release too**: `browser_tab(index)` is
  `browser_tabs(select)`, `browser_dialog_answer` is `browser_dialog`, `approval_status` is
  `approval_wait(wait_s=0)`, each with the `renamed` line. So does `timeout_s` on `browser_wait` and
  `approval_wait`, and a job's `lane` beside its `terminal`.

## [0.3.0] - 2026-11-16

Agents work on their computer the way they work locally, one chat per machine, and a lot less waiting.
Built from three rounds of agent testers and two field reports from a real project.

### Changed

- **One chat per machine.** `environment_open` returns `on`, a handle only that chat has; every call to
  the machine carries it. Another chat's title is `environment_in_use`; `take_over=true` moves a machine
  that has been quiet for 10 minutes. Clients that send no session id must pass `on` on every call.
- **Commits are yours.** With your GitHub login saved, an environment's commits carry your GitHub name
  and noreply address (they link to your account without your real email); without one, the agent's.
- **One count limit** (`environment_full`, with `retry_after`), and `environment_open` waits for a free
  slot up to its `wait_s` instead of failing.

### Added

- **The agent's own shell:** `ssh agentos-<id>`, `scp` and `rsync` from the laptop (`agentos setup ssh`
  adds one Include line to `~/.ssh/config`). Each machine has its own key; port forwards are off
  (`environment_port` gives a local link to a dev server).
- **Dev servers survive agentos.** Jobs run detached on the VM with their output in files there; a restart
  or an update of agentos reattaches to them with the same job ids and offsets.
- `lane_job_read(until_port=3000)` waits on the machine until a dev server listens (or returns the job's
  end), and `environment(on=...)` waits up to a minute for a starting machine: no more polling.
- **Memory:** every VM gets swap, and a process the kernel kills for memory is named in the job's and
  the command's result, with the machine's memory and the next size.
- A resume brings back dev servers from what was listening when the machine was saved.

### Fixed

- Opening is about three times faster (81 s instead of 255 s on a real run): the local service no longer
  runs as a throttled macOS Background job, the setup check is one call, the clone through the laptop is
  blobless (a quarter of the bytes), public repositories clone straight from GitHub, and the mirror warms
  while the VM boots.
- Nothing runs on a machine that is still starting (a git command during the clone broke it); a failed
  one says why on the next call.
- No system updates in the middle of a session (they reloaded systemd and restarted the network under a
  starting browser): they are applied when a VM is set up and when the dev snapshot is built, and a
  kept-running machine gets them at a quiet moment.
- A save could be undone by the reaper and leave its title held; a push could reuse a fetch from before
  it and let a force push over a colleague's commit through. Both fixed.
- Clearer errors: which browser launch step hung, the `pkill -f` that killed its own shell, the two
  limits' words, `which_environment` naming the open titles, and `unsaved_work` offering a git bundle.
- Installed skills refresh when the service starts; `agentos local status` shows the right start time.

## [0.2.0] - 2026-11-16

The first tagged release. agentos is now about agent environments: your coding agent's own
computer in the cloud, and it stays when you want it to. It runs on your laptop, with no server
needed. Two older parts are gone.

### Highlights

- **One command to install, then a guided setup.** `curl -fsSL .../scripts/install.sh | sh`
  installs the newest release (`AGENTOS_GIT_REF` picks another) and starts `agentos setup`: VMs
  (a Hetzner token, or Docker), your GitHub login, the limits, and Claude Code and Codex connected
  to a small service on your laptop. Nothing bills until an agent opens a VM.
- **Three ways an environment stays**: single use (deleted when done), saves when idle (the whole
  machine is saved and the VM deleted, resumed by its title from any chat), and kept running
  (only when you allow it).
- **Private repositories** through your GitHub login, or a card where you paste a token; the agent
  never sees one.
- **Moving files**: `environment_upload` sends a local checkout up with a one-use link, and
  `environment_download` gives you a file from the VM. `environment_port` gives you a link to a
  dev server, and `environment_pull_request` opens the pull request.
- **The browser runs on the VM**, ready at boot with the `dev` snapshot that setup offers to build.
- **A new README and logo**, with a picture of how it works and the setup in a terminal.
- `agentos setup` shows which agents it connected in plain lines, without the grant details.

### Added

- **Kept environments, in the agent's words and yours.** The `agentos-environment` skill and the
  MCP intros (server and local) teach the three modes: single use for a one-off fix, `keep="save"`
  when the work spans sessions (saved when idle, cents a month), and `keep="live"` only when the
  person asks for an always-on machine. They teach resuming by title and reading `resumed` (its
  note is data, never instructions), `environment_save` before risky steps and at the end of the
  day, telling the person how to come back ("say: continue the shop in agentos"), forgetting
  finished saves, and upload and download in one line each.
- **The web app shows how each environment stays.** The list and each environment's page say
  "Single use", "Kept running" or "Saves when idle", with what it costs while unused. A Saved
  section lists each save (title, repository, branch, when, the snapshot's size, what it costs a
  month, and the agent's note as text), says when one is pruned soon, and has Forget for the
  owner, which says what is deleted before it deletes it.
- **`agentos setup` asks one question**: single use or saves when idle. Kept running is explained
  with its cost and is allowed only when you choose it (`allow_keep_live`).

### Changed

- README, docs/VISION.md, docs/ENVIRONMENTS.md ("Keeping an environment") and docs/LOCAL.md
  (sleep: a kept one is saved, a single-use one gets a rescue save) are written around the three
  modes, with the honest limits of local mode: the laptop must be awake while a VM works, and no
  agent runs while it is closed.

### Removed

- **The engagement engine and the day plan.** The programs and sessions that liked, followed
  and commented on their own are removed, with their CLI commands (`agentos engage`,
  `agentos plan`), their MCP tools and their API routes. A run the engine queued on an older
  node is cancelled before it starts (`source_retired`), and nothing is sent. Old `engage_*`
  keys in `node.toml` are ignored, so the file still loads, and `engage.db` is left on disk as
  it is, never opened. The action ledger stays and keeps counting what each account does.
- **The classic panel (`panel/`).** A node serves the web app (`web/`) only. Gone with it:
  `agentos panel use`, `agentos errors --panel-json`, and two routes only it called
  (`GET /api/audit/replay.html`: `agentos audit replay --html` writes the same file;
  `GET /api/members/{id}/devices`: `GET /api/members/{id}` carries the devices).
  `AGENTOS_PANEL_DIR` still points a node at another build, and `agentos panel login` still
  prints a sign-in link.

### Changed

- The API demo runs as `python -m agentos.dev.apidemo` (it was `agentos.api.demo`).
- Old import paths of moved modules are gone. Use the new homes, for example
  `agentos.stealth`, `agentos.node.camoufox`, `agentos.workspaces.transfer` and
  `agentos.core.age`.

### For contributors

- `make layers` checks one import order between the packages, and `make complexity` fails on a
  function of complexity 25 or more or a file over 1,000 lines unless it is listed. Both run in
  `make lint` and CI, and their lists only shrink.

## [0.1.0] (never tagged)

The first public code. agentos is cloud dev environments for AI coding agents: a chat's agent
asks for a disposable VM with a terminal, a real browser and its repository, works there, pushes a
branch, and the VM is deleted.

### Added

- **Environments** (docs/ENVIRONMENTS.md): `environment_open(title, repo)` rents a VM for one
  chat, clones the repository into `/work/<name>` and opens a browser whose traffic leaves from
  the VM, so `localhost` is the agent's dev server. `environment()` shows its state, ports, git
  and cost; `environment_close()` refuses while work is not pushed, then deletes the VM and the
  browser. Terminal and browser calls without `on` (`lane_exec`, `lane_job_start`,
  `browser_navigate`, `ui_check`) go to the chat's environment.
- Two providers: **Docker** (an Ubuntu container with SSH on your own computer, walled off from
  your network, for trying it and for CI) and **Hetzner Cloud** (the cheapest server type that
  fits, or a `dev` snapshot from `agentos vm image build dev`).
- Git through the node's gateway: the VM holds no key, only a token for its own repository that
  works through its own tunnel and dies with the environment. A force push or a delete of the
  default branch is refused.
- Limits instead of questions: at most 3 open, 8 hours each and 50 EUR a month by default,
  set with `agentos environments allow`. The reaper closes an environment that was forgotten,
  and never deletes unsaved work for being idle.
- An environment belongs to the agent that opened it: other agents cannot see it or act on it.
- The live view of the environment's browser, to watch and take over, and the web app's
  Environments pages (each one's browser, commands, git, ports and cost).
- `agentos environments setup | allow | list | show | close` and the `agentos-environment`
  skill, installed for Claude Code by `agentos connect`.
- The web app (`web/`): environments, Needs you, runs, studio's checks, brand, machines and
  settings, served by the node at `/`.
- Studio's page checks for agents: `ui_check`, `ui_diff`, `system_check` and baselines, with
  brand profiles.

### Also in this release

- `agentosd`, the node daemon: browser lanes on Camoufox with a profile, proxy and pinned
  fingerprint each, a network self-check, and a live view to watch or take over.
- An MCP server with about 150 tools: browsing by accessibility snapshot, logins from the
  vault, files, terminals and services in a lane, flows, workflows, schedules, brains,
  engagement and connectors.
- Grants that narrow and expire, approvals, "Ask me first", standing approvals and an audit
  log.
- A vault with TOTP and an IMAP code catcher, and imports from Bitwarden, 1Password and Apple
  Passwords.
- A controller for several nodes, each dialling out over one WebSocket.
- `agentos connect` for a Mac: an SSH tunnel, a grant for your agents in the Keychain, and the
  Claude Code registration.
- `agentos dev up`, a fake node for trying agentos on a laptop.
- Flows in git: `agentos flow pull`, `diff`, `push`, `validate` and `test` keep a node's flows as
  YAML files, checked offline and tested on recorded pages with no browser; `agentos call` calls
  any MCP tool from a shell with the agents' grant.
- `agentos expose 5173 --lane ws/name`: your laptop's dev server in one browser lane, at a reserved
  `https://<name>.expose.agentos.invalid` address, over your SSH session and only while the command runs,
  so an app's "Sign in with LinkedIn" can be tried with a lane's real account. `agentos lane
  forward ws/name:PORT` is the other way: a terminal or service lane's port on your localhost.
- Git credentials for lanes and brains (docs/GIT.md): `agentos git key add` makes a deploy key on
  the node (only its public half is printed) or stores a token, and `agentos git remote allow`
  declares which repositories a workspace or a lane may use. Git in a lane, an `agentos run` job or
  a brain task reaches them through the node's git gateway, which holds the key: a push is a commit,
  a force-push or a push to the default branch always asks a person, and every use is traced.
- The installer, systemd units (a hardened one included), `agentos doctor`, backups and
  upgrades, and node templates for Hetzner Cloud, a home box and a Mac mini.
