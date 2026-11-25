# Developing openajar

How to work on openajar on your Mac and test it on a node. This page is the day-to-day loop;
[CONTRIBUTING.md](../CONTRIBUTING.md) says how to send a change.

## Run it on your laptop

No VPS, no Linux, no Camoufox or Xvfb: `openajar dev up` runs the node that the daemon runs,
with fakes for the browser, the display and the network. Claude Code talks to its real MCP
server, and you use the real web app, on your Mac.

```
uv sync                          # once
uv run openajar dev up --seed     # serves on 127.0.0.1:7450
```

It prints three things. The MCP URL. A `claude mcp add` line with an agent grant in it: run it
once and Claude Code has the `openajar-dev` server. The grant is issued at each start for
`claude-code@dev` (every workspace, up to commit, reading memory but not writing it), the way `openajar connect` issues one
on a real node. The node token is never printed: the agent is gated as an agent, so a post after
it read a page waits for a person, as on a real node. With `--home DIR` each start revokes the
grant the last start printed, so run the new line after each start. A sign-in link for the web app: it works
once and expires in 10 minutes. Ctrl-C stops the node and closes every lane.

The web app is built from `web/` (`pnpm install && pnpm build`, once; [web/README.md](../web/README.md)).
Until it is built the sign-in link opens a placeholder page; the API and MCP work without it.

What is real, and what is fake:

- Real: the policy and grants, the vault (with a key file), the runner and flows, workspaces,
  approvals and questions, lane control (take over, hand back, holds, quarantine), the panel,
  MCP, and the daemon's shutdown.
- Fake, in `src/openajar/dev/`: the browser (`lanes.py`, `web.py`). Each lane has its own tab
  over a small social site: a feed with a "New post" button that opens a composer dialog, a
  post with a Like button that changes state, and a captcha page that reports the captcha
  signal. Each page plays its traffic and console through the tab's fake network
  (`net.py`, `page_requests` and `page_console`): a profile page (`/u/biz-a`) shows what a broken
  web app does, a page error with its stack, a warning, an API call that answers 500 and an image
  whose host refuses the connection, so `browser_console` and `browser_network` have something to
  find. The display (`display.py`): each lane's live view is a VNC server that shows its
  tab as a wireframe picture (no words; the snapshot has them).
- Fake: the brains. They run `openajar.dev.fakecli`, which answers from fixed scripts. No
  model is called and no account is used.
- Nothing is fetched from the network, and no proxy is used.

Flags:

- `--seed` adds two workspaces (`biz-a`, `biz-b`) with four lanes, a saved flow
  (`biz-a.social.post`), a captcha that holds `biz-b/social`, a lane under a human's control
  (`biz-b/forum`), a pending approval and an open question.
- `--home DIR` keeps the node's files (vault key, `panel.db`, runs) between runs. Without it
  they go to a temporary folder that is removed on exit. Lanes live in memory, so each start
  creates the seed lanes again; the workspaces, flow and approvals are added only when missing.
- `--port N` (default 7450) for the MCP and the panel.

Try it from Claude Code: `browsers_list`, `browser_open` on `biz-a/social`, `browser_snapshot`,
then `browser_act` on the "New post" ref. Open `biz-b/social` in the panel to see the captcha
and the live view; hand it back from the panel once you have looked.
For the devtools tools, `browser_navigate` to `https://social.example/u/biz-a`, then
`tools_call` `browser_console` (level `error`) and `browser_network` (`failed_only`, then `id` of
the 500 to read its body).

Tests for this path: `uv run pytest tests/unit/test_dev_*.py`. `test_dev_up.py` runs the node
in process, calls MCP over HTTP with the printed agent grant (and checks a post after reading a
page asks first), signs in with the printed code and
checks the live view over the viewer's WebSocket, then shuts down and checks every port closed.

## Environments on Docker, with the dev node

For working on environments themselves: the dev node with the docker provider, so each
environment is an Ubuntu container with SSH instead of a cloud VM. People who only use openajar
run `openajar setup` instead (the [README](../README.md) quickstart). You need Docker running.

```sh
uv sync --extra studio
mkdir -p ~/openajar-try && echo 'provider = "docker"' > ~/openajar-try/vms.toml
uv run openajar dev up --home ~/openajar-try --connect
```

`dev up` starts a node on `127.0.0.1:7450` and, with `--connect`, registers it with Claude Code and
Codex (when they are installed) as `openajar-dev`, with the `openajar-environment` skill. Without
`--home` and on its own port it registers by itself; with `--home` or another `--port` (a second node,
a test's, a builder's) it writes nothing into their folders unless `--connect` says so. Leave it running. In a
second terminal, build the environment image once (a few minutes):

```sh
export OPENAJAR_HOME=~/openajar-try
uv run openajar environments setup --build
```

Connect the repository the agent should work on: a deploy key to paste into the repository's
deploy keys (with write access), or a fine-grained token with `--token`. A public repository clones
without this, read only; a private one that is not connected is refused before anything is
rented, with this command in the error.

```sh
uv run openajar environments repo add github.com/you/app
```

Then start a new Claude Code session and ask it to work on that repository in openajar. On the dev
node the account lanes are simulated; the environment's container, terminal, browser, git and
studio's checks are real.

The own-machine provider (`provider = "own"`, openajar.vms.own) is tested against a stand-in machine: a
container with its own sshd and its own Docker, reached over ssh on a loopback port. Build it once, and
its tests (`tests/unit/test_environments_own_docker.py`, `test_environments_own_browser_docker.py`) run
instead of being skipped:

```sh
docker build -t openajar-test-machine tests/fixtures/own_machine
uv run pytest -q tests/unit/test_environments_own_docker.py tests/unit/test_environments_own_browser_docker.py
```

The first run copies the environments image into the machine's Docker (a minute or two); it stays in
the volume `openajar-test-machine-docker` (about 4 GB, `docker volume rm` frees it). The same image
is a quick way to try `openajar environments machine add` by hand.

To see whether a change makes environments better or worse for the agents that use them (done,
time, tokens of tool output read, wrong turns), run the environments eval on the same Docker:
`make eval`, then `uv run python tests/env_eval/run.py compare`. [EVALS.md](EVALS.md) has the
tasks, the metrics, paid runs with a real model and A/B between two commits.

## On the Mac

```
uv sync                        # Python 3.12+ env with the dev tools
uv run pytest                  # tests/unit: pure Python, no browser, no Xvfb
uv run ruff check src tests
uv run openajar --help
```

