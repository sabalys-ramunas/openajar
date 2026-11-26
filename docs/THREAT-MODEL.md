# Threat model: credentials, and what "a person is present" means

openajar runs a person's real accounts for their agents. The agent is the threat it is built against: a model
that read a page, an email or a webhook can be talked into anything, and an agent with a shell can read any file
its user can. So the question every protection asks is the same: **did a person make this request, or could an
agent have?** This page lists every credential, where it lives, what it can do, and the one rule that answers
that question.

## A person is present

Every request to the node's HTTP API carries a presence level (`openajar.core.presence.Presence`), worked out
from the credential it arrived with and from nothing the caller sends:

| Level | What it is |
|---|---|
| `agent` | a grant's bearer token: an agent's, an app's, an admin grant |
| `node_token` | the node's own token (`mcp_token` in `node.toml`) as a bearer |
| `device` | the owner's device credential, the session `openajar connect` keeps on the owner's computer |
| `person` | a panel session from a one-time code (`openajar panel login`) or a passkey, or the node's own CLI |
| `verified` | `person`, with a passkey check inside the step-up window (15 minutes) |

The line is `person`. **Everything below it is a credential an agent can hold**: its grant, the node token
(older connections gave it to agents, and an agent with root on the node reads it), and the device credential
(it sits in the Mac's login Keychain, which an agent with a shell as that user can read). So these need `person`,
and are refused below it with `presence_required` (403):

- approving an approval, one or in a batch, and answering a question;
- making a standing approval (a member also needs `verified`);
- turning Ask me first off, and reading or changing the confirmation setting;
- widening or changing a lane's site list, promoting a gate shape, and an owner view link (which can hand back, add a
  site or save to the vault from the viewer; see Viewer links);
