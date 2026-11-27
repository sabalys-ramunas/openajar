# The node's HTTP API

The node's HTTP API: the web app (through its `/api/orgs/{org}/...` contract, docs/WEB-NODE.md), the
CLI on the node and the Mac menu-bar app use it. Served by the same process as the MCP endpoint (the
node daemon in single-box mode, the controller from M5), under `/api`, with the built web app at `/`.

All JSON. Times are ISO 8601 UTC. Secrets never appear in any response: vault entries
show names, domains and lanes only; proxies show label, host and port only.

## Auth

- `Authorization: Bearer <token>` where the token is the node token (the owner) or any grant
  token (`openajar grants issue`, or `POST /api/grants`), or
- a session cookie (`agentos_session`, HttpOnly, SameSite=Strict) obtained by
  `POST /api/session` with `{ "code": "<one-time code>" }` (the owner: `openajar panel login`
  prints a URL carrying a one-time code that expires in 10 minutes), or by a passkey
  (`POST /api/session/passkey`, the owner or a member, see Members below).
- `DELETE /api/session` logs out. Unauthenticated requests get 401.
- State-changing requests made with a cookie must send `X-Agentos-CSRF` equal to the
  `csrf` value returned by `GET /api/status` (or by the sign-in), and an `Origin` of this host
  when the browser sends one.
- The node answers only on localhost and `extra_allowed_hosts` (421 otherwise), on every route.
- A member's authority is never a token: a member grant is not a bearer token (401), and a
  grant token, even an admin grant, is refused on the member routes (403).

### Who may call what

A request runs as one caller: the owner (a signed-in session, the node token, or an admin
grant from `grants issue --admin`), a **member** (a person signed in with their passkey, with
roles per workspace), or a grant, limited to what that grant covers. With the policy service on
the node every rule below is enforced; a node without it only lets the owner in.
`GET /api/status` says which one you are (`principal.owner`; the node token's `principal.kind` is
`node`), `GET /api/me` what you may do.