Unit tests never start a browser. They drive the real MCP server, CLI and daemon against
in-memory fakes that implement the Protocols in `src/openajar/node/interfaces.py`: the lane
managers in `src/openajar/dev/lanes.py` (`FakeLaneManager` and friends, re-exported by
`tests/unit/fakes.py`, which also holds the test `FakeInteraction`).
Tests that need a real Camoufox and Xvfb are marked `node` and are skipped here
(`addopts = "-m 'not node'"` in `pyproject.toml`).

Tests and time: a test must not pass or fail on the machine's speed (CI runs on busy macOS runners).
To wait for something to happen, use `wait_for(check)` or `await until(check)` from
`tests/testkit/timing.py`: they return as soon as the check holds and give up after 10 s, never a
fixed sleep before an assert or a loop of a few short sleeps. To check that something is fast or does
not wait, measure what the code did (returned while a lock was still held, how many calls it made)
where you can; where only the clock can tell, bound it with `budget(seconds)`, which CI scales with
`OPENAJAR_TEST_SLOW`, and keep the slow alternative (a timeout, a sleep) well above the scaled bound.
A port is `free_port()` or port 0, never a fixed number.

Two kinds of recorded fixtures, each with a make target that records it again (review the diff:
it is what changed on the wire or for agents):

- `make protocol-doc`: a real controller and node session, recorded into
  `tests/fixtures/protocol/messages.json` and rendered into docs/PROTOCOL.md's examples. CI fails
  when the doc is stale (`scripts/protocol_doc.py --check`), and `test_protocol_doc.py` when a new
  session's messages no longer have the fixture's shape.
- `make golden`: a lane session on the dev web (`tests/golden/dev-social/`: the tool calls, every
  engine call, a HAR of the pages). `test_golden.py` replays the tool calls with the engine played
  back from the recording, so the tool layer's outlines, refs, deltas, signals and page results
  are checked without a browser. Only the dev web is ever recorded, never a real site.

Keep `import camoufox` and `import playwright` inside the functions that launch a
browser, so everything else imports on a Mac. `test_daemon.py` checks this for the
CLI, the daemon and the MCP server.

## On the node

A dev node is any Ubuntu 24.04 box in your SSH config. Tell the scripts which one, and which
directory on it holds the dev checkouts, with one line in `.agentos-node.env` at the top of
the checkout (gitignored):

```
OPENAJAR_NODE=mynode:/srv/agentos-dev
```

(or export `OPENAJAR_NODE=mynode` and `OPENAJAR_DEV_ROOT=/srv/agentos-dev`). Without it the
scripts stop and say so. Two scripts do the round trip:

```
scripts/sync-node.sh /srv/agentos-dev/mcp           # rsync this checkout (no .venv, .git, poc/shots)
scripts/node-test.sh mcp                            # sync, uv sync, uv run pytest -m node
scripts/node-test.sh lanes tests/node/test_lanes_node.py -x -k warm
```

`node-test.sh <name>` uses `$OPENAJAR_DEV_ROOT/<name>` and exports the agent's display and
port ranges so parallel agents never collide:

| Variable | lanes | interaction | mcp |
|---|---|---|---|
| `OPENAJAR_TEST_DISPLAY_BASE` | 110 | 120 | 130 |
| `OPENAJAR_TEST_VIEWER_BASE` | 6110 | 6120 | 6130 |
| `OPENAJAR_TEST_VNC_BASE` | 5110 | 5120 | 5130 |
| `OPENAJAR_TEST_MCP_PORT_BASE` | 7410 | 7420 | 7430 |

Any other name uses `OPENAJAR_TEST_SLOT` (11 to 59, default 14). Slot S gets displays from
`S0`, viewer ports from `6000 + 10S`, VNC ports from `5000 + 10S` and MCP ports from
`7400 + 10(S - 10)`, ten of each. The script refuses a slot whose ports would touch the PoC
(5900, 6080) or the production node (5910 to 5919, 6100 to 6109, 7400).

Node tests should build their `NodeConfig` from these instead of the defaults.

`--python` runs a script in the slot instead of pytest (`scripts/node-test.sh bench --python
scripts/bench_node.py --runs 5`). The lane benchmarks (`make bench`) and the soak and chaos runs
(`scripts/node-test.sh soak -m soak tests/soak`) are described in [BENCH.md](BENCH.md).

Rules on the node:

- Never touch a production install, display `:99`, or ports 5900 and 6080.
  `sync-node.sh` refuses any target outside `$OPENAJAR_DEV_ROOT/`.
- No real accounts in automated tests. Use the fixture site in `tests/fixtures/site/`.
- Do not leave long-running processes behind. Run `openajar node serve` in the
  foreground and stop it with Ctrl-C (it closes every lane on the way out).

### A dev install

The installer takes a prefix and a home, so a dev copy never touches `/opt` or systemd:

```
scripts/sync-node.sh /srv/agentos-dev/mcp
ssh mynode
cd /srv/agentos-dev/mcp
INSTALL_PREFIX=$PWD OPENAJAR_HOME=$PWD/.agentos-home \
  OPENAJAR_INIT_ARGS="--name dev-mcp --mcp-port 7430 --display-base 130 --viewer-base 6130" \
  deploy/install-node.sh --dev
OPENAJAR_HOME=$PWD/.agentos-home .venv/bin/openajar node serve
```

`.agentos-home` survives later syncs. The installer is safe to run again: it skips
installed apt packages and an installed Camoufox build, and keeps an existing
`node.toml` and its token. `--skip-apt` and `--skip-browser` skip those steps.

### The stealth and isolation check