- unfreezing (with members on the node, also a fresh passkey check);
- adding a passkey, inviting a member, changing a member's roles;
- a lane's worker controls, and the lead lists with addresses in them;
- handing a lane back, taking over a lane that is held, stopped or quarantined, and the assisted login
  (`/api/lanes/{ws}/{name}/login`, which hands a held lane back once signed in; `openajar lane login` sends
  the CLI's one-time code);
- what an account looks like to its site: pointing a proxy label that exists at another host, port or login,
  moving a lane to another device, a device beside lanes that already use its proxy, a new lane (alone, from a
  template or on a device) behind a proxy another lane uses, and applying a workspace import;
- what runs as the owner: a schedule under the owner's own grant, turning one on, and an import that turns its
  schedules on (see the sweep below).

Below `person` a caller can still read what its scope covers, deny, start things (which are routed like an agent's
call, Ask me first and approvals included) and freeze. A route declares its minimum in `endpoint(..., presence=...)`;
a handler whose need depends on the body (Ask me first on or off, batch approve or deny) asks
`Access.require_presence`. The node's lane routes outside the panel (`openajar.node.viewer_app`) read the same level.

The node's CLI is a person because it runs where `panel.db` can be written: it writes a one-time code there and
sends it beside the node token (`X-Agentos-Presence`), and the daemon spends it for that one request. Whoever can
write `panel.db` can already mint a panel sign-in link, so this gives the CLI nothing a sign-in would not.
`openajar dash` run on the node with the token from `node.toml` does the same for its writes, so it still approves.

A grant's subject id is a name, not an identity: two grants may share one. Every check of who asked for
something (self-approval, a brain task's owner) is keyed on the grant family (the
root grant id), and the subject ids the node's own issuers use (`site-key:...`) are refused to every other.

The device credential is routed exactly as the node token that made it: its actions go through the policy like a
token's (pacing, taint, the trifecta, Ask me first), never as the owner in person. `openajar node rotate-token` ends
every device credential, since whoever held the old token could have made one.

## Sweep: identity, network and protections below a person

Every action a credential below `person` can still take that changes an account's identity, a lane's
network or a protection, and why it is gated or safe (2026-11-07):

| Action | Below a person |
|---|---|
| `POST /api/proxies` on a label that exists, any field changed | gated: a new IP or login for every account on it |
| `POST /api/proxies` with a new label | open: no lane uses it, so no account changes |
| `POST /api/lanes/{ws}/{name}/device` (`openajar lane set-device`) | gated: a new exit or a new computer to the site |
| `POST /api/browser-devices` with `confirm` | gated: a second computer on accounts' exit; without `confirm` it is refused when others share the proxy, and `from_lane` keeps that lane's pin and proxy |
| `POST /api/lanes`, `POST /api/templates/apply` behind a proxy another lane uses (also through a device) | gated; on a label nothing uses: open, since the lane is no account until the owner logs in |
| `POST /api/workspaces/import` (apply; also `enable_schedules`, `reissue_grants`) | gated: it brings copies of logged-in browsers. A dry run is open |
| `PATCH /api/workspaces/{id}` `default_proxy` | open: only later plans use it, existing lanes are never moved, and a lane it puts behind a used exit is gated above |
| MCP `workspace_apply` (also `override_proxy_rules`) | an approval a person answers: a commit on no one account always asks, and the card shows every lane and proxy |
| MCP `terminal_create` | no exit proxy argument; a shared network is `exec` off an isolated lane, which asks |
| `POST /api/schedules` under the owner's grant, turning one on | gated: it would run as the owner and never end |
| a schedule under any other grant | bound to it: its lanes and effect are checked when it is made and at every fire, it turns off at the first tick after the grant is revoked, expires or is unknown, and it runs only the version it was made with or one the owner saved in person |
| `POST /api/flows` with the device credential | saved as an agent's: `publishes: false` counts for nothing and Ask me first is inherited |
| a flow id whose tool name another flow or tool has | refused: it would take that tool from another workspace |
| an unknown proxy label | the error names no other label |
| sites, egress, Ask me first, confirmation, unfreeze, hand back, pace, meters, gate log, standing approvals | gated (above) |
| workflow on, triggers | a person turns on or approves each |
| controller `config_update` | applies the heartbeat only; a confirmation layer can only add. The controller key is no agent credential |
| `openajar proxy add`, `lane set-proxy`, `lane create`, `backup restore` | the node's CLI, in its own process: root on the node, which is the owner (below) |

Still open, for the owner to decide:

- **A running run** finishes its steps after its grant is revoked: a revoke stops what is queued and every
  schedule, but not a run already acting (a freeze does stop it).
- **Workflow steps** run their flow's current version, so an agent's `flow_save` on a lane it covers changes
  what a step of a workflow the owner turned on does, under that workflow's grant. Schedules are closed; steps
  are not.
- **Export** (`POST /api/workspaces/{id}/export`) is closed: it holds the lanes' cookies and vault values, and
  with `with_proxies` the proxy logins, so it needs a person (the panel session, or the on-node CLI's code).
- **A service** an agent started (a person approved it) runs until its TTL, not until the grant ends; an event
  route of a revoked grant shows as on until three failed deliveries pause it.

## Viewer links

`browser_view` gives the agent a live-view link for a person, and an agent with a shell can open that link
itself. So a link is never a person on its own. What only a person may do (hand back, which clears a hold, a
quarantine and a person's stop; take over a lane that is held, stopped or quarantined; the assisted login;
adding a site) needs one of two things on top of the link, the same two the rest of the node trusts:

- an **owner link**, made in person (the view-link route from the owner's panel session, or
  `openajar lane view --control` on the node with its one-time code). `browser_view`, grants, the node token and
  the device credential never get one;
- the **owner's panel session in the same browser**. The viewer is served at the panel's own address, so the
  session cookie (SameSite strict) comes with the page's requests, and an agent's `curl` has no such cookie.
  A member's session and the device credential do not count. A page opened by a signed-in owner swaps to an
  owner link of its own.

Without either, those actions get `presence_required`, and the relay passes no input from the link, so
an agent holding the link can watch, take a lane nothing holds (which only stops agents) and stop an agent,
nothing more; its input never reaches the lane, since a human's input is never stopped by the commit gate. Every link also has a hard end, signed into it, that renewing never moves: 30 minutes for a link
anyone but a person made, 8 hours for an owner link. The owner's control API follows the same rule: a
hand-back, and a take over of a held lane, need `person`.

**An agent environment's own browser** (`env/<id>-browser`, decided 2026-11-15) is the one exception. It holds
no account of the owner's but the logins the owner bound to that computer
([Logins in an environment's browser](#logins-in-an-environments-browser)), and its traffic leaves from the
environment's own VM, so there is nothing behind it that a person must guard beyond what the owner chose to
put there. A control link to a browser signed in to a bound login reaches that account as the agent does:
share it as you would the account. With the desktop on, the screen is more than that browser (its dock
opens a root terminal on the machine, see "An environment's desktop" below), so on a server node the
exception no longer holds there and such a link gets the rules above; in local mode it still does (the
viewer is on 127.0.0.1). There, a control link alone may take over (also a held lane),
type, copy and hand back; the page says so and the relay passes its input while control is `human`. What it
does is recorded as `link holder`, never as the owner. The node decides which lane that is (the environments
service: an open environment whose browser lane is exactly this one), never the link or the page: a lane of an
environment that closed, every account lane, and a lane the service cannot answer about keep every rule above.
A view link still only watches, a freeze still ends the link, the hard end still holds, and adding a site and
the assisted login stay a person's (an environment's browser signs in only with the logins the owner bound).

A **proof link** (`/view/env/<id>-browser/proof/<proof>.png?t=...`) opens one screenshot an agent kept as
proof of its work in an environment, and nothing else: no live view, no control. It is signed with the viewer
key for that one image (purpose `proof`, 15 minutes), so replacing the key revokes it, and a live-view token
does not open it. The image is the agent's own work, which the agent may already pass on. A **video proof**
(`browser_record`, docs/ENVIRONMENTS.md "Proof of the work") is the same kind of thing, at `.mp4` and `.gif` under
the same link and token: ffmpeg records the screen of the environment's own browser only (an account lane has
none to record: `browser_record` refuses any lane that is not an open environment's browser), as a process of its
own on the machine (or, with `browser = "node"`, on that lane's display of the node), with the display's cookie
in its XAUTHORITY file, never on its command line. The node reads the MP4's length and size itself and keeps
only files of the right kind and size from the machine's archive (the agent is root there). A recording shows
whatever the page showed while it ran: a secret the app printed, what was typed.

**Proofs in a pull request.** `environment_pull_request` puts the environment's proofs in the repository, on
the `agentos-proofs` branch (an orphan branch, never in the pull request's diff), with the token that opens the
pull request, and links them from the body. A proof pushed there is visible to everyone who can see the
repository: for a public repository, the world. It shows whatever the page showed. Only an environment's own
proofs are ever sent (there are none of an account lane), and `environment_pull_request(proofs=false)` or
`pr_proofs = false` in `environments.toml` sends none. The branch is the repository's: deleting it removes the
files from the repository (forks and clones made meanwhile keep theirs). With no token, nothing leaves the node.

A **replay frame** (`/view/env/<id>-browser/replay/<frame>.jpg?t=...`, docs/ENVIRONMENTS.md "Replay") counts as
a proof-like artefact: the node took it of the environment's browser on its own, after the agent's page calls,
while a person had control and when the environment closed, so it can show whatever that page showed (a secret
the app printed, a token in a dev page, what a person typed in the live view). The node takes frames only of an
open environment's own browser (`env/<id>-browser`, decided by the environments service, never by the caller),
never of an account lane. Frames stay on the node (`environments/<id>/replay/`, the folder 0700) for
`replay_days` after the close, within a cap per environment and on the node. Only the owner reads them: the
owner-only web routes (a member's role and an agent's grant get `denied`), `openajar environments replay` on the
node, and the links those routes sign with the viewer key (purpose `replay`, one environment, 15 minutes;
replacing the key revokes them, a live-view or proof token does not open them). An agent gets only a count
(`environment()`'s `replay`: frames, since, bytes), never a frame or a link. `replay = false` in
`environments.toml` takes none.

An **upload link** (`/view/env/<id>/upload?t=...`, from `environment_upload`) puts one archive into an
environment's machine, a machine the agent can already write to with `terminal_exec`. It is signed with the viewer
key (purpose `upload`, 15 minutes), spent on its first use, kept only in the node's memory and dead once the
environment closes, so a link that leaks into a log is good for one upload into that one VM at most. The node
takes at most 512 MB, removes the temp file in every case, and checks every member before the VM sees any:
no absolute path, no `..`, no link out of the folder, no device or fifo (tarfile's data filter, run on an
empty folder so it never reads the node's own files). It never extracts anything on the node itself.

A **download link** (`/view/env/<id>/download?t=...`, from `environment_download` or the owner's `openajar
environments download`) gives one archive of a file or a folder under the environment's `/work`, off a
machine the agent can already read with `terminal_exec`. It is signed with the viewer key under its own purpose
(`download`, 15 minutes), so it never opens an upload and an upload link never opens it; it is spent on its
first use (a `HEAD` spends nothing), kept only in the node's memory and dead once the environment closes. A
link that leaks is good for one copy of what the agent already packed. What leaves is only what the VM's own
tar packs: the path is resolved on the VM and refused unless it names a file or a folder under `/work`, and a
link inside it is packed as a link, never followed. The check and the packing are two steps, so the agent
could swap a folder for a link between them, but `/work` is a policy line on the agent's own machine, not a
boundary between the agent and anything it cannot already read there; nothing on the node is ever read. The
archive streams to a file of the node's own naming (512 MB at most, mode 600, deleted once served, at expiry
and on close) and is always served as `application/gzip` with `attachment`, `nosniff`, `no-store` and
`default-src 'none'`, so nothing from the VM renders as a page on the node's own address. The CLI unpacks
with tarfile's data filter and skips any member that would leave the folder it unpacks into.

Copying out of a lane (`POST /view/{ws}/{name}/clipboard`) reads the lane's clipboard for the person's own
computer. A clipboard can hold what was never on the screen (a password a page's Copy button put there, a
token), so the read is at least as strict as the relay's input gate: a control link, control `human`, and a
person (an owner link or the owner's panel session in this browser), with this host's `Origin`. An agent's
link never reads it, even on a lane it took. The page reads only on the person's own copy key or a click on
"Copy from the lane", never on a timer, so a page in the lane cannot push text onto the owner's clipboard by
itself. The text goes back in the answer (`no-store`) and nowhere else: the action log records a
`lane_clipboard` row with who, the lane and the number of characters, and nothing logs the text. A link reads
at most 30 times a minute, and over 1 MB is refused whole.

## Over MCP

Nothing that arrives on `/mcp` is a person. A grant's token is `agent` and the node token is `node_token`
(`openajar.policy.wrap` sets it on every call's context), so every lane gate (a takeover, Stop agent, a freeze,
another caller's claim, the lane's site list) holds every MCP caller, and no MCP tool approves or answers on the
caller's word. (`approval_confirm` asks the person in their own chat client, for an approval that needs no more than
chat and came before any untrusted text: docs/CONFIRMATIONS.md.) Every
call goes through the same wrapper first: the token is resolved (revoked, expired and another node's grants
refused), tool pins are checked, the tool's effect and the lane in its lane argument are prechecked against the
grant's scope and ceiling, and a freeze refuses the call or interrupts it. Then each tool authorizes what it will
really do (routing, Ask me first, approvals, budget) before it acts. What each toolset checks on top:

| Toolset | Scope checks beyond the wrapper |
|---|---|
| `core`, `accounts`, `browser` | browser and terminal tools authorize on the lane before acting and run inside the lane's control gate; `browsers_list` shows only lanes the grant covers; `login` needs the grant's `secret` special for that lane and an entry bound to the lane and the page's site; approval tools show only the caller's own family's approvals (all of them to an admin grant) |
| `control` | authorized on the lane named; a freeze stops agents, and only a person in the panel unfreezes |
| `vault` | `vault_list` shows only entries bound to lanes the grant reaches (all to an admin grant); `vault_fill` as `login` |
| `flows` | `flow_run` and `flow_save` authorize the flow's lane; a lane pattern must be inside the grant as a pattern (`biz-a/insta*` within `biz-a/*`, never `*`), and a new version of a flow bound outside the grant is refused; `flows_list`, `flow_run` and `ask_first_on` see only flows bound where the grant reaches (a pattern: where it can match a lane of the grant), and a flow outside is not_found; `run_status` and `run_cancel` on a run with no lane need its workflow's whole workspace (or an admin grant) |
| `workflows` | a workflow, a lead list and a suppression belong to a workspace, so the grant must cover all of it (never one made-up lane of it); `workflow_save` and `workflow_run` check every step against the grant, and `workflow_save` never replaces a workflow outside it; `workflow_propose` checks every step, and it, `leads_propose` and `send_suppress` authorize before they ask or save |
| `connectors` | an operation authorizes on its surface's lane; lists show only surfaces the grant covers |
| `site_apis` | `site_api_try` and `site_api_propose` authorize on every surface's lane before a draft is saved or a version staged; only a person activates; `site_apis_list` filters APIs by lane, not each API's surface list (open) |
| `mail` | authorized on the mailbox's workspace; sending is a publish, so after untrusted text it waits for a person |
| `memory` | the grant's `memory` special and the workspaces it covers; private and owner-only notes stay with a person |
| `workspaces` | lists and counts are filtered by the grant; a workspace with no lane in the grant answers as one that is not there; `workspace_plan` and `workspace_apply` check every lane the template would make before planning, and a proxy conflict never names a lane outside the grant |
| `worker`, `terminals` | authorized on the lane named; commands, services and installs are their own effects or specials, which a grant must hold |
| `admin` | the caller's own grant, approvals and traces (its family; an admin grant sees all) |
| `brains` | `brain_run` checks every lane and the workspace before it authorizes; status, transcript and the task list show an admin grant every task, any other grant its own family's and those whose lanes it all reaches; cancel and resume need the task's own grant family (the root grant that started it, never the subject id, which two grants may share) or an admin grant; `brain_use` needs the whole workspace, and the node's default a grant for every workspace; a task's child grant covers only its lanes |
| `automation` | `trigger_propose` checks the lanes it listens on, wakes and grants, a pattern as a pattern (every lane it can match is in the grant), and a trigger with no lane needs the whole workspace; the owner approves every trigger as shown |

## The credentials

| Credential | Where it lives | Presence | Can | Cannot |
|---|---|---|---|---|
| **Node token** | `node.toml` on the node (root, mode 600); on old connections also the agent's Keychain item | `node_token`, on the API and on `/mcp` | everything the owner can on the API and over MCP that does not need a person: lanes, grants, settings, freezing, minting a device credential, a new proxy label; a lane a person paused or took over holds it back | approve or answer (also `self_approval` for its own family), standing approvals, Ask me first off, confirmation, sites, unfreeze, passkeys, invites, an account's exit or device, an import, a schedule that runs as the owner |
| **Agent grant** | the agent's MCP config: on a Mac, Keychain service `openajar`, read by the headers helper | `agent` | MCP tools inside its workspaces and ceiling; on the API, the routes its scope covers | decide anything (`not_approver`), widen itself, read the owner's credential item |
| **Admin grant** | wherever its holder keeps it | `agent` | what the node token can on owner-only routes; issue grants no wider than itself | everything that needs `person` (a share link included), deciding its own family's asks, and a grant wider than itself in lanes, effects, specials, budget or expiry |
| **Member session** | the member's browser (cookie, 12 h idle, 7 days at most), from their passkey | `person`, `verified` after a check | what their roles allow in their workspaces; a decision needs a passkey check in the last 15 minutes, `pay` and `irreversible` a fresh one for that approval | other workspaces; four-eyes keeps them from deciding their own agent's request |
| **Owner panel session** | the owner's browser (cookie, 7 days), from `openajar panel login` or the owner's passkey | `person` | everything | nothing is held back from the owner in person |
| **Device credential** | the owner's computer, Keychain service `agentos-owner` (made by `openajar connect` with the node token) | `device` | `openajar dash`, `openajar approvals list` and `deny`, `openajar open` (a control link that is not an owner link), `openajar expose` and `openajar lane forward` (see Exposes and forwards), revoking the connection's grant; issuing a grant only within `policy.device_grants` (by default every workspace, up to commit, no specials, 90 days at most) | approve or answer (the CLI says to open Needs you in the panel through the tunnel), a grant wider than `policy.device_grants`, reissuing an import's grants, and everything else that needs `person`; MCP refuses it |
| **Controller key** | the controller's own key files; the node pins its public key at `openajar node join` (`controller_pubkey`) | not on this API: the node channel | signed `config_update` and revoke; decisions it relays count up to `controller_max_level`, and a relayed decision with no proof reaches at most `signed_in` | lower anything the node's owner set (its confirmation layer can only add), report a `chat` answer, exceed the controller ceiling in `node.toml` |
| **Phone link** | a push notification (signed, one approval, expiring) | not a session | deny that one approval; a freeze link freezes | approve: a link never approves, it opens the panel |
| **One-time login code** | `panel.db` on the node (hash only), printed once by `openajar panel login` | becomes a `person` session | one sign-in, within 10 minutes | anything a second time |

## Exposes and forwards

`openajar expose 5173 --lane ws/name` opens the owner's laptop dev server in one browser lane at
`https://<name>.expose.openajar.invalid`; `openajar lane forward ws/name:PORT` puts a terminal or service lane's
port on the laptop's localhost (docs/DEVELOPING.md, "Your dev server in a lane"; `openajar.node.expose`).

**Who may.** `/api/exposes` needs the owner at `device` or above: the device credential, a person's panel
session or the node's CLI. A grant (an admin grant too) and the node token get `presence_required`, and no MCP
tool reaches it. `device`, not `person`, because what it opens is the owner's own computer to one of the
owner's lanes: whoever holds the device credential has a shell as the owner on that computer (the Keychain),
so already reaches the dev server, the lane's SSH host and anything a page could send to the dev server.
An agent with only MCP cannot make one. The SSH half needs the owner's SSH key as well.

**What it opens.**

- In the named lane's browser, one origin: `https://<name>.expose.openajar.invalid`, port 443. Requests to
  it are answered inside the browser's driver from a unix socket in `<OPENAJAR_HOME>/run/expose/` (a
  directory of mode 700; the socket is sshd's for `ssh -R`, handed to agentosd's user, mode 600, and the
  node checks owner and type before it routes). The URL picks the socket, never an address: no other host,
  port or path on the node or its network is dialled. Another port on that name, an unknown name and a
  name after the end fail as a name that does not resolve.
- The lane's egress guard counts that host as inside the lane's site list and every grant's egress list
  while the expose lasts (`LaneGuard.exposed`). Effect rules, Ask me first and the commit gate judge what
  is sent there as anywhere. Under taint, `browser_navigate` there still waits for a person, as any host
  outside the grant's egress list (the policy's sink rule is not widened).
- For `forward`: a socket of agentosd's (mode 600) that connects only to 127.0.0.1:<port> inside that
  lane's own network namespace. A lane that shares the node's network is refused, since its 127.0.0.1 is
  the node's.

**What it does not open.**

- Other lanes: the route is on the named lane's browser only. Elsewhere the `.invalid` name resolves
  nowhere (RFC 6761), so a lane that is not the named one, or the named one after the end, reaches nothing.
- The node's loopback and private network: the private-network PAC and the proxy stay as they were for
  every other request (tests/node/test_expose_node.py checks a loopback fetch from the exposed page fails).
- The lane's proxy: exposed traffic never goes through it and never leaves the node except over the
  owner's SSH session. A redirect hop to the exposed host is not routed (Juggler), so it reaches the
  proxy or the node's resolver and fails there; that hop carries the host name only (https: the path and
  query stay in the browser), and the node then navigates the same frame again through the route.
- Another site's fetches: a request that is not a navigation is answered only when a page of an exposed
  origin of that lane sent it (Sec-Fetch-Site, Origin, or the frame). A service worker's request has none
  and is refused.

**The actual guarantee, honestly.** It is one browser. While an expose is on, any page in that lane may
*navigate* to the exposed origin (a link, a redirect, a form post, a frame), with the dev app's
`SameSite=None` cookies, exactly as any website can send a browser to `localhost` on a laptop: the dev
app must not trust a cross-site navigation more than a real deployment would. A page of another site cannot
read the answer (same-origin policy) or fetch from it. The lane's own cookies for other sites are never sent
there (another origin). It ends on Ctrl-C, on `--for` (at most 12 hours), when the SSH session ends,
or 30 seconds after the laptop last renewed it through the tunnel; the node checks the end on every
request, so a request after it is refused before the route is even removed. agentosd keeps none across a
restart. Each start and end (with its reason) is in the action log and the lane's events, and `browsers_list`
shows agents the address while it lasts.

**Costs it brings.** Any route turns the HTTP cache off for that browser while it exists
(docs/design/egress-guard.md 3.2), so sites in that lane see every asset fetched again for the expose's
length. WebSockets to the exposed host are not routed (they fail), and nothing patches the page's
`WebSocket` to route them.

## Git credentials (docs/GIT.md)

A deploy key or a token for a git host is a credential the agent must be able to use overnight and
must never hold. It is a vault machine secret (`git:<name>`), bound to a host and its repositories;
a deploy key is made on the node by `openajar git key add` and only its public half is ever printed.
Git in a lane or a brain task talks plain HTTP to the node's git gateway (a listener inside the
lane's own network namespace, or the node's loopback for brains), with a gateway token that names
the lane or task and the grant behind the command; the token is no credential for the remote.

| Question | Answer |
|---|---|
| Who opens the credential | agentosd only, for one fetch or push, with the purpose recorded in the vault |
| Where the key is while it is used | in agentosd's memory; a one-operation SSH agent on a 0700 socket of agentosd's signs an SSH login as the expected user (never a commit or file signature, at most four times); no key file is ever written |
| Where a token is while it is used | in the `GIT_CONFIG_*` environment of agentosd's own git process (no file, no argv), sent as HTTP auth to the declared host only |
| What a lane can reach | only declared remotes (`openajar git remote allow`, the node's CLI, so the owner); an undeclared host or repository is refused at the gateway whatever the vault holds; the lane needs no allowlist entry for the git host |
| What a push is | `commit` on the lane (Ask me first, the untrusted-content downgrade, freezes and limits apply); `irreversible` for a force-push (checked on the mirror with the pushed objects), a deletion, a moved tag or a protected branch (the default branch unless allowed): always a person, never a standing approval, and above the ceiling of most grants |
| What is recorded | `git_fetch`, `git_push` (pushed, approval_pending, denied, failed) and `git_refused`, with the remote, the refs and the grant; the vault's use count and last purpose of the credential |
| What a lane sees on a refusal | the gateway's own sentence, scrubbed of vault values; never the remote's raw output |

Below a person, an agent can use a declared remote through a grant that covers the lane, and no
further: it cannot declare a remote, add or read a credential, or approve its own push. Not covered:
a workbench (env0) lane or one that shares the node's filesystem, processes or network (it is the
owner's shell, and can read OPENAJAR_HOME); two grants' commands in one lane with shared processes
(one can read the other's gateway token and push as it, within the declaration and the policy);
what the pushed commits contain; and a lane whose own egress allowlist lists the git host, which
can reach it directly with a credential the agent brought itself (never the node's).

## An environment's desktop

With `browser = "vm"` and `desktop = true` (the default), an environment's screen is a whole desktop on the
agent's machine: the browser as a window, a dock with Terminal and Files, any app the agent or a person
starts, and the agent's commands window, which shows every `terminal_exec` and its output as the node reads it
(openajar.environments.desklink, desktop/desk.py). The live view shows all of it. What follows is what that
means for each party.

| Who | What they get |
|---|---|
| A **control link** to the environment's screen | Everything a person at that screen has. The dock's Terminal is a root shell on the machine, which holds the repository, uploaded code and the gateway token that pushes that one repository (see the ssh table below). In local mode the viewer listens on 127.0.0.1 only, so only this computer's processes reach the link, and the agent there is root on the machine already. On a server node the link is reachable from wherever the panel is, so with the desktop on, a control link alone does not take input there: it needs the owner's panel session in the same browser or an owner link (openajar.node.viewer_app `_holder`). Give a control link only to someone you would give a root shell on that machine |
| A **view link** | Watches the whole screen: every command the agent runs and its output (a token in a command line, a secret a command prints), what a person types on that screen (a password field shows dots, other fields do not), every window. It cannot type, click or take over. Anyone who can watch can also record a clip (below). Treat a view link as a read of everything the agent does there |
| **The record button** | Records what this page already draws into a video in the watcher's own browser and saves it on their computer. Nothing is uploaded and the node is asked for nothing new (viewer/assets/record.js); a clip stops by itself after three minutes |
| **The agent, while it has control** | Its screen tools (`screen_snapshot`, `screen_act`, `screen_open`: real X input through XTEST, audited like browser actions, the text it types on stdin only) and its commands get the screen (`DISPLAY` and the screen's cookie) |
| **The agent, while a person has control** | No openajar tool shows it the screen or types on it: every screen tool is refused, as every browser tool is, and an action that was running is stopped on the machine within about a second with every key and button let go. Its commands are not handed `DISPLAY` or the cookie. Its terminal tools keep working (the take over is of the screen) |
| **What stays true by construction** | The agent's commands run as root on its own machine. Root can read the screen's cookie (`/run/agentos-browser/xauth`) and reach its socket, read the browser's profile and every file, and so watch the screen or read what a person types there, with ten lines of python-xlib, outside the tools and outside the audit trail. There is no cheap barrier: putting the screen under a user the agent cannot become needs an agent that is not root, and the agent is root there by design (`apt-get install`, services, the whole machine). So the take over keeps the agent's tools out of a person's way; it does not keep a hostile agent out of its own machine. The live view says so under Take over ("This is the agent's computer: what you type here, the agent could read"). Sign in on that screen only to accounts the agent is meant to use |
| **What the machine sends the node** | The machine is the agent's, so all of it is untrusted. On `desk.sock` the helper sends two kinds of line, each under 4 KB: where the browser window is (whole numbers inside the screen, or the agent's pointer is not drawn) and "open the browser" from the dock, which the node acts on at most once every 10 seconds and never past a stop or a freeze (the ask is never taken as a person's). Nothing else it sends is acted on. The screen tools' answers (window titles, a PNG) are bounded and checked before use, and titles are data, never instructions. The control scripts are sent by the node with each call; the copies on the machine are never run by the node |
| **What the node sends the machine** | The commands window's text and the top bar's status, over the environment's SSH tunnel to unix sockets in a folder only root opens. The commands window draws output only after a gutter and only with the colours its design uses: output cannot move the cursor, clear the window or draw a fake command or exit line, also when an escape sequence is split across reads (desktop/deskterm.py). Nothing listens on TCP: no X, VNC or helper port, and the screen's cookie is new each time the screen starts |

## An environment's network and its app's secrets (docs/ENVIRONMENTS.md)

An environment's agent is root on its VM, so nothing set inside the VM can bound it. Its `network`
(`full`, `limited`, `none`) is held outside: on Hetzner a firewall whose one outbound rule (ping to the
node) makes Hetzner drop every other packet out; on docker the entrypoint's closed wall, set before sshd
with no NET_ADMIN left. A closed machine's one way out is the node's allowlist proxy, forwarded into it
over the SSH connection the node holds. A secret bound to environments stays in the node's vault; the
VM holds a placeholder, and the node's TLS swap puts the value in for the bound host only.

| Question | Answer |
|---|---|
| Who sets the network and the bindings | the owner, at the node (`environments.toml`, `openajar environments allow --network/--host`, `openajar environments secret add`, `openajar environments env push`). An agent may only narrow the network on `environment_open`; no tool, page, repository file or command widens it or binds a secret |
| What a closed machine reaches | the node's proxy (registries and code hosts for `limited`, the owner's hosts, bound hosts), the git gateway, its own loopback. Names are resolved on the node, and private, loopback and link-local addresses are refused there |
| Where a secret's value is | in the vault, and in agentosd's memory while requests use it (read from the vault again each minute). Never on the VM, in a tool result, a log line, the record or the web app: they hold the variable's name only |
| When the value is put in | a request on a TLS connection whose server name is a bound host, and whose Host header is that same host, with a target that names no other host (a full URL naming another host gets nothing, one naming the bound host is sent as a path): headers (a Basic credential included) and the URL's query. Not bodies, not plain HTTP, not another host. A placeholder sent anywhere else goes out as it is and is listed under `blocked` |
| The environment's CA | made per environment on the node; its key never leaves the node and is deleted at close; it signs certificates for bound hosts only; the VM trusts it |
| The swap's own connection out | to the bound host by name, an address the node resolved itself (public only), with full certificate checks against the node's trust store; HTTP/1.1 |
| What is recorded | the refused hosts (host, count, last) with the environment, shown in `environment()`, the web app and the pull request; each use of a secret in the vault's use count and the node's log (entry and host, no value) |

What this does not stop:

- **DNS on a closed Hetzner machine.** Hetzner's firewall always lets DNS to Hetzner's own resolvers
  through (and answers the metadata service on the host, which holds only the machine's own first-boot
  config). Root on a `limited` or `none` Hetzner machine can still send name lookups there, a slow way
  out for data. A closed docker machine has no DNS at all: Docker's resolver is refused and given
  nowhere to forward (`--dns 0.0.0.0`).
- **DNS on a `full` machine.** A full machine resolves names itself and reaches any public host; the
  network setting is the guard, so choose `limited` or `none` where it matters. A bound host is
  mapped in the VM's `/etc/hosts`, which root on the VM can change: a request that bypasses the
  relay then gets no value (the secret is never on the VM), it fails.
- **What the agent can still read.** Everything in the repository, the files it cloned and whatever
  the allowed hosts serve. `limited` reaches github.com and the registries: code pushed to a public
  package, gist or issue there leaves too. Data leaves through any allowed host that accepts data.
- **What the bound host does with the request.** The agent can make any request it likes to the bound
  API with the real key (that is what binding it allows), and an API that echoes the key back in a
  response hands it to the VM.
- **Certificate pinning and HTTP/3.** A client that pins the API's certificate refuses the
  environment's CA, and its requests fail (they never carry the value). QUIC (UDP) is not proxied:
  clients fall back to HTTP/1.1 or 2 over TLS, and the swap speaks HTTP/1.1 only.
- **A key bound to a host the agent controls.** The binding is the owner's; bind a key only to the
  host that issued it.

### The browser's proxy (docs/ENVIRONMENTS.md "The browser's proxy")

The owner's proxies (`openajar proxy add`, `secrets/proxies.json`, 0600) can carry an environment's browser
(`browser = "vm"`, `browser_proxies` in `environments.toml`). The agent is root on the machine, so the proxy's
credentials never go there.

| Question | Answer |
|---|---|
| Who chooses the proxy | the owner, at the node (`openajar proxy add/remove`, `browser_proxies`). The node picks one per environment at open and keeps it in the save. `environment_open` has no proxy argument, and no tool, page, repository file or command names or changes one |
| Where the credentials are | in `secrets/proxies.json` and in agentosd's memory: the node's forwarder for that browser (an HTTP proxy on this computer's loopback) adds them on its way to the owner's proxy. Never in what is sent to the machine (the browser's options and prefs, the control script's requests, the tunnel's argv), a tool result, a log line or the record |
| What the agent sees | `network.browser_proxy` in `environment()`: the label, the exit IP and its country. Not the proxy's host, port, username or password |
| What reaches the proxy | the forwarder forwarded to the machine's `127.0.0.1:3132` over its SSH connection. Any process on the machine can use that port without credentials, as with `3130`: root there can send its own traffic out through the owner's proxy too (from the proxy's address, at the proxy's cost). On a closed network it keeps to the network's list; on a `full` one it takes any public host on any port and refuses private, loopback and link-local addresses and local names |
| When the proxy fails | the browser does not start (the node checks the proxy before each launch), and once it runs the forwarder answers 502: the browser never goes out from the machine's own address. Other programs on the machine never used the proxy |

What this does not stop: the proxy provider sees every host the browser visits and the plain-HTTP traffic
(HTTPS stays end to end), and a process on the machine that ignores the browser's settings goes out the
machine's own way, as before. On the node, any local process can use the forwarder's loopback port, as it can
the allowlist proxy's. And `secrets/proxies.json` is 0600 for the user openajar runs as: any program running as
that user reads it. With openajar local that includes an agent with a shell on the owner's own computer (Claude
Code, Codex): the file keeps the credentials from the machine and from the agent's tools, not from the owner's
own account.

#### This computer as the exit (`this-computer`)

`openajar proxy this-computer` puts the reserved label `this-computer` in `browser_proxies`. It is off by
default and only the owner turns it on, at the node. An environment that takes it has a forwarder with no
upstream: it connects to the sites itself, from the node. With openajar local the node is the owner's own
computer, so the address sites see is the owner's home address.

| Question | Answer |
|---|---|
| Who can reach the internet from the owner's address | while such an environment is open, anything on its machine, not only the browser: any process there can use `127.0.0.1:3132`, and root on the machine is the agent. It reaches public hosts from the owner's home address, as the owner's own browser would. On a closed network it keeps to the network's list |
| What it never reaches | the owner's home network and router, and this computer's own loopback services. The forwarder looks names up on the node once per connection and connects to the address it checked; it refuses local names (`localhost`, `.local`, `.internal`, `.home.arpa`, `.lan`) before any lookup, every address that is not global (private, loopback, link-local, CGNAT `100.64/10`, `0.0.0.0`, multicast, reserved and documentation ranges, the same inside an IPv4-mapped or NAT64 IPv6), this computer's own public addresses and the household's IPv6 `/56` around them (IPv6 has no NAT: the printer has a global address too), and the exit address itself (the router, from inside). A closed network's list cannot open any of these: an entry naming a private address stays refused |
| Mail | port 25 is never opened, so the machine cannot send mail from the owner's address |
| Which address | the exit check (ipinfo.io, before each launch) asks over IPv4 first, and the forwarder connects over the family that check answered over, so sites see the address the browser's WebRTC and its place claim. It takes the other family only for a name with no address in this one |
| When it fails | this computer offline or asleep: the check fails and the browser does not start. It never falls back to the machine's own address |

What this does not stop: what an agent does on a site is done from the owner's home address, so it counts for
the household (a ban, a rate limit or a CAPTCHA reaches the owner's own browsing too), and every environment
that takes `this-computer` shares that one address. The owner's internet provider sees the hosts the browser
visits. The household's IPv6 prefix is taken as a `/56`: a delegation wider than that, with home devices in
another `/56` of it, is not covered. On a server node the "home" address is the server's own, which gains
nothing over the machine's.

## Your own machine as an environment's computer (docs/ENVIRONMENTS.md "Your own machine")

With `provider = "own"` an environment is a container on a machine the owner brought in, reached over
SSH (openajar.vms.own). The agent is root in its container, as on any environment's machine, so what
bounds it is set outside the container, on the machine and on the node.

| Question | Answer |
|---|---|
| How the node reaches the machine | SSH with a key made for that machine (`vms/machines/<name>/id_ed25519`, 0600, on the node) or a key file the owner names, with the machine's host key pinned when it was added and confirmed by the owner. No config file, no ssh agent, no password: openajar never asks for one, never sees the one the owner's own ssh may ask for when it installs the key, and stores none |
| What the node's key may do there | run commands and carry forwards (`restrict,port-forwarding` in `authorized_keys`): it runs Docker, and a login that runs Docker is root on that machine. It is the owner's node acting on the owner's machine |
| Where that key is | on the node only. Never on the environment's machine, in a tool result or in the record. In local mode the agent runs as the owner on the same computer and could read the node's files, as it could read anything of the owner's there; a server node keeps it away from the agent entirely |
| What an environment's container gets | the docker provider's walls: a network of its own with no traffic between containers; every capability dropped but the few a dev box and sshd need; no folder, socket or device of the machine mounted; CPU, memory and process limits; and a firewall inside it, set before its sshd starts and out of reach after (no `NET_ADMIN`), that refuses new connections to private, shared and link-local addresses. So it reaches the internet, and not the machine's own services, the owner's home or office network, or another environment (checked for real in tests/unit/test_environments_own_docker.py) |
| What listens on the machine | the container's sshd, published on the machine's loopback only (`127.0.0.1`, a port Docker picks). It takes one key, the node's key for that environment, and shows a host key the node made. Another account on the same machine can connect to that port and gets no further without the key |
| How the node reaches the container | through its SSH connection to the machine (a ProxyCommand that only carries the connection), then SSH to the container with that environment's own key and pinned host key. The machine carries the bytes and cannot read them, short of its root swapping the container |
| What openajar changes on the machine | one `authorized_keys` line (not with the owner's own key), the environments image, and per environment a container and a network, per save an image, all labelled with this node's home id. No firewall rule, no service, no open port. It never stops or restarts the machine |
| What it deletes there | only containers, networks and images with its own home label (the reaper's orphan sweep and `machine remove` filter by it), the image when it put it there, and its own `authorized_keys` line |
| A machine that does not answer | is not "deleted": the provider raises instead of listing nothing, so the reaper ends no environment for it and deletes nothing. The owner removes a machine that is gone for good (`machine remove --force`) |

What this does not stop:

- **A container escape.** The container shares the machine's kernel. Code that breaks out is root on the
  owner's machine, next to their files. A rented VM is the isolated choice for code the owner does not trust.
- **The machine's own root.** Whoever is root on the machine can read an environment's container, its
  saves (images in that Docker, sign-in cookies included) and the traffic of its browser. It is the
  owner's machine: a machine shared with others is shared with them.
- **Outbound traffic from the machine's address.** A `full` environment goes out to the internet from the
  machine's own connection, so sites see that address (a home address, say). `limited` and `none` close
  it as on docker.
- **A rootless or foreign Docker.** Rootless Docker and Podman cannot set the inner firewall, so `machine
  add` refuses them instead of running without the wall.

### A machine handed over whole (`machine add --whole`)

No container: the environment's computer is the machine itself (openajar.vms.whole), and the agent is root
on it. What bounds a container on the owner's machine does not exist here, and the page says so.

| Question | Answer |
|---|---|
| What the agent is | root on the whole machine, for as long as its environment is open. It can read and change everything on it |
| What it reaches | whatever the machine reaches: the internet from the machine's address, and the owner's home or office network when the machine is on it. No wall is set, and `network` `limited` and `none` are refused, because a wall on the machine is one its root takes down |
| Which machine this is for | a fresh Ubuntu 24.04 with nothing of the owner's on it. `machine add --whole` names the signs of use it finds (containers, listening ports, several accounts, files in the usual places) and hands such a machine over only on the owner's own yes at a terminal, never on `--yes` |
| How an environment signs in | as root, with a key the node made for that environment alone. Its public half is one line in root's `authorized_keys` while the environment is open, removed at its close. The machine's sshd and host key are not changed; the environment pins the host key the owner confirmed |
| What a close cleans | that key's line, the processes the environment left (its jobs, what works in `/work`, the browser and its screen), `/work`, and openajar's files of the session. Not what openajar cannot tell from the system's own: installed packages, services, Docker containers, files outside `/work` |
| What that means for the next environment | it runs on the same system, and finds what the last one installed or left outside `/work`. A backdoor one agent leaves (a service, a cron job, a changed binary) is there for the next. One owner's agents on one machine: the same trust as one long-lived machine. For a clean one, install its system again |
| What openajar never does there | restart, power off or reinstall the machine, or stop a service or a container it did not start through a job |
| A machine that does not answer | is not "free" and not "deleted": the provider raises, so nothing is opened on it and no environment is ended for it |

## Logins in an environment's browser

[ENVIRONMENTS.md, Signing in](ENVIRONMENTS.md#signing-in) is how it works. The owner binds a vault login to environments (`openajar environments login add <site> --title/--repo/--all`),
and the agent's `login` signs the environment's browser in with it. A bound login reaches the browser of the
computers it is bound to, its session cookies stay in that browser and in its saves (encrypted,
`encrypt_saves`), the agent (the model) never sees the value, and anything running as root on that machine
could read what the browser holds, so bind only what that computer's work needs.

| Question | Answer |
|---|---|
| Who binds | the owner, at the node: the CLI writes the vault entry with the vault's key (its metadata, the bindings included, is sealed with it). No tool, page, repository file or command adds or widens one; an agent names a login only by the name `environment()` gives it |
| Which computers | the ones the binding covers, read from the node's own records each time a field is filled (openajar.vault.policy.entry_bound). `--title`: the computer of the one agent (grant family) that had a computer of that title, open or saved, when you bound it, across its saves and resumes; another agent that picks the same title gets nothing, since a title is the agent's own choice (bind refuses when no computer or several agents' computers have the title, and names the fix). `--repo`: every environment opened with that repository, by any agent you connected; the repository is also the agent's choice, so bind by repository only an account every such agent may use. `--all`: every environment of every agent. An environment the node does not know as open covers nothing |
| Where the value goes | where the browser runs. With `browser = "vm"` (a laptop) the node types it into the VM's browser over the connection it drives (Playwright through the environment's SSH tunnel), so it passes through that browser's process on the VM; with `browser = "node"` it stays on the node. Never on a command line (the CLI reads a hidden prompt or stdin), in a log, a trace, a tool result, a replay caption or the record |
| Which fields | the vault's rules, as on any lane: the page's site is the login's (https, or the machine's own `localhost` over http), no frame from another origin, and the password only into a password input (the page masks it). A TOTP code from the stored seed; an emailed or texted code, a CAPTCHA or a device approval stays with the person, in the live view |
| What a picture shows | no replay frame is taken while a fill runs. A frame or a video proof can show the page after it, and before the submit a username as typed into a visible field (as the live view does) |
| What stays after | the site's session cookies and storage, in the browser and in the saves (`save_signins`, sealed with the saves key). A resumed environment is signed in; `openajar environments login remove` stops new sign-ins, and the session lasts until the site ends it |

What this does not stop:

- **Root on the VM.** With `browser = "vm"` the agent is root on the machine where the browser runs. It
  cannot read the vault, but it can read the browser's process and profile: a typed password while it is
  in the page, and the session cookies for as long as they last. The binding is the owner's statement that
  this computer may hold that account.
- **What the signed-in agent does.** Signed in, the agent acts in the account as the account can. Nothing
  in an environment asks a person (docs/design/environments.md), so bind an account whose reach fits the
  work, a test or staging account where one exists.
- **A link holder.** A control link of that browser types into the signed-in session (above).

## The owner's solving service

[ENVIRONMENTS.md, The owner's solving service](ENVIRONMENTS.md#the-owners-solving-service) is how it works. It is
off until the owner runs `openajar solver add`; openajar ships no key and shares none.

| Question | Answer |
|---|---|
| Who turns it on | the owner, at the node: the CLI writes `secrets/solvers.json` (600, in the 700 secrets folder, as `proxies.json`). No tool, page or agent adds, changes or reads an entry; an agent only calls `browser_challenge` |
| Where the key goes | only to the owner's own service: in the request body to a built-in (`clientKey`, over https), the `Authorization` header to a webhook (https, or http to this computer itself, no redirects followed), or the environment of the owner's own command (never its command line). Never to an environment's machine, a page, a tool result, a log, a trace or the solve log. Every message from a service or a hook (an error answer, a hook's error or stderr) is redacted with the key as a whole message, and only then cut to its first 300 characters (a stderr tail to its last 300), so no part of the key is left at a cut |
| Who calls the service | the node (with openajar local, this computer), never the machine. The machine and its root agent never see the key or the request |
| What the service learns | a new third party in the owner's work: the page's scheme, host and path (never its query or fragment), the widget's site key and kind, Turnstile's action and cData, the time, and the node's address. Never a proxy, its address or its credentials (only proxyless task types are used), and never the page's cookies |
| Which browsers | every browser the caller may use, an environment's and an account's alike: the checkbox's ref, the one click and `browser_challenge` are the same in both. What goes to the human is the same in both too: a check nothing got past (the browser is held) |
| Which pages | any site. Not sent, because no token from a service works there: Cloudflare's full-page check (by its signal and by its own markers: `#challenge-form`, `#challenge-stage`, the orchestrate script) and a widget inside another site's frame (only one in the page's own document, so the key sent is the page's own). The token goes in only while the page is at the address it was asked for, which the script that writes it checks in the same step |
| What is kept | `solver/solves.jsonl` in the node's home (600): one line per request with when, which browser, the host, the widget's kind, the service, the result and the cost the service reported. Never the key, the token, the path or the query. It is a log only: when it cannot be written or read, the node logs a warning and the solve goes on |

What is not bounded, and what this does not stop:

- **Spend.** openajar sets no limit on what a service may cost. An agent that calls `browser_challenge` again and
  again spends the owner's balance at the service, one solve per call, until the balance or the service's own
  limits stop it. `solver log` and `solver list` show what was asked and what the services reported.
- **A check on a person's own account.** Since this version an account's browser is no longer held for a checkbox
  check, and with a solving service its puzzles are answered too: a check on the owner's own signed-in account is
  clicked and answered without the owner. A site that sees it (an automated click, a solved token, many checks
  in a row) may act on that account: ask for more checks, lock it, or close it. Before this version such a check
  held the browser and went to the owner.
- **Which sites.** Any page a browser opens can have a widget answered, and the page chooses the
  site key it shows: a hostile page can have the owner's service answer another site's widget and read the token
  through its own callback. Sign-up, login and payment pages are not told apart.
- **The token in the page.** It has to be there: the page sends it to the site with its form. Anything on the
  machine can read it while it is valid (minutes), as it can read the rest of the browser.
- **A service that keeps what it learns.** It knows which sites the owner's agents work on and when. Pick one
  you would tell that to.
- **The site's own view.** A site may see a solved token as against its terms, and may ban the address or the
  account that sent it.
- **This computer's own user.** `secrets/solvers.json` is 0600 for the user openajar runs as, as `proxies.json`
  is: any program running as that user reads the key, an agent with a shell on the owner's own computer (openajar
  local with Claude Code or Codex) included. The key is kept from the machine and from the agent's tools, not
  from the owner's own account.

## Saves, the default way an environment stays (docs/ENVIRONMENTS.md "Keeping an environment")

An environment saves when idle unless the agent asks for single use (`keep="none"`) or the owner sets
`default_keep = "none"`. So by default its code and its browser's sign-ins outlive the VM:

| | |
|---|---|
| Where a save lives | the disk snapshot (or, on Morph, the paused machine) in the owner's own provider account, or an image in the Docker of this computer or of the owner's own machine; the file part on the node, sealed with the saves key the vault holds (`encrypt_saves`) |
| What it holds | the machine's disk as the agent left it (code, installs, anything the agent wrote, a secret the app printed included), and the browser's storage state: cookies and local storage of the environment's own browser, never an account lane's or the vault's (`signins=false` or `save_signins = false`: none) |
| What it never holds | the git gateway's token and `~/.git-credentials` (removed before the snapshot; a resume mints a new token), the machine's SSH keys, process memory, swap |
| Who brings it back | an agent of the same grant family by its title, or the owner (`openajar environments resume`); within the node's limits |
| How it ends | `environment_forget`, `environment_close(forget=true)`, the owner, or retention (`saved_days`, `keep_last`), which deletes the snapshot or the paused machine too |

A sign-in kept in a save is a session on that site: anyone who can read the owner's provider account can
read the snapshot. Single use keeps nothing once the work is pushed (a rescue save only for unpushed work).

## The VM firewall follows this computer (local mode)

In local mode (docs/LOCAL.md) the node is the owner's laptop, and the provider's firewall (Hetzner) lets SSH and
ping into each machine from this computer's public address only. That address moves, also while the laptop is
awake, so openajar learns it again: at the service's start, after a sleep, when a tunnel to a machine drops, and
once a minute while machines run (openajar.vms.following). Learning it means asking a "what is my IP" service, so
a third party's answer opens a firewall.

| Question | Answer |
|---|---|
| What the rule names | this computer's address now: one IPv4 `/32`, and one IPv6 `/128` when it has one, for SSH (port 22) and ping. Never a range |
| When the old address goes | in the same call that adds the new one (Hetzner's `set_rules` replaces the list), never after the new link is up. The old address is no longer this computer's (the next customer of that network may hold it), and the tunnels that used it are dead already. So the firewall never names two IPv4 addresses, and between the move and the change it names only the old one, which nobody here uses: the link waits, and nothing else is open |
| Which services | ipify (`api.ipify.org`, `api6.ipify.org`), with ipinfo (`ipinfo.io/ip`) when it does not answer, over HTTPS, from this computer's own connection |
| What a wrong answer cannot do | name a private, loopback, link-local, CGNAT, multicast or reserved address, or IPv4 written as IPv6: such an answer is dropped, and with no address left the firewall stays as it was |
| What one service alone cannot do | move the firewall while the laptop is awake: a change asks a second service (ipinfo first), and when the two give different addresses nothing changes and the log says so. A second service that does not answer does not stop it (the same trust as at a start or a wake) |
| What an attacker gains with a lie | SSH reachable from one address they chose, which still needs the node's key for that machine (key only, no password) and gets the machine's pinned host key. The owner's own tunnels then fail until the next look, a minute later at most |
| How to opt out | `ssh_from` in `~/.openajar/vms.toml` names fixed addresses, and then nothing is looked up |

While the link is down a call that needs the machine runs nothing and says so (`environment_not_ready`, with
`retry_after`), and `environment()` waits up to a minute for the link to come back.

## The agent's own ssh to its environment (local mode)

In local mode (docs/LOCAL.md) the agent runs on the same laptop as openajar, and each ready environment
gets a host entry `openajar-<id>` in `~/.openajar/ssh/config` (openajar.environments.sshaccess), so the
agent's own shell reaches the VM: `ssh`, `scp`, `rsync`, git.

| Question | Answer |
|---|---|
| Which key | one made for that environment (`~/.openajar/ssh/<id>/id_ed25519`, 0600), not the node's VM key; deleted when the environment closes, fails or is saved, and a new one on resume |
| Which host | the VM's host key that openajar made and gave the VM, pinned (`StrictHostKeyChecking yes`, a known_hosts file of one line under the entry's own alias): a reused IP after the VM is deleted is refused, and the entry is removed first anyway |
| What the key may do on the VM | `restrict,pty` and a forced command (`/usr/local/lib/agentos-env/agent-ssh`) in root's authorized_keys: a terminal and the asked command, sftp for scp; no port forwarding either way (a `-R` would let code on the VM reach the laptop's loopback, where openajar and the git gateway listen), no ssh agent (the person's keys never reach the VM), no X11. The entry also sets `ForwardAgent no` and `IdentityAgent none` |
| What is logged | only that a session ran (its start, and whether it is running, for the idle rule, read over the node's own connection). The commands are **not** in openajar's call log, and policy (effects, Ask me first, taint) does not see them. The VM's git gateway still applies the push rules to every push, ssh or not |
| Does it widen what the agent reaches | no. The agent already runs as the person on the laptop, and `terminal_exec` already runs any command as root on the VM. The VM holds no key or token of the person: its one credential is the gateway token for the environment's own repository (`openajar.git.service.vm_token`: bound to that VM's lane and its listener, `repos=(that repo,)`, revoked by `vm_down` at close), in `~/.config/agentos/gitconfig` on the VM. The person's GitHub token stays sealed in the vault on the laptop |
| Server nodes | no entry and no `ssh` field: the agent is on another computer, and works through the tools only |

What this gives up: a person who reads the call log to see what an agent did on its machine sees the ssh
sessions' times, not their commands. Pushes and pull requests are still recorded by the gateway.

## The web app's download (docs/LOCAL.md, `openajar web install`)

The prebuilt web app is JavaScript the owner's browser runs against the node's API with the owner's
session, so whoever can change the archive can act as the owner in the panel. Where its trust comes from:

| Question | Answer |
|---|---|
| Where it comes from | the GitHub release of the openajar version installed (`v<version>`, never "latest"), the same repository the installer takes the code from |
| What it is checked against | GitHub's own digest of the archive on that release, read from GitHub's API, and only when GitHub keeps the release immutable (once published, no file or tag of it can change). The `.sha256` beside the archive must agree, but alone it proves nothing: the same release serves it |
| What is refused | a release that is not immutable, a digest that differs, any other source (`OPENAJAR_WEB_RELEASE_URL`: a fork, a mirror) unless the owner pins the archive's sha256 with `OPENAJAR_WEB_SHA256`, and an archive with a link, an absolute path or a `..` |
| How a release gets it | the `web-release` workflow builds it on the release's draft and attaches it, never replacing a file that is there (no `--clobber`); the release is published after, immutable (docs/DEVELOPING.md, "Releases") |

What this does not stop: whoever can publish a release of the repository (its maintainers, a stolen token
with write access, a poisoned build dependency) ships both the code and the web app, and the installer
trusts them as it trusts the code. The check stops a file swapped on a published release, not a bad release.

## What this does not stop

- **An agent with the node's root.** It can read `node.toml` and write `panel.db`, so it is the owner. Keep agents
  out of the node's own shell; lanes and environments run in their own sandboxes.
- **The node token over MCP, as the owner's authority.** On `/mcp` the node token is `node_token`, never a person:
  a lane a person paused or took over holds it back like any grant, and it cannot approve. It still has the
  owner's scope (every lane, every effect, the whole vault), so an agent holding it can do whatever needs no
  person. Give agents their own grant (`openajar connect --upgrade`) and rotate the node token.
- **An agent with the owner's SSH key.** It can do what the owner does on the node, including `openajar panel login`.
  The device credential being below `person` keeps a Keychain read from approving, but not an SSH session.
- **Chat answers.** A chat client is the agent's own: an agent with a shell can open an MCP session with its token
  and answer its own prompt. Chat therefore never carries `pay`, `irreversible`, a question or anything after
  untrusted text, and the one exception (a publish the commit gate caught, `publish_in_chat`) is off unless the
  owner turns it on (docs/CONFIRMATIONS.md).
- **A person tricked into approving.** The card shows what will leave, bound to the content, and that is all the
  node can do for a person who approves without reading.

See also docs/CONFIRMATIONS.md (levels and methods), src/openajar/policy/TOKENS.md (grants) and
src/openajar/client/README.md (the two credentials on the owner's computer).