**A person is present.** Some rows below say "a person": a signed-in panel session (a one-time code or a
passkey) or the node's own CLI, never a bearer token (a grant, an admin grant, the node token) and never the
device credential of `POST /api/session/device`, because an agent may hold any of those. Those routes answer
403 `presence_required` otherwise. They are: approve (one or in a batch), answer a question, create a standing
approval, Ask me first off, `/api/confirmation`, a lane's worker controls, a lane's site list and gate-log
changes, an owner view link, unfreeze, adding a passkey, inviting a member and changing a member's roles,
pointing a proxy label that exists at another host, port or login, moving a lane to another device, making a
device beside lanes that already use its proxy (`confirm`), a new lane (`POST /api/lanes`, on its own proxy or
on a device's) or a template's lanes (`POST /api/templates/apply`) behind a proxy label another lane already
uses, applying a workspace import (it brings logged-in browsers; a dry run stays open), making a schedule that would run under the owner's own grant or turning one on, and a
workspace import with `enable_schedules` (docs/THREAT-MODEL.md).

Member roles give capabilities per workspace,
listed in `GET /api/me` `can` (`"*"`: every workspace):

| Capability | viewer | auditor | approver | operator | admin |
|---|---|---|---|---|---|
| `see`: lanes, states, runs, flows, Activity of the workspace (every principal) | yes | no | yes | yes | yes |
| `approvals`: approvals, questions and previews; deny | yes | no | yes | yes | yes |
| `decide`: approve (the grant's `approve`: commit, pay with `max_usd`) | no | no | yes | no | yes |
| `answer`: agent questions | no | no | yes | yes | yes |
| `watch`: the live view and screenshots, watch only (the grant's `control`) | opt-in (`watch`) | no | opt-in (`watch`) | yes | yes |
| `operate`: open and close lanes, run flows (routed like an agent unless they also decide) | no | no | no | yes | yes |
| `connect`: their own agent's grant in the workspace, up to commit, no specials but `memory_read` | no | no | no | yes | yes |
| `standing`: standing approvals; schedules on, off and delete | no | no | no | no | yes |
| `vault`: add or remove entries bound to the workspace's lanes (never read) | no | no | no | no | yes |
| `members`: invite, change and remove members of the workspace (roles up to admin) | no | no | no | no | yes |

The auditor sees nothing in the panel yet (evidence is phase 3). Taking over a lane stays the
owner's in this phase (members watch only). The one exception is the assisted login's form: a member
whose grant may take control of the lane (operator, admin) may log it in through the form, which
holds the lane for them while it runs; they never get input in the live view.

| Routes | Owner | A member | A grant |
|---|---|---|---|
| `POST /api/session` | public (the one-time code is the credential, rate limited) | | |
| `POST /api/session/device` `{label, ttl_s?}` -> `{session, csrf, id, label, expires_at}` | the node token only, as a bearer (`openajar connect` reads it over SSH for this call): the owner's device credential for the CLI tools on a computer, an owner session with `via` device (90 days by default). It is sent back as the session cookie with the CSRF header; /mcp and the bearer path refuse it. It reads, denies and controls lanes, and is never a person (it never approves) | no (403) | no (403, an admin grant too) |
| `POST /api/invites/accept/options`, `POST /api/invites/accept`, `POST /api/session/passkey/options`, `POST /api/session/passkey` | public: the invite code and the passkey are the credentials; rate limited, a foreign `Origin` is refused | | |
| `GET /`, `/assets/...`, any other page path | public: the built panel's files and page shell, never data | | |
| `DELETE /api/session`, `GET /api/status`, `GET /api/templates`, `GET /api/brains`, `GET /api/me` | yes | yes (status counts only their workspaces) | yes (status counts only its lanes and approvals; no node-wide cost or service list) |
| `GET /api/me/devices`, `DELETE /api/me/devices/{id}`, `GET`, `PUT /api/me/notify`, `POST /api/me/passkeys/options`, `POST /api/me/passkeys`, `POST /api/session/verify/options`, `POST /api/session/verify` | yes (a session; the notify routes also with the node token) | yes | no (403) |
| `GET /api/lanes`, `GET /api/workspaces` | every lane | `see`: the lanes of their workspaces, and those workspaces | only the lanes the grant covers, and their workspaces |
| `GET /api/lanes/{ws}/{name}` | yes | `see` in `ws` | inside the grant's lanes only (403 outside, checked before any lookup) |
| `POST .../open`, `POST .../close` | yes | `operate` in `ws` | inside the grant's lanes; traced, effect read |
| `GET .../screenshot` | yes | `watch` on the lane | inside the grant's lanes |
| `GET`, `POST /api/lanes/{ws}/{name}/login`, `POST .../login/step`, `.../refresh`, `.../take`, `.../resume`, `.../cancel`, `GET .../login/screenshot` (assisted login) | yes, in person (a session or the node token) | a grant that may take control of the lane (`operate`: operator and admin; watch-only is 403) | no (403, an admin grant too): agents ask with `browser_login_request` |
| `POST /api/lanes/{ws}/{name}/login/save` | yes, in person | `vault` in `ws` (admin), and control of the lane | no (403, an admin grant too) |
| `GET /api/lanes/{ws}/{name}/check` | yes | `see` in `ws` | inside the grant's lanes only (403 outside, checked before any lookup); traced, effect read |
| `GET /api/lanes/{ws}/{name}/health` | yes | `see` in `ws` | inside the grant's lanes only (403 outside, checked before any lookup); traced, effect read |
| `GET /api/lanes/checks` | every lane | the lanes they see | only the lanes the grant covers |
| `GET /api/lanes/{ws}/{name}/keepalive` | yes | `see` in `ws` | inside the grant's lanes only; traced, effect read |
| `GET /api/lanes/{ws}/{name}/terminal` (WebSocket) | yes | no member role carries `exec` (4403) | the `exec` special on that lane, through policy: scope, the exec special and its routing (a shared lane or a first use waits for approval, close code 4202); 4403 when refused |
| `GET /api/lanes/{ws}/{name}/vnc` (WebSocket) | yes | `watch` on the lane: watch only, input never passes (4403 without) | no (4403): the relay carries keyboard and pointer input, which is taking over the lane |
| `GET /api/approvals`, `GET /api/approvals/{id}`, `GET .../preview.png`, `GET .../files/{param}`, `GET /api/questions` | all | `approvals`: those on the lanes of their workspaces (404 for others) | only approvals its own grant family asked for (404 for others) |
| `POST /api/approvals/{id}/approve` | a person (403 `presence_required` for every token and the device credential), at or above what it requires (403 `proof_too_weak`) | `decide`, and the node checks the member grant (403 `not_approver`, `four_eyes`); needs a recent passkey check (403 `step_up_required`), and what it requires (403 `proof_too_weak`) | no |
| `POST /api/approvals/{id}/deny`, `POST /api/approvals/batch` | yes | `decide`, in that approval's workspace: a no is a decision too, the same right as a yes (batch approves as above) | no |
| `POST /api/approvals/{id}/clear` ("Got it" on a failed outcome) | yes | `decide`, in that approval's workspace: clearing takes the failed outcome away for everyone, so it is not a viewer's (403 `denied` otherwise) | no |
| `POST /api/questions/{id}/answer` | a person (403 `presence_required` for every token and the device credential) | `answer`, with a recent passkey check | no (403) |
| `GET`, `POST /api/approvals/standing`, `DELETE /api/approvals/standing/{id}` | yes; `POST` only as a person, and `from_approvals` must be approved or used and for `lane` (400) | `standing` on every lane of the standing approval; `POST` with a recent passkey check, and each of `from_approvals` one their role could approve (four-eyes included) | no |
| `POST /api/approvals/{id}/action?d=&e=&s=` | the phone's Approve and Deny buttons: the signed, single-approval, expiring link is the only credential; 401 without a valid signature | | |
| `GET /api/activity` | all | `see`: every principal's events in their workspaces (a member is named by `handle`); a `lane` or `workspace` filter outside them is 403 | its own grant family's events; a `lane` filter outside the grant is 403 |
| `GET /api/runs`, `GET /api/runs/{id}` | all | `see`: runs on their lanes | runs on its lanes |
| `GET /api/audit/replay`, `GET /api/audit/runs/{id}/screenshot`, `GET /api/audit/captures/{id}` | all | `see`: every principal's steps on lanes of their workspaces (403 outside) | its own grant family's steps, on lanes its grant covers (403 outside) |
| `GET /api/audit/verify`, `GET`, `POST /api/audit/shares`, `DELETE /api/audit/shares/{id}` | yes; `POST /api/audit/shares` only as a person (403 `presence_required` for every token and the device credential) | no (403) | no (403); an admin grant reads and revokes, and never makes a link |
| `GET /api/audit/shared/{token}`, `GET /api/audit/shared/{token}/runs/{id}/screenshot` | a read-only share link: its signature is the only credential (404 `share_invalid` when forged, expired or revoked); every view is logged | | |
| `GET /api/flows` | all | `see`: flows bound to their lanes, and lane-pattern flows | flows bound to its lanes, and lane-pattern flows |
| `GET /api/ask-first` | all | `see`: the switches on their lanes | the switches on its lanes |
| `POST /api/ask-first` | yes; off only as a person (a bearer token, the node token included, and the device credential only turn it on) | on: any member who sees the lane. Off: `standing` in the lane's workspace (admin) | on, on its lanes; never off |
| `GET /api/lanes/{ws}/{name}/worker` | a person (a bearer token never, the node token included, nor the device credential) | `see` in `ws` | no (403) |
| `PUT /api/lanes/{ws}/{name}/worker` | a person (a bearer token never, the node token included, nor the device credential) | off: any member who sees the lane. On: `standing` in `ws` (admin) | no (403) |
| `PUT /api/lanes/{ws}/{name}/worker/ask-first` | yes; off only as a person (a bearer token, the node token included, and the device credential only turn it on) | on: any member who sees the lane. Off: `standing` in `ws` (admin) | on, on its lanes; never off |
| `GET /api/confirmation`, `GET /api/confirmation/methods` | a person (a bearer token never, the node token included, nor the device credential) | yes | no (403) |
| `PUT /api/confirmation` | a person (a bearer token never, the node token included, nor the device credential) | an admin of every workspace (`standing` at `*`), with a recent passkey check: `commit`, and turning chat off | no (403) |
| `POST /api/flows/{id}/run` | yes (the click is the co-sign: the run starts authorized) | `operate` on the flow's lane; it starts authorized when their `approve` covers the flow's effect there; otherwise it is routed like an agent's, so a commit that asks first waits for an approver (`waiting_approval`) | the flow's effect on the flow's lane, through policy; a run that asks first is created `waiting_approval` with its `approval_id` |
| `GET /api/schedules`, `GET /api/schedules/{id}` | all | `standing`: schedules on their lanes | its own schedules (the `grant_id` they were created under); 404 for another grant's |
| `PATCH`, `DELETE /api/schedules/{id}` | yes | `standing` on the schedule's lane (404 otherwise) | no (403) |
| `GET /api/workflows`, `/catalog`, `/lists`, `/{id}`, `/{id}/runs`, `/{id}/runs/{run}` | all | `see`: their workspaces (404 otherwise) | its workspaces |
| `POST /api/workflows`, `PUT /{id}`, `POST /{id}/disable`, `/{id}/run`, `/{id}/runs/{run}/retry`, `/suppressions` | yes | `operate`: their workspaces | no (403); agents use the tools (docs/WORKFLOWS.md) |
| `POST /api/workflows/{id}/enable`, `DELETE /{id}`, `POST /lists`, `DELETE /suppressions`, `PUT /limits/{account}` | signed in, never a token | `standing`: their workspaces | no (403) |
| `GET`, `DELETE /api/workflows/leads`, `POST /leads/note`, `GET`, `POST /leads/{batch}`, `GET /lists/{list}/leads` | signed in, never a token | no (403): a lead's address is shown only to the owner, in person | no (403) |
| `GET`, `POST /api/vault`, `DELETE /api/vault/{id}` | yes | `vault`: entries bound only to lanes of their workspaces; deleting is `irreversible` and co-signed by their `approve` | no (403) |
| `GET`, `POST /api/grants`, `POST /api/grants/{id}/revoke` | yes | `connect`: their own agents only (`subject_owner` is the member), in their workspaces, up to commit, no specials but `memory_read`; revoked when the member is removed | no (403) |
| `GET /api/meters`, `GET /api/lanes/{ws}/{name}/meter`, `/queue`, `/taint` | all | `see`: their lanes and their own agents' grants; taint without grant ids | itself and its lanes; taint without grant ids |
| `PUT /api/lanes/{ws}/{name}/meter` (a lane's limits) | yes | no (403) | no (403) |
| `GET /api/lanes/{ws}/{name}/pace` | all | `see`: their lanes | its lanes |
| `PUT /api/lanes/{ws}/{name}/pace`, `GET`/`PUT /api/pace/workspaces/{ws}` (posting pace) | yes | no (403) | no (403) |
| `POST /api/grants/{id}/taint/clear` | the owner signed in, in person, with a fresh step-up and the typed confirmation; never a token or a device credential | no (403) | no (403) |
| `GET`, `POST /api/invites`, `DELETE /api/invites/{id}`, `GET /api/members`, `GET`, `PATCH`, `DELETE /api/members/{id}`, `DELETE /api/members/{id}/devices/{id}` | yes (a session or the node token) | `members`: invites and members whose every role is in a workspace they administer; roles only there (an admin of every workspace for `*`) | no (403, an admin grant too) |
| `POST /api/workspaces`, `POST /api/templates/apply`, `POST /api/lanes`, `POST /api/schedules`, nodes, brain order and tasks, proxies, notification settings, code cards, and the feature packages' routes below | yes | no (403) | no (403) |
| `GET /api/site-keys`, `GET /api/site-apis/{api}/usage` | yes (a session, the node token or an admin grant) | no (403) | no (403) |
| `GET`, `POST /api/site-apis`, `POST /api/site-apis/discover`, `GET`, `PATCH`, `DELETE /api/site-apis/{id}`, `POST /api/site-apis/{id}/pause`, `/try` | yes (a session, the node token or an admin grant); a token counts as an agent's, so it cannot pause, remove or touch what a person wrote | no (403) | no (403) |
| `POST /api/site-apis/{id}/activate` | signed in, never a token: only the owner activates. The same goes for `Activate` on a proposal's card: a token can answer it, and it stays a draft | no (403) | no (403, an admin grant too) |
| `POST /api/site-keys`, `PATCH`, `DELETE /api/site-keys/{id}` | signed in, never a token: only a person touches a key | no (403) | no (403, an admin grant too) |
| `/v1/sites/...` | a site key, or the owner's session with the CSRF header on every method; the node token and every grant are 401 | no (401) | no (401) |
| `GET /api/events` (SSE) | every event | `lane` and `run` events for their lanes; `login` events for lanes they may log in; `schedule` events with `standing`; `approval`, `question`, `trace`, `meter` and `member` events without their payload (a hint to refetch); nothing else | `lane` and `run` events for its lanes; `schedule` events for its own schedules; `approval`, `question`, `trace` and `meter` events without their payload (a hint to refetch through the routes above); never `login`; nothing else |

Every action above read is traced in Activity as kind `panel`, tool `panel.<action>`
(`panel.lane.open`, `panel.lane.terminal`, `panel.approval.approve`, `panel.flow.run`,
`panel.grant.issue`, `panel.vault.delete`, `panel.invite.create`, `panel.member.remove`, ...),
denied attempts included; a member's events carry `via` (passkey) and `member` (their handle). A
signed-in human is the co-signer: the owner's session gets the policy's scope, ceiling and
special checks without the approval routing; a member co-signs only what their member grant's
`approve` covers on that lane (and no four-eyes rule says otherwise), below pay and irreversible
and within the step-up window; anything else is routed
like an agent's; a bearer token (an agent, an app) gets the full routing, approvals included,
exactly as the same action over MCP would. `DELETE /api/vault/{id}` is `irreversible`: with a
token it waits for an approval.

### Members

People other than the owner. Each member holds roles per
workspace; the node holds one member grant per scope for them (kind `human`, never a token),
re-minted on every role change and revoked when they are removed. The panel re-reads the
member, their roles and their grants on every request, so a removal or a downgrade takes effect
at the next one.

- `POST /api/invites` `{handle, display_name?, email?, roles: [{scope, role, options?}], ttl?}`
  (scope: a workspace id or `*`; role: admin, approver, operator, viewer, auditor; options:
  `effects`, `max_usd`, `watch`, `lanes`, `max_session_s`; `ttl` like `90d` ends the
  membership and its grants) -> the invite with `code` and `link` (`<public_base_url>/invite#code=...`),
  shown once. The link works once, for 24 hours. An admin gives roles only in the workspaces
  they administer. `GET /api/invites` lists pending ones; `DELETE /api/invites/{id}` revokes one.
- Accepting: `POST /api/invites/accept/options {code}` -> `{invite, publicKey}` (creation
  options as JSON: a discoverable passkey, user verification required, no attestation), then
  `navigator.credentials.create()`, then `POST /api/invites/accept {code, credential,
  display_name?, timezone?, device_label?}` with the credential as JSON (`toJSON()`) -> a
  session (`{csrf, expires_at, member}` and the cookie).
- Signing in: `POST /api/session/passkey/options {}` -> `{publicKey}`, then
  `navigator.credentials.get()`, then `POST /api/session/passkey {credential}`. The owner can add
  a passkey too (`POST /api/me/passkeys/options`, `POST /api/me/passkeys`) and keeps the code
  sign-in. The RP id is the host of `public_base_url` (the API setting, else the notifier's), else
  `localhost`.
- Sessions: the owner's as before; a member's ends after 12 hours idle or 7 days. An open event
  stream ends at its next heartbeat once the member's session is gone.
- Step-up: a member's approve or answer needs a passkey check from the last 15 minutes, whatever the
  approval requires (403 `step_up_required` otherwise). What requires `present` (by default `pay` and
  `irreversible`, the owner's too) needs a fresh check made for that approval (two minutes, used once),
  or 403 `proof_too_weak` with `requires` and `approval_id`. `POST /api/session/verify/options
  {approval_id?}` -> `{publicKey}` (their passkeys), then `POST /api/session/verify {credential}`.
  Denying never needs it. See Confirmation, under Needs you.
- `GET /api/me` -> `{principal, member, roles, can, session, passkeys}`; `GET /api/me/devices` ->
  `{passkeys, sessions}`; `DELETE /api/me/devices/{id}` removes a passkey (and ends the sessions
  made with it) or ends a session (its `id`).
- `GET`, `PUT /api/me/notify`: my push channel, timezone, quiet hours and digest (kept by the
  notifier when it supports members, else stored in panel.db for it).
- `GET /api/members`, `GET /api/members/{id}` (with devices), `PATCH /api/members/{id}`
  `{roles, scopes?, ttl?}` (replace the roles in `scopes`, by default the scopes named in
  `roles`; an empty list removes them), `DELETE /api/members/{id}` (revokes their grants and their
  agents' grants, ends their sessions, removes their passkeys; the handle can be invited again as
  a new member), `DELETE /api/members/{id}/devices/{id}`.

### The feature packages' own routes

Each package's `api.py` was checked against the routes above. Duplicates are not mounted (the
panel's route serves the real service through `openajar.api.bridge`); the rest are mounted by
`openajar.api.mounts` behind the same auth, owner-only, CSRF on cookie writes, writes traced:

| Package | Mounted | Not mounted (covered above, or why) |
|---|---|---|
| vault | `GET /api/vault/status`, `POST /api/vault/unseal`, `POST /api/vault/seal` | list, create, delete |
| policy | none (its handlers authenticate bearer tokens only, so a session could not use them) | approvals, standing, grants, activity, notifications; the signed phone link is served by the panel itself (above); `GET /api/grants/{id}` is left out; questions are answered by `POST /api/questions/{id}/answer` (above), not the policy's `/answer` |
| runner | `POST /api/runs/{id}/cancel`, `POST /api/runs/{id}/decide`, `GET /api/flows/{id}`, `POST /api/flows`, `POST /api/schedules/{id}/enable` and `/disable` (the panel and the dashboard use PATCH) | runs, flows and schedules lists, flow run, schedule create, get, update and delete (above); the webhook receiver (`/api/hooks/{token}` is not mounted, because the node answers only on localhost: a webhook arrives through the controller, `POST /hooks/{node_id}/{token}`, docs/WORKFLOWS.md) |
| connectors | `GET /api/surfaces`, `POST /api/surfaces`, `GET` and `DELETE /api/surfaces/{workspace}/{name}`, `GET /api/connectors`, `POST /api/connectors/{operation}/run` (`{on, params, idempotency_key, version}`: the run binds through the surface and waits for its approval like a tool call; a site API runs only its active version, and any other `version` is 403). Writing a surface (`POST`, `DELETE`) needs the owner himself, in person: a panel session, so a token and an admin are refused before anything changes | none |
| brains | `POST /api/brains/tasks/{id}/cancel`, `POST /api/brains/tasks/{id}/resume` | brains, order, tasks, task, chip (the status chip) |
| agenda | `GET` and `POST /api/agenda`, `GET` and `PATCH /api/agenda/{id}`, `POST /api/agenda/{id}/propose`, `/approve` and `/cancel` (see "Agenda" below). A member approves an entry in Needs you instead (403 here), where their role and passkey are checked | none |
| env | none | the terminal WebSocket (the panel's own, above, with the policy check) |

## Errors

`{ "error": { "code": "lane_not_found", "message": "..." } }` with a fitting HTTP
status (400 invalid, 401, 403 denied, 404 not found, 409 busy or conflict, 503
unavailable). Codes match `openajar.core.errors`. A `fix` line, where the catalog has one, is for the owner
and agents. A member's error carries it only when it names no command, token or grant (`catalog.member_fix`).

## Resources

### Status
`GET /api/status` ->
```json
{ "node": "node-1", "version": "0.1.0", "csrf": "...", "timezone": "Europe/Vilnius",
  "needs_you": 3, "needs_you_detail": { "approvals": 1, "questions": 1, "codes": 0, "lanes": ["biz-b/reddit"] },
  "cost_today": { "tokens": 120000, "usd": 0.42 },
  "brain": { "name": "claude-max", "state": "ready", "offline_ready": true },
  "nodes": [{ "id": "node-1", "state": "up", "degraded": false }] }
```
`timezone` is the IANA name the node's times are in: the configured `ApiSettings.timezone`, else the
host's zone (TZ, then /etc/localtime), else UTC. Schedule times (`next_fire_at`) are in it.
`needs_you` counts approvals, open questions, open code cards (the owner's only) and lanes that need a
human.

### Workspaces and lanes
- `GET /api/workspaces` -> `[ { "id": "biz-a", "name": "Biz A", "color": "#4f7cff",
  "health": "ok|attention|blocked|idle", "summary": "6 lanes, all ok",
  "lanes": [Lane] } ]`
- `POST /api/workspaces` `{ "id", "name", "color"? }`
- `POST /api/templates/apply` `{ "template": "social-6", "workspaces": ["biz-a"],
  "proxy_per_workspace": { "biz-a": "lt1" } }` -> created lanes
- `GET /api/lanes/{ws}/{name}` -> Lane (with `viewer` when warm)
- `POST /api/lanes` `{ "id": "biz-a/instagram", "kind": "browser", "preset"?,
  "proxy_label"?, "engine"?, "device"? }` (a lane on a device takes the device's proxy). Every Lane
  carries `device`: the browser device it runs on, or null for a device of its own.
- `POST /api/lanes/{ws}/{name}/open` | `/close`
- `GET /api/lanes/{ws}/{name}/screenshot` -> `image/png`. A lane that is not open (cold, or still warming)
  answers 409 `lane_cold`: open it first. Terminal and service lanes have no screen and answer 409
  `lane_cold` too. Nothing else is captured; a lane outside the caller's grant answers 403.
- `GET /api/lanes/{ws}/{name}/vnc` (WebSocket) -> relayed to the lane's noVNC
  websockify, so the panel can embed a live view without a separate tunnel
- `GET /api/lanes/{ws}/{name}/terminal` (WebSocket) -> PTY for terminal lanes
  (text frames in and out; resize message `{ "type": "resize", "cols", "rows" }`). Close
  codes: 4202 waiting for approval (the reason names it), 4401 signed out, 4403 not allowed,
  4404 no terminal, 4503 terminals not configured. Terminal and service lanes (openajar.env)
  are listed with the browser lanes.
- Live streams end when access does (openajar.core.live). The event stream, the live view and the
  terminal are checked again after they open: at once when a grant is revoked, a member is changed or
  removed, a panel session is deleted or a freeze lands, and every `live_recheck_seconds` (5) for
  anything else. The socket then closes with 4401 (signed out, the session or grant is gone) or 4403
  (still signed in, but the lane left the grant or is frozen), the reason saying which; the event
  stream simply ends. A freeze closes everyone's streams in its scope except the owner's own panel
  session: the owner can still look and type during a freeze, agents cannot. A member keeps their
  event stream while some lane their grants reach is open; it closes when none is, and it sends
  nothing about a frozen lane or workspace in the meantime.
- Viewer links (`/viewer-ws/...` and `/view/.../events`) are rechecked the same way. A freeze of the lane, its
  workspace or the node closes them (4403), except an owner link, which only the owner in person makes and
  which keeps its view and its take over as the owner's panel session does. Every link closes at its expiry
  and when the viewer key is replaced (4401); the page's renew moves the expiry of what it opened. While
  frozen, a new viewer on any other link is refused (WebSocket 4403, event stream 409 `frozen`), and so are
  its `/control`, `/renew` and login routes; a link a freeze closed or refused stays refused after the
  unfreeze, as a grant the freeze revoked does. The event stream sends `ended` `{ lane, reason }` before it
  ends.

### Lane check (is this lane safe to use right now?)
The same checklist as `openajar lane check` and the `browser_check` MCP tool (openajar.stealth.lanecheck). A check
never warms the lane, opens a page or contacts the account's site.

- `GET /api/lanes/{ws}/{name}/check` -> LaneCheck, run now (a proxy probe is reused for 60 s).
- `GET /api/lanes/checks` -> `{ "lanes": [LaneCheckBrief] }`: the worst level per lane from the last run
  (agentosd's health service checks every lane about every 5 minutes); a lane not checked yet is
  `unknown` with `at: null`. `?fresh=1` runs every listed lane now. Without the health service the list
  is every lane at `unknown`, with a `note`.

LaneCheck (values made up):
```json
{ "lane": "biz-a/instagram", "level": "warn", "safe": true, "state": "cold",
  "at": "2026-11-04T12:00:00+00:00",
  "summary": "WebRTC stays behind the proxy on normal pages, but a page can make it send UDP from this node's own IP through a blank frame.",
  "counts": { "ok": 7, "unknown": 0, "warn": 1, "fail": 0 },
  "checks": [
    { "id": "proxy", "title": "Proxy", "level": "ok", "detail": "Proxy lt1 answers (180 ms).", "fix": null,
      "data": { "proxy": "lt1", "host": "203.0.113.7", "port": 12323, "took_ms": 180 } },
    { "id": "exit_ip", "title": "Exit IP", "level": "ok", "detail": "Exits from 203.0.113.7, the proxy's own address.",
      "fix": null, "data": { "exit_ip": "203.0.113.7", "proxy": "lt1", "host": "203.0.113.7", "pinned": "203.0.113.7" } },
    { "id": "location", "title": "Location", "level": "ok",
      "detail": "The exit is in LT (Europe/Vilnius, UTC+02:00), the same timezone.", "fix": null,
      "data": { "exit_country": "LT", "exit_timezone": "Europe/Vilnius", "exit_offset": "UTC+02:00",
                "pinned_timezone": "Europe/Vilnius", "pinned_offset": "UTC+02:00", "pinned_country": "LT",
                "locale": "lt-LT", "looked_up_at": "2026-11-04T08:14:03+00:00" } },
    { "id": "webrtc", "title": "WebRTC", "level": "warn", "detail": "...", "fix": "add user_pref(...) to .../user.js ..." },
    { "id": "dns", "title": "DNS", "level": "ok", "detail": "Host names go to the proxy, which resolves them; the node resolves none." },
    { "id": "profile", "title": "Profile", "level": "ok", "detail": "Profile intact: pin version 2, Firefox 158, 210.4 MiB used, 31.2 GiB free." },
    { "id": "session", "title": "Signed in", "level": "ok", "detail": "Signed in to Instagram (session cookie present).",
      "data": { "source": "file", "sites": { "instagram": { "signed_in": true, "missing_cookie": null,
                "cookies_seen": ["sessionid"] } } } },
    { "id": "account", "title": "Account", "level": "ok", "detail": "Signed in as the expected Instagram account (@acme.coffee).",
      "data": { "pinned": true, "site": "instagram", "handle": "@acme.coffee", "visible": true, "status": "match" } },
    { "id": "hold", "title": "Hold", "level": "ok", "detail": "No hold, no quarantine; agents have control." }
  ] }
```
LaneCheckBrief: `{ "lane", "level", "safe", "state", "at", "failing": ["session"], "warning": ["webrtc"],
"health": { "grade", "label", "headline", "facts_line" } | null }` (`health` is the lane's health below, from
that same last run, with the default 30 days; null when the lane has no run yet).
The `session` check's `data.sites.<site>.expires_at` is when the first of that site's session cookies
expires (ISO, UTC), or null for a cookie that lives for the browser session only.

### Lane health
One grade per lane from the checks the node already runs (openajar.stealth.health, the same as `openajar lane
health`): the lane check, the `openajar lane audit` pin findings that name the lane or its device, and the
holds and quarantines the node recorded. It reports consistency, leaks and the challenges sites actually
showed. It never calls a lane safe or undetectable, and scores nothing about how a person behaves.

- `GET /api/lanes/{ws}/{name}/health?days=30` -> LaneHealth, from the lane check's last run (run now when
  there is none, or with `?fresh=1`). `days` (1 to 365, else 400 `invalid`) is the window for challenges and
  quarantines. Authorised as the check is: a grant sees only its lanes, a member needs `see`.
- `grade`: `stop` ("Stop") when a check or pin finding fails or the lane is quarantined now; `check` ("Check
  this") on any warning, a challenge in the last 14 days, 2 or more challenges in the window, no check run
  yet, or not one check that passed; else `normal` ("Looks normal", with the headline "No known
  problems"). A check that could not run is listed as `unknown`, never counted as a pass, and on its own
  moves no grade.
- A challenge is a hold the node set because a page showed a captcha, a checkpoint, a block page or a rate
  limit (`needs_human:<signal>`, `backoff:rate_limited`); a hold written again after a hand back (the same
  challenge still on screen) is not counted twice.

LaneHealth (values made up):
```json
{ "lane": "biz-a/instagram", "grade": "check", "label": "Check this", "headline": "1 thing to look at",
  "facts": ["8 of 9 checks pass", "1 could not run", "2 challenges in 30 days", "pin 41 days old"],
  "facts_line": "8 of 9 checks pass · 1 could not run · 2 challenges in 30 days · pin 41 days old",
  "findings": [
    { "id": "challenges", "level": "warn", "source": "events",
      "sentence": "Sites showed a captcha on 2026-11-08, a rate limit on 2026-11-02 (2 challenges in 30 days).",
      "fix": "Let the lane rest and lower its pace. Look at what agents did just before each one (openajar lane view biz-a/instagram)." },
    { "id": "check.account", "level": "unknown", "source": "check", "fix": null,
      "sentence": "The lane has no expected account set, so which account is signed in is not checked." } ],
  "days": 30, "at": "2026-11-11T12:00:00+00:00", "checked_at": "2026-11-11T11:56:00+00:00", "state": "cold",
  "counts": { "checks_total": 9, "checks_ok": 8, "checks_unknown": 1, "checks_warn": 0, "checks_fail": 0,
              "quarantined_now": false, "challenges": 2, "challenges_recent": 1, "quarantines": 0,
              "other_holds": 0, "pin_age_days": 41 },
  "checks": ["The lane check: proxy, exit IP, ..."], "cannot_see": ["The site's own risk model, ..."],
  "note": "A good grade means these checks found no problem. ...", "docs": "docs/RESPONSIBLE-USE.md" }
```
- `findings[].level`: `fail`, `warn`, `unknown` or `note` (a fact worth knowing that moves no grade);
  `id` is `check.<check id>`, `pin.<audit check>`, `challenges`, `quarantine`, `quarantines`, `other_holds`,
  `session_ends`, `not_checked` or `nothing_verified`. `counts` may also carry `browser_lag` (installed
  Firefox minus the pinned one), `session_days_left` and `last_quarantine`.
- `cannot_see` is fixed: the site's own risk model and the account's history, how the site rates the exit
  IP, how the lane behaves over weeks, and whether the site's terms allow automation (docs/RESPONSIBLE-USE.md).

### Lane keepalive (docs/design/keepalive.md)
- `GET /api/lanes/{ws}/{name}/keepalive` -> `{ "available": true, "lane", "enabled", "opted_out", "site",
  "next_at", "due_at", "activity_at", "session_expires_at", "timezone", "window_today": ["07:40", "23:10"],
  "blocked": {"code", "reason"} | null, "waiting"?, "status": "signed_out" | "challenged" | null,
  "status_reason", "last_visit", "visits": [{ "at", "trigger", "site", "outcome", "reason", "scrolls",
  "opened_item", "session", "host", "seconds" }] }`. `{ "available": false }` when agentosd runs no
  keepalive service. Reads only: it plans, it never opens the lane.

### Profiles (docs/PROFILES.md)
The owner in person only (a session cookie): a bearer token, the owner token included, a grant and a
member get 403 `denied`. `{ "available": false }` without the profiles service.
- `GET /api/profiles[?workspace=]` -> `{ "available": true, "profiles": [{ "profile": "ws/id", "kind",
  "display_name", "headline", "accounts", "version", "state", "draft_waiting" }] }`
- `GET /api/profiles/sites` -> `[{ "site", "domain", "fields": [{ "name", "source", "limit",
  "people_only" }] }]`: each site's profile fields, limits and login domain.
- `POST /api/profiles` `{ profile }`: save a whole profile, live at once.
- `GET /api/profiles/{ws}/{id}[?version=]`, `PATCH /api/profiles/{ws}/{id}` `{ "changes" }` (nested fields
  merge, lists replace, null removes), `GET /api/profiles/{ws}/{id}/status` (per account and field:
  in_sync, failed, syncing, learning, learn_failed, proving_elsewhere, held, relearn, manual,
  done_by_hand).
- `POST /api/profiles/{ws}/{id}/approve` `{ "version" }`, `.../reject` `{ "version", "note"? }`: an agent's draft.
- `POST /api/profiles/{ws}/{id}/attach` `{ "surface", "site"?, "overrides"? }`, `.../detach` `{ "surface" }`,
  `.../mark` `{ "surface", "fields" }` (manual fields changed by hand).
- `POST /api/profiles/{ws}/{id}/photo/{avatar|cover}?name=` with the image as the body, then
  `GET /api/profile-photos/{sha256 hex}` serves it.
A save answers `{ "profile", "version", "state", "created", "changes": { surface: [fields] }, "summary",
"next" }`; a value a site would not take is 400 `profile_invalid` naming the account and field.

- `level` per check: `ok`, `warn`, `fail` or `unknown`. The lane's `level` is the worst of `ok`, `warn` and
  `fail` (`unknown` never raises it); `safe` is false on `fail`. A check that crashed is a `warn`, and the summary names the checks that could not run. `detail` is one sentence, `fix` what to do.
- `data` holds names, booleans, counts, addresses and times only: never a cookie value, an account id
  read from a cookie, a token or a proxy credential.
- The checks, and how each decides:
  - `proxy`: a TCP connect and one HTTPS request through the proxy, from the node (`fail` when it does not
    answer or rejects its credentials). A lane without a proxy is `warn` when it is tied to a site.
  - `exit_ip`: the lane self-check's rule. An IP proxy must exit from its own address, a gateway from
    the exit pinned when a lane first opened through it, and no proxy from the node's own IP. An unpinned
    gateway is `unknown` while the node's own IP cannot be read, and so is any gateway whose pin cannot be read.
  - `location`: the exit's country against the locale's region (`en-US`: US) and the pin's country,
    and its timezone's UTC offset now against the pinned timezone's (`warn` with both values; `unknown`
    when the lookup gave no timezone or the pinned one is not known, since nothing was compared). The
    place comes from ipinfo.io's JSON answer, asked through the proxy at most once a day per proxy
    (it is that run's proxy probe, so no extra request), cached in `OPENAJAR_HOME/cache/exit-geo.json`.
    The request carries no cookie and nothing about a lane or an account.
  - `webrtc`: from the effective prefs (camoufox.cfg defaults, the launch prefs, the profile's prefs.js
    and user.js, locked prefs). `ok` when WebRTC is off or `media.peerconnection.ice.proxy_only` is set;
    `warn` with only Camoufox's `proxy_only_if_behind_proxy` (measured: an about:blank frame still sends
    STUN from the node's IP); `fail` with neither, or when host addresses are not hidden.
  - `dns`: an HTTP or SOCKS5 proxy resolves host names (Playwright's proxy filter hands every request to
    it by name; measured: the node resolves none). No proxy: names resolve on the node, as its traffic does.
  - `profile`: the folder exists and is readable, the pin file is readable and version 2 (version 1 is a
    `warn`, completed on the next launch), the pinned Firefox against the installed one, no browser
    nobody owns, no stale lock, the lane's disk use and the disk's free space.
  - `session`: the site's session cookies (openajar.node.sites) are present, read from the warm
    browser's context or from a copy of the profile's cookies.sqlite. `unknown` when they cannot be read
    or the lane names no site.
  - `account`: the account id in the site's cookies against the lane's pinned `AccountRef` (AUD-12):
    `fail` for another account, `warn` when no id is pinned, `unknown` when the site keeps none in cookies.
  - `hold`: a quarantine or a hold for a human is `fail`; a self-clearing hold, a human in control or a
    bad last signal without a hold is `warn`. `data.cleared_by` says who can clear it (`human` or `time`).

### Lane control (take over and hand back)
- `POST /api/lanes/{ws}/{name}/control` `{ "action": "take" | "hand_back" | "stop_agent" | "deny_request",
  "note"? }` -> Lane. `take`: a human takes control (agent calls on the lane get `human_in_control`);
  `hand_back`: agents may act again (also clears a pause or a pending request); its optional `note` (at most
  300 characters, `hand_back` only) is what the human tells the agents, shown to them once in the hand-back
  note (below) and kept on the control record as `handback_note`;
  `stop_agent`: pause the agent and cancel its runs and brain tasks on this lane;
  `deny_request`: refuse an agent's request for control back. `hand_back` always, and `take` while the lane
  is held, stopped or quarantined, need the owner in person (the panel session, or `openajar lane handback`
  and `take` on the node, which send a one-time code beside the token): the node token alone and the device
  credential get `presence_required` (403). `stop_agent` and `deny_request` need no more than the route.
- `POST /api/lanes/{ws}/{name}/view-link` `{ "mode": "view" | "control", "ttl_s"? }` ->
  `{ "url": "/view/biz-a/instagram?t=...", "expires_at": "...", "mode": ..., "owner": bool }`. A signed,
  short-lived link for one lane (default 15 minutes), usable without a session (it is what the agent
  hands to a human for a CAPTCHA or a first login). A control link asked for by the owner in person (the
  panel, or `openajar lane view --control` on the node) is an owner link (`owner: true`): from it, the
  owner can allow a refused host in one click (below). `browser_view` and grants (even admin grants) never
  get one. Every link has a hard end from when it was made that renewing never moves: 30 minutes for a
  link anyone but a person made (`browser_view`, a grant, the node token, the device credential), 8 hours for
  an owner link. On a link that is not an owner link, a hand-back, a take over of a held or stopped lane,
  the assisted login and adding a site need the owner's panel session in the same browser (the viewer is
  served on the panel's own address, so the cookie goes with it): the page then works on an owner link of
  its own (`owner_link` in the control answer, or the page's boot when the owner was already signed in).
  Without it they get `presence_required` (403), and the relay passes no input from it at all.
- `GET /api/lanes/{ws}/{name}/sites` -> `{ lane, sites, strict }`: the list as it stands. Anyone who can see the
  lane reads it; a grant outside the lane gets 403.
- `POST /api/lanes/{ws}/{name}/sites` `{ "op": "add" | "remove" | "set", "sites": [...], "strict"? }` ->
  `{ lane, sites, strict, changed, running }`. Changes the lane's site list on the daemon's own lane
  manager, so a warm lane's egress guard has it at once (no restart; `running` says a warm lane got it).
  `add` appends what is not there, `remove` refuses entries that are not on the list, `set` replaces it
  (`[]`: no lane restriction). Entries as in `openajar lane sites` (docs/design/egress-guard.md, section 5:
  `frame:<host>`, `accounts.google.@cc`). The owner in person only: the owner's panel session, or
  `openajar lane sites` on the node (the node token with a one-time code from panel.db). The node token
  alone and the device credential get `presence_required` (403); a member or any grant, an admin grant
  included, gets `denied` (403). `openajar lane sites` uses it while agentosd runs and writes the store
  when it does not.
- `GET /api/lanes/{ws}/{name}/gate-log?since=24h` -> `{ lane, shapes: [{ id, host, path, method, op,
  fields, field_sets, decisions, why, rule, count, first, last, promoted? }], promoted: [...], identities:
  [{ site, id, field, rules, count, flow, verified_field, why, first, last }] }`: what the commit gate
  classified without a rule while agents drove the lane, by shape (names only, never a value), and the
  identities openajar does not know that publishes on the lane acted as (egress-guard.md 8.10; ids and
  field names only). `POST` the same path with `{ "allow_read": id }`, `{ "as_commit": name, "shape": id }` or
  `{ "remove": id }` -> `{ lane, promoted | removed }`: the node's own read allowlist and commit rules
  (docs/design/egress-guard.md 8.9), each change a `gate_shape` row in the action log. The owner only:
  the node token or the owner's own session; a member's session or any grant gets `denied` (403).
  `openajar lane gate-log` uses it while agentosd runs and writes the file itself when it does not.
  `?observed=1` also lists the shapes observe mode traced that no commit or write rule matched
  (`observed_only` when the gate never classified one); those can be named with `as_commit` only.
- `GET /api/rules/health?lane=&stale_days=14` -> `{ generated_at, since, stale_days, rules: [{ id, site,
  effect, kind, host, verified, owner, count, first, last, lanes }], unknown: [{ id, site, host, path,
  method, op, count, decisions, sources, lanes, first, last, write_like, kind, promoted }], unruled: [{ lane,
  sites, hosts, shapes }], flags: [{ kind: stale_rule | new_unknowns | no_rules, site, rule?, rules?, lanes,
  shapes, message }] }`: effect rule health from the lanes' egress traces (docs/design/egress-guard.md
  8.12; names and shapes only). The owner only, like the gate log. `openajar lane rules` prints the same.
- `GET /view/{ws}/{name}?t=<token>`: the standalone viewer page (the live view as a card scaled
  to the window, a pill that says who has control with Take over or Hand back, Stop agent and the
  agent's recent actions on this lane under More, and a banner when the agent asks for control
  back). Its boot data carries `agent`, the agent's name for an environment's lane (the
  environment's agent, else its MCP client), or null. The page's own
  `POST /view/{ws}/{name}/control?t=`, `/renew?t=` and `/sites?t=` need an `Origin` of
  this host: a request with a foreign Origin or none is refused (403 `denied`), so a link alone
  cannot take a lane or change its site list from a script. `browser_view` called with a grant from a controller's org
  returns a link to watch only, unless the node's controller ceiling has `control`.
- `POST /view/{ws}/{name}/sites?t=<token>` `{ "add": "<entry>" }` -> the same answer as `/api/.../sites`.
  The one-click allow of a block notice; an owner link only (403 for any other link).
- `POST /view/{ws}/{name}/clipboard?t=<token>` -> `{ lane, text, chars }` with `Cache-Control: no-store`:
  the text on the warm lane's clipboard (its CLIPBOARD selection, read with `xclip` on the lane's display),
  for the page to put on the viewer's own clipboard after a copy in the lane. `text` is `""` when nothing is
  copied there (no text, an image only, nobody owns it). At least as strict as the relay's input gate: a
  control link (403 `denied` for a view link), control `human` (403 `denied` otherwise), and a person (403
  `presence_required`: an owner link, or the owner's panel session in this browser), with an `Origin` of this
  host. Then 409 `lane_cold` when the lane is not running, 429 `rate_limited` past 30 reads a link in a
  minute, 503 `xclip_missing` (its fix is the install command), 413 `clipboard_too_large` over 1 MB (never
  cut short) and 409 `clipboard_unreadable` (no answer within 2 s, not UTF-8). Each read is a
  `lane_clipboard` row in the action log with who, the lane and the number of characters, never the text.
  The page calls it only on the person's own key (a copy or cut in the lane) or click (Copy from the lane),
  never on a timer.
- `GET /view/{ws}/{name}/events?t=<token>` (SSE): `hello`, `control`, `lane`, `action`, `actions`, `gone`, `ended`,
  and `egress`: a document the lane's site list refused while a human had control,
  `{ lane, seq, at, host, path, kind: subframe | redirect | navigation | popup | committed, frame, rule,
  reason, repeats, allow, command }` (host and path only, never the query). `allow` is the entry that would
  admit it and `command` the `openajar lane sites <lane> add <allow>` line. At most one per (rule, host,
  kind) every 30 s, with `repeats` counting what was folded in, and five a lane in any 30 s.
- `GET /viewer-ws/{ws}/{name}?t=<token>` (WebSocket): the live-view relay. The server drops
  every keyboard, pointer and clipboard message from the viewer unless the lane's control
  is `human`, the token's mode is `control` and a person is behind it (an owner link, or the
  owner's panel session), so read-only is enforced server-side, not in the page. The VNC
  clipboard carries nothing (x11vnc and the lane's browser exchange no selections): pasting types
  the text as keys, and copying out uses `/clipboard` above. Every accepted input event counts as human activity for the idle hand-back.
- On an open agent environment's own browser (`env/<id>-browser`) a control link counts as the person for
  `/control`, `/clipboard` and the relay: it takes over (a held lane too), types, copies and hands back with no
  panel session, recorded `by: "link holder"` (docs/THREAT-MODEL.md, "Viewer links"). Every account lane keeps
  the rules above, and so does an environment whose screen runs the desktop on a server node (not in local
  mode): its dock opens a root terminal on the machine (docs/THREAT-MODEL.md, "An environment's desktop").
- `GET /view/env/{id}-browser/proof/{proof}.png?t=<token>` -> the PNG of a proof an agent kept in that
  environment (`browser_screenshot(proof=...)`), `Cache-Control: private, no-store`. The token is the proof's
  own signed link (15 minutes, from `environment()`), not a viewer token; any other answers 403 `denied`.
- `GET /view/env/{id}-browser/replay/{frame}.jpg?t=<token>` -> the JPEG of one frame of that environment's
  replay (docs/ENVIRONMENTS.md, "Replay"), `Cache-Control: private, no-store`. The token is the replay link
  the owner's `GET /api/orgs/{org}/environments/{id}/replay` signed (15 minutes, that one environment's frames);
  any other answers 403 `denied`.
- `PUT /view/env/{id}/upload?t=<token>` (or `POST`) with a `.tar.gz` or `.tar` body -> `{dest, files, bytes,
  unpacked_bytes, paths, more?, sha256, next}`: the archive extracted into `dest` on the environment's machine
  (an overlay, nothing deleted). The token is a one-use link from `environment_upload` (15 minutes); a changed,
  expired or used one answers 403 `denied`. Also 400 `invalid` (a member it names), 409 `busy`, 413
  `too_large` (over 512 MB), 502 `error` (tar on the machine failed). docs/design/environments.md, "Uploads".
- `GET /view/env/{id}/download?t=<token>` -> the `.tar.gz` that `environment_download` (or `POST
  /api/environments/{id}/download`) packed on the environment's machine, always as an attachment:
  `Content-Type: application/gzip`, `Content-Disposition: attachment; filename="<name>.tgz"`, `Content-Length`,
  `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src
  'none'`. The token is a one-use link (15 minutes, purpose `download`: an upload link never opens it). A
  changed, expired or used one, or one of a closed environment, answers 403 `denied`; 403 `frozen` for an
  agent's link while the lane is frozen; a `HEAD` answers 405 and spends nothing. The file on the node is
  deleted once served. docs/design/environments.md, "Downloads".

### Exposes and forwards (`openajar expose`, `openajar lane forward`)
- `GET /api/exposes` -> `{ links: [{ id, kind, lane, port, state, by, created_at, expires_at, socket, name?,
  host?, url? }] }`: what is open now.
- `POST /api/exposes` `{ "kind": "expose", "lane", "name", "port", "ttl_s"? }` -> the link, `state:
  "waiting"`, with `socket` (the path the laptop's `ssh -R` binds), `owner_uid`, `lease_s` and
  `heartbeat_s`. `{ "kind": "forward", "lane", "port", "ttl_s"? }` opens the node's socket into a warm
  terminal or service lane at once. `ttl_s` defaults to 2 hours, at most 12.
- `POST /api/exposes/{id}/ready` -> the link, `state: "active"`, `routed`: the socket is there, a socket,
  agentosd's, so the lane routes the address (now when it is warm, else when it wakes).
- `POST /api/exposes/{id}/heartbeat` -> the link: renews the lease (30 s) without moving the hard end; 404
  with the reason once it ended. Not traced (every 10 seconds); the start and the end are.
- `DELETE /api/exposes/{id}` -> the link, ended.

All five need the owner at `device` or above (the device credential, a person's session, the node's CLI): a
grant, an admin grant included, and the node token get `presence_required` (403). docs/THREAT-MODEL.md,
"Exposes and forwards", says what they open.

### Freeze (break glass)
- `GET /api/freeze` -> `{ available, node, frozen: [FreezeRecord], lanes: [{lane, state, by}], node_frozen,
  problem, journal }`. A member or a grant sees the freezes over what it can see (no journal).
- `POST /api/freeze` `{ "scope": "lane" | "workspace" | "node", "target"?, "reason" }` -> the freeze report:
  `{ freeze_id, scope, target, already_frozen, durable, details_saved, lanes, lanes_warm, stopped: {calls, ms,
  still_running}, revoked: {grants: [{id, subject, note}], cascades_to, standing}, runs_cancelled,
  schedules_paused, triggers_paused, brain_tasks_cancelled, errors, took_ms }`. The owner (the node token, an
  admin grant, a signed-in session), or a member who is an admin of that workspace (lane and workspace scope).
  Freezing again is harmless (`already_frozen`, and the sweep runs again). `revoked` lists every agent grant
  for a node freeze; for a lane or workspace freeze only the grants whose whole reach is inside the scope. A
  wider grant is kept, and every call it makes inside the scope fails with `frozen` until the unfreeze.
- `POST /api/unfreeze` `{ "scope": "lane" | "workspace" | "node" | "mesh", "target"? }`: the owner signed in, in
  person (a cookie session). When the node has members, the session needs a passkey check inside the step-up
  window (`step_up_required` otherwise). No bearer token unfreezes; on the node, `openajar unfreeze`. Nothing
  is restored: revoked grants stay revoked, paused schedules and triggers stay paused.
- `POST /api/freeze/link?scope=&t=&e=&n=&s=`: a push's one-tap Freeze button. The signed link (single use, 30
  minutes) is the only credential, and it can only freeze. Anything else is 401.

An agent's call on a frozen scope fails with `frozen` (409).

Lane control object (part of every Lane):
```json
{ "state": "agent|human|paused", "by": "owner", "since": "...",
  "takeback_requested": false, "takeback_reason": null, "takeback_by": null,
  "last_human_input": "..." }
```
Control returns to agents automatically after `control_idle_seconds` (default 600) with no
human input. SSE event type `control` carries `{ "lane": id, "control": {...} }`. After a hand back
the record also has `handback_note` (the human's note, or null).

The hand-back note (openajar.node.handback): when agents get a lane back (a hand back, the idle hand back,
a resume, the end of a hold), the next thing each agent session sees on that lane carries `handback`: the
`browser_wait_handback` or `browser_control` result, or the first `browser_navigate`, `browser_snapshot`,
`browser_act` or `browser_wait` result. It says what changed while agents did not have the lane:
```json
{ "what": "A human had test/handback from 08:01:48 to 08:01:54 UTC; handed back by owner.",
  "by": "owner", "from": "...", "to": "...", "human_note": "New street; post the order",
  "url": { "before": ".../handback.html", "after": ".../handback.html#review", "changed": true },
  "title": { "before": "Shipping details", "after": "Shipping details" },
  "signal": { "before": "ok", "after": "ok" },
  "tabs": { "open": 2, "opened": [{ "url": ".../feed.html", "title": "Fixture feed" }] },
  "downloads": [], "uploads": [],
  "fields": [{ "ref": "e8", "role": "textbox", "label": "Street", "value": "22 New Street",
               "typed_by_human": true, "was": "1 Old Road" },
             { "ref": "e9", "role": "textbox", "label": "Password", "value": null,
               "hidden": "a password or code field: its value is never shown" },
             { "ref": "e13", "role": "checkbox", "label": "Gift wrap", "checked": true }],
  "refs": "Your refs from before the hand back are stale: take a new browser_snapshot before you act. ...",
  "untrusted": "Titles, labels and field text come from the page or from the human's typing: ..." }
```
Text values are clipped to 120 characters and scrubbed of secret shapes; a password, one-time code, card
number or anything whose name says it is a secret never shows a value, and hidden inputs are left out.
Captured cheaply: no screenshot, no snapshot when control is taken, one snapshot (for refs) at the hand
back. Each agent session gets the note once, for 15 minutes.

Lane:
```json
{ "id": "biz-a/instagram", "workspace": "biz-a", "name": "instagram",
  "kind": "browser", "engine": "camoufox", "preset": "account",
  "state": "cold|warming|warm|active|quarantined",
  "signal": "ok|captcha|rate_limited|blocked|logged_out|unknown",
  "proxy": { "label": "lt1", "host": "203.0.113.10", "port": 12323 },
  "exit_ip": "203.0.113.10", "last_used": "...", "node": "node-1",
  "quarantine_reason": null, "logged_in_hint": "needs_login|ok|unknown",
  "control": { "state": "agent|human|paused", "by": null, "since": null },
  "frozen": false,
  "claim": { "holder": "agent claude-code", "kind": "agent", "ref": "3f9a1c02",
             "since": "...", "purpose": "posting a reply", "held_s": 42.0, "implicit": false,
             "ttl_s": 120.0, "expires_in_s": 98.5, "renewable_until_s": 858.0 },
  "queue_length": 1 }
```

`frozen` is whether a break-glass freeze covers the lane (`null` on a node without the freeze
service).

`claim` is who holds the lane for a multi-step job, or `null`.
`holder` is the agent's label (`<kind> <subject>`) or, for a flow or connector run, `flow run
<run id>` with `run_id` set and `implicit: true` (a run's claim lasts the run, so it has no
`ttl_s`, `expires_in_s` or `renewable_until_s`). `ref` is a short stable id of the holder's
session; it is never a token or a grant id. `expires_in_s` is how long the lease lasts without
another call by its holder, and `renewable_until_s` how long it can still be renewed while others
wait (`claim_max_seconds`). `queue_length` counts the callers waiting for the lane (reads, acts,
claims and runs). The lane's page shows both, with who waits and since when
(`GET /api/lanes/{ws}/{name}/queue`, "Meters, queues and taint" below); agents see the same through
`browser_queue`.

### Needs you (approvals and questions)
- `GET /api/approvals?state=pending|approved|denied|expired&limit=50`
- `POST /api/approvals/{id}/approve` `{ "note"?, "content_hash"?, "edit"? }` | `/deny` `{ "note"? }`
- `POST /api/approvals/batch` `{ "ids": [...], "decision": "approve|deny" }`
- `GET /api/approvals/{id}/messages?page=&cursor=`: a send card's messages, 25 a page, read in order
  (`next: {page, cursor}`); the last page gives `seen`, which `POST /api/approvals/{id}/approve` needs
  (`{"seen": ...}`) when the preview is cut (`messages.read_all_first`), or 409 `see_whole_card`.
- `GET /api/approvals/{id}/files/{param}`: an image the card shows (the photo a post attaches), for
  whoever sees the approval (404 otherwise). Read from the lane's inbox only while its bytes still hash
  to the digest the approval binds.
- `POST /api/approvals/standing` `{ "lane", "effect": "commit", "ttl_seconds", "from_approvals"?, "shape"? }`:
  with `shape: true` ("always allow this shape") it covers the flow version or connector operation of
  `from_approvals` on that lane (the surface of a connector run), whatever it posts, until it expires (the
  panel offers a day or a week). Never pay, irreversible or install_base (the routing table refuses
  those), pace limits still apply, and every other standing rule the policy has (for example exact content
  after untrusted text) is checked when it is used.

Approval:
```json
{ "id": "ap_7f3", "state": "pending", "effect": "commit",
  "lane": "biz-a/instagram", "summary": "Post 1 photo with caption to instagram.com as biz-a",
  "content_hash": "sha256:...", "preview": { "text": "...", "image": "/api/approvals/ap_7f3/preview.png" },
  "requested_by": { "kind": "agent", "id": "claude-code", "thread": "T-91" },
  "created_at": "...", "expires_at": "...",
  "requires": "chat", "methods": ["panel", "cli", "chat"],
  "decided_method": null, "decided_level": null, "decided_detail": null }
```
The `summary` is written by the node from what it will actually do, never by the
model. Approving binds to `content_hash`; different content needs a new approval.

**Confirmation (docs/CONFIRMATIONS.md).** `requires` is how strong the yes must be: `chat`, `signed_in`,
`present` or `device_key`, weakest first. It is fixed when the approval is made, from the strictest thing
that applies, so a setting changed later does not move it. `methods` are the ready ways that could reach it
for the person behind the request. A decision records `decided_method`, `decided_level` and, for chat,
`decided_detail: { "client": "..." }` (the client's own word). The node works these out from how the
decision arrived; nothing a caller sends sets them. `decided_via` stays as it was.

**Who decided, and how.** `decided_by` names the owner one way, `owner`, however they decided. Beside it,
`decided_who` and `decided_how` say it for a person to read: `the owner` and `in the panel`; `the owner`
and `on the node, in person` for the node's CLI with its one-time code; `the node's token` (and no how) for
a bare node token, which proves no person. Activity entries carry the same `who` and `how`, and a panel
action's trace says `args.via`: `session`, `cli`, `device`, `node_token` or `token`. A trace made with the
node's token has `principal_kind: node`, never `human`.

| How it arrives | Method | Level |
|---|---|---|
| The owner's session | `panel` | `signed_in` |
| A member's session with a passkey check from the last 15 minutes | `panel` | `signed_in` |
| Either, with a fresh check made for this approval | `passkey` | `present` |
| `openajar approvals` on the node | `cli` | `signed_in` |
| `approval_confirm` in a chat client | `chat` | `chat` |
| A bearer token (the node token, an admin grant) | none | none: it proves no person, and reaches up to `signed_in` |
| A controller with a proof | its method | at most `controller_max_level` |
| A controller with no proof | none | none: reaches up to `signed_in` |
| A push's Deny | none | none: denying needs no level |

An approve below `requires` is 403 `proof_too_weak` with `requires` and `approval_id` in the error, so the
panel can ask for the check. A batch reports it for that approval and goes on. A method that is planned or
turned off is 403 `method_unavailable`.

- `GET /api/confirmation` -> the setting: `{ commit: { when, level }, pay: { when, level }, irreversible:
  { when, level }, after_untrusted: { level }, methods_off, controller_max_level, defaults, can_turn_off, set_by,
  set_at, controller, chat_clients: [{ client, decisions, last }] }`. The rows are what is in force: the stricter
  of what the node holds and what a controller asks for. `controller` is that layer on its own (`{ the rows it
  named, set_by, set_at }`, or null), so a row that will not come down can say why. `own_workspaces` is the
  node's own per-workspace rows with no controller in them: a `PUT` of `workspaces` replaces the whole map, so a
  change of one workspace sends this map back with that one changed (the app does this). `commit.when` is `[routing]
  ask_first_default`: one setting, and the switches of Ask me first go on working above it.
- `PUT /api/confirmation` `{ commit?, pay?, irreversible?, after_untrusted?, methods_off?, publish_in_chat?, controller_max_level?,
  apply_to_waiting? }` -> the same. Whole or not at all. It counts for new approvals. With `apply_to_waiting: true`
  it also reaches what already waits: `requires` is worked out again from the setting in force, never below a floor,
  the answer adds `applied_to_waiting: [{ id, was, now }]`, and Activity says so (`confirmation_applied`). Refused 403 `denied` (`floor`) when it would
  lower a floor: `pay` and `irreversible` always ask, at `signed_in` or above, and `after_untrusted` stays at
  `signed_in` or above and at what `policy.toml` says; `owner_only` when an admin names a row of the owner's;
  403 `method_unavailable` for a level no ready method reaches, so nothing is locked out.
- What can be turned off: `chat` and `telegram`, and only those (`methods_off: ["chat"]`, or
  `openajar confirmation set chat off`; the same for `telegram`).
  `panel`, `cli` and `passkey` cannot, so a person can always decide. Raising `commit.level` to `signed_in`
  has the same effect for commits. Only the owner turns chat back on.
- `GET /api/confirmation/methods` -> `[{ id, label, level, status, on, can_turn_off }]`: ready and planned.
- On the node: `openajar confirmation show`, `openajar confirmation set <row.field> <value> [--apply-to-waiting]`.

`card` (openajar.policy.cards) is what the approval will do, as the node built it from the content it
binds (null when there is nothing more to show than the summary and preview):
```json
{ "type": "post", "operation": "instagram.post_photo", "title": "Post a photo with a caption",
  "site": "instagram", "site_label": "Instagram", "surface": "biz-a/instagram", "acts_as": "@biza.coffee",
  "through": "biz-a/instagram", "line": "as @biza.coffee on Instagram, through biz-a/instagram",
  "text": { "param": "caption", "value": "Autumn at the lake" },
  "images": [{ "name": "image", "file_name": "lake.jpg", "size": 48213, "mime": "image/jpeg",
               "url": "/api/approvals/ap_7f3/files/image" }],
  "params": [{ "name": "caption", "value": "Autumn at the lake", "editable": true },
             { "name": "image", "file": true, "image": true, "file_name": "lake.jpg" }],
  "editable": ["caption"] }
```
A flow run's card is the same with `flow` and `version` instead of `operation` and the lane as
`surface`. A browser_act click or Enter that submits a form gets `{ "type": "form", "action": "host/path",
"method", "fields": [{ "label", "role", "name", "changed", "value", "was" } | { ..., "checked",
"was_checked" } | { ..., "value": null, "hidden": "..." }], "changed": 2, "editable": [], "edit_note" }`:
what the form sends against what the page loaded with, never a password, code or hidden value. It
cannot be edited: deny it and tell the agent.

Inline edit: `"edit": { "caption": "the new text" }` on approve, for the card's `editable` params only
(text the content shows verbatim; never a hidden or file param, never a browser_act call). It needs the
same right as approving (members: `decide`). The approval then binds the edited content (its
`content_digest` changes, `content_hash` does not), the summary says "approved with an edit by
<who>: caption", `edit` is `{ "params": ["caption"], "by", "at" }`, and Activity records it twice:
`approval.approve` "with an edit by va (caption)" and the run's `approval_edit` when the runner releases
the parked run with the edited params. A retried call with the old content never consumes an edited
approval, and a run whose edited params no longer make the approved content fails with `edit_mismatch`.
Right before a run an approval released executes, the runner makes the run's binding again
(`ActionBinding` in `openajar.policy.approvals`, the one way every card binds its content): when the
approval no longer covers it (other content, another lane, or other params), the run fails with `approval_mismatch` and nothing is done. A trigger proposal and a
workflow send step check their card the same way; a changed send step asks again.

### Questions (agent questions from `human_ask`)
A question is an approval of kind `question`. It is also listed under `/api/approvals` (with
`"kind": "question"`), so the Needs you approvals list keeps showing it; a client that wants only
actions filters on `kind`.
- `GET /api/questions?state=open&limit=50` -> `open` (waiting; the default), `answered`, `declined` or
  `expired`. A grant sees the questions its own grant family asked.
- `POST /api/questions/{id}/answer` `{ "answer": "..." }` -> the question (owner only). With `options`
  the answer must be one of them. Answering a question twice is 409 `conflict`. The answer goes through
  the same decision the policy's own answer route makes, and is traced as `panel.question.answer`.

Question:
```json
{ "id": "ap_7f3", "state": "open", "lane": null, "question": "Use the blue photo?", "options": ["yes", "no"],
  "answer": null, "asked_by": { "kind": "agent", "id": "claude-code", "thread": "T-91" },
  "created_at": "...", "expires_at": "...", "answered_at": null, "answered_by": null }
```

### Code cards (a one-time code the owner types)
A login whose vault entry uses the code card (`{"catcher": {"kind": "relay"}}`, docs/design/phones.md)
waits at the code step for the owner. The node opens a code request for that one lane, site and time
window, pushes the owner a link to `/needs-you#<id>`, and types the code into the site once the owner
gives it. Requests live in the node's memory only.
- `GET /api/codes` -> the open requests: `[{ "id": "cr_01jc...", "state": "open", "lane": "biz-a/instagram",
  "site": "instagram.com", "requested_by": { "kind": "agent", "id": "claude-code" }, "created_at": "...",
  "expires_at": "..." }]`.
- `POST /api/codes/{id}` `{ "code": "424 242" }` -> the request with `state: "used"`. The code is 4 to 12
  letters or digits (spaces and dashes are dropped). It is taken once: a second code, a closed or expired
  request is 404 `vault_code_request_closed`. The code is write-only: it goes to the typist, and no
  response, trace, event, log line or database row ever holds it.
- `POST /api/codes/{id}/decline` -> `state: "declined"`; the login stops with `needs: "human"`.
- The web contract (`src/openajar/api/web/codes.py`) lists only the open cards, so the app never shows one that
  is closed.
Both are traced as `panel.code.enter` and `panel.code.decline`; the login's own call traces
`vault.code_requested`, then `vault.code_used` (`source: "manual"`), `vault.code_declined` or
`vault.code_timeout`.

### Repository cards (a token the owner pastes for a private repository, on the node's own page)
`environment_open` on a repository the node cannot read opens a question in Needs you, one per
repository (openajar.environments.connect). `GET /api/questions` marks it with `repo_connect`:
`{ "repo": "github.com/acme/app", "host": "github.com", "reason": "not_connected" | "refused",
"token_url", "classic_token_url", "deploy_keys_url" }` (the last three on GitHub only). The routes take
the card's approval id, and all of them need the owner signed in with a person present: an agent's
token, any grant and the node token alone are `presence_required`, a member is `denied`.
- `POST /api/repo-connect/{id}/form` -> a form trip (`{ "id", "kind": "repo_token", "stage": "waiting",
  "subject", "form_url", "expires_at", "repo" }`, as `POST /api/forms` answers). An owner with a passkey
  confirms first (`step_up_required`). No route takes the token: the web app never holds one
  (docs/WEB-NODE.md). The app opens `form_url`, the node's own page (`GET /api/forms/{token}`: no
  script, no outside asset, a strict CSP, `no-store`, only over https or on this computer), and waits on
  `GET /api/forms/trips/{id}`. The page's POST (urlencoded `token`) asks
  `GET https://api.github.com/repos/{owner}/{name}` with the token, then runs `git ls-remote` with it as
  the gateway fetches, keeps it as `openajar environments repo add --token` does (this one repository,
  declared for `env`), and approves the card. A check that fails shows what to do on the page (400), and
  nothing is kept; the same link takes another try until it runs out (15 minutes). One that works ends
  the link, and the trip says `"stage": "done"` with `"outcome": { "repo", "token_expires_at" }`
  (GitHub's `github-authentication-token-expiration`, or null). A card closed meanwhile ends its link
  (410). The token is write-only: no response, trace, approval, log line or database row holds it (the
  vault holds it sealed).
- `POST /api/repo-connect/{id}/deploy-key` -> `{ "repo", "name", "public_key", "fingerprint",
  "settings_url" }`: the vault's deploy key for the repository, or a new one (the same on every call).
- `POST /api/repo-connect/{id}/check` -> `{ "repo", "connected": true }` once a machine can clone it (the
  card closes), else 400 `repo_connect_failed` (`Not yet: ...`).
- `POST /api/repo-connect/{id}/decline` -> `{ "repo", "declined": true }`: the card is denied.
A closed card is 404. They are traced as `panel.repo.token_form`, `panel.repo.deploy_key`,
`panel.repo.check` and `panel.repo.decline`, without the token.

### Environment downloads (the owner copies a file or a folder off a machine)
- `POST /api/environments/{id}/download` `{ "path" }` -> `{ "url", "expires_at", "path", "kind": "file" |
  "folder", "files", "bytes", "sha256", "max_bytes", "fetch", "unpack", "next" }`: what `environment_download`
  answers an agent, for `openajar environments download ID PATH [--to DIR]`. `path` is a file or a folder
  under `/work` (a relative one starts at the workdir). The owner only, as on the other owner routes: an agent's
  grant is `denied` and uses the tool instead. 400 `invalid` (outside `/work`, a link out
  of it, missing, over 512 MB packed), 404 (no such environment), 409 `environment_not_ready`. Traced as
  `panel.environment.download`; the fetch itself is an `environment.download` row with `by: "owner"`.

### Prepared machines (the owner builds one now)
- `POST /api/environments/prepare` `{ "repo", "ref"?, "force"? }` -> `{ "repo", "key", "started", "why" }`: starts
  the build of the repository's prepared machine (its `.openajar/environment.toml`, docs/ENVIRONMENTS.md
  "Prepared machines"), for `openajar environments prepare`, which then follows the build's steps in the node's
  store. The owner in person only (a panel session, or the node's CLI with its one-time code in panel.db; the
  bare node token is 403 `presence_required`, because an agent may hold it). 400 `invalid` when the repository has no usable file, when one is ready already
  (without `force`), or when the node has no room for a build now. Traced as `panel.environment.prepare`.
- `GET /api/orgs/{org}/environments/prepared` -> `{ "machines": [...], "eur_month", "eur_per_gb_month", "pool",
  "month" }` for the app. `machines` are the rows of `openajar environments prepared --json`, newest build first;
  `eur_month` what their images cost together, at `eur_per_gb_month` (`snapshot_eur_per_gb_month`). `pool` is
  the warm pool (`warm`, `target`, `minutes`, `machines`) plus `price_hourly_eur` (a warm machine's own price,
  else what the pool would rent at the default size; null when the provider cannot say), `worst_eur_month`
  (the most it costs a month if no open ever takes a machine: `target` x (730 h x 60 / `minutes`) lives x the
  started hours of each, `ceil(minutes / 60)`, x `price_hourly_eur`; 0 on docker) and `month_eur` (this
  month's warm machines no open took, so far). `month` is this calendar month's opens: `{ "opens",
  "from_prepared", "from_warm", "setup_saved_s", "recent": [{ "at", "seconds", "from": "warm" | "prepared" |
  "cold" }] }`, `recent` the last 40 that became ready, oldest first (`at` the open, `seconds` until ready).
  The owner only (a member or a grant is 403); 404 on a computer with no environments.

### Saved environments (keep "save", openajar.environments.saving)
An environment opened with `keep: "save"` is saved when it idles, at its time limit and on close, and
`environment_open` with its title resumes it; one with `keep: "none"` gets a rescue save when the reaper
must delete it with unsaved work (docs/design/environments.md, "Keeping"). The web contract shows them:
- `GET /api/orgs/{org}/environments` adds `keep` (`none`, `live`, `save`), `last_save` (`{ "id", "at" }` or
  null) and `save_error` to each summary, and two keys to the answer: `saved`, the saves of environments
  that are not open, newest first, each `{ "id", "title", "repo", "branch", "head", "saved_at", "snapshot_gb",
  "files_bytes", "cost_eur_month", "note", "agent", "prune_at", "paused", "kind", "env_id", "provider",
  "server_type", "price_hourly_eur", "services" }` (`head` is 12 characters, `note` the agent's own words
  cut to 300, `cost_eur_month` the snapshot at `snapshot_eur_per_gb_month`, or the paused machine's own;
  `paused` true when it keeps the machine itself asleep; `provider` and `server_type` the machine it wakes
  on; `price_hourly_eur` what that bills an hour once awake, from its environment's record, null once the node
  no longer keeps it; `services` `[{ "cmd", "port" }]`, the commands it starts again, each cut to about 200
  characters), and `snapshots_eur_month`, what every kept snapshot costs a month. A member sees the saves of
  their own agents, a grant its own family's.
- `DELETE /api/orgs/{org}/environments/saves/{save_id}` -> `{ "forgot": [ids], "snapshots_deleted": n }`, and
  `"kept": [{ "id", "why" }]` for a save whose snapshot the provider did not delete (it stays, so nothing billed
  is lost track of), and `"snapshots_left": [{ "id", "save", "provider", "where", "why", "words" }]` for a save
  whose snapshot reads back as another openajar home's (a save imported from another node): the save is
  forgotten, the snapshot stays where it is, and openajar no longer tracks it. The owner in person only: a token, a grant and a member get 403. An unknown id is 404.
  Traced as `environment.forget`.
- `POST /api/orgs/{org}/environments/{id}/close` is the owner's close: it deletes a `keep: "live"` machine too,
  and saves a `keep: "save"` one first (502 `save_failed` keeps the machine).
- `GET /api/orgs/{org}/environments/{id}`: each of `ports` is `{ "port", "process", "link", "tunnel"? }`.
  `link` is null unless the agent shared the port with `environment_port`; then it is the node's forward,
  `http://127.0.0.1:<p>/` (on openajar local it opens as is), and on a server node `tunnel` is the
  `ssh -N -L <p>:127.0.0.1:<p> <node>` line that brings it to the person's computer first. The forward
  ends when the environment closes.
- `GET /api/orgs/{org}/environments/{id}` also has `bringup`, its bring-up step by step: `{ "total_s", "ok",
  "steps": [{ "step", "at_s", "seconds", "ok", "skipped"?, "saved_s"? }], "running": [{ "step", "at_s",
  "so_far_s" }], "restore_from"?: "snapshot" | "paused" | "files" }`, or null for an environment from before
  steps were kept. `step` is a name like `VM create`, `VM ready`, `setup check`, `tunnels and git`, `network`,
  `browser`, `clone`, `restore`, `checkout`, `refresh`, `setup`, `app`, `apt ready`, `swap`, `tools top-up` or
  `ssh`; `at_s` the seconds after the bring-up began (steps that ran at once overlap); `ok` false for a step that
  raised. While it starts, `steps` are the ones done so far, `running` the ones in progress, `total_s` the
  seconds so far and `ok` null. Work the open did not need is named: `setup` with `skipped: "prepared
  machine"`, `seconds: 0` and `saved_s` (the build's setup less the refresh here) when it booted a prepared
  machine, and `VM create` and `VM ready` with `skipped: "warm machine"` (their measured seconds kept) when it
  took a warm one. `restore_from` is what a resume woke from. Also `replay` (`{ "frames", "since", "bytes",
  "kept_until" }`, `kept_until` null while it is open; null when the node keeps no replays), and the resume:
  `resumed` (as `environment_open`'s, null when it was not resumed), `resumed_by` (`"owner"` or null),
  `owner_woken` (the owner resumed it and no chat has taken it yet) and `resumed_at` (its open, which is when
  the resume began; null when not resumed). `environment()` gets `bringup` too.

### Environment tools (openajar.environments.tools, builder I3)
What an agent gets back, beside the web contract above:
- `environment_open(title?, repo?, ..., keep?, signins?)`: no `title` with `repo` uses that repository's one
  open or saved environment (several: `environment_choice`, which lists each title, newest first; none: a new
  one named after the repository). `signins: false` keeps no browser sign-ins in its saves. `resumed` adds
  `expires_at`, `signins` (in words, or null), `running_jobs_lost` (when no saved service came back) and, in
  each of `services`, `{ "cmd", "cwd", "port", "running", "listening"?, "job_id"?, "why"?, "restart"? }`
  (`restart`: a ready `terminal_job_start(...)` call).
- `environment(id?)` always adds `open` (`this_chat: true` on the one bound here) and `saved`, the agent's
  `note`, and `git.pull_requests` (`{ "url", "number", "branch", "base" }`).
- `environment_close(id?, discard?, final?, note?, forget?)` -> as before, plus `saves`: `{ "made": save id or
  null, "left"?: [{ "id", "saved_at", "snapshot_gb", "cost_eur_month" }], "forgot"?, "snapshots_deleted"? }`,
  and with a save `pause` ("saved: about EUR 0.02 a month, resumes in about a minute"). `keep` is in the
  answer only when it saved (keep `save`) or let go (keep `live`). `forget: true` deletes the machine and
  every save of the title; with unpushed work it needs `discard: true` (else `unsaved_work`).
- `environment_port(port)` -> `{ "port", "local_port", "url", "tunnel"?, "listening", "next" }`. Errors:
  `invalid` (not 1 to 65535, or 22 or 3129), `environment_not_ready`.
- `environment_pull_request(title, body?, base?, draft?, branch?)` -> `{ "url", "number", "branch", "base",
  "draft", "state", "created", "repo", "next" }` (`created: false`: one was open for that branch already).
  Only a branch the environment created; never a merge. Errors: `denied`, `invalid` (not on github.com),
  `pull_request_failed` (no token on the node, or GitHub refused; the token never appears).

### Checkpoints, watching and the owner's resume (openajar.environments.checkpoints, watching, waking)
Checkpoints are git's, on the machine (docs/ENVIRONMENTS.md, "Checkpoints"); a person watching counts as
use; the owner resumes a save held by no chat.
- `GET /api/orgs/{org}/environments/{id}` adds `watched` (null, or `{ "since", "viewers", "live_views",
  "port_connections" }`: live views of its browser and connections through an `environment_port` link open
  now) and `checkpoints`, newest first, at most about 20:
  `[{ "id": 3, "note": "before the refactor", "at": "2026-11-18T11:50:03+00:00", "auto": null, "repos": [{
  "dir": "/work/shop", "head": "2723947348e2", "files": 4, "diff": { "files_added": 1, "files_changed": 2,
  "files_removed": 1, "lines_added": 40, "lines_removed": 12 } }] }]`. `head` is the commit the checkpoint
  sits on (12 characters), `files` how many files it had changed from that head, `auto` the rollback an
  automatic one was made before (`"before rollback to 2"`) or null, and `diff` what changed since, against
  the work tree now (null when the machine did not answer; read at most every 15 s per environment, so
  polling the page is cheap). Polling this route never counts as use of the machine.
- `GET /api/orgs/{org}/environments/{id}/checkpoints/{checkpoint}` -> `{ "checkpoint": 3, "max_bytes": 200000,
  "max_files": 500, "repos": [{ "dir": "/work/shop", "files_total": 4, "files": [{ "path": "src/cart.ts",
  "status": "M", "lines_added": 12, "lines_removed": 3 }], "patch": "diff --git a/src/cart.ts ...",
  "patch_bytes": 5120, "truncated": false }] }`: one checkpoint against the work tree now, for a diff view.
  `status` is git's `A`, `M`, `D` or `T` (from the checkpoint to now: `A` is a file made since, which a
  rollback removes); `lines_*` are null for a binary file. The patch of every repository together is cut
  at `max_bytes` and each file list at `max_files` (`truncated: true`). The owner only (a member or a grant
  is 403). 404 `checkpoint_not_found` when no repository has it, 503 `environment_not_ready` when the
  machine is not ready, 409 `checkpoint_failed` when git or the machine could not answer.
- `POST /api/orgs/{org}/environments/saves/{save_id}/resume` -> EnvSummary plus `{ "opened": "resumed" |
  "already_open", "resumed": { "save_id", "saved_at", "from": "snapshot" | "paused" | "files", "branch",
  "head", "state": "restoring", "note", ... }, "links": { "browser": "http://localhost:7400/view/env/<id>-browser?t=...",
  "browser_expires_at", "tunnel"? } }`. It resumes the save as `environment_open(title)` does, within the
  same limits (409 `environment_limit` or `environment_full`, whose fix names the setting), held by no chat (`binding` `kept:<id>`):
  the state is `starting` and the live-view link shows a page that reloads until the browser is up, then
  the browser. `already_open`: its title is open, and that one is returned unchanged. The next
  `environment_open` with its title takes it with `opened: "resumed"`. The owner in person only, as Forget.
  Traced as `environment.resume`.
- `POST /api/environments/resume` `{ "ref": "<title or save id>" }` -> the same answer, for `openajar environments
  resume`, the owner in person only (a panel session, or the node's CLI with its one-time code in panel.db; the
  bare node token is 403 `presence_required`). A title shared by saves of several
  agents is 400 `invalid` naming their save ids.
- The live view of a saved environment: `GET /view/env/<id>-browser?t=...` after its machine closed answers
  a small page, not 404: for the owner (an owner link, or the panel session in that browser) the title, when
  it was saved, the agent's note and a Resume form, `POST /view/env/<id>-browser/resume?t=...`, which needs
  that same person and this page's `Origin`, resumes it and answers 303 to the new environment's live view
  (an owner link); for anyone else only that it is saved. A link to one that is open again says so (the
  owner gets its live view), one still starting reloads itself, one closed with no save is 410. An expired
  link shows the page only to the signed-in owner.
- MCP `environment_checkpoint(note?, on?)` -> `{ "checkpoint", "note", "at", "repos": [{ "dir", "head",
  "branch", "files_changed" }], "compare": "git diff refs/agentos/checkpoints/<n>", "rollback", "ignored",
  "stays", "next" }`; `environment_rollback(checkpoint, on?)` -> `{ "checkpoint", "repos": [{ "dir",
  "branch", "head", "files_restored", "files_removed", "was": { "branch", "head" }, "commits_undone"?,
  "branch_was_at"? }], "untouched": [dirs without that checkpoint], "undo": { "checkpoint", "rollback" },
  "failed"?, "ignored", "next" }`. Errors: `checkpoint_failed`, `checkpoint_not_found`, `rollback_refused`.
  `environment()` adds `checkpoints` (as above, without `diff`) and `watched`.
- A call with the `on` of an environment saved and closed since is `no_environment`, saying when it was
  saved and `environment_open(title="...")` (or that it is open again and how to take it); it is never
  resumed for the agent.

### Assisted login (the owner fills a form, the node types it)
A person should almost never log in through the live view. `openajar.node.login_assist` reads the
lane's sign-in page, shows the owner a form with exactly the fields this step asks for, types what
the owner enters with the vault's typist at human pace, presses the step's own continue button, and
reads the next page. The panel's lane modal, the live view's side section and `openajar lane login`
are three faces of it. One login per lane; it takes control of the lane while it runs (agents get
`human_in_control`) and gives it back as it was when it ends.

- `POST /api/lanes/{ws}/{name}/login` `{ "url"? }` -> a Login. Warms the lane and opens the site's
  sign-in page from the sites table (`openajar.node.sites`, `Site.login`), the `url` given, or stays on
  the page the lane shows when it is already on the site. A login already running is returned as is.
- `GET /api/lanes/{ws}/{name}/login` -> the Login, or `{ "lane", "state": "none" }`.
  `?after=<version>&wait=<s>` (at most 30) answers when the version moves past `after`.
- `POST .../login/step` `{ "step": "s3", "values": { "username"?, "password"?, "code"? } }` or
  `{ "step": "s3", "choice": "c2" }` -> the Login after the step. Values are text of 1 to 1024
  characters; the step's fields only (400 otherwise, naming the field, never its value). A `step` that
  is not the one shown, or a page that changed since, is 409 `login_step_changed` and nothing is typed.
- `POST .../login/refresh` reads the page again ("I approved it on my phone"); `POST .../login/take`
  goes to the live view (state `paused`); `POST .../login/resume` back to the form (a hand back of the
  lane does the same); `POST .../login/cancel` `{ "done"? }` ends it (after a finished login it only
  drops the save offer).
- `POST .../login/save` `{ "name"? }` -> the Login with `entry` (the vault entry, names only): the
  username and password the owner typed, as a password entry bound to this lane and the registrable
  domain they were typed on. The `login` tool then signs the lane in with no form.
- `GET .../login/screenshot` -> PNG of the active page at the last step, every password and one-time
  code field painted over on the node before it leaves (404 when there is none).
- With no login running, every action but `POST .../login` is 404 `login_closed`.

A Login: `{ "id": "la_...", "lane", "state", "site", "site_name", "origin", "host", "step", "note",
"error", "version", "screenshot", "can_save", "saved", "by", "started_at", "updated_at", "requested"? }`.
`state` is `working`, `ask` (the form), `waiting` (approve on your phone), `stopped` (a captcha or a step
it cannot read: take control), `paused` (the owner has the live view), `signed_in`, `cancelled` or
`done`. `step` is `{ "id", "kind", "heading", "message", "number", "fields": [{ "name", "label",
"secret", "length", "autocomplete" }], "choices": [{ "id", "label", "kind" }], "submit", "reason" }`,
with `kind` one of `sign_in`, `code`, `approve`, `choose`, `confirm`, `captcha`, `stuck`, `signed_in`
(openajar.vault.steps). Heading, message, labels and the number are the page's own words, for the
owner only; they never decide anything. `requested` is an agent's `browser_login_request` (`why`, `by`,
`at`). `signed_in` is the site's session cookie plus an `ok` signal.

The path of a value: the panel session, the node token (CLI) or a live-view control link carries it
in one POST; the route reads it by hand into a Secret (no validation error can echo it) and hands it
to the service, which reads the page again, checks it is the step the owner saw, checks each field
with the vault's own decision for a one-off entry bound to this lane and this step's host (https, or
http on loopback; no frame from another origin; the field's role), and types it. No response, trace,
event or log line holds it; a trace names the step and the fields (`panel.lane.login.step`). Username
and password stay in the node's memory until the login ends, for the save offer, then are dropped;
a code never is kept. Live events (`login`) carry the lane, state, version and step kind only.

The live view has the same form in a side section (openajar.node.viewer_app), on its signed link:
`GET`, `POST /view/{ws}/{name}/login?t=` (`{"action": start|step|refresh|take|resume|cancel|save, ...}`)
and `GET /view/{ws}/{name}/login.png?t=`. Only a person runs it: an owner link, or a control link with the
owner's panel session in the same browser (`presence_required` otherwise); a view link is refused.

### Activity (the action log) and runs
- `GET /api/activity?lane=&workspace=&kind=&before=&limit=100` -> trace events
  `{ "id", "at", "kind", "tool", "lane", "principal", "effect", "status",
  "summary", "cost": { "tokens", "usd" }, "run_id" }` (secrets redacted at write)
- `GET /api/runs?status=&limit=` -> runs with honest status
  (`succeeded_verified|succeeded_unverified|partial|failed|...`) and proof
  (`{ "url", "id", "screenshot" }`)
- `GET /api/runs/{id}` -> run with steps
- `POST /api/runs/{id}/decide {decision: retry|skip|fail}` (owner or an admin grant) -> resolves a
  partial run. A token never decides a run its own grant family started (403 `self_approval`), and
  is refused when the run's grant is revoked or unknown; a signed-in session always can

### Audit (the tamper-evident action log)

The action log is hash-chained and checkpointed with a node audit key (src/openajar/policy/AUDIT-LOG.md).
These routes read it; the panel's timeline scrubber is built on `replay`.

- `GET /api/audit/replay?lane=&day=&tz=` -> one lane's actions on one day (default today, in the
  node's timezone unless `tz` is given), oldest first:

  ```json
  { "lane": "biz-a/instagram", "day": "2026-11-03", "timezone": "Europe/Vilnius",
    "from": "2026-11-02T22:00:00Z", "to": "2026-11-03T22:00:00Z", "truncated": false,
    "chain": { "status": "ok", "proven_through": { "mark": 41, "seq": 48211, "at": "2026-11-03T21:50:00Z" },
               "findings": [] },
    "steps": [
      { "id": "tr_...", "seq": 48190, "at": "2026-11-03T09:41:07Z", "ended_at": "2026-11-03T09:41:08Z",
        "kind": "call", "tool": "flow_run", "effect": "commit", "status": "ok",
        "summary": "Posted to instagram.com as biz-a", "args": { "caption": "..." }, "error": null,
        "duration_ms": 812, "principal": { "kind": "agent", "id": "claude-code" },
        "grant": { "id": "gr_...", "label": "claude-code, up to commit on biz-a: weekly posts" },
        "run": { "id": "run_...", "flow": "instagram-post", "status": "succeeded_verified", "lane": "biz-a/instagram",
                 "summary": "...", "proof": { "url": "https://www.instagram.com/p/...", "readbacks": [...], "note": null } },
        "screenshot": "/api/audit/runs/run_.../screenshot",
        "events": [ { "seq": 48191, "at": "...", "kind": "page", "status": "ok", "summary": "...", "args": {...} } ],
        "chain": { "status": "ok", "rows": [ { "seq": 48190, "status": "ok" }, { "seq": 48191, "status": "ok" } ] } }
    ] }
  ```

  A step is one tool call (its `tool_start`, its `tool_end`, and the events traced inside it) or one
  standalone event (`kind` is then the trace kind: `panel`, `authorize`, `share_view`, ...). `status` is
  the call's end status, `unfinished` when it has none yet. `args` are the call's own arguments, redacted
  at write. `run` and `screenshot` are present when the call belongs to a flow run that kept a proof (or
  failure) screenshot; `screenshot` is an image URL under the same scope. Chain status per row and per
  step (the worst of its rows): `ok` (covered by a signed checkpoint), `unprovable` (written after the
  latest checkpoint), `unchained` (written before the chain began), `tampered`. `chain.findings` are the
  verifier's sentences (`{ "level", "text", "seq": [first, last], "mark" }`); a caller other than the
  owner sees only the findings that touch its own rows. At most 2,000 steps (`truncated` says so).
  Without `lane`: `{ "day", "timezone", "from", "to", "lanes": [ { "lane", "rows" } ] }`, the lanes with
  actions that day that the caller may replay.
  Each step also has `approvals`: who allowed a call that needed a person, `[ { "route": "approved" |
  "standing", "id", "found", "by", "at", "how", "level", "requires", "summary", "preview", ... } ]` (read
  from the approvals table; `found` is false when the approval is no longer on the node, and a shared view
  leaves `id` out), `after_untrusted` (the untrusted text the call followed, in words, or null) and
  `capture` (the screenshot the node kept after an agent's commit, `{ "trace", "sha256", "url", "title",
  "bytes", "format" }`, or null; its image is the step's `screenshot` when the call has no run screenshot).
- `GET /api/audit/runs/{id}/screenshot` -> the PNG of a run's proof (or failure) screenshot.
- `GET /api/audit/captures/{id}` -> the screenshot a `screenshot_kept` row (`id`) recorded, under the same
  scope as the replay (404 for another grant family's call, or once retention removed the file).
- `GET /api/audit/verify?day=&lane=&tz=` (owner) -> the verify report:
  `{ "status": "ok|pending|tampered|unverifiable", "checked", "rows": { "first", "last", "in_scope" },
  "proven_through", "chain", "keys": [ { "kid", "pub", "from_mark" } ], "unchained", "findings" }`.
- `POST /api/audit/shares` (the owner in person) `{ "trace": "tr_..." }` or `{ "lane", "day" }`, with optional
  `"ttl": "24h"` (default 24 h, at most 7 d) and `"screenshots": false` -> `{ "id", "token", "url", "api_url",
  "kind", "target", "expires_at", "screenshots" }`. The token is shown once. `url` is the link to hand out:
  the panel's read-only page, `<public_base_url>/shared#token=<token>` (no sign-in, no panel chrome; the
  token stays in the fragment, so it never reaches a server log); the page reads `api_url`, the route below.
- `GET /api/audit/shares` (owner) -> `[ { "id", "kind", "target", "state": "active|expired|revoked",
  "views", "last_view_at", "expires_at", ... } ]`; `DELETE /api/audit/shares/{id}` revokes.
- `GET /api/audit/shared/{token}` (no sign-in) -> `{ "share": { "id", "kind", "expires_at" }, ...the
  replay above }` for a lane-day, or `{ "share", "trace", "lane", "step", "chain" }` for one trace.
  Grant ids are left out (the label stays); screenshots only when the share allows them, at
  `/api/audit/shared/{token}/runs/{id}/screenshot`. Each view is written to the action log as kind
  `share_view` and counted on the share.

### Flows and schedules
- `GET /api/flows` -> `[ { "id": "biz-a.instagram.post", "lane", "version",
  "params": {...schema}, "effect", "last_run": {...} } ]`
- `POST /api/flows/{id}/run` `{ "params": {...} }` -> run
  The effect it is routed by is the flow's real one: the stronger of what it declares and what its
  steps can do (a step that types or clicks can publish, unless the owner marked it `publishes: false`).
  The run is pinned to the version that was checked, so a version saved in between does not run under it.
- `GET /api/ask-first` -> `[ Setting ]`, `POST /api/ask-first` `{ "scope": "flow" | "lane", "id",
  "ask_first": bool }` -> `Setting` (docs/PROTOCOL.md, "Ask me first"). Each flow in `GET /api/flows`
  carries `ask_first` and `ask_first_by`.
- `GET /api/lanes/{ws}/{name}/worker` -> `LaneWorker`: the browser worker on one lane and that lane's
  Ask me first. `PUT /api/lanes/{ws}/{name}/worker` `{ "on": bool }` -> `LaneWorker` turns the worker on
  or off there (`src/openajar/pilot/README.md`). `PUT /api/lanes/{ws}/{name}/worker/ask-first`
  `{ "ask_first": bool }` -> `LaneWorker` is the person's choice: `true` is "Asks you first", `false` is
  "Runs on its own". `true` takes any caller whose grant reaches the lane, a token included, as
  `POST /api/ask-first` does; `false` and the other two are for a person signed in. A lane that does not
  exist is 404.

LaneWorker:
```json
{ "lane": "biz-a/social",
  "worker": { "plugin": "browser-worker", "plugin_status": "running", "on": true, "problem": null },
  "ask_first": { "choice": null, "asks": false, "written_words_ask": true, "after_a_page_asks": true,
                 "set_by": null, "by_person": null, "set_at": null } }
```
- `worker.on`: `false` when a person turned it off here, or `problem` says the plugin state cannot be
  read. It says nothing about the node: `plugin_status` is `openajar plugins`' status, and only
  `running` means a run can start at all.
- `ask_first.choice`: `"asks_first"`, `"runs_on_its_own"`, or `null` when nobody chose yet.
- `ask_first.asks`: a publish on this lane asks because of a switch now (the lane's, a connector surface
  that posts through it, or the node's default for a lane nobody set).
- `ask_first.written_words_ask`: words the worker's `write` job wrote wait for a person. `false` only
  after a person chose "Runs on its own".
- `ask_first.after_a_page_asks`: `commit` is in the node's `[taint] downgrade`, so a publish that
  follows a page still asks, whatever `choice` says. The worker always reads a page first, so while
  this is `true`, "Runs on its own" does not make its publishes go alone. Show it.

- `GET /api/schedules` (the owner sees all; a grant sees its own) | `GET /api/schedules/{id}` |
  `POST /api/schedules` `{ "flow", "cron"|"watch", "params", "deadman_minutes"? }` (owner; it runs under
  the caller's grant: an admin grant's schedule is bound to that grant, and one under the owner's own grant
  needs a person) | `PATCH /api/schedules/{id}` `{ "enabled": false }` (owner; pauses or resumes it, and
  keeps the grant it runs under; turning on one under the owner's grant needs a person) |
  `DELETE /api/schedules/{id}` (owner). A schedule whose grant was revoked, has expired or is unknown is
  turned off at the next tick, with `state.stopped` saying why

Schedule:
```json
{ "id": "sch_4a1", "flow": "biz-a.instagram.post", "kind": "cron", "lane": "biz-a/instagram",
  "cron": "0 9 * * 1-5", "watch": null, "params": {}, "deadman_minutes": 90, "enabled": true,
  "next_fire_at": "2026-11-05T09:00:00+02:00", "next_run": "2026-11-05T07:00:00+00:00",
  "last_run": { "id": "run_9", "status": "succeeded_verified" }, "grant_id": "owner", "webhook": null }
```
`next_fire_at` is the next slot in the node's timezone (null when paused, or for a watch or webhook).
`last_run` is the latest run the scheduler started for it, shaped as in `/api/runs` (null before the
first). `grant_id` is the grant every fire runs under; pausing and resuming keep it. `next_run` is the
same instant in UTC.

### Agenda

Planned one-off actions per workspace, their approval and their proof (docs/AGENDA.md). Owner
routes (the owner or an admin grant), mounted from `openajar.agenda.api`.

- `GET /api/agenda?workspace=&start=&end=&lane=&kind=&state=&managed=` -> the timeline for `[start, end)`
  (ISO 8601; default: from a day ago for 8 days; at most 93 days): `{ "start", "end", "workspace",
  "items": [...], "truncated", "counts": { "<state>": n }, "managed_by": { "keepalive": {...} } }`.
  `kind` is `flow`, `connector`, `route` or `keepalive`; `state` is a
  comma list of entry states, `projected` and `planned`. Every item has `type` (`entry`, `projected`,
  `run` or `managed`), `id`, `at`, `workspace`, `lane`, `kind`, `state` and `title`. Without
  `workspace`, every workspace on the node. `managed=0` leaves out what other services plan on their
  own (below).
- `POST /api/agenda` `{ "title", "action", "when"?, "notes"?, "propose"? }` -> 201 entry. `action` is
  `{ "flow", "params"?, "on"?, "version"? }`, `{ "connector", "params"?, "on" }` or `{ "trigger", "on"? }`;
  `when` is `{ "at" }`, `{ "not_before"?, "not_after"? }` or `{}` (as soon as approved).
- `GET /api/agenda/{id}` -> entry. `PATCH /api/agenda/{id}` `{ "title"?, "notes"?, "action"?, "when"? }` ->
  entry: a new action or time sends an approved or waiting entry back to `proposed`.
- `POST /api/agenda/{id}/propose` -> entry (`scheduled` when nothing asks, else `proposed` with
  `approval.id`, a normal approval in Needs you).
- `POST /api/agenda/{id}/approve` `{ "note"? }` -> entry. The same decision as
  `POST /api/approvals/{id}/approve`; a draft is proposed first. A token never approves what its own
  grant family proposed (403 `denied`, self_approval). 409 `agenda_state` when it is not waiting.
- `POST /api/agenda/{id}/cancel` `{ "reason"? }` -> entry: the waiting approval is withdrawn and an
  unfinished run cancelled.

Entry (`type: "entry"`):
```json
{ "id": "ag_1f2e3d4c5b6a", "type": "entry", "workspace": "biz-a", "title": "Send the November invoice reminder",
  "notes": null, "state": "scheduled", "reason": null, "kind": "flow", "lane": "biz-a/billing", "effect": "commit",
  "action": { "kind": "flow", "target": "biz-a.billing.remind", "version": "f3a9c1", "inputs": { "month": "2026-11" },
              "on": null, "lane": "biz-a/billing", "effect": "commit" },
  "when": { "kind": "at", "not_before": "2026-12-01T07:00:00+00:00", "not_after": "2026-12-01T07:30:00+00:00",
            "words": "at 2026-12-01T07:00+00:00" },
  "at": "2026-12-01T07:00:00+00:00",
  "approval": { "id": "ap_7c1d", "via": "person", "state": "approved", "by": "owner", "hash": "sha256:...",
                "expires_at": "2026-12-01T07:30:00+00:00", "capped": false,
                "approver": { "kind": "owner", "id": "owner", "grant_id": "owner" } },
  "approved": true, "run_id": null, "event_id": null, "retry_at": null, "proof": null,
  "drafted_by": { "kind": "agent", "id": "claude-code" }, "grant_id": "g_51b", "authority": null,
  "next": "scheduled: runs at ..." }
```
`state` is one of `draft`, `proposed`, `scheduled`, `running`, `done`, `failed`, `skipped`, `expired`,
`cancelled`; `reason` says why in words. `proof` (once finished) is what the run left: `{ "run_id",
"status", "summary", "readbacks": [{ "passed", "expected", "found" }], "url", "screenshot" }`, or for a
trigger `{ "event_id", "deliveries": [...] }`; a `note` says when the run recorded no readback, page or
output. A projected item (`type: "projected"`) is a recurring schedule's upcoming fire: `schedule_id`,
`cron`, `timezone`, and `paced` when the lane's pace will move it. A run item (`type: "run"`) is a run
that already happened (not an entry's own), with `status` and `proof`. While an entry is `proposed`,
`approval.expires_at` is when its request in Needs you closes (the entry's start plus 30 minutes, at
least a day and at most `agenda_approval_max_days` after proposing) and `approval.capped` whether the cap
set it. Once a person approved it, `approval.approver` names them (`kind` owner or member, `id`, and
the `grant_id` they decided with). `authority` is set when the drafter's grant had expired and the run
went under that approval instead: `{ "grant_id": the one-run grant, "approver", "hash", "at", "why",
"spent" }` (docs/AGENDA.md, "Whose authority a run carries"); otherwise null. An entry whose drafter's
grant was revoked is `cancelled`, with the revocation as its `reason`.

A managed item (`type: "managed"`, `state: "planned"`) is work another service plans on a lane on its
own, read only (no approve or cancel; `manage` names the command that manages it). `source` and
`managed_by` say which: `keepalive` (the next visit per lane, `approx: true`, with `waits` when a gate
holds it for now). `managed_by.<source>` says what that service can show: `available`, a `note` (its
horizon, or why nothing is shown) and `skipped` (lanes it does not visit, with why).
```json
{ "id": "keepalive:biz-a/instagram@2026-11-09T13:42:07+00:00", "type": "managed", "source": "keepalive",
  "managed_by": "keepalive", "kind": "keepalive", "state": "planned", "at": "2026-11-09T13:42:07+00:00",
  "approx": true, "workspace": "biz-a", "lane": "biz-a/instagram", "site": "Instagram",
  "title": "Keepalive visit to Instagram", "effect": "read", "due_now": false, "timezone": "Europe/Vilnius",
  "manage": "openajar lane keepalive biz-a/instagram" }
```

### Nodes
`GET /api/nodes` -> `[ { "id", "state": "up|degraded|down", "capabilities": [...],
"cpu", "mem": { "used_mb", "total_mb" }, "warm_lanes": 2, "max_warm_lanes": 3,
"last_seen", "timezone" } ]`

### Brains
- `GET /api/brains` -> `[ { "name": "claude-max", "kind": "subscription|local|metered",
  "provider": "anthropic", "state": "ready|cooling_down|needs_auth|down",
  "seats": { "in_use": 1, "total": 2 }, "window_used_pct": 40 } ]`
  A chat-completions brain is `kind: metered`, `provider: openai-compatible`, `runner: chat`.
- `POST /api/brains/order` `{ "order": ["claude-max", "ollama-local", "anthropic-api"] }`
- `GET /api/brains/tasks` | `GET /api/brains/tasks/{id}` (transcript tail, status). A task carries
  `started_at`, `finished_at`, `summary` (its prompt preview) and `lane` (none in the list); the detail
  carries `tail` and `transcript_tail`, the same lines.

### Mail (docs/EMAIL.md)
A mailbox id goes in a path as `biz-a~mail` for `biz-a/mail`. Owner only.
- `POST /api/mail/discover` `{ "address" }` -> `{ address, imap, smtp, source, trust, provider, needs_app_password,
  help_url, words, notes, needs_yes, digest, untrusted_fields }`, where a server is `{ host, port, security:
  tls|starttls, username, typed }`. `digest` stands for exactly the hosts, ports and TLS shown. Nothing secret is sent
  anywhere, and only the domain leaves the node.
- `POST /api/mail/connect` `{ "id", "address", "password", "digest"?, "imap"?, "smtp"?, "confirmed"? }` -> the mailbox.
  Node only: the owner signed in to this node's own panel, with the CSRF header. A token is refused (`denied`), the
  node token included, so a controller has no way in. It uses the hosts `digest` stands for and looks nothing up, and
  anything else is refused (`mail_confirm_hosts`, as it is when only DNS named the servers and `confirmed` is not true).
  `imap` and `smtp` are what a person typed, may be on a private address, and both typed need no `digest`. Both logins
  come first, and a failure saves nothing (`mail_connect_failed`). This is one of the two ways to change a mailbox that
  exists, and `openajar mail connect` is the other.
- `GET /api/mail/requests` -> `{ requests: [ { id, address, mailbox } ] }`: what agents asked to connect, by the id of
  each card. In person.
- `POST /api/mail/requests/{id}/link` -> `{ url, path, tunnel, expires_at, found }`: the link for one open card. In
  person, and the only way to one: an agent gets none, and a card holds none.
- `GET /api/mail` -> `[ { id, address, provider, imap, smtp, health: { ok, words, at, unreadable? } | null } ]`, never
  a password. `unreadable` counts the messages a poll moved past because they could not be read.
- `DELETE /api/mail/{id}` -> `{ removed }`: the mailbox, its password and where its poll stood. In person.
- `GET /api/mail/{id}/messages` `?unread=1&since=2026-11-05&limit=20` -> `{ mailbox, messages }`. In person: an agent
  uses `mail_list`, which labels what it read.
- `GET` and `POST /api/mail/form/{token}`: the page for one card, for one mailbox, 15 minutes, 5 wrong passwords. Its
  token is the credential and it needs no sign-in. It answers only over https or from this computer (the SSH tunnel),
  loads nothing, and never writes a password back into a page. It adds a mailbox and never changes one (`mail_id_taken`),
  and the `POST` carries the digest of the servers the page showed.

### Vault (names only)
- `GET /api/vault` -> `[ { "id", "name", "kind": "password|totp|cookie|api_key",
  "domain", "lanes": [...], "created_at", "last_used" } ]`
- `POST /api/vault` `{ "name", "kind", "domain", "lanes", "username"?, "secret", "totp"? }` (`totp`: a
  password entry's 2FA seed or otpauth:// URI, kept on the same entry so `login` types its codes)
  (used by the Mac app after Touch ID; the secret is write-only)
- `DELETE /api/vault/{id}`

### Grants and tokens
- `GET /api/grants` -> issued grants (subject, scope, expiry, revoked), each with `explain`: the grant in
  plain sentences, most important first (where it reaches, what it may do, what waits for a person,
  budgets and expiry), the same lines `openajar grants explain <id>` prints
  (`openajar.policy.explain`). A revoked or expired grant has one sentence saying it allows nothing now.
  Scoped like the list itself: a member sees only their own agents' grants.
- `POST /api/grants` `{ "subject", "workspaces", "lanes"?, "ceiling", "specials",
  "budget"?, "ttl" }` -> `{ "id", "token" }` (token shown once). `specials` names any of
  `secret`, `exec`, `install_overlay`, `install_base`, `service`, `brain` and `memory`; `memory`
  reads and writes the grant's workspaces' memory, without private or owner-only entries
  (`openajar grants issue --special memory` sets the other fields); `memory_read` is the same
  special without write (memory_context, memory_search, memory_get), what `openajar connect` gives an
  agent by default
- `POST /api/grants/{id}/revoke` (cascades to children)
- `POST /api/grants/{id}/route` `{ "ssh_host", "local_port" }` (the owner): how the computer that holds this
  grant reaches the node. `openajar connect` records it for the grant it issued or kept, and `browser_view` then
  gives that grant (and those narrowed from it) a link on that local port and a tunnel command for that SSH
  host, instead of this node's own port and hostname. Kept in `OPENAJAR_HOME/connect-routes.json`.

### Site APIs (`/api/site-apis`, docs/SITE-APIS.md)

Making and looking after a site API. Who counts as a person is decided as for workflows: the owner or a
member signed in to the panel. A bearer token never does, even the owner's.

- `GET /api/site-apis` -> `{apis: [{api, state, title, domain, version, site, acts_as, surfaces, by_person,
  author, digest, edited, installed, proposal, updated_at, endpoints: {name: {title, effect,
  declared_effect, cache_s, tool}}}]}`. `state` is `draft`, `proposed`, `active` or `paused`. `effect` is
  what the steps can do: an agent's input step shows as `commit` until a person vouches. `edited`: the
  draft has moved on from what is live.
- `POST /api/site-apis` saves a draft, and takes one of a definition (or `{definition}`), `{api, url,
  candidates, surfaces, title}` (what `discover` returned) or `{api, endpoint, recording, candidate,
  surfaces, vouch}` (a draft from `flow_record_stop`, with `vouch` when a person says its input steps
  publish nothing). 201 with the row, plus `problems` (what stops it compiling) and `dropped` (steps
  that typed into a sensitive field). `pay` and `irreversible` are refused here. The patch version goes up
  when the content changed.
- `POST /api/site-apis/discover` `{on, url}` -> `{on, signal, url, candidates, truncated, note}`, with
  `candidates: [{name, kind, items, pick, fields, sample: {untrusted: true, rows}, count, confidence}]`.
  `on` is a lane or surface. On a challenge `signal` says which and `candidates` is empty. With a `url`,
  `json: [{name, kind, path, shape, untrusted: true, url, items, count, fields}]` lists what the page's own
  code fetched as JSON, by URL path and keys and types only (docs/SITE-APIS.md); the last four only where
  it found a list of records.
- `GET /api/site-apis/{id}` is the row with `definition`. `PATCH` merges fields into the definition and
  saves it, and nothing live moves until `activate`.
- `POST /api/site-apis/{id}/activate` `{vouch}`: only the owner, signed in. 403 while an agent's input
  steps wait for a vouch. Also wakes a paused API.
- `POST /api/site-apis/{id}/pause`: a person. The API answers `paused` and its queued runs wait.
- `POST /api/site-apis/{id}/try` `{endpoint, params, on}` -> `{run, status, effect, data, untrusted_fields,
  private_fields, truncated, problems}`. Only `read`, or `draft` for a person. A commit step never runs.
  It counts against the endpoint's pace. A paused API answers `paused` unless a person is trying it.
- `DELETE /api/site-apis/{id}`: a person. Refused while a schedule or workflow uses it. Runs stay in
  history.

`openajar site-api discover|new|try|activate|pause|list|show|remove` does the same at the node;
`discover`, `new --url` and `try` go through agentosd, which owns the lanes' browsers.

### Site APIs over HTTP (`/v1/sites`, docs/SITE-APIS.md)

The developer surface: the stable part of a site API, for code that is not a panel. `GET /v1/sites`,
`GET /v1/sites/{api}/openapi.json`, `GET` and `POST /v1/sites/{api}/{endpoint}`, `GET
/v1/sites/{api}/runs/{run}`. docs/SITE-APIS.md is the contract, with the answers and the error table;
this is what it leaves to the node.

**Auth.** `Authorization: Bearer aos_...`, or the owner's panel session with `X-Agentos-CSRF` on every
method, GET too (the panel's try box). Nothing else opens it. A key opens nothing else: any other route
of the panel app answers it 401, the public pages too, and `/mcp` does as well. A key in a query string
is refused with 401 even beside a good header, and should be replaced, since it was in a URL. The node
writes no access log, and a key's shape is scrubbed from every log line and trace.

**A call.** A key's call runs as an agent's tool call under the key's backing grant, through the same
function as the MCP tool: binding, health, pace, Ask me first, approvals, meters and taint all apply. A
read's labels go on the key's grant family, so a commit by the same key after a read that holds
untrusted fields waits for a person (202 `approval_pending`). Keep reading and committing on separate
keys if that is not what you want. Activity shows the call as `site_api_call`, with the key's label and
prefix.

**Controls** are query names or headers: `on` or `Agentos-On`, `wait` or `Agentos-Wait`, `max_age` or
`Agentos-Max-Age`, `stale_ok` or `Agentos-Stale-Ok`. A query name the input schema does not have is 400,
so made-up params cannot spend a run. Query values follow the input schema: integers, numbers,
`true`/`false`/`1`/`0`, repeated names for arrays, JSON for objects. Saving a definition refuses an
input param named like a control.

**Caching.** Auth, the key's limit, scope, a pause, the grant and a freeze are all checked before the
cache is read, on every call. A hit still labels the caller's family and writes Activity; it spends no
pace and no meter. Results go out with `Cache-Control: private, no-cache`, or `no-store` when the
endpoint has a private field, so nothing downstream can answer without coming back through the key
check; `If-None-Match` then costs no body.

**A hit on a held lane answers.** A hold keeps work off the lane so that a challenged account is not
retried on. A hit does nothing on the lane, and the value was read in good standing inside the
`cache_s` the owner chose. Past that, a call gets `needs_human` or `rate_limited`, and `stale_ok=1` is
how a caller asks for the last good value anyway, up to 7 days old. A freeze is different: a person said
stop, so a hit stops too.

**Waiting.** A call is held open for at most `wait` seconds, and at most 8 calls of one key (64 on the
node) are held at once: past that a call answers 202 at once and the run goes on. A client that goes
away cancels nothing. A commit's `Idempotency-Key` is kept under the key's own id, and a repeat returns
the first run; on a read it is ignored, because a repeat of a read is what the cache is for. A read or a
draft that a hold put back in the queue is cancelled and answers the hold; a commit stays queued and
answers 202, because it may still post.

**Limits.** 256 KB of body, in 10 seconds. A key's `rate_per_min` is a sliding minute, and a refused
call costs nothing. 20 bad keys a minute from one peer address refuses that address for a while,
good keys included, because a right guess must not get through; behind a proxy every caller shares that
address.

**Keys.** `POST /api/site-keys` `{label, apis, endpoints?, surfaces?, ceiling?, rate_per_min?,
expires_at?}` -> `201 {key, secret}`, the only time the secret exists. `endpoints` are
`<api>.<endpoint>` (a bare name when the key has one API). `ceiling` is `read`, `draft` or `commit`.
With no `surfaces` a key covers what its APIs are bound to now, not what is bound later. `GET
/api/site-keys` -> `{keys}`, each with `state`: `active`, `revoked`, `expired`, or `grant_ended` when
its grant ended some other way. `PATCH /api/site-keys/{id}` takes the same fields; a change of scope or
of the end is a new grant, and a run still queued under an old one stops. `DELETE
/api/site-keys/{id}` revokes the key and every grant of it. A freeze that holds all of a key's lanes (a node
freeze holds every key) revokes its grant like any other: the key answers 503 `frozen` during the
freeze and 401 after it, because unfreezing restores no grant, so make a new key. A key that also
reaches lanes outside a lane or workspace freeze keeps its grant: its calls in the frozen scope
answer `frozen` until the unfreeze, and its other lanes keep working. `openajar site-api keys add|list|revoke` does the same
on the node. A key with an end gets a grant that outlives it. A key with no end outlives its grant: a
call in the old one's last 30 days gets it a new grant, the old one stays live until its end so that
what is queued or parked under it finishes, and the new one starts with the labels of the old. A key
unused past that end gets a new grant on its next call, so it shows as `active`; a grant a person or a
freeze ended is never replaced.

**Usage.** `GET /api/site-apis/{api}/usage?hours=48&endpoint=` -> `{api, endpoint, hours, points,
totals}`, one point per hour with no gaps, ready to plot: `{hour, calls, cached, fresh, errors: {code:
n}, p50_ms, p95_ms}`. `hours` is 1 to 720. A call without a real API and endpoint behind it, and a run
poll, write no row.

`openajar site-api openapi <api> [--server URL] [--out FILE]` prints the document with every endpoint
and surface; the route gives each key its own.

### Meters, queues and taint
Today's use against daily and hourly limits, per grant and per lane (`openajar.policy.meters`,
ROUTING.md step 5). A day is the owner's calendar day in the node's time zone (`[meters] timezone` in
policy.toml, else the host's): it resets at local midnight, so a DST day has 23 or 25 hours, and where
the clocks jump over midnight the day starts at the first instant after the jump. A zone change keeps
the current day's counters until that day ends. Hours are UTC clock hours, so up to twice an hourly
limit can run across the turn of an hour. Limits: `usd_per_day`, `tokens_per_day`, `commits_per_hour`,
`commits_per_day`. A commit is a call authorized as commit, pay or irreversible (flow and connector runs
at their effective effect), and a `browser_act` click whose publish request the lane's egress guard
matched to a commit rule (past a commit limit that request is aborted before it leaves). A call that
names no lane counts against the lane its grant implies when the grant reaches a single lane; otherwise
it is grant-metered only. A brain attempt's tokens and metered dollars are charged to the task's grant
when it ends. What a person does in person in the panel is not metered. A grant's come from its budget (`POST /api/grants` `budget`); a lane's
from the owner (below, or `openajar grants meters --lane <id> --limit commits_per_day=6`), else the first
`[meters.lanes]` pattern in policy.toml that matches it.

- `GET /api/meters` -> `{ "timezone", "day", "resets_at", "warn_at": [0.8], "grants": [Meter], "lanes": [Meter] }`.
  The owner sees every live agent grant and every lane; a member the lanes they can see and their own
  agents; a grant itself and its lanes.
- `GET /api/lanes/{ws}/{name}/meter` -> Meter (anyone who can see the lane)
- `PUT /api/lanes/{ws}/{name}/meter` `{ "limits": { "commits_per_day": 6, ... } | null }` -> Meter: the owner
  only. `{}` means no limits on this lane (it also overrides a policy.toml pattern); `null` removes the
  lane's own limits. Traced as `lane.limits` and `meter_limits`.
- `GET /api/lanes/{ws}/{name}/pace` -> LanePace (anyone who can see the lane): the lane's posting pace
  (`openajar.policy.pace`, AUD-13). `{ "lane", "pace": { "commits_per_day", "min_gap_minutes",
  "active_hours": ["08:00-22:00", "sat,sun 10:00-23:00"], "timezone" } | null, "paced", "zone": { "timezone",
  "source": "lane|pin|workspace|node", "source_words", "exit_timezone", "unreadable", "note" }, "local_now",
  "workspace_timezone", "preset", "set", "posts_today", "last_post_at", "may_post_now", "blocked_by":
  "active_hours|min_gap|commits_per_day|timezone" | null, "next_allowed_at" (UTC), "next_allowed_local",
  "explain" }`. The zone is the lane's own `timezone`, else its pinned fingerprint's (its proxy's region, what
  sites see), else the workspace default, else the node's. `exit_timezone` and `note` say when the lane check
  saw the exit somewhere else (the pin still decides). A pin that cannot be read keeps the last zone read for
  the lane; with none, `unreadable` is true and a paced publish is refused (`blocked_by` `timezone`).
- `PUT /api/lanes/{ws}/{name}/pace` `{ "pace": {...} | null }` or `{ "preset": true }` -> LanePace: the owner
  only. `null` removes the pace (the lane is not paced); the preset is 08:00-22:00 lane time, 45 minutes
  apart, 3 a day. Traced as `lane.pace` and `lane_pace`. A publish outside the pace is refused with
  `lane_paced` whatever is approved; a run moves to the next allowed time.
- `GET`, `PUT /api/pace/workspaces/{ws}` `{ "timezone": "America/New_York" | null }` -> `{ "workspace",
  "timezone" }`: the owner only; the zone of the workspace's lanes that have no pin and no zone of their own.
- `GET /api/lanes/{ws}/{name}/queue` -> `{ "lane", "claim", "queue_length", "queue": [{ "position", "mode",
  "holder", "kind", "ref", "waited_s", "estimate_s", "kept_out_by_claim" }], "inside": [{ "mode", "holder",
  "for_s" }], "call_estimate_s" }`: who holds the lane, who is on it now and who waits; `yours` marks the
  caller's own claim or place.
- `GET /api/lanes/{ws}/{name}/taint` -> `{ "lane", "tainted", "families": [{ "grant_id", "grants": [{ "id",
  "label" }], "untrusted", "since", "follows": [{ "kind", "host", "last" }], "after_untrusted", "labels", ... }],
  "waits_for_a_person": [effects] }`: the grant families that reach the lane and what untrusted text they
  read (taint lives on a grant family, not on a lane), its own context and every turn token's in it. The
  owner sees every family, with `grant_id` and `grants`; anyone else only their own (a grant its family, a
  member their own agents), without them.
- `POST /api/grants/{id}/taint/clear` `{ "confirm": "clear" }` -> `{ "grant_id", "family", "origins_cleared" }`:
  the owner signed in to the panel, in person, starts the grant's family clean, turns included (as
  `openajar grants taint <id> --clear`). It is a security decision, so it always needs a fresh in-person
  step, members or not: the typed confirmation (400 without it), and a passkey check within the step-up
  window when the owner has a passkey (`step_up_required`), else a panel sign-in with a new one-time code
  within that window (403 says to sign in again). A token, even the owner's or an admin grant, and a
  device credential from `openajar connect` never clear taint (403). Traced as `taint.clear` and
  `taint_cleared`.

Meter:
```json
{ "scope": "lane", "id": "biz-a/instagram", "label": "biz-a/instagram", "state": "ok|warning|cut_off",
  "explain": "Lane biz-a/instagram reached its limit of 6 commits a day (6 used today), so posting, paying and irreversible steps are paused until it resets at 00:00 (Europe/Vilnius), in 9 h 12 min.",
  "usage": { "usd": 0.42, "tokens": 18000, "commits_today": 6, "commits_this_hour": 2,
             "actions": { "read": 30, "commit": 6 }, "actions_today": 36 },
  "limits": [{ "name": "commits_per_day", "value": 6, "used": 6, "left": 0, "fraction": 1.0,
               "unit": "commits", "period": "day", "resets_at": "...", "set_on": null,
               "state": "cut_off", "explain": "..." }],
  "limits_from": "owner|policy.toml biz-a/*|null", "day": "2026-11-04", "timezone": "Europe/Vilnius",
  "warn_at": [0.8], "resets_at": "...", "hour_resets_at": "..." }
```
A grant's meter adds `subject` and shows every limit of its chain under the block that set it: `set_on`
is `null` for the grant's own, else the parent block's id, with `scope_note` saying that limit counts
what every grant narrowed from that block used (its `used` is the block's, not this grant's) and an
`explain` that names that block. `state` is the worst of its limits: `warning` from the first `warn_at`
fraction, `cut_off` at the limit. A call refused by a meter is `denied` with reason `limit_reached` and
the same sentence. Each threshold sends one `meter` trace, one `meter` event and one push per limit and
window (the day, or the hour of an hourly limit), to the node owner and the member the grant belongs to
only; a call a limit refuses, and a lane limit set below today's use, send the cutoff too. Agents see
their own meters in `grant_info` (`meters`).

### Settings
- `GET /api/proxies` (public fields only) | `POST /api/proxies` (credentials write-only). A new label is
  open to the owner's token; a label that exists, with any field changed (host, port, username or
  password), needs a person: it is the exit of every account on it
- Browser devices (docs/PROXIES.md, AUD-02; owner only): `GET /api/browser-devices` -> `[ { "name",
  "proxy", "created_at", "from_lane", "note", "pinned", "pin": { "os", "screen", "window", "gpu",
  "timezone", "locale", "firefox", "pinned_at", "pinned_exit_ip", "pinned_country" } | null,
  "lanes": [ { "id", "state", "proxy", "pin_copy" } ], "workspaces" } ]` |
  `GET /api/browser-devices/{name}` (the same plus `health`: the lane check's proxy, exit IP and
  location checks for the device) | `POST /api/browser-devices` `{ "name", "proxy_label"?,
  "from_lane"?, "note"?, "confirm"? }` (`confirm`, which puts the device beside lanes that already leave
  through its proxy, needs a person) | `DELETE /api/browser-devices/{name}` (only when no lane uses it)
- `GET /api/lanes/{ws}/{name}/device-move?to=<device|none>` -> `{ "lane", "from", "to", "changes",
  "warnings", "confirm", "proxy": { "from", "to" } }`: what the move changes for the lane's site,
  before anything changes | `POST /api/lanes/{ws}/{name}/device` `{ "device": str | null,
  "confirm": bool }` -> `{ "plan", "lane" }`. A move the lane's site would notice (it has launched,
  so a new fingerprint or a new IP) without `confirm` answers 409 `confirm_required` and changes
  nothing. The move needs a person (the panel, or `openajar lane set-device` on the node).
- `GET /api/settings/notifications` | `PUT` | `POST /api/settings/notifications/test`

### Live updates
`GET /api/events` (Server-Sent Events). Every event has an `id:`, an `event:` name and one JSON
`data:` line, in the same object shape as the matching resource:
- `lane` (state change), `control` (`{ "lane", "control" }`), `approval` (created, decided), `question`
  (asked, answered, declined, expired), `run` (status), `trace` (new event), `node` (health),
  `schedule` (created, changed; `deleted: true` with its last shape when removed), `brain` (state,
  seats), `task` (status, tail), `code` (a code card opened or closed, never the code), `login` (an
  assisted login moved: `{ "id", "lane", "state", "version", "step" }`, never a value or page text), `meter`
  (a warning, a cutoff or new lane limits; `{}` for a member or a grant, who refetch `GET /api/meters`), `freeze` (a
  freeze made or lifted: `{ "state": "frozen" | "unfrozen", "scope", "target" }`, so read `GET /api/freeze` again), `pointer`
  (where the agent acts on a screen: `{ "lane", "x", "y", "w", "h", "kind" }` in the screen's pixels, or
  `{ "lane", "hide" }`; sent only while a live view of that lane is open through `/api/lanes/{ws}/{name}/vnc`,
  with no `id:`, never replayed, and only to the owner and members who may watch that screen), and `resync`
  (events were missed: refetch what you show).
- A comment, `: ping`, whenever the wire has been quiet for `sse_heartbeat` seconds (15 by default),
  so a proxy keeps the stream open. Events the caller may not see do not count as traffic.
- `retry: 3000` on connect. Reconnect with `Last-Event-ID` (the header, or `?last_event_id=`): the
  events after it are replayed from the node's last 500. If they are gone, or the id is ahead of this
  node (it restarted), the stream begins with one `resync`; that frame carries no `id:`.
- What a caller hears is limited by its scope (see "Who may call what"). `brain`, `task` and `code` are
  the owner's.

The bus is the runtime service `events`, registered first on the node. Approvals, questions and
traces come from the policy service, run status from the runner, lane control changes from the lane
manager. Lane states are polled every 2 seconds (`lane_pump_seconds`). Schedules are published when a
route changes them, and polled every 5 seconds (`schedule_pump_seconds`) for what the scheduler changes
on its own (a fire, a dead-man pause). Brain tasks are polled every 5 seconds (`brain_pump_seconds`),
brains every 30 seconds (each one is probed). Each change is published once.