`openajar lane stealth-check --slot 33 --lanes 3 --out /tmp/stealth` opens scratch lanes in a
throwaway home on a test slot (never the node's own displays, ports or lanes) and reports what a
page's own script sees, drift between launches, values lanes share, memory per lane and wake times;
`--soak-minutes 10 --kill-every 60` adds lanes cycling while browsers are killed. It stops before the
available memory falls under `--min-available-mb` (2048). It refuses a slot that touches any node on
the machine (the node.toml of OPENAJAR_HOME, /var/lib/agentos and every running agentosd), closes its
lanes on Ctrl-C, SIGTERM or a closed terminal, and stops what an earlier check killed outright left.
`openajar lane audit` reads every pin of a node and starts nothing. docs/design/stealth.md has the
table of checks and a Docker node for a laptop.

### Upgrading Camoufox

The browser build follows the camoufox package: each release carries a `browser-pin.json` naming
the one build it was tested with, and `python -m camoufox fetch` fetches that build. So the build
is pinned by the camoufox version in `uv.lock` (0.5.9: 158.0.1-beta.41). To move to a new one:

1. Bump camoufox in `pyproject.toml` and `uv.lock` on a branch. Check the new build in a dev install
   on the node with a private Camoufox cache, so nothing lands in root's `~/.cache/camoufox`, which
   agentosd uses:
   ```
   cd /srv/agentos-dev/<name>
   export XDG_CACHE_HOME=$PWD/.cache        # camoufox keeps its builds in $XDG_CACHE_HOME/camoufox
   .venv/bin/python -m camoufox path        # must print a path under this checkout
   .venv/bin/python -m camoufox fetch
   OPENAJAR_HOME=$PWD/.agentos-home .venv/bin/openajar lane stealth-check --slot <S> --lanes 2 \
     --relaunches 2 --os windows,linux --soak-minutes 2 --kill-every 30 --out /tmp/stealth-new
   ```
   and compare with docs/design/stealth.md, section 3. Any new failure: do not roll it out.
2. Roll out: merge, then on the node `cd <checkout> && git pull && sudo openajar upgrade --source .`.
   It drains the lanes, runs the installer, migrates, restarts agentosd and puts the previous code
   back on a failed health check. The installer fetches the build the new package pins into root's
   `~/.cache/camoufox`, beside the old one (camoufox launches only the build its package pins, so
   nothing else changes the browser). `sudo openajar doctor` then names the build on its camoufox
   line. Warm lanes come back cold; on each lane's first launch after that, agentosd moves the pin's
   Firefox version to the new browser's (only the user agent's version; the lane event
   `fingerprint_version_bumped` and `fingerprint.json.firefox-<old>.bak` record it), and
   `openajar lane audit` shows no freshness warning.
3. Roll back: `git checkout <old> && sudo openajar upgrade --source .` (or restore the snapshot of
   the prefix the upgrade printed). The old package launches the old build again, which is still in
   the cache, so nothing is fetched. Then put each moved lane's version back before it warms, with
   `openajar lane refresh-fingerprint <ws/name> --keep-device --version <old>` (or by restoring its
   `fingerprint.json.firefox-<old>.bak`): agentosd never moves a version back by itself, and a pin
   that claims a newer Firefox than the browser fails `openajar lane audit`. The lane events
   `fingerprint_version_bumped` list the lanes that moved.

## Production install

```
git clone <the openajar repository URL> openajar && cd openajar
sudo deploy/install-node.sh --systemd --user agentos --hardened
openajar logs -f                 # or: journalctl -u agentosd -f
```

This copies the checkout to `/opt/agentos`, creates `/var/lib/agentos` (chmod 700),
runs `openajar node init` once and enables `agentosd.service`. To upgrade, pull and
run the installer again; it restarts the service, unless lanes other sessions hold are open:
then it names them, installs without restarting and exits 3 (`--yes` restarts anyway; `openajar
node restart` asks first, see deploy/README.md "Restarts and the lanes they close"). A connector
install needs no restart: agentosd reloads its tools by itself. On stop, systemd sends SIGTERM to
agentosd only (`KillMode=mixed`), which closes every browser gracefully so cookies are
flushed, and kills what is left after 30 seconds.

A lane's live view is x11vnc on a unix socket, `OPENAJAR_HOME/vnc/display-<n>.sock` (the directory is
0700), not a TCP port. Each display also has an Xauthority file beside it, `display-<n>.xauth` (mode
0600, a fresh cookie per start): Xvfb runs with `-auth` on it, and x11vnc and the browser get it
through XAUTHORITY, so a local user other than agentosd cannot connect to the display even though
Xvfb listens on an abstract unix socket. Lanes warmed by a build from before that keep their TCP
listener until agentosd restarts (the installer and `openajar upgrade` restart it; the lanes come
back cold), and `openajar doctor` fails its displays check while one is left.

After an install or an upgrade, run the pre-flight on the node. It drives one scratch lane
(`preflight/scratch`, no proxy, no account) through every layer the way an agent does, then
deletes it. The layers are MCP, an approval, take control and hand back, the egress guard,
the lane check, freeze (it revokes only the scratch lane's own grant: a second grant for the
whole `preflight` workspace is kept, refused on the frozen lane and working again after the
unfreeze), the audit log, the logs and the panel API. On a node with no UI built it skips the
panel page with a note. It visits only https://example.com and httpbin.org's sample form, and
prints one line per step:

```
OPENAJAR_HOME=/var/lib/agentos /opt/agentos/.venv/bin/python scripts/preflight_node.py
```

Each run leaves three test decisions in the `preflight` workspace's memory. The audit replay
reads "not sealed yet" for the newest rows until the next signed checkpoint: one every 10
minutes while actions keep coming, every 1,000 rows, and at a restart.

### Running agentosd as its own user instead of root

