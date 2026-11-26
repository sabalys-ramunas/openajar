# Environments: a cloud computer for each agent

Status: being built (branch t3/environments). This page is the plan and the contract between the
builders; docs/ENVIRONMENTS.md is the user guide.

## What openajar is

openajar gives every agent its own computer in the cloud. You open a chat, and when the agent
decides it needs one (or you tell it to work in openajar), it asks for an **environment**: a fresh
VM with a terminal, a real browser and your repository on it. It works there, not on your laptop,
commits and pushes a branch, and when it is done the VM is deleted. You can watch its browser and
its commands live, from the web app or from the chat (T3 Code, Claude Code, Codex), and take over.

The trust layer is what makes that safe to leave running: grants say what an agent may do, the
node's limits bound what environments cost, approvals ask you before payments and before acting
on your own accounts (never inside an environment), the vault keeps keys and tokens off the VM,
and the audit log records every call.

Everything else is a feature on top: studio's page checks (`ui_check`, `ui_diff`), profiles with a
brand, logged-in browser lanes for your own accounts and routines.

## The agent's story

1. A chat starts. The MCP intro tells the agent, in its first lines, that it can have its own cloud
   computer: `environment_open(title=..., repo=...)`.
2. `environment_open` rents a VM (the cheapest that fits, unless the owner set a size), clones the
   repo into `/work/<name>` through the node's git gateway, and opens a browser whose traffic goes
   through the VM. It returns the environment, its workdir, and a link for the human: the
   browser's live view, served by the node (no web app page: the public repository has none).
3. Every terminal and browser call **without `on`** goes to the environment of this chat:
   `terminal_exec(cmd="npm run dev", cwd=...)`, `terminal_job_start`, `shell_open`, `browser_navigate(url=
   "http://localhost:3000")`, `browser_snapshot`, `ui_check(url="http://localhost:3000")`.
   `localhost` in the browser is the VM's localhost.
4. The agent commits and runs `git push origin <branch>` on the VM. The push goes through the
   node's git gateway (the key never reaches the VM; its one gateway token reaches only this
   repository). It never asks a person, the default branch included; a force push or a delete of
   the default branch, or of a branch the environment did not create, is refused (`push_refused`).
5. `environment_close()` checks for unsaved work (uncommitted changes or commits not pushed),
   refuses with `unsaved_work` unless `discard=true`, then deletes the VM and the browser.
   If the agent forgets, the node closes it: idle for `idle_minutes` (default 60) or past its time
   limit. A VM with unsaved work is never deleted for idleness; it waits to the time limit and the
   owner gets a notification first.

## Decisions (owner, 2026-11-13)

