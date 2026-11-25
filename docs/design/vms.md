# Disposable cloud VMs as lanes

Status: phase 1 built (openajar.vms), not yet run against a real Hetzner project.

## The story

"In T3 (or Claude Code), I say: test this on a fresh VM." The agent calls `vm_create`, gets a lane,
runs the work with `lane_exec` or a shell, copies out what it needs, and calls `vm_destroy`. If it
forgets, crashes, or the laptop sleeps, the node deletes the VM anyway. Nothing bills for long.

## Why the node does it, not a Hetzner MCP in the agent

- **The token stays on the node.** A Hetzner token is per project and either read or read-write.
  In an agent it could delete every server in the project (including the node itself) or make fifty.
  The node exposes only "a VM of a profile the owner set up, for at most this long" and "delete a VM
  this node made".
- **The reaper outlives the agent.** Deletion must not depend on the session that asked.
- **One policy.** The `vm` effect goes through grants, approvals, standing approvals, freezes,
  taint and the audit log like everything else.
- **Every agent gets it.** Claude Code, Codex, T3, Hermes, the node's brain.
- **Keys never reach the model.** SSH keys are per VM, made and kept on the node.

## Shape

A VM is a **terminal lane whose commands run over SSH** (`EnvLane.remote`). The env manager picks
`SshLauncher` for such lanes, so `lane_exec`, `shell_*`, `lane_install` and service lanes work on it
unchanged. Nothing of openajar runs on the VM: no agentosd, no token, no vault. Dev work runs code
that openajar should not trust (install scripts, a PR's tests), so openajar stays off that box.

The other option, a VM that joins the controller as a node, fits long-lived capacity (more browser
lanes, a company's lanes on their own IP) and already exists as node templates plus `node join`.
Both share the provider client and the reaper's rules; the agent only ever sees a lane id.

## Size: the cheapest that fits

Unless the owner says what they need, the node rents the cheapest server type that fits. A profile
in vms.toml is a set of minimums (`min_cpu`, `min_memory_gb`, `min_disk_gb`, `arch`, `cpu_type`);
`small` has none, so it is the cheapest x86 type of the day. At create time the node reads the
provider's types and prices (cached 30 minutes), keeps those orderable in the location now (and
matching a snapshot's architecture and disk), and takes the cheapest. Sold out: the next two
cheapest, never more than twice the price, all named in the approval. A profile with
`server_type` pins one type, no picking.

An agent can state its own size on `vm_create`: `cpu`, `memory_gb`, `disk_gb`, `arch` (minimums,
picked again) or `server_type` (pinned). Its tool text tells it to leave the size alone unless the
owner asked or the task needs it. Such a VM's profile is `custom`, which a grant limited to
profiles refuses. `openajar vm types` lists what can be ordered and what each profile rents today.

## Creating one

1. Policy: `vm` effect on the lane, with the summary the node writes (server type and its specs,
   location, image, time limit, hourly price, the most it can cost, the sold-out fallbacks).
   Default routing: a person approves; a standing approval may cover it. A grant's `vm` cap can
   limit lanes (`dev/*`), profiles and repos.
2. Node limits (vms.toml, enforced whatever the grant says): `max_live`, `max_ttl_minutes`,
   `monthly_budget_eur` (counted as started hours times the hourly price).
3. Keys: an ed25519 client key and an ed25519 **host key** made on the node. The host key goes to
   the VM through cloud-init (`ssh_keys`), so the first connection is already pinned
   (StrictHostKeyChecking=yes, a one-line known_hosts under a HostKeyAlias).
4. Provider: a firewall per node (SSH and ping only from this node's public IP), a throwaway SSH key
   resource (deleted right after the server exists), the server with labels `agentos-node`,
   `agentos-vm`, `agentos-lane`.
5. Boot: wait for SSH with the pinned key (default up to 300 s), then register the VM lane.
   A VM that never answers is deleted and marked failed.

## Deleting one (the reaper, every minute)

- past its time limit;
- idle (no command, no open shell or service for `idle_minutes`) in the last
  `hour_margin_minutes` of a started hour: Hetzner bills by the started hour, so deleting earlier
  saves nothing, and the next task of the same agent can still use it;
- `vm_destroy`, `openajar vm destroy`;
- a server labelled with this node and no live record: an orphan, deleted (after 10 minutes'
  grace while a create is in flight);
- a record whose server is gone: marked deleted, its lane closed.

Deletes retry with backoff until the provider confirms. A VM still alive 10 minutes past its limit
sends one urgent notification. A daemon restart never deletes VMs; the reaper resumes.

## Private repos

`vm_clone(on, repo, ref, dest, depth)` puts a private repo on a VM without the token ever reaching
it. The owner stores a read-only token per repo or per owner with `openajar vm repo add
github.com/acme/*` (GitHub: a fine-grained token, only those repos, "Contents: read"). It is a
vault machine secret; listing shows patterns, never tokens.

The node clones into a private temp dir with the token in git's environment (GIT_ASKPASS), never in
the URL, argv or .git/config, then streams the checkout (with its .git) to the VM as a tar over the
VM's SSH and deletes its copy. `origin` on the VM is the plain https URL. Only https remotes on
public host names (no IPs, localhost, file://, ext:: or submodules), so an agent cannot point the
node at its own network. Limits: `clone_max_mb` (2048), `clone_timeout_s` (600).

Policy: the `vm` effect again, with the repo as a fact; a grant's `repos` (`--vm-repo
github.com/acme/*`) limits which. The approval says the code lands on a box with internet access.
A public repo needs none of this: the agent runs git clone on the VM.

## Images

`openajar vm image build dev` makes a snapshot (git, build tools, Python with uv, Node 22, Docker),
cleaned of keys and cloud-init state. A profile with `image = "snapshot:dev"` then boots ready
in under a minute instead of installing on every VM.

## Providers and what they can do

`provider` in vms.toml picks one: `hetzner` (the default), `docker` (local containers,
openajar.vms.docker), `morph` (Morph Cloud, openajar.vms.morph) or `own` (containers on the owner's
own machines over SSH, openajar.vms.own). Each declares `capabilities`, and
callers check a name (`openajar.vms.provider.capable`) before they use what it stands for:

| Capability | Meaning | Hetzner | Docker | Morph | Own machine |
|---|---|---|---|---|---|
| `firewall` | a provider firewall lets SSH in from the node only | yes | no (loopback) | no (its gateway) | no (the machine's loopback) |
| `snapshot` | a disk snapshot that new machines boot from | yes | yes (`docker commit`) | yes | yes (`docker commit` there) |
| `pause` | pause and resume with memory: processes keep running | no | no | yes | no |
| `branch` | copy a running machine, memory included (not used yet) | no | no | yes | no |
| `wake_on_http` | a paused machine wakes on an HTTP request (not used yet) | no | no | yes | no |
| `network_policy` | openajar can limit a machine's outbound traffic | yes | yes | no | yes |
| `cloud_init` | first-boot user data runs | yes | no | no | no |

Beside the capabilities a provider says what it is with plain attributes, so no caller asks for it by
name: `firewall` (False: no provider firewall to make), `closes_network` (it closes a limited or none
network itself, openajar.vms.netwall), `size_type(size)` (an environment's size is a server type of its own there, for containers),
`place(image)` (where a new machine goes when an image lives in one place: an own machine's saves),
and on a `Server` `via` (the SSH host it is reached through: a container on an own machine listens on
that machine's loopback). `openajar.vms.providers.FREE` names the providers that rent nothing, so
costs read "free" there.

### Your own machine

`openajar.vms.own` is the docker provider with every docker call run as `ssh <machine> docker ...`
(`MachineDocker`), behind `OwnProvider`, which holds every registered machine
(`vms/machines/<name>/`: a record, a key, a pinned host key). A server's id is `<machine>:<container
id>` and an image's `<machine>//<docker image>`, so each later call finds its machine. A machine that
does not answer raises from `list_servers` and `get_server`: the reaper and the liveness check read
"no answer" as nothing, never as "deleted". The user guide is docs/ENVIRONMENTS.md "Your own
machine"; what an environment there can and cannot reach is in docs/THREAT-MODEL.md.

Without `cloud_init` the node runs the environment's setup over SSH as soon as the machine answers
(openajar.environments.bringup), and the image a snapshot is built from needs none.

### Morph Cloud

Bring your own account: `openajar vm setup --provider morph` asks for a Morph API key (hidden),
checks it (`GET /instance`), keeps it in `$OPENAJAR_HOME/secrets/morph.token` (0600), pins the host
key of Morph's SSH gateway in `vms/morph_known_hosts` (ssh-keyscan, trust on first use; the
fingerprints are printed to compare) and sets `provider = "morph"`. `openajar setup` offers Morph
next to Hetzner and Docker. openajar calls Morph's REST API with httpx; the morphcloud package is not
a dependency.

- **Sizes.** On Morph the resources come with the snapshot a machine starts from. A start
  (`POST /instance`) restores the snapshot's memory and processes at the snapshot's size, in about a
  second; a boot (`POST /snapshot/{id}/boot`, `POST /image/{id}/boot`) is a cold start into the
  vCPU, memory and disk asked for. The server types are openajar's sizes: `morph-small` (2 vCPU,
  4 GB, 32 GB), `morph-medium` (4, 8, 64), `morph-large` (8, 16, 128), `morph-xlarge` (16, 32, 256).
  A machine starts warm when its snapshot has its size (the `dev` snapshot is built at the default
  size) and boots cold into any other. The cheapest that fits is picked as on Hetzner.
- **Prices**, in vms.toml, from Morph's pricing page (2026): a machine costs
  max(vCPU, memory GB / 4, disk GB / 16) MCUs an hour, `morph_usd_per_mcu = 0.05`; stored snapshots
  and paused machines are 5 TB-hours an MCU, `morph_storage_usd_per_gb_month = 0.0073`; and
  `morph_eur_per_usd = 0.86` turns them into EUR, so the node's EUR limits hold. They are estimates
  (`estimate: true` in the server types; a paused save says "an estimate"); set them to your plan
  and the day's rate. `morph-small` is about EUR 0.09 an hour.
- **SSH.** Through the gateway, as `<instance id>@ssh.cloud.morph.so`, with the instance's own key
  (`GET /instance/{id}/ssh/key`), written 0600 in place of openajar's own key, and the pinned gateway
  key under the VM's HostKeyAlias. Nothing secret is on a command line: Morph's SDK puts
  `<instance id>:<api key>` in the user name, which `ps` on the node would show, and openajar never
  does. An image build keeps the machine's SSH keys (Morph's gateway signs in with them).
- **Labels** are Morph metadata (`metadata[key]=value` filters lists), for the reaper's reconcile,
  the orphan sweep, `snapshot:<name>` images and saves.
- **The backstop.** Every instance has a Morph TTL (action `stop`): its time limit plus the save's
  grace plus an hour, moved along with each extend, so a machine the node forgot (a node that never
  came back) does not bill forever. A paused one's TTL is the save's life (`saved_days`) plus a day.
- **No firewall, no egress control.** The gateway is the only way in, and Morph's API has no
  outbound rules, so `network_policy` is off.

**Pause.** A paused Morph machine keeps its memory and bills only its storage; it resumes in
seconds with its processes running. An environment's save uses that (openajar.environments.pausing,
docs/ENVIRONMENTS.md "Keeping an environment"). The VM record turns `paused`: not live, so it counts
toward no limit and its hours stop; its keys move to `vms/paused/<vm id>/`, so a lane of the closed
environment reaches nothing; wake on SSH and HTTP are turned off first; and the reaper's orphan
check leaves a paused machine alone. Resuming makes a new VM record for the same instance.

**Not verified against a live account yet** (no key was available when it was built; built from
Morph's docs, its OpenAPI spec and its SDK). `scripts/morph_live_check.py` checks each of these
with a real key for a few cents and deletes what it made:

- that the instance's own key signs in as `<instance id>@ssh.cloud.morph.so` (the docs' sharing
  bundle says so; the key may also need the bundle's password, which openajar does not use);
- that `-D`, `-R` (documented by Morph) and `-L` to a Unix socket (the "vm" browser's live view)
  pass the gateway;
- that the gateway's host key is stable, and that `morphvm-minimal` has apt, systemd and python3
  for the image script;
- that pausing keeps a process running across a resume, that SSH to a paused machine does not
  wake it, and that a TTL runs out while a machine is paused;
- the prices, against the account's bill.

## Settings (`$OPENAJAR_HOME/vms.toml`)

```toml
location = "nbg1"
default_profile = "small"
max_live = 3
default_ttl_minutes = 60
max_ttl_minutes = 1440
idle_minutes = 15
hour_margin_minutes = 5
monthly_budget_eur = 50
ssh_from = []          # empty: this node's public IP, looked up

[profiles.small]          # no minimums: the cheapest x86 type of the day
image = "ubuntu-24.04"

[profiles.build]
min_cpu = 8
min_memory_gb = 16
image = "snapshot:dev"

[profiles.arm]
arch = "arm"
min_memory_gb = 8

[profiles.pinned]
server_type = "ccx23"     # exactly this type (`openajar vm types` lists them)
```

The token lives in `$OPENAJAR_HOME/secrets/hetzner.token` (0600), written by `openajar vm setup`. Use
a Hetzner project that holds nothing but these VMs: the token can delete any server in it.

On Morph:

```toml
provider = "morph"
morph_usd_per_mcu = 0.05                 # Morph's price of an MCU (cloud.morph.so/web/subscribe)
morph_storage_usd_per_gb_month = 0.0073  # stored snapshots and paused machines
morph_eur_per_usd = 0.86                 # the day's rate, for the EUR limits
morph_base_image = "morphvm-minimal"     # what a machine starts from with no snapshot
```

The key lives in `$OPENAJAR_HOME/secrets/morph.token` (0600), written by `openajar vm setup
--provider morph`. openajar rents only what it labels, but the key reaches every instance of the
account: use an account (or organization) for openajar alone.

## Not in phase 1

- Copying files in and out other than through commands (`lane_exec` with stdin, `git`): a
  `files_put`/`files_get` for VM lanes over SFTP.
- Reusing an idle VM for another task of the same grant family, and a warm pool.
- A panel tile and a doctor line for live VMs and the month's spend; the meters do not count VM
  spend yet (it is in EUR and has its own monthly budget).
- Other providers (Scaleway bills by the minute), and VMs as controller nodes for capacity.
- Morph's branch (copy a running machine) and wake on HTTP: declared as capabilities, not used.
- Killing a remote command when its ssh dies without a TTY (the VM is disposable, so for now the
  process lives until the VM goes).