<!-- legacy-name: begin (a server node's service user is still named agentos: docs/LEGACY-NAMES.md) -->
A node installed with `--systemd` alone runs agentosd as root, and `openajar doctor` warns about it
(`service.user`). One command moves it to the unprivileged `agentos` user with the hardened unit
(deploy/agentosd-hardened.service), and one command puts it back. On the node:

```
sudo /opt/agentos/.venv/bin/openajar migrate-service-user            # asks first; --yes to skip
sudo /opt/agentos/.venv/bin/openajar doctor                          # service: active as agentos
```

It checks first and changes nothing when something would break or get worse: it needs root, systemd
and OPENAJAR_HOME outside /root and /home; brains must keep a user of their own (brains settings
`run_as`, default `agentos-brain`) that is not root, not `agentos`, not a login account and not in
the `agentos` group, a privileged group (adm, sudo, docker, systemd-journal, disk and the like) or a
group that owns anything in OPENAJAR_HOME, and the confinement check must not be off; the prefix must ship
`deploy/agentos-runas.service` (upgrade an older one first). It refuses while isolated terminal or
service lanes exist (they need root) or while its env database cannot be read, and when `--user` is
an existing login account (its vault would go to a person's account). Then it saves the installed
unit, /etc/logrotate.d/agentos, any agentos-runas unit and the owner of OPENAJAR_HOME to
`/etc/systemd/system/agentosd.service-user-rollback.json` (0600), drains the warm lanes, stops
agentosd, runs the prefix's own installer (`--systemd --no-restart --skip-apt --user agentos
--hardened`: it creates the user, gives it OPENAJAR_HOME, copies root's Camoufox into
OPENAJAR_HOME/.cache, creates the brain user named by `run_as` if needed, passed to it as
OPENAJAR_BRAIN_USER, and installs agentos-runas for that user), starts agentosd and checks it:
active as `agentos`, MCP answers (401 without the token, 2xx with it), OPENAJAR_HOME is private to
`agentos`, agentos-runas is active and serves that brain user, and `openajar brains confinement` run
as `agentos` passes. If any of that fails, it rolls back on the spot. Warm lanes come back cold; open them again
as usual.
<!-- legacy-name: end -->

How brains start without root. A brain is the owner's agent CLI with a Bash tool, so it must never
run as `openajar` (which owns the vault key, the node token and the live views). A root agentosd
drops to `agentos-brain` itself. An unprivileged one cannot, and its hardened unit sets
NoNewPrivileges, so sudo and setuid programs are out. The one privileged piece is agentos-runas
(`deploy/agentos-runas.service`, `src/openajar/runas.py`), a small root service with a unix socket
in `/run/agentos-runas/` (directory 0750, socket 0660, both group `openajar`) that also checks every
peer's uid is `openajar`. It does one thing: start a program either as the brain user (attached:
agentosd relays signals, gets the exit code, and closing the connection kills the program and its
scope) or as `openajar` itself (detached, for the per-task supervisor so a task survives an agentosd
restart). Both users are fixed on its command line by the installer, never chosen in a request. A
request is one JSON object with a fixed schema (argv, env, cwd, the three stdio descriptors, an
optional `agentos-brain-*` scope with MemoryMax, TasksMax and CPUQuota inside fixed bounds, and
rlimits that only go down), at most 1 MiB and four levels deep, read under a timeout; anything
else is refused, and the descriptors that came with it are closed. A brain starts through fixed
programs run as root with the helper's own environment (prlimit, `unshare --pid --fork
--mount-proc`, `setpriv` for the drop), then `env -i` with the request's environment, so each
brain is alone in a PID namespace: brains share one uid, and this is what keeps one from reading
another's `/proc/<pid>/environ` (its child grant token). The helper moves the namespace into its
scope over D-Bus and only then lets the program run. When the caller hangs up, the namespace's
init is killed and the kernel kills the rest, setsid or not. Every attached brain is recorded in
`/run/agentos-runas/brains/` while it runs; a helper that crashes or is killed has its brains
swept by ExecStopPost and again by its next start. `whoami` tells agentosd which brain user it
serves: agentosd starts brains through it only when that is `run_as`, and the confinement probe
must report exactly that uid. It refuses to start when either user is root, when they are the
same, when the brain user is in `openajar`'s group or a privileged group, when util-linux's
prlimit, unshare or setpriv is missing, or when the code it runs as root (this one file, the
interpreter and the standard library: it runs as a script with `python -I -S`, so no
site-packages and no `.pth` file) can be changed by anyone but root. Its unit sets NoNewPrivileges
and RestrictSUIDSGID, which every brain inherits, and keeps CAP_SYS_ADMIN in its bounding set for
the namespaces. A root agentosd starts its brains through the same chain. A polkit rule was not used: systemd hands polkit the
unit name and verb for a transient unit, never its properties, so a rule that lets `openajar` start
`agentos-brain-*` units would also let it start one with `User=root`.

Before each brain starts, agentosd runs the AUD-08 confinement check as the brain user, started
through the same path (src/openajar/brains/README.md has the list of what it tries). To run it by
hand, as the service user:

```
sudo runuser -u agentos -- env OPENAJAR_HOME=/var/lib/agentos /opt/agentos/.venv/bin/openajar brains confinement
sudo /opt/agentos/.venv/bin/openajar env confinement <lane>        # the same probe inside an isolated env lane
systemctl status agentos-runas                                    # the helper
```

What a hardened node cannot run. Sandboxed terminal and service lanes (presets sandbox, locked and
service, and so `openajar run`'s default lane) need agentosd to run as root: a sandbox is more than
bubblewrap's namespaces, and the rest is done outside bwrap as root (the overlay mount of the base
image and the lane's kept files, the loop-mounted disk cap, the network namespace whose only route out
is the egress forwarder, and the cgroup that caps and kills its programs). Unprivileged bubblewrap with
user namespaces would need each of those reworked, and Ubuntu 24.04 and later restrict unprivileged user
namespaces by default, so this is not done. On a hardened node `openajar env create` and `terminal_create`
refuse such a lane (`no_sandbox`, nothing is created), `terminal_exec`, `terminal_job_start`, `shell_open` and
`service_start` on one made earlier fail before anyone is asked to approve, and `service_status` says
why in `detail`. The choice is the owner's: go back to root (below), or a workbench lane.

A workbench lane has no sandbox at all: its commands run as the user agentosd runs as. On a hardened
node that is `openajar`, which owns the vault, the node token (node.toml) and every lane's browser
profile, and can reach the node's own MCP, which is what brains are kept off it for. So a workbench
lane is made only by the owner, only with `openajar env create WS/NAME --preset workbench
--can-read-node-secrets`; its isolation line says which user it runs as and what that can read; every
command an agent sends to it (`terminal_exec`, jobs, shells, the panel's terminal for a grant) waits for a
person, never a standing approval and whatever the routing table says; and `openajar doctor` warns
(`env.workbench`) while one exists. Remove it when the work is done (`openajar env remove WS/NAME`,
which goes through the running agentosd, as `openajar env doctor` does).

The same holds for any lane whose filesystem or processes are shared, whatever its other dials: a
sandbox with `--filesystem shared` binds the node's `/`, and one with `--processes shared` can reach
the node's files through other processes, so `--preset workbench --network off` or `--preset sandbox
--filesystem shared` needs the flag too, says so in its isolation line, and every agent command in it
(also `terminal_install`, `terminal_upload` and `service_start`) waits for a person. A shared network alone
does not count: every service on the node's loopback asks for a secret the lane cannot read. A lane
made before this rule is treated the same way, with no migration (`openajar.env.exposure`).

To go back to root, any time later:

```
sudo /opt/agentos/.venv/bin/openajar migrate-service-user --rollback
```

That stops agentosd, puts the saved unit and logrotate file back, disables and removes agentos-runas
(or puts back the one that was there before the switch), gives OPENAJAR_HOME back to its old owner
(`chown -R`), starts agentosd, checks it as root, and only then deletes the saved state. When
the check fails, the state file stays and the command says so; `journalctl -u agentosd -n 50` shows
why. The `openajar` and `agentos-brain` users and OPENAJAR_HOME/.cache are left in place (harmless,
and the next switch reuses them). `openajar upgrade` after a switch needs `--user agentos --hardened`, or it installs
the root unit again.

## Connecting Claude Code

From the Mac, `openajar connect mynode` does all of it (src/openajar/client/README.md): the
tunnel, a grant of its own for Claude Code (`claude-code@<your Mac>`, every lane, up to
commit), and your owner credential for `openajar dash`, `openajar approvals` and `openajar open`.
The node token stays on the node: connect reads it over SSH for two calls and never stores it.

By hand, keep the tunnel open and give the agent a grant on the node:

```
ssh -N -L 7400:localhost:7400 mynode
openajar grants issue --subject claude-code --workspace '*' --ceiling commit --ttl 90d   # on the node
claude mcp add --transport http openajar http://localhost:7400/mcp --header "Authorization: Bearer <grant token>"
```

For a dev daemon, use its port (7430 for mcp-cli) in both lines.

Then, from Claude Code, the loop is `browsers_list`, `browser_navigate`,
`browser_snapshot`, `browser_act` with a ref, and `browser_view` when a human has to log
in or pass a CAPTCHA. `browser_view` returns the tunnel command for the live viewer.

What your agents ask for, you approve from the Mac (`openajar dash`, or `openajar approvals
list` and `openajar approvals approve <id>`), with the owner credential: a token never
decides what its own grant family asked for, and the agents' grant decides nothing.

### Moving off the node token

Connections made before agent grants gave Claude Code the node token. They keep working;
`openajar doctor` on the Mac warns about them. To move:

1. On the Mac: `openajar connect --upgrade` (or `openajar connect mynode` again).
2. Check: `openajar doctor` on the Mac shows no warning; `openajar nodes` shows a `grant gr_...`.
3. On the node, when every computer is moved: `openajar node rotate-token` and
   `systemctl restart agentosd`. Old copies of the node token (a `claude mcp add --header`
   pasted earlier) stop working; agent grants and owner credentials keep working.

## Running a command on the node: `openajar run`

A long test suite does not have to hold the laptop. From a git checkout on the computer that ran
`openajar connect`:

```
openajar run -- make check
openajar run --node mynode -- uv run pytest -q tests/unit -k policy
openajar run --detach -- make check          # prints the job id; then:
openajar run --follow job-1a2b3c4d5e         # stream it (Ctrl-C stops following, the job keeps running)
openajar run --logs job-1a2b3c4d5e           # what it printed so far
openajar run --list                          # the jobs on the node
openajar run --cancel job-1a2b3c4d5e
```

What it does, all through the connection's tunnel and its agents' grant (the MCP tools any agent
has, so the node's policy, approvals and audit trail apply as they do to an agent):

1. **The lane**: `dev/run-<repo>` (`--workspace`, or `--lane ws/name`), a sandbox terminal lane with
   a kept (overlay) filesystem, 4 GB of memory, 2 CPUs and 8 GB of disk (`--memory`, `--cpus`,
   `--disk`, up to the node's `agent_max_*` caps in `env.toml`). It is made with `terminal_create` on the
   first run; making it is the first command in that lane, so a person approves it once.
2. **The files**: what `git ls-files --cached --others --exclude-standard` lists (tracked files,
   and untracked ones `.gitignore` does not exclude), cut into 1 MB chunks named by their sha256.
   `terminal_upload_check` says which chunks the node lacks and `terminal_upload` sends only those, so the
   second run sends only what changed. The tree lands in `~/src/<repo>` in the lane, written by the
   lane's own `tar` inside its sandbox. Files deleted here are deleted there; what commands made
   there (`.venv`, `node_modules`, caches in `~/.cache`) stays for the next run. `--fresh` wipes the
   directory first.
3. **The run**: `terminal_job_start` applies the sync and starts the command in the same directory
   relative to the repo as here, in the background, with its output logged on the node. stdout
   and stderr come back as they are written (`terminal_job_read`, a long poll from byte offsets),
   `openajar run` exits with the command's exit code (124 after `--timeout`, 130 when cancelled),
   and Ctrl-C cancels the command on the node (`terminal_job_cancel`). Status lines go to stderr.
   `PYTHONUNBUFFERED=1` is set, so Python's output arrives line by line through the pipe.

**The grant.** The connection's grant needs the `exec` special, which `openajar connect` does not
give by default: `openajar connect mynode --special exec --special memory_read`. Without it,
`openajar run` says so before it does anything. When a step needs a person (the lane's first
command, or every command once the grant's agents have read untrusted text such as a web page:
policy/ROUTING.md, "After untrusted content"), `openajar run` prints what waits and the Needs you
link, and waits; nothing runs until it is approved. The sync and the command it runs are one
approval.

**The network.** A new lane has no network. A command that installs packages needs its index,
named when the lane is made, and reached only through the lane's egress forwarder:

```
openajar run --allow-host pypi.org --allow-host files.pythonhosted.org -- uv sync
openajar run --allow-host registry.npmjs.org -- npm ci
```

**Git.** A command's git reaches the remotes the owner declared for the lane's workspace through
the node, with the vault's deploy key, also in a lane with no network: after `openajar git key add`
and `openajar git remote allow dev github.com/me/app` on the node, `openajar run -- git push origin
my-branch` pushes (a commit on the lane; a force-push or a push to the default branch asks a person).
No `--allow-host` for the git host is needed, and none should be given. See [GIT.md](GIT.md).

`--allow-host` applies when the lane is made; `--recreate` makes it again with new hosts, size or
`--image` (deleting a lane always asks a person). Lanes see the node's `/usr`, which usually has no uv:
install it into the lane once, for example
`openajar run --allow-host pypi.org --allow-host files.pythonhosted.org -- "python3 -m venv ~/.tools && ~/.tools/bin/pip install uv"`,
then `openajar run -- "~/.tools/bin/uv run pytest -q"`. A node that built the dev-box image can
start the lane from it with `--image dev-box@1` (git, Python with pip, Node.js with npm).

**On the node.** Jobs keep their output in `<OPENAJAR_HOME>/lanes/<ws>/<name>/logs/jobs/` (64 MB
per stream, the newest 20 finished jobs per lane), uploaded chunks in `.../sync/` (4 GB per lane).
`env.toml` changes these: `job_log_max_bytes`, `jobs_keep_per_lane`, `upload_max_mb`,
`max_jobs_per_lane`, `max_timeout_s` (an hour by default; longer suites need more).
`openajar dev up` runs only workbench lanes on a Mac, so try it there with a lane the owner made
(`openajar env create dev/box --preset workbench --can-read-node-secrets`, then `openajar run --lane
dev/box -- ...`; every command waits for a person there).

**On a hardened node** (agentosd as the `agentos` user, `openajar migrate-service-user`) no sandboxed
lane can run, so the default `dev/run-<repo>` lane is refused with `no_sandbox` and nothing is made.
`--lane` with an owner-made workbench lane works, with what that gives away (it runs as `openajar` and
can read the vault and the node token; see "What a hardened node cannot run" above). On a root node
nothing changes.

## Your dev server in a lane

To try your own app with a lane's real, logged-in accounts ("Sign in with LinkedIn" and the redirect
back, an embed, a cookie a third party sets), open your laptop's dev server in that one lane:

```
$ npm run dev                                   # Vite on localhost:5173, in another terminal
$ openajar expose 5173 --lane personal/linkedin --name myapp --for 2h
https://myapp.expose.openajar.invalid/ now opens your localhost:5173 in personal/linkedin on mynode, until 2026-11-08T16:05:00Z (UTC).
Only that lane reaches it. Press Ctrl-C to end it.
^C
Ended.
```

Open `https://myapp.expose.openajar.invalid/` in the lane (`openajar open personal/linkedin`, or an
agent's `browser_navigate`: `browsers_list` shows the address under `exposed` while it lasts). In your
OAuth app's settings, add `https://myapp.expose.openajar.invalid/<your callback path>` as a redirect
URI. Your app sees `Host: myapp.expose.openajar.invalid` and `X-Forwarded-Proto: https`, so redirect
URIs it builds from the request come out right. Vite refuses a Host it does not know: add
`server: { allowedHosts: ['.expose.openajar.invalid'] }` to `vite.config`.

`openajar lane forward` is the other direction: a port of a terminal or service lane on your laptop's
localhost, for example the dev server a service lane runs:

```
$ openajar lane forward dev/web:3000 --local 8080
http://localhost:8080 now reaches port 3000 in dev/web on mynode, until 2026-11-08T16:05:00Z (UTC). Press Ctrl-C to end it.
```

**How it works.** `openajar expose` asks the node, with your owner credential (an agent's grant is
refused), to reserve the address on that lane, then runs `ssh -R <socket>:localhost:5173` to the
same host as the connection's tunnel. sshd binds a unix socket in `<OPENAJAR_HOME>/run/expose/` (mode
600, agentosd's; a short remote command hands it over when you sign in as root). The lane's browser
gets one more route: requests for that origin are answered from the socket inside the browser's
driver, never sent to the network, and the lane's egress guard counts the host as on its site list
and every grant's egress list while it lasts. Every other request goes as before: through the lane's
proxy, with its fingerprint. `openajar lane forward` has the node open a socket into the lane's own network
namespace instead, and `ssh -L 127.0.0.1:<local>:<socket>` reaches it. Neither opens a port on the
node.

**When it ends.** Ctrl-C, `--for` (2 hours by default, at most 12), the SSH session ending, or the
node not hearing from your laptop for 30 seconds (it renews every 10 through the tunnel: a laptop
that sleeps or loses the tunnel ends it). Then the route goes and the name no longer resolves, in that
lane or anywhere. `openajar status` shows what is exposed, and the action log has each start and end.

**What does not work (yet).**

- **WebSockets**, so no hot reload: Vite says it lost the server; reload the page by hand. (A
  WebSocket's handshake is never routed, and Playwright's WebSocket routing would change every page's
  `WebSocket` in that browser, on every site.)
- **Server-sent events and long polls**: an answer arrives only once it ends (a route fulfils whole
  bodies).
- **Share previews and server-side fetches**: LinkedIn's servers fetching your page for a preview
  cannot reach it. Only the lane's browser can.
- **An OAuth provider that insists on a public domain** (Google refuses redirect URIs on a reserved
  name). LinkedIn, GitHub and most others accept them.
- **The HTTP cache is off in that lane while an expose is on** (any route does this,
  docs/design/egress-guard.md 3.2): pages there load everything again.
- **A redirect that posts** (307 or 308 with a body) to the exposed address fails; a 302 or 303 back
  (what OAuth does) works.

**The address.** `.invalid` is reserved (RFC 6761): no resolver anywhere answers for it, so anything
that is not that lane's route (another lane, a redirect hop the route cannot see, the lane after the
expose ended) fails as a name that does not exist, and never reaches another machine. `localhost`
would fall through to the node's own loopback, and `.internal` can be answered by a resolver on the
node's network. https, because the browser never connects for it: the page is a secure context, so
`Secure` cookies and https-only redirect URIs work, and a redirect hop that does leave the browser
carries only the host name to the lane's proxy, never the path or the code in its query.

## Lanes and proxies from the CLI

```
openajar proxy add lt1 1.2.3.4:12323:user:pass     # or leave out :pass to be prompted
openajar proxy list                                # never shows credentials
openajar lane create personal/reddit --proxy lt1
openajar lane import personal/reddit --profile /path/to/profile --fingerprint fp.json --proxy lt1
openajar lane list
openajar device create biz-a-pc --proxy lt1            # one computer for a company's lanes
openajar device create biz-a-pc --from-lane biz-a/instagram   # or the one its site already knows
openajar lane create biz-a/facebook --device biz-a-pc
openajar lane set-device biz-a/reddit biz-a-pc         # shows what its site will notice; --yes to move
openajar device list | show <name> | remove <name>
```

Which proxies to buy and how lanes share devices: [PROXIES.md](PROXIES.md). Decide it before the
first logins.

Lane commands use the store through a LaneManager directly, so they work without the
daemon. They never call `start()`, so they cannot sweep the browsers of a running
daemon.

What the commit gate held or let go without a rule while agents drove a lane (a site's own
logging, a read no rule names), and promoting a shape from it (docs/design/egress-guard.md 8.9):

```
openajar lane gate-log personal/reddit --since 24h        # each shape: host, path, method, operation, field names, counts
openajar lane gate-log personal/reddit --allow-read sh_3f2a9c0e1b7d          # it publishes nothing
openajar lane gate-log personal/reddit --as-commit post.scheduled --shape sh_3f2a9c0e1b7d
openajar lane gate-log personal/reddit --remove sh_3f2a9c0e1b7d              # undo
```

Promotions are the node's own (`OPENAJAR_HOME/gate-shapes.json`, not code), each change has a
`gate_shape` row in the action log, and every lane's guard reads the file within two seconds. The
owner only: the CLI on the node, the node token, or the owner's panel session
(`GET`/`POST /api/lanes/{ws}/{name}/gate-log`); a member's session or a grant is refused. Promote a
read only when you know the request publishes nothing: a wrong read lets a publish go without a
person, while a wrong commit rule only asks once too often.

## Add a VA

A VA, a teammate or a client signs in to the panel with their own passkey and sees only the
workspaces you give them. Nothing changes for a solo node until
the first invite. On the node, with the daemon running:

```
openajar members invite va --role approver --workspace biz-c --ttl 90d
```

It prints a link that works once, for 24 hours, and a QR code (black on white, on a terminal or
with `--qr`). The link uses `public_base_url` when it is set, so it opens on the VA's phone over
the tailnet; without it the link is `http://localhost:7400/invite#code=...` and the phone needs
the tunnel. The VA opens it, picks the name others see, makes a passkey (Face ID, Touch ID or the
screen lock), and lands in My notifications: their own ntfy topic to subscribe to, their
timezone, quiet hours (21:00 to 08:00 by default) and digest times.

From then on their panel has Home, Needs you, Activity and Settings, for biz-c only. They approve
biz-c posts (the node checks their member grant), with a passkey check when the last one is older
than 15 minutes, and every time for a payment or something that cannot be undone. Needs you's
History says "Approved by va with a passkey", and Activity names them by their handle.

```
openajar members list                                     # people, roles, passkeys, open invites
openajar members role va --role approver --role operator -w biz-c --watch   # replaces biz-c's roles
openajar members role va --none -w biz-c                  # takes biz-c away, keeps the rest
openajar members remove va                                # asks first; --yes to skip
```

Roles: `viewer` (looks, may deny), `approver` (decides posts; `--max-usd 50` adds payments up to
that), `operator` (opens lanes, runs flows, watches; an approver still approves), `admin` (the
workspace, its members and vault entries), `auditor` (evidence, from phase 3). `--watch` lets a
viewer or approver see the live view, which shows whatever the account shows. A role can be
`approver@biz-c`; repeat `--role` for several. Removing someone ends every session at their next
click, deletes their passkeys, revokes their member grants and their agents' grants, and retires
their push topic.

The same is in the web app under Settings, Members (invite, roles, devices, remove). To try it
with no node at all, run the web app on its mock (`pnpm dev` in web/, [web/README.md](../web/README.md)).

## Console and network for agents (devtools)

`browser_console` and `browser_network` (the `devtools` toolset, Effect read) let an agent that
tests a web app in a lane see why a page broke. `src/openajar/node/devtools.py` keeps the data:
one `DevtoolsCapture` per browser context, attached with the lane's `PageTracker` when its browser
starts (on the dev node, with the tab's fake network). It only listens to Playwright's context
events (`console`, `weberror`, `request`, `response`, `requestfinished`, `requestfailed`): nothing
is injected into the page and no route is added. `src/openajar/node/devtools_tools.py` has the tools.

What is kept, per lane, in two ring buffers (500 entries and about 256 KB of console, 500 entries
and about 512 KB of requests; older entries are dropped and counted):

- console messages and uncaught page errors: level, text (cut to 4000 characters), where it was
  logged, the page's URL and a page error's stack (15 lines);
- requests: method, URL, resource type, status, time taken, failure text, content type, and the
  request and response headers with every sensitive one masked.

What is never returned:

- request bodies (only their size), cookies, `Set-Cookie`, `Authorization` and any header named
  like a token, key, session or password;
- the value of a query or fragment parameter named like a secret (token, code, password, key,
  session, signature) or shaped like one (a JWT, a long random string), anywhere a URL appears
  (in a body or a console message too), and secret-named pairs in JSON, form bodies and hidden or
  password inputs;
- the body of a login's answer: a POST sent inside the vault's login window, to a sign-in, token
  or session address, or answered with a cookie;
- a response body from a host the lane's egress guard would not load for this caller (the lane's
  site list, the grant's egress list).

Response bodies are never buffered: `browser_network(id=...)` asks the browser for that one body
(text types only, at most 2 MB read and 20,000 characters returned) and says plainly when it is
gone. Everything returned also passes the vault scrub like every tool result, and is labelled
untrusted page data (it taints the grant family as a snapshot does). Tests:
`tests/unit/test_devtools_capture.py` and `tests/unit/test_devtools_tools.py`; on a node,
`tests/node/test_devtools_node.py`.

## Where things live

- `src/openajar/node/tools/`: the node's own MCP tools as ToolSpecs, one module per group (`lanes`,
  `browser`, `wait`, `recipes`), with `tool_specs` in `__init__.py`. Every browser tool runs inside a
  `lanes.using(on, ...)` session (`session.lane_session`), which yields the lane's page.
  `src/openajar/node/io_tools.py` holds the file, upload, tab and dialog tools.
  `src/openajar/mcp/registry.py` wraps every call (grant, arguments, approvals, redaction), and
  `src/openajar/mcp/server.py` is a compatibility front for older callers. Errors from
  `openajar.core.errors` come back as tool errors that start with their code (`busy: ...`);
  anything else is `engine_error: ...`. Every result and error passes through a redactor
  (`src/openajar/policy/redactor.py`) that masks known secrets, the proxy logins among them. Only http
  and https URLs reach the browser (`check_url` in `src/openajar/node/interaction/urls.py`).
- `src/openajar/node/daemon.py`: the composition root. `_build_lane_manager` and
  `_build_interaction` are the only code that names the concrete classes.
- `src/openajar/cli.py`: the `openajar` command.
- `src/openajar/node/devices.py`: devices (AUD-02), one pin and one proxy shared by a company's
  lanes; the lane manager launches a device lane with `devices/<name>/fingerprint.json` and keeps a
  copy in the lane's directory.
- `src/openajar/dev/`: the laptop node (`openajar dev up`): `node.py` is the composition,
  `lanes.py` the lane managers, `web.py` the fake web the lanes browse, `display.py` a fake VNC
  display per lane, which the live view draws, `golden.py` the golden traces.
- `src/openajar/node/lanefmt.py`: the lane directory's format version (`lane-format.json`) and its
  migrations, run by `openajar migrate run` beside the SQLite ones.
- `deploy/`: the installer (`install-node.sh`), the systemd units and backup timer, and the node
  templates. `scripts/`: the helpers: the CLI installer (`install.sh`), the node's sync, test and
  check scripts (`sync-node.sh`, `node-test.sh`, `preflight_node.py`), the benchmarks, and the
  public export (`public_export.py`).

## Layers and size limits

`make lint` (and `make merge-check`, and CI's lint step) runs two structure checks:

- `make layers`: no import goes up a layer ([ARCHITECTURE.md](ARCHITECTURE.md), section 13). The
  imports that still do are in `scripts/layers.toml` with a reason; delete a line when you fix it.
- `make complexity`: a function of cyclomatic complexity 25 or more (ruff's C901), or a file over
  1,000 lines, fails unless `scripts/complexity-allowlist.txt` or `scripts/file-length-allowlist.txt`
  lists it, and a listed one must not grow. Both lists only shrink:
  `uv run python scripts/complexity.py --update` deletes what you fixed and lowers the numbers.

## Releases

A release is a `vX.Y.Z` tag on the public repository's `main`, with a GitHub release. The
installer (`scripts/install.sh`) installs the newest such tag unless `OPENAJAR_GIT_REF` names
another ref, so people get a release, never whatever `main` is today. To cut one:

1. Set the version in `pyproject.toml`, `src/openajar/__init__.py`, `integrations/npm/package.json` and
   `server.json` (its own `version` and each package's), then `uv lock`.
   `python3 scripts/check_release_versions.py --tag vX.Y.Z` says whether they all agree with the tag
   (tests/unit/test_release_versions.py checks that they agree with each other).
2. In `CHANGELOG.md`, rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` and open a new empty
   `[Unreleased]` above it.
3. Push to `main`, wait for CI to pass there, then tag it and make the release as a **draft**:
   `git tag vX.Y.Z && git push origin vX.Y.Z`, then
   `gh release create vX.Y.Z --draft --verify-tag --title "openajar X.Y.Z" --notes-file NOTES.md`, with the
   notes taken from that version's changelog section.
4. Start `web-release` for the tag: `gh workflow run web-release.yml -f tag=vX.Y.Z` (GitHub sends no
   release events for a draft, so creating one starts nothing). It builds the web app in live mode and attaches
   `openajar-web-X.Y.Z.tar.gz` and its `.sha256` to the draft (`scripts/web_asset.py`; `make web-asset`
   makes the same archive locally). It never replaces a file: if it failed halfway, delete the asset from
   the draft (`gh release delete-asset vX.Y.Z <file>`) and run it again from Actions > web-release > Run
   workflow with the tag.
5. Check both files are on the draft, then publish it: `gh release edit vX.Y.Z --draft=false`. The
   repository has **release immutability** on (Settings > General > Releases), so once published no file
   or tag of the release can change. `openajar web install` and setup's web step trust only GitHub's own
   digest of the archive on an immutable release (docs/THREAT-MODEL.md, "The web app's download"), so a
   release published before its web app was attached, or with immutability off, has no installable web app.
   Publishing also starts `env-image`, which pushes the docker provider's machine image
   (`ghcr.io/<owner>/agentos-env:X.Y.Z`).

6. Publishing the release also starts `publish` (.github/workflows/publish.yml), which puts the same
   version on the three registries, in this order:
   - **PyPI** (`openajar`, sdist and wheel): trusted publishing, no token. It checks the versions
     again, builds with `uv build`, tries the wheel (`openajar --version`, `openajar mcp` answers
     initialize), then publishes from the GitHub environment `pypi`. The long description is
     README.md, whose `<!-- mcp-name: com.openajar/openajar -->` line is what the MCP Registry
     reads to accept the PyPI package.
   - **npm** (`openajar`, integrations/npm): `npm test`, then `npm publish --provenance --access
     public`, signed in by trusted publishing (OIDC), or by the `NPM_TOKEN` secret when it is set.
     The registry accepts the package by its `mcpName` in package.json.
   - **the MCP Registry** (`com.openajar/openajar`, server.json): it waits until PyPI and npm serve the
     version, runs `mcp-publisher validate`, signs in as the domain (`mcp-publisher login http --domain
     openajar.com --private-key` with the `MCP_REGISTRY_KEY` secret) and runs `mcp-publisher publish`.
   Check each one after: https://pypi.org/project/openajar/, https://www.npmjs.com/package/openajar and
   `curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=com.openajar/openajar"`.
   When one job fails, fix the cause and use Re-run failed jobs (or Actions > publish > Run workflow
   with the tag): PyPI skips files it already has, and npm and the registry refuse a version they
   already have, so a job that passed is not run again.

   Set up once, before the first release:
   - PyPI: on https://pypi.org/manage/account/publishing/, a pending trusted publisher for the project
     `openajar`: the repository github.com/sabalys-ramunas/openajar, workflow `publish.yml`,
     environment `pypi`. In the repository, Settings > Environments > New environment `pypi` (add yourself as a
     required reviewer to approve each upload).
   - npm: publish 0.5.2 (or the first version) once by hand from `integrations/npm` (`npm login`, then
     `npm publish --access public`), then on npmjs.com > openajar > Settings > Trusted publishing add
     GitHub Actions for the repository github.com/sabalys-ramunas/openajar and the workflow
     `publish.yml`. Until then, an automation token in the `NPM_TOKEN` secret works too.
   - The MCP Registry: an Ed25519 key pair (`openssl genpkey -algorithm Ed25519 -out key.pem`). Serve
     `v=MCPv1; k=ed25519; p=<public key>` at
     `https://openajar.com/.well-known/mcp-registry-auth` (the public key: `openssl pkey -in key.pem
     -pubout -outform DER | tail -c 32 | base64`), and put the private key, as hex, in the
     `MCP_REGISTRY_KEY` secret (`openssl pkey -in key.pem -noout -text | grep -A3 "priv:" | tail -n +2 |
     tr -d ' :\n'`).
   - MCP marketplaces: glama.json at the repository root names the maintainer for glama.ai. The other
     directories (Smithery, mcp.so, PulseMCP and the like) read the MCP Registry or take a link to the
     repository; each lists the server as a command: `npx -y openajar`, or `uvx openajar mcp`.

Before 1.0, a minor version may change tools or settings; say so in the release notes.

## Troubleshooting

- **401 from /mcp**: the token Claude Code sends is not accepted: an agent grant that
  expired or was revoked (`openajar connect mynode --new-grant` on the Mac), or an old copy of
  the node token after `openajar node rotate-token` (`openajar connect --upgrade`).
- **421 Invalid Host header**: the MCP endpoint only answers requests addressed to
  `localhost` or `127.0.0.1` (DNS rebinding protection). Connect through the SSH tunnel.
- **`busy: ...`**: every warm slot is in use. Close a lane or raise `max_warm_lanes`.
- **`openajar.node.lanes is not part of this build yet`**: the lane manager has not
  merged into this branch.