- An environment is a **cloud VM**, rented by the node (Hetzner first, through `openajar.vms`).
- **One chat holds a live machine** (2026-11-16, the owner: "Only one agent chat should have access
  to the same machine at the same time, even if it is the same project. Another chat should only be
  able to request a new VM."). It replaces "the title names the computer" of the same day, under
  which a second chat of a 2026-07-28 client (no session id) attached to the first chat's machine by
  its title, or reached it with a call without `on`. Now `environment_open` gives the calling chat
  a handle (`on`: the id, a dot and 16 random letters and digits; the node keeps its sha256 in
  `Environment.lease`, never the handle). Only calls carrying it reach the machine
  (`EnvironmentManager.reach`); without a session id a call without `on` is `which_environment`
  even with one open, and a title or an id never reaches a live machine. A session client holds its
  machines by its session too, so its implicit calls work as before. The title of a live machine
  another chat holds is `environment_in_use` (fix: another title, like "<title> 2"); with
  `take_over=true`, once its holder made no call for `take_over_minutes` (10), it moves with a fresh
  handle, the old one stops, the chat that lost it is told (`handoffs`, with the old lease), and the
  move counts as a call. A machine kept live and let go has no holder. A saved one's title resumes
  it into a new machine leased to the resumer. Listings show no handle or id of a machine the
  caller does not hold. Concurrent opens of one title get one machine and one holder. No error
  tells an agent to close or discard an environment it did not open.
- **Whose it is** (2026-11-13). An environment belongs to the grant family that opened it (its
  root grant, and every grant narrowed from it) and records its opener: grant root, subject, and
  the member whose agent it is, if any. No other grant acts on its lanes, reads it or closes it,
  and to it the environment does not exist (`lane_not_found`, `no environment`); over MCP that
  holds for the node's token too. The owner sees and closes every one from the web app and the
  CLI, a member those of their own agents. Ids are `e` and 10 characters (31^10), not guessable.
- **Deleted when finished**, by the agent or by the reaper.
- **No asking inside** (widened 2026-11-13: "No questions ever if it runs in agents enviroment").
  Nothing in an environment waits for a person, before or after untrusted text: not opening it,
  not commands on the VM, not its browser (`env/<id>`, `env/<id>-browser`: exec, installs, jobs,
  shells, uploads, files, a click the commit gate reads as a publish, studio checks through its
  proxy), not a push. The VM holds only what the work needs. No account, no key; its browser no
  login and no vault entry; one gateway token that fetches and pushes `env.repo` only, good only
  through this VM's own tunnel, revoked when the environment closes or its lane goes down; no
  token at all without a declared repo. It reaches the VM's git config through stdin, never a
  command line on the node. The real guards stay, as limits and refusals (an error with a fix,
  never an approval):
  - Opening one costs money, so the node's limits bound it, and they hold without any command:
    at most `max_live` open at once (3), the longest life (8 hours, `extend` included), and a
    monthly budget that counts open environments to their time limits (EUR 50). Past one:
    `environment_limit`, naming the limit and the command that raises it. `openajar environments
    allow` changes the limits; it grants nothing. The grant still needs the `vm` special for the
    lane. Untrusted text never changes this.
  - A push never asks, on any branch, the default branch included. A force push or a delete of
    the default branch (or a `protect` glob), a force push or a delete of a branch the
    environment did not create (it was on the remote before), or moving or deleting a tag, is
    refused (`push_refused`: push a new branch and open a pull request). The branches it created
    are on its record (`own_branches`, with the commit it last pushed to each in `own_heads`: a branch someone else recreated or added to is no longer its own), so a restart keeps them.
  - Reading a page in the environment's browser still labels the grant family untrusted, so a
    call on an account lane afterwards (a post on `personal/x`) asks as before. Pay and
    irreversible effects still ask anywhere. `environment_close` with unsaved work stays the
    error `unsaved_work`.
- Socials, browser lanes for accounts and studio stay, as features. The README leads with
  environments.

## How it is built

- **Machine**: a VM from `openajar.vms` (`VmManager.plan/create/destroy`), lane `env/<id>` (an SSH
  terminal lane, so `terminal_exec`, `terminal_job_*`, `shell_*`, services work unchanged). Image: the
  `dev` snapshot when the node has one, else Ubuntu 24.04 with git, curl, build tools, Node 22 and
  Python with uv installed by cloud-init. Working folder `/work`.
- **Git**: the VM's SSH connection carries a reverse tunnel to a gateway listener of its own on the
  node (127.0.0.1:3129 on the VM goes to it), and the VM's git config sends the environment's repo
  there (`url.<gateway>/github.com/me/shop.insteadOf`) with its token. Clone, fetch and push of
  that repo go through it, declared in workspace `env` (the owner declares `openajar git remote
  allow env github.com/me/*`). Every other repo, and a public repo that is not declared, is fetched
  directly on the VM.
- **Browser**: a browser lane on the node, `env/<id>-browser`, with no account, no vault entry and
  no keepalive, whose proxy is a SOCKS tunnel over the VM's SSH (`ssh -D`). Every request, DNS
  included, leaves from the VM: `localhost` is the VM, and sites see the VM's IP. The browser tools,
  the live view and human takeover work as on any lane. Studio's review browser takes the same
  proxy, so `ui_check` on `http://localhost:3000` checks the dev server on the VM.
- **Browser on the VM** (2026-11-15, for local mode: a node on a Mac has no Linux browser stack).
  `environments.toml` `browser = "vm"` (default `"node"`, kept per environment as `Environment.browser`).
  The lane stays the node's `env/<id>-browser`; `openajar.environments.vmbrowser.install` puts a router
  in front of the lane manager's engine and displays, so only that lane launches elsewhere. On the VM
  (`vmbrowser_ctl.py`, sent with each call over the terminal lane's SSH): Xvfb :99 with its own cookie,
  x11vnc on `/run/agentos-browser/vnc.sock` (no TCP), and Camoufox's browser server
  (`camoufox.server.launch_server`) on `127.0.0.1:47613` at a random path. The tunnel's ssh adds
  `-L <dir>/vnc:<that socket>` and `-L <dir>/browser:127.0.0.1:47613`; Playwright on the node connects
  through a guard on a loopback port (as the SOCKS one) and the viewer relay opens the vnc socket as a
  local display's, so the live view, take over and the copy button (xclip on the VM) are unchanged.
  A browser server serves no persistent profile, so each open is a fresh context, which an environment
  never needs. The stack (`browser.sh`, `browser-requirements.txt` with hashes, at uv.lock's camoufox
  and playwright) is in the docker image and the `dev` snapshot, else installed during bring-up. Close
  removes the browser lane before the tunnel goes.
- **Ports**: `environment()` lists listening TCP ports on the VM (`ss -ltnp`). For each the node can
  forward, it gives a link the human can open (the node forwards it over SSH, behind the app's sign-in).
- **Routing** (`openajar.environments.picking`, `routing`): an environment is found only by its own
  grant family. A call without `on` goes, with an MCP session id (header `mcp-session-id`), to that
  session's bound environment; without one (2026-07-28, stdio), to the family's only open one
  (a kept one the agent let go does not count), and with several it fails with `which_environment`,
  listing each as "title" (on="env/<id>"). With none, `no_environment` and the fix
  `environment_open(title=...)`. `on` may name an environment by its title (any case), id or
  `env/<id>` (an account lane id always has a "/", so a title never shadows one; a title with a "/"
  is named by its id); a browser tool gets its browser lane. The result of such a call carries
  `environment: {id, title}`. An account lane in `on` works as before; another family's environment
  lane is refused as not found. Each call on an environment records the MCP client (clientInfo).

## Keeping (owner, 2026-11-15)

"Every agent gets its own computer that stays." The agent's inference stays on the person's laptop;
openajar (on the laptop or a server) rents the machines, and nothing runs an agent while the laptop is
closed. An environment's `keep` says what happens to its machine when the work stops
(openajar.environments.saving):

| keep | Close by the agent | Idle (`idle_minutes`) | Time limit, or a laptop that woke past it |
|---|---|---|---|
| `none` (default) | refused with `unsaved_work`, else deleted | deleted when clean; unsaved work: kept, the owner told | deleted; unsaved work gets a file-level rescue save first and the owner hears its id |
| `live` | the chat lets it go; the machine stays (`final=true` deletes it) | never deleted | renewed 24 hours at a time while the month's budget allows; then as `none` |
| `save` | saved, then deleted (no `unsaved_work`) | saved, then deleted | saved, then deleted |

- `live` bills the machine all month (a cx23 is about EUR 7.80), so the owner opts in
  (`allow_keep_live`, `openajar environments allow --keep-live`) and `max_live_kept` (1) caps them; else
  `keep_live_not_allowed`. Its VM is `kept`: max_hours and vms.toml max_ttl_minutes do not bound it, the
  monthly budget does.
- The default keep is environments.toml `default_keep` ("save" since 2026-11-18: openajar is a computer
  that stays; "none" stays one argument away; "live" there needs the opt-in).
- While a `save` one works, a file-level autosave runs every `autosave_minutes` (15) after use, without
  a snapshot; only the newest autosave of a title is kept, and none older than a full save.

**A save** is a provider disk snapshot (keep `save`, unless `snapshots = false`) plus a file-level part
that is always made:

- the snapshot: Hetzner `create_image` of type snapshot (labels: the node's, `agentos-save`,
  `agentos-env`, `agentos-family`, `agentos-title`), docker `commit` to `<image>:save-<id>`. Before it the
  node stops the terminal_job_start services and the docker containers, removes the gateway token's git config
  (`~/.config/agentos/gitconfig`) and `~/.git-credentials`, and syncs the disk. The machine's SSH keys are
  per machine: the node forgets the old one's key pair when it is deleted, and a machine booted from the
  snapshot gets new ones at its first boot. After the snapshot the node checks the provider lists it, and
  keeps its id and size (GB) in the manifest.
- the file part (scripts.SAVE on the machine): per repository under /work, a git bundle of the commits and
  stashes no remote has (checked with `git bundle verify` there), branch, HEAD, the stash messages and the
  deleted paths; a tarball of `git ls-files -m -o --exclude-standard` (changed and new files git does not
  ignore) and of loose files in /work outside any repository; never a `.env` file or `node_modules`, never
  anything outside /work and `keep_paths` (extra folders, opt in, at most `keep_paths_max_mb`); the services
  that ran (command, folder, port) and the containers; a resume note: an automatic summary (git, the last
  calls, services, proofs, pushed branches with their compare links) plus the agent's own words
  (`environment_save(note=...)`), framed as untrusted when they come back.
- Never saved: process memory, the gateway token (a resume mints a new one), the SSH keys.

**Where:** `$OPENAJAR_HOME/environments/saves/<family>/<title>/<save id>/` (folders 0700, files 0600):
`manifest.json` (what the listing shows, readable while the vault is sealed) and the payload sealed with
ChaCha20-Poly1305 in 1 MB pieces under a 32-byte key the vault holds as the machine secret
`agentos/environments/saves-key` (encrypt_saves; a node with no vault keeps them unsealed and logs it).

**Verify before delete:** the file part is written to a temp folder on the node, then checked (every file's
sha256 matches the machine's, the tarball lists and holds only saved folders, each bundle is one), then
the snapshot is taken and checked listed, then the save is sealed and moved into place, and only then is
the machine deleted. A save that fails keeps the machine (`save_failed`), tells the owner once, and is
tried again after 10 minutes. Past the time limit the VMs' reaper gives an environment's VM 15 more
minutes (openajar.vms.manager.ENV_SAVE_GRACE) before it deletes it whatever happened, and the owner's
notice says when.

**Resume:** `environment_open` with a title its grant family saved (from any chat) resumes the newest save
of that title: a new environment (a new id) on the save's server type, booted from the snapshot when the
provider still lists it (`from: "snapshot"`), else a fresh machine with a clone (`from: "files"`, and
`missing` says the snapshot is gone). Then each repository's bundle is fetched, its branch checked out at
HEAD, its stashes stored again, its deleted paths deleted, the files put back, the containers (snapshot
only) and services started again, and `own_branches` and `own_heads` carried over. A save of another
repository than the one asked for is `invalid` (use another title, or environment_forget). Two chats
resuming the same title at once: one wins, the other gets `environment_in_use`. A resume that fails is
`restore_failed`: its machine is deleted and the save is untouched.

**Retention** (environments.toml): `keep_last` (3) saves of a title, `max_saves_gb` (5) of file parts on
the node (past it: `save_limit`, nothing deleted), `saved_days` (30): the owner is told a day before a save
is removed for its age. Forgetting a save deletes its snapshot; a snapshot the provider did not delete
keeps its save, so nothing billed is lost track of.

**Cost:** Hetzner bills snapshots per GB a month by their compressed size. `snapshot_eur_per_gb_month`
defaults to EUR 0.0119 (Hetzner's list price, net of VAT, 2026; the code had no price before). Kept
snapshots show in `environment()`'s `saved`, the web data (`cost_eur_month`, `snapshots_eur_month`) and
`openajar environments saves`.

## Contract

### MCP tools (everyday list, not in a toolset)

`environment_open(title: str, repo: str | None = None, ref: str | None = None, size: str | None = None,
hours: float | None = None, wait_s: float = 240, keep: "none" | "live" | "save" | None = None) -> dict`

Returns:

```json
{"id": "e7k2", "title": "Black Friday page", "state": "ready",
 "workdir": "/work/shop", "repo": "github.com/me/shop", "branch": "main",
 "machine": {"provider": "hetzner", "server_type": "cpx22", "cpu": 2, "memory_gb": 4,
             "location": "nbg1", "price_hourly_eur": 0.012},
 "expires_at": "2026-11-13T18:00:00Z",
 "links": {"browser": "http://localhost:7450/...the node's live view, signed..."},
 "next": "Give the human links.browser. Then terminal_exec(cmd=\"ls\", cwd=\"/work/shop\") ..."}
```

Since 2026-11-18 it has no `lanes`: an agent took `lanes.browser` for an `on`. A lane's name as `on`
(`env/<id>`, `env/<id>-browser`, `<id>-browser`) reaches the machine only for the session that holds it;
anyone else hears it is a lane's name and to pass the `on`.

Since 2026-11-15 the result adds `keep`, and when it resumes a save `resumed`: `{save_id, saved_at, from:
"snapshot" | "files", branch, head, files_restored, commits_restored, services: [{cmd, cwd, port, running}],
note, missing: [...]}` (while it still comes up, `state: "restoring"` and zero counts; environment() gives
the whole of it once it is ready).

Since 2026-11-18, for a repository with `.openajar/environment.toml` (ENVIRONMENTS.md, "Prepared machines"),
the result and `environment()` add `prepared`: `{used: true, from: {commit, built_at, age}, checkout, refresh,
setup_s: {build, here, saved}}` when the machine booted from a prepared machine, else `{used: false, why,
setup: {command, ok, exit_code, seconds, tail?}, build: {started, why?}}`; `prepared.warm` when it came from the
warm pool. With `start` and `port`, `app`: `{command, port, listening, job_id, seconds, link?, tunnel?}`, and
when it did not listen `why`, `log_tail`, `listening_instead` and `next`. Neither ever fails the open.

`state` is `starting` (still booting: call `environment()` again) or `ready`. A second call in the
same chat returns the same environment. `links.browser` is the node's own live view (a signed
take-over link), null until the browser lane exists; there is no `links.environment` (changed
2026-11-13 by F2: it pointed at a web app page the public repository does not have).
`terminal_exec`, `terminal_job_start`, `terminal_job_read` and `terminal_job_cancel` are in the everyday list with
the environment tools; the rest of the terminal tools are `tools_more("terminals")`.

`environment(id: str | None = None) -> dict`: the bound (or named) environment: state, machine, time
left, cost so far, `ports` (`[{"port": 3000, "process": "node", "link": "..."}]`), `git` (`{"branch",
"ahead", "dirty": n, "pushed": [{"branch", "commit", "at"}]}`), the last 10 `calls`. With no
environment bound and no id: `{"environment": null, "open": [...open ones of this grant, by title...],
"saved": [{title, repo, branch, saved_at, snapshot_gb, cost_eur_month, note}], "next":
"environment_open(title=...)"}` (`saved`: the newest save of each title of the family that is not open).

`environment_close(id: str | None = None, discard: bool = False, final: bool = False) -> dict`: for keep
`save` (without discard) it saves first and adds `saved` (as environment_save returns it) with `machine:
"deleted"`, never `unsaved_work`, and `save_failed` keeps the machine; for keep `live` without `final` it
only unbinds (`machine: "kept"`); `discard` drops changes since the last save and never deletes saves.
Otherwise, as before: `unsaved_work` error
(listing dirty files, unpushed commits and stashes, fix: commit and push, or discard=true) unless
clean or discard; then deletes the VM and the browser lane. Unsaved is any repository under
`/work`: changed or untracked files, commits on any local branch no remote has, stashes. Returns
`{"id", "state": "closed", "machine", "cost_eur", "minutes", "pushed": [...], "deleted": [...]}`:
`machine` is `deleted` only once the provider has no such server, `deleting` while it still has it
(the node retries), `none` when none was rented, `unknown` when the provider could not be asked;
`pushed` is each branch at its commit on the remote now, `deleted` the branches it deleted.

`environment_save(note: str | None = None) -> dict` (the terminals toolset): `{save_id, saved_at, snapshot:
{id, gb} | null, files, bytes, commits, verified: true}`; a snapshot only for keep `save` (its services
stop for it and start again). It never waits for a person. `environment_forget(title: str, save_id: str |
None = None) -> dict` (the terminals toolset): `{forgot: [ids], snapshots_deleted: n}`, and `snapshots_left:
[{id, save, where, why, words}]` when a save's snapshot is another home's (an import: left in place, no longer
tracked), only the family that made the save (any other: not found), or the owner.

Errors (in docs/ERRORS.md, generated): `save_failed` (the machine is kept), `restore_failed` (the save is
untouched), `save_limit`, `keep_live_not_allowed`, `no_environment`, `unsaved_work`, `environment_limit` (the
node's `max_live`, the longest life, or the budget, which counts open environments to their time
limits), `environment_failed` (boot failed; the VM is deleted), `environment_in_use` (another chat holds
the live machine with this title; `take_over=true` once it is quiet), `environment_already_open` (the
title names an open one with another repository), `which_environment` (no handle: a call without `on`
from a client with no session id, or a title or an id in `on`), `environment_not_ready` (a studio check in a
chat whose environment is starting or whose tunnel is down; it never checks the node instead).

A repository the node cannot read is checked before anything is rented (`GitService.access`: declared
for `env` with the vault's key or token, or public). Since 2026-11-15 the open does not end there:
it returns `status: "approval_pending"` with `kind: "repo_connect"`, the card's `approval_id`, a
`review_url` and a `fallback` line (`ssh <node> openajar environments repo add <repo> --token`), and
`can_answer_in_chat: false` (openajar.environments.connect). The card is a question in Needs you, one
per repository (a second open reuses it). From the card the owner, in person and with the panel's
step-up, opens the node's own token page (`POST /api/repo-connect/{id}/form`, a `repo_token` form in
openajar.node.forms, docs/API.md), so the web app never holds the token (docs/WEB-NODE.md), and
pastes it there; the node checks it with GitHub's API and with
`git ls-remote` as the gateway fetches, keeps it as `repo add --token` does, and approves the card, so
`approval_wait` returns and the same open goes through. A token needs no admin rights on the
repository, so a collaborator connects one without a deploy key. The card closes however the
repository gets connected: on the card, at the node (the reaper's sweep checks a declared repository
with a credential once a minute), or by the agent's next open. A stored key or token that was refused
gets the same card, to replace it. No tool takes a token: anything in a tool argument is in the chat's
transcript, and a client's secret references (T3 Code's `request_secret`) are resolved only by that
client's own tools, never by the node, so `review_url` in the client's preview pane is the in-chat way.
`repo_not_connected` is left for a node without approvals and for a clone that still fails on the
machine (its machine is deleted then).

### Web routes (the app polls; no live events needed)

```
GET  /api/orgs/{org}/environments?state=open|all       {"environments": [EnvSummary]}
GET  /api/orgs/{org}/environments/{id}                 EnvDetail
GET  /api/orgs/{org}/environments/{id}/proofs/{proof}.png   a proof's PNG (cap see; added 2026-11-15)
GET  /api/orgs/{org}/environments/{id}/replay?after=&limit=  the browser's replay: frames and calls by time (owner)
GET  /api/orgs/{org}/environments/{id}/replay/{frame}.jpg    a replay frame's JPEG (owner; added 2026-11-18)
POST /api/orgs/{org}/environments/{id}/close {"discard": false}   EnvSummary  (409 unsaved_work)
POST /api/orgs/{org}/environments/{id}/extend {"minutes": 60}     EnvSummary
DELETE /api/orgs/{org}/environments/saves/{save_id}               {forgot, snapshots_deleted} (owner, in person)
```

The listing adds `saved` and `snapshots_eur_month`, and each EnvSummary `keep`, `last_save` and
`save_error` (docs/API.md, "Saved environments").

EnvSummary: `id, title, state ("starting" | "ready" | "working" | "idle" | "gone" | "closing" | "closed" |
"failed"), repo?, branch?, created_at, last_call_at?, closed_at?, expires_at, machine {provider,
server_type, cpu, memory_gb, location}, price_hourly_eur, cost_eur, agent {name, client?},
lanes {terminal, browser}`. "working" means a call in the last 2 minutes, "idle" none for 10. "gone"
(0.5.1) is an open environment whose machine stopped for good; it carries `machine_gone {at, why, next}`
(`next` is the agent's line), and the web app offers only Close.

EnvDetail: EnvSummary plus `ports [{port, process?, link?}]`, `git {branch, ahead, dirty,
pushed [{branch, commit, at}]}`, `calls [{at, tool, summary, ok}]` (newest first, 50),
`commands [{id, cmd, state, started_at, exit_code?}]` (the terminal lane's jobs and execs),
`viewer {path} | null` (the browser lane's live view, as accounts' `lane_viewer_path`),
`closed_reason?`, `unsaved {dirty_files: [..], unpushed: [..], stashes: [..], unreadable?: [..]} | null`
(`stashes` and `unreadable` added by F2), `proofs [{id, title, taken_at, page_url, width, height, src}]`
(added 2026-11-15: the screenshots the agent kept with `browser_screenshot(proof=...)`, oldest first;
`src` is the PNG route above).

For the agent (2026-11-15): `environment()` and `environment_close` list `proofs` with a signed `url`
on the node's own address (`/view/env/<id>-browser/proof/<proof>.png?t=...`, 15 minutes) and its
`expires_at`; each `git.pushed` entry of a branch on GitHub other than the default carries `compare`
(`https://github.com/<owner>/<repo>/compare/<branch>?expand=1`), and `git.compare` is the newest. The
file tools `file_read`, `file_write` and `file_edit` are in the everyday list with
terminal_exec and route without `on` to the terminal lane.

Uploads (added 2026-11-15, openajar.environments.uploads): `environment_upload(dest: str | None = None)`
(the terminals toolset, routed without `on` to the terminal lane; authorized as `exec` on it, so it
never waits for a person) returns `{"url", "expires_at", "dest", "max_bytes", "pack", "send", "next"}`.
`url` is a signed link on the node's own address, `/view/env/<id>/upload?t=...`, signed with the viewer
key as a proof's link is, valid 15 minutes, spent on first use, held in the node's memory (a restart
ends it) and dead once the environment closes. The agent runs `pack` (`git ls-files -co
--exclude-standard -z | COPYFILE_DISABLE=1 tar --null -T - -czf work.tgz`: tracked and new files,
not what git ignores; `COPYFILE_DISABLE` keeps macOS tar from adding `._` files) and `send` (`curl
--fail-with-body -T work.tgz "<url>"`) in its own shell, so the bytes never go through tool arguments.

```
PUT|POST /view/env/{id}/upload?t=...   a .tar.gz or .tar body -> {dest, files, bytes, unpacked_bytes,
                                       paths (the first 10), more?, sha256, next}
```

The node streams the body to `$OPENAJAR_HOME/environments/<id>/uploads/` (512 MB at most, two at once
on a node, one at a time in an environment), checks every member before the VM sees any (no absolute
path, no `..`, a regular file, folder or link only, tarfile's data filter on an empty folder so no
link leaves `dest`; 200,000 members and 4 GB unpacked at most), then streams it over the
environment's SSH into `tar -x --no-same-owner` in `dest` (EnvManager.put_archive): an overlay,
nothing deleted. The temp file goes in every case. Refusals: 403 `denied` (changed, expired, used, or
the environment closed), 403 `frozen`, 409 `busy` or `environment_not_ready` (the link still works),
413 `too_large`, 400 `invalid` (not a tarball, empty, or a member it names), 502 `error` (tar on the
VM failed, with its words). Each upload is an audit row (tool `environment.upload`, the link's grant,
the archive's sha256) and a call `upload` in `environment()`'s last calls.

Downloads (added 2026-11-15, openajar.environments.downloads), the reverse: `environment_download(path:
str)` (the terminals toolset, routed without `on` to the terminal lane, authorized as `exec` on it, so it
never waits for a person) packs a file or a folder under `/work` and returns `{"url", "expires_at",
"path", "kind", "files", "bytes", "sha256", "max_bytes", "fetch", "unpack", "next"}`. The agent runs
`fetch` (`curl --fail-with-body -o out.tgz "<url>"`) and `unpack` (`tar -xzf out.tgz`) in its own shell.

1. The path is checked as text (under `/work`; a relative one starts at the workdir), then resolved on
   the machine (`readlink -f`): what it names must be a file or a folder under `/work`, so a symbolic
   link that leaves `/work` is refused. Packing starts at the resolved path, so no link on the way is
   followed; links inside a folder are packed as links, never followed.
2. `EnvManager.get_archive(lane, src, dest_file, paths=, gzip=, timeout_s=, max_bytes=)` runs
   `tar -czf - ./<name>` in `src` in the lane (over the environment's SSH for a VM lane, as
   put_archive) and streams stdout into `$OPENAJAR_HOME/environments/<id>/downloads/d<hex>.tgz` (made
   new, mode 600). Past `max_bytes` it ends the workload, deletes the partial file and raises
   ArchiveTooLarge. It returns `{"bytes", "files"}`; `paths` are relative to `src` (no absolute path,
   no `..`, each passed as `./<path>` so a name never reads as an option). Kept environments use it
   for their saves.
3. The cap is 512 MB packed, the largest upload, so a project that went up comes back down; the
   archive streams to disk and never sits in memory, two pack at once on a node, one environment
   holds at most 3 unfetched, and packing needs the cap plus 1 GB free on the node's disk.
4. The link is `/view/env/<id>/download?t=...`, signed with the viewer key under its own purpose
   (`download`, never `upload`), 15 minutes, spent on first use, held in memory. It is always the
   tarball, even for one small file: one shape for every download (the same two commands), the file's
   name and mode kept, and nothing from the machine ever served as a page on the node's own address.

The temp file goes once it is served, when its link expires (each new link and the reaper's pass sweep
what no live link names, which also covers a restart), and when the environment closes. Each fetch is
an audit row (tool `environment.download`, the link's grant, path, files, bytes, sha256, `by`) and a call
`download` in `environment()`'s last calls, after the tool's own `environment_download`.

Reading needs `see` in workspace `env`, close and extend `operate`; every route checks the caller's
grant as the jobs routes do. The owner sees every environment, a member only those their own agents
opened; any other is not found. `calls[].summary` is scrubbed as the audit log's traces are.

### CLI

`openajar environments` (alias `openajar envs`): `list`, `show ID`, `download ID PATH [--to DIR]` (agentosd
packs it as for `environment_download`, through `POST /api/environments/{id}/download` with the node
token; the CLI fetches the link and unpacks it into DIR with tarfile's data filter, skipping and naming
any link that points outside), `close ID [--discard]` (a keep `save` one is saved first), `allow` (the
limits that bound `environment_open`: `--max-live`, `--max-hours`, `--monthly-eur`, and since 2026-11-15
`--keep-live` and `--max-live-kept`; since 2026-11-13 it grants nothing, as opening never asks), `saves`,
`forget TITLE [--save ID]`, `save-export ID FILE` (the save unsealed, 0600, for another node) and
`save-import FILE [--family ROOT]`, and
`setup` (checks the provider token, the image, the git declarations, and says what is missing). Since 2026-11-18:
`prepare REPO [--ref R] [--force]` (agentosd builds the prepared machine, through `POST
/api/environments/prepare`; the CLI follows its steps), `prepared` (each one with its cost, and the warm pool),
`prepared forget REPO`, and `allow --warm N`.

## Builders and files

| Builder | Owns |
|---|---|
| E1 core | `src/openajar/environments/` (new), the `on` default routing, `src/openajar/vms/` changes, `src/openajar/api/web/environments.py`, CLI, tests under `tests/unit/test_environments*.py` |
| E2 web | `web/src/features/environments/`, the rail and Home entry, mocks and hooks for the routes above |
| E3 front door | README.md, docs/ENVIRONMENTS.md, docs/VISION.md, docs/ARCHITECTURE.md, the MCP intro (`INSTRUCTIONS` in src/openajar/node/tools/recipes.py), `whoami` text, the skill `src/openajar/client/skills/openajar-environment/`, CLI root help, the public export |
