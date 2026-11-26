"""The steps of `openajar setup` (openajar.setup.cli): VMs, GitHub, limits, the local service and
the agents, the health check, a test VM, and the summary.

Everything that leaves this process (the Hetzner and GitHub APIs, gh, docker, the local service, a
test VM) goes through one small function here, so the tests replace exactly those. A token is read
from a hidden prompt or a file, checked, stored (the Hetzner token where `openajar vm setup` keeps
it, a GitHub token sealed in the vault) and never printed.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from openajar.core.errors import OpenajarError

if TYPE_CHECKING:
    from openajar.setup.cli import Ctx

# Local mode's limits: low, because a laptop that sleeps keeps a VM billing until it wakes.
LOCAL_MONTHLY_EUR = 10.0
LOCAL_MAX_HOURS = 4.0
LOCAL_MAX_LIVE = 2
HETZNER_HOURLY = "about EUR 0.011 an hour"
LOCATIONS = (("nbg1", "Nuremberg, Germany"), ("fsn1", "Falkenstein, Germany"), ("hel1", "Helsinki, Finland"),
             ("ash", "Ashburn, USA"), ("hil", "Hillsboro, USA"), ("sin", "Singapore"))
VM_SIZES = (("small", "small: 2 CPU, 4 GB (the cheapest, a cx23)"), ("medium", "medium: 4 CPU, 8 GB"),
            ("large", "large: 8 CPU, 16 GB"))
TEST_LANE = "setup/test-vm"
# The `dev` snapshot (openajar vm image build dev): Ubuntu with the tools and the environment's browser.
SNAPSHOT_GB = 4
SNAPSHOT_PRICE = "about EUR 0.012 per GB a month"  # Hetzner's snapshot storage
GH_UNREACHABLE = "could not reach GitHub to check it"
ANY_REPO_QUESTION = "Let environments clone and push any repository this GitHub login can reach?"
CLIENT_NAMES = ("Claude Code", "Codex", "Skills")  # the agents step's lines that name a client
PUSH_RULES = "Push rules still apply: no force push or delete of branches the environment did not create."


# ------------------------------------------------------------------ the outside world (tests replace these)


def local(name: str) -> Any:
    """openajar.local.<name> (the local runtime: paths, service, register)."""
    return importlib.import_module(f"openajar.local.{name}")


def local_home() -> Path:
    try:
        return Path(local("paths").default_home())
    except ModuleNotFoundError:
        from openajar.core.config import local_home as core_local_home

        return core_local_home()


def hetzner_check(token: str, api_url: str) -> str | None:
    """None when Hetzner takes the token, else what is wrong, in words."""
    from openajar.vms.hetzner import HetznerProvider

    try:
        asyncio.run(HetznerProvider(token, api_url=api_url).check())
    except OpenajarError as e:
        return e.message
    return None


def docker_state(docker_bin: str, image: str) -> tuple[bool, bool, bool]:
    """(installed, running, the image is there)."""
    exe = shutil.which(docker_bin)
    if exe is None:
        return False, False, False
    try:
        running = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                                 timeout=20, check=False).returncode == 0
        built = running and subprocess.run([exe, "image", "inspect", image], capture_output=True, timeout=20,
                                           check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True, False, False
    return True, running, built


def get_docker_image(image: str, docker_bin: str, pull: str) -> bool:
    """The prebuilt image for this version, downloaded, or built here when it cannot be."""
    from openajar.environments import cli as envcli

    try:
        envcli._get_image(image, docker_bin, pull)
    except typer.Exit as e:
        return not e.exit_code
    return True


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh(*args: str, interactive: bool = False) -> tuple[int, str]:
    """Run gh: (exit code, what it printed). Interactive runs on this terminal and prints nothing back."""
    exe = shutil.which("gh")
    if exe is None:
        return 127, ""
    if interactive:
        return subprocess.run([exe, *args], check=False).returncode, ""
    try:
        done = subprocess.run([exe, *args], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def github_user(token: str) -> tuple[str | None, str | None]:
    """(the login the token belongs to, None) or (None, what is wrong)."""
    import httpx

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "User-Agent": "openajar-setup"}
    try:
        r = httpx.get("https://api.github.com/user", headers=headers, timeout=15)
    except httpx.HTTPError:
        return None, GH_UNREACHABLE
    if r.status_code == 401:
        return None, "GitHub refused it (expired, revoked or mistyped)"
    if r.status_code >= 400:
        return None, f"GitHub answered {r.status_code}"
    try:
        return str((r.json() or {}).get("login") or ""), None
    except ValueError:
        return "", None


def vault(home: Path, *, create: bool = True) -> Any:
    """This home's vault (OPENAJAR_HOME is `home`), made with a key file when there is none yet and
    `create`; else None."""
    from openajar.commands.base import cli_runtime

    found = cli_runtime().get("vault")
    if found is not None or not create:
        return found
    make_vault(home)
    return cli_runtime().get("vault")


def make_vault(home: Path) -> None:
    """A key-file vault, as `openajar vault init` makes one: the key in secrets/vault.key (0600)."""
    import os

    from openajar.vault import crypto
    from openajar.vault.service import keyfile_path, pending_keyfile_path
    from openajar.vault.store import VaultStore

    store = VaultStore(home / "vault.db")
    keyfile = keyfile_path(home)
    pending = pending_keyfile_path(keyfile)
    master = os.urandom(crypto.KEY_LEN)
    crypto.write_keyfile(pending, master)
    store.initialize(mode="keyfile", master=master, kdf=None)
    os.replace(pending, keyfile)
    crypto.fsync_dir(keyfile.parent)


class _NoLanes:
    """The test VM is no lane: VmManager asks the lanes for nothing it keeps."""
    remote_launcher = "none"  # not None: the manager gives no ssh launcher

    def has(self, lane_id: str) -> bool:
        return False

    def create_remote(self, lane_id: str, remote: Any) -> None:
        return None


def open_test_vm(home: Path) -> tuple[str, float | None]:
    """Rent a VM, run `uname -a` on it, delete it: (what it printed, what it cost in EUR)."""
    from openajar.core.models import utcnow
    from openajar.core.runtime import Runtime
    from openajar.vms import ssh
    from openajar.vms.manager import VmManager
    from openajar.vms.models import VmError

    rt = Runtime()
    rt.register("envs", _NoLanes())
    mgr = VmManager(home, runtime=rt)

    async def go() -> tuple[str, float | None]:
        rec = await mgr.create(TEST_LANE, ttl_minutes=15, purpose="openajar setup: a test VM")
        try:
            if rec.state != "ready":
                raise VmError(rec.error or f"the VM is {rec.state}, not ready")
            code, out = await ssh.run(mgr.remote(rec), "uname -a", timeout_s=60)
            if code != 0:
                raise VmError(f"uname -a ended with {code}")
            said = out.strip().splitlines()[-1] if out.strip() else ""
        finally:
            rec = await mgr.destroy(rec.id, reason="openajar setup: the test is done")
        return said, rec.cost_eur(utcnow())

    return asyncio.run(go())


def dev_snapshot_state(home: Path) -> bool | None:
    """True when the Hetzner project has the `dev` snapshot, False when it has none, None when it
    cannot tell (Hetzner did not answer)."""
    from openajar.vms.manager import VmManager

    try:
        asyncio.run(VmManager(home).provider().resolve_image("snapshot:dev"))
    except OpenajarError as e:
        return False if e.message.startswith("no snapshot named dev") else None
    return True


def build_dev_snapshot(home: Path, say: Any) -> float | None:
    """`openajar vm image build dev`: a VM from Ubuntu, the tools, the environment's setup and browser, a
    snapshot, the VM deleted. Returns what it cost in EUR; raises OpenajarError when it fails."""
    from openajar.core.runtime import Runtime
    from openajar.environments.machine import dev_image_script
    from openajar.vms.manager import VmManager

    rt = Runtime()
    rt.register("envs", _NoLanes())
    mgr = VmManager(home, runtime=rt, follow_ip=True)  # a laptop: SSH from where it is now
    out = asyncio.run(mgr.build_image("dev", script=dev_image_script(), on_step=say))
    return out.get("cost_eur")


def read_token_file(path: Path) -> str:
    text = sys.stdin.readline() if str(path) == "-" else Path(path).expanduser().read_text()
    return text.strip()


# ------------------------------------------------------------------ settings files


def _write_toml(path: Path, updates: dict[str, Any], check: Any) -> None:
    import tomllib

    import tomli_w

    from openajar.core.config import write_private

    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data.update(updates)
    check(**data)  # refuse what the service would not start with
    write_private(path, tomli_w.dumps(data))


def write_vm_settings(home: Path, updates: dict[str, Any]) -> None:
    from openajar.vms.models import SETTINGS_FILE, VmSettings

    _write_toml(home / SETTINGS_FILE, updates, VmSettings)


def size_updates(home: Path, size: str) -> dict[str, Any]:
    """vms.toml keys that make `size` the default machine."""
    from openajar.environments.models import SIZES
    from openajar.vms.models import load_settings

    if size == "small":
        return {"default_profile": "small"}
    cpu, memory = SIZES[size]
    profiles = {n: p.model_dump(exclude_defaults=True) for n, p in load_settings(home).profiles.items()}
    profiles[size] = {"min_cpu": cpu, "min_memory_gb": memory, "description": f"the default: {cpu}+ CPU, {memory}+ GB"}
    return {"profiles": profiles, "default_profile": size}


# ------------------------------------------------------------------ step: VMs


def vms(ctx: Ctx) -> None:
    from openajar.vms.models import TOKEN_FILE, load_settings

    ui, home = ctx.ui, ctx.home
    try:
        settings = load_settings(home)
    except (ValueError, OSError) as e:
        ui.say(f"vms.toml cannot be read ({type(e).__name__}); fix or remove {home / 'vms.toml'} first.")
        ctx.todo.append("VMs")
        return
    has_token = (home / TOKEN_FILE).exists()
    installed, running, built = docker_state(settings.docker_bin, settings.docker_image)
    options = []
    if has_token:
        options.append(("keep", "Keep the saved Hetzner token"))
    options.append(("hetzner", f"Hetzner Cloud: a real VM for each chat, {HETZNER_HOURLY} while one is open"))
    options.append(("morph", "Morph Cloud: starts in seconds, and a save pauses it with its memory"))
    if running:
        options.append(("docker", "Docker on this computer: free, but less isolated than a cloud VM"))
    options.append(("own", "Your own machine: a Linux box you reach over ssh (at home, or a server elsewhere); free"))
    options.append(("skip", "Skip for now (openajar setup vms later)"))
    if installed and not running:
        ui.say("Docker is installed but not running. Start it and run `openajar setup vms` to use it.")
    choice = ui.choose("Where should VMs come from?", options, _vm_default(ctx, settings, has_token, running))
    if choice == "docker":
        _use_docker(ctx, settings, built)
    elif choice == "morph":
        from openajar.setup.morph import use_morph

        use_morph(ctx, settings)
    elif choice in ("hetzner", "keep"):
        _use_hetzner(ctx, settings, keep=choice == "keep")
    elif choice == "own":
        _use_own(ctx, settings)
    else:
        ui.say("No VMs for now. Agents cannot open an environment until you run `openajar setup vms`.")
        ctx.todo.append("VMs")


def _vm_default(ctx: Ctx, settings: Any, has_token: bool, running: bool) -> str:
    opts = ctx.opts
    if opts.docker and running:
        return "docker"
    if opts.hetzner_token_file is not None:
        return "hetzner"
    if settings.provider == "docker" and running:
        return "docker"
    if settings.provider == "morph":
        return "morph"
    if settings.provider == "own":
        return "own"
    if has_token:
        return "keep"
    return "skip" if ctx.ui.yes else "hetzner"


def _use_own(ctx: Ctx, settings: Any) -> None:
    """Your own machine (openajar.vms.own). A machine is brought in with its own command, which asks what
    setup cannot (its address, its host key); here the one already set up becomes where environments open."""
    from openajar.vms import own

    ui = ctx.ui
    add = "openajar environments machine add NAME user@host"
    names = [m.name for m in own.every(ctx.home)]
    if not names:
        ui.say("Bring your machine in with one command (it checks the machine over ssh and puts the environments "
               f"image in its Docker), then run `openajar setup vms` again: {add}. A fresh Ubuntu 24.04 server can "
               "be handed over as a whole instead, with no Docker: add --whole.")
        ctx.todo.append(f"your machine ({add})")
        return
    name = own.default_name(ctx.home, settings) if settings.provider == "own" else None
    name = name or names[0]
    write_vm_settings(ctx.home, {"provider": "own", "location": name})
    if own.is_whole(ctx.home, name):
        ui.say(f"Environments open on your machine {name}, handed over as a whole: one at a time, the agent root "
               "on it. It is free.")
    else:
        ui.say(f"Environments open as containers on your machine {name}. They are free, and share that machine's "
               "kernel.")
    ctx.vms = f"your own machine {name} (free)"


def _use_docker(ctx: Ctx, settings: Any, built: bool) -> None:
    ui = ctx.ui
    write_vm_settings(ctx.home, {"provider": "docker"})
    ui.say("VMs are Docker containers on this computer. They are free, and share this computer's kernel.")
    ui.say("The machine image has each environment's browser, so the browser starts at once.")
    if not built and ui.confirm(f"Get the machine image {settings.docker_image} now? It downloads the prebuilt one "
                                "(a minute or two), or builds it here when it cannot (10 to 15 minutes).",
                                default=True):
        built = get_docker_image(settings.docker_image, settings.docker_bin, settings.docker_pull)
        ui.say("The image is ready." if built else "The image did not download or build. Try again: openajar setup vms")
    if not built:
        ctx.todo.append("the Docker image (openajar environments setup --pull)")
    ctx.vms = "Docker on this computer (free)"


def _hetzner_token(ctx: Ctx, api_url: str) -> str | None:
    """A token Hetzner takes, from the file or the prompt (three tries), or None."""
    ui, opts = ctx.ui, ctx.opts
    if opts.hetzner_token_file is not None:
        token = read_token_file(opts.hetzner_token_file)
        problem = hetzner_check(token, api_url) if token else "the file is empty"
        if problem:
            ui.say(f"The Hetzner token in {opts.hetzner_token_file} did not work: {problem}")
            return None
        return token
    ui.say("Make a token in the Hetzner Cloud console: your project, Security, API tokens, Read & Write.")
    ui.say("Use a project for openajar alone: the token can delete any server in it.")
    for _ in range(3):
        token = ui.secret("Paste the Hetzner token (hidden; Enter to skip)")
        if not token:
            return None
        problem = hetzner_check(token, api_url)
        if problem is None:
            return token
        ui.say(f"That token did not work: {problem}")
    return None


def _use_hetzner(ctx: Ctx, settings: Any, *, keep: bool) -> None:
    from openajar.core.config import write_private
    from openajar.vms.models import TOKEN_FILE

    ui, home = ctx.ui, ctx.home
    path = home / TOKEN_FILE
    problem = None
    if keep:
        problem = hetzner_check(path.read_text().strip(), settings.api_url)
        ui.say("The saved Hetzner token works." if problem is None else f"The saved Hetzner token did not work: "
               f"{problem}. Run `openajar setup vms` with a new one.")
        if problem is not None:
            ctx.todo.append("VMs")
    else:
        token = _hetzner_token(ctx, settings.api_url)
        if token is None:
            ui.say("No Hetzner token saved. Run `openajar setup vms` when you have one.")
            ctx.todo.append("VMs")
            return
        write_private(path, token + "\n")
        ui.say(f"The token works. It is saved in {path}, and only you can read it.")
    location, size = settings.location if settings.provider == "hetzner" else "nbg1", settings.default_profile
    if not ctx.quick:
        location = ui.choose("Where should VMs run?", [(k, f"{k}: {w}") for k, w in LOCATIONS],
                             location if location in dict(LOCATIONS) else "nbg1")
        size = ui.choose("How big is a VM, unless an agent asks for more?", list(VM_SIZES),
                         size if size in dict(VM_SIZES) else "small")
    updates = {"provider": "hetzner", "location": location}
    if size in dict(VM_SIZES):
        updates.update(size_updates(home, size))
    write_vm_settings(home, updates)
    where = dict(LOCATIONS).get(location, location)
    ctx.vms = f"Hetzner Cloud in {where}, {size if size in dict(VM_SIZES) else 'small'} machines ({HETZNER_HOURLY})"
    if problem is None:
        _offer_dev_snapshot(ctx)


def _offer_dev_snapshot(ctx: Ctx) -> None:
    """Each environment's browser runs on its VM. The `dev` snapshot has it (and the tools) ready;
    without it, every VM installs the browser when it opens."""
    ui = ctx.ui
    state = dev_snapshot_state(ctx.home)
    if state is True:
        ui.say("The dev snapshot is there, so each VM boots with its tools and browser ready.")
        return
    if state is None:
        ui.say("openajar could not check for the dev snapshot just now. `openajar setup vms` checks again.")
        return
    ui.say("Each environment gets a real browser, which runs on its VM.")
    ui.say("Without a snapshot, each VM installs the browser when it opens. That adds 2 to 4 minutes to every "
           "environment.")
    ui.say("The dev snapshot has it ready. Building it takes a few minutes and costs a cent or two. Hetzner "
           f"then bills its storage (about {SNAPSHOT_GB} GB, {SNAPSHOT_PRICE}) until you delete it.")
    if not ui.confirm("Build the dev snapshot now?", default=not ui.yes):
        ui.say("No snapshot for now. `openajar setup vms` offers it again, or run `openajar vm image build dev`.")
        return
    ui.say("Building the dev snapshot (a few minutes)...")
    try:
        cost = build_dev_snapshot(ctx.home, lambda step: ui.say(f"  {step}"))
    except OpenajarError as e:
        ui.say(f"The dev snapshot was not built: {e.message}")
        ui.say("Environments still work: each VM installs the browser when it opens. Try again with "
               "`openajar setup vms`.")
        ctx.todo.append("the dev snapshot")
        return
    ui.say("The dev snapshot is built. Each VM now boots with its tools and browser ready." +
           (f" It cost EUR {cost:.3f}." if cost else ""))


# ------------------------------------------------------------------ step: GitHub


def _gh_login() -> str | None:
    """The account gh is signed in to on github.com, or None."""
    if not gh_available():
        return None
    code, out = gh("auth", "status", "--hostname", "github.com")
    if code != 0:
        return None
    m = re.search(r"(?:account|as)\s+([A-Za-z0-9-]+)", out)
    return m.group(1) if m else ""


def _saved_login(ctx: Ctx) -> Any:
    from openajar.git import login

    saved = login.load(ctx.home)
    if saved is None:
        return None
    store = vault(ctx.home, create=False)
    try:
        held = store is not None and asyncio.run(store.has_secret(login.REF))
    except OpenajarError:
        held = False
    return saved if held else None


def _github_options(saved: Any, gh_account: str | None) -> list[tuple[str, str]]:
    options = []
    if saved is not None:
        options.append(("keep", f"Keep the saved login{f' ({saved.login})' if saved.login else ''}"))
    if gh_account is not None:
        options.append(("gh", f"Use your gh login{f' ({gh_account})' if gh_account else ''}"))
    elif gh_available():
        options.append(("gh-login", "Sign in with gh now (gh auth login)"))
    options.append(("token", "Paste a GitHub token"))
    options.append(("skip", "Skip: public repositories only"))
    if saved is not None:
        options.append(("forget", "Remove the saved login"))
    return options


def _github_default(ctx: Ctx, saved: Any, gh_account: str | None) -> str:
    if ctx.opts.github_token_file is not None:
        return "token"
    if saved is not None:
        return "keep"
    if gh_account is not None:
        return "gh"
    if ctx.ui.yes:
        return "skip"
    return "gh-login" if gh_available() else "token"


def github(ctx: Ctx) -> None:
    ui = ctx.ui
    ui.say("Environments clone and push your repositories through openajar on this computer.")
    ui.say("Your GitHub token stays here. A VM never sees it.")
    saved = _saved_login(ctx)
    gh_account = _gh_login()
    choice = ui.choose("How should openajar reach GitHub?", _github_options(saved, gh_account),
                       _github_default(ctx, saved, gh_account))
    if choice == "skip":
        ctx.github = "public repositories only"
        ui.say("Public repositories only. Run `openajar setup github` to add a login later.")
        return
    if choice == "forget":
        _forget_login(ctx)
        return
    if choice == "keep":
        _keep_login(ctx, saved)
        return
    if choice == "gh-login":
        if gh("auth", "login", "--hostname", "github.com", "--git-protocol", "https", interactive=True)[0] != 0:
            ui.say("gh did not sign in. Run `openajar setup github` to try again.")
            ctx.github = "public repositories only"
            return
        gh_account, choice = _gh_login() or "", "gh"
    token = _github_token(ctx, choice)
    if token is None:
        ctx.github = "public repositories only"
        return
    _store_login(ctx, token, gh_account if choice == "gh" else None, source="gh" if choice == "gh" else "token")


def _github_token(ctx: Ctx, choice: str) -> str | None:
    ui, opts = ctx.ui, ctx.opts
    if choice == "gh":
        code, out = gh("auth", "token", "--hostname", "github.com")
        token = out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""
        if not token:
            ui.say("gh did not give a token. Run `gh auth login`, then `openajar setup github`.")
            return None
        return token
    if opts.github_token_file is not None:
        return read_token_file(opts.github_token_file) or None
    ui.say("Make one at https://github.com/settings/personal-access-tokens: the repositories agents may use,")
    ui.say("with Contents: read and write, and Pull requests: read and write.")
    return ui.secret("Paste the GitHub token (hidden; Enter to skip)") or None


def _store_login(ctx: Ctx, token: str, gh_account: str | None, *, source: str) -> None:
    from openajar.git import login

    ui = ctx.ui
    user, problem = github_user(token)
    if problem == GH_UNREACHABLE:
        ui.say(f"openajar {problem} now. It is saved anyway, and checked on the first clone.")
        user = gh_account or ""
    elif problem is not None:
        ui.say(f"That token did not work: {problem}. Nothing was saved.")
        ctx.github, ctx.todo = "public repositories only", [*ctx.todo, "GitHub"]
        return
    ui.say(PUSH_RULES)
    any_repo = ui.confirm(ANY_REPO_QUESTION, default=True)
    saved = asyncio.run(login.store(vault(ctx.home), ctx.home, token, login=user or "", source=source,
                                    any_repo=any_repo))
    ui.say(f"Saved the GitHub login{f' {saved.login}' if saved.login else ''}, sealed in the openajar vault "
           f"({ctx.home / 'vault.db'}).")
    if source == "gh":
        ui.say("It is gh's token. When gh signs in again or the token changes, run `openajar setup github` again.")
    _say_reach(ctx, saved)


def _keep_login(ctx: Ctx, saved: Any) -> None:
    from openajar.git import login

    ctx.ui.say(PUSH_RULES)
    any_repo = ctx.ui.confirm(ANY_REPO_QUESTION, default=saved.any_repo or ctx.quick)
    _say_reach(ctx, login.set_any_repo(ctx.home, any_repo) or saved)


def _forget_login(ctx: Ctx) -> None:
    from openajar.git import login

    asyncio.run(login.forget(vault(ctx.home), ctx.home))
    ctx.github = "public repositories only"
    ctx.ui.say("Removed the saved GitHub login. Public repositories only now.")


def _say_reach(ctx: Ctx, saved: Any) -> None:
    who = saved.login or "this login"
    ctx.github_login = saved.login or ""
    if saved.any_repo:
        ctx.github = f"{who}: any repository it can reach"
        ctx.ui.say(f"Environments may clone and push any repository {who} can reach.")
    else:
        ctx.github = f"{who}: repositories you connect one by one"
        ctx.ui.say("Connect repositories one by one: openajar environments repo add github.com/<owner>/<name>")


# ------------------------------------------------------------------ step: limits


def limits(ctx: Ctx) -> None:
    from openajar.environments import cli as envcli
    from openajar.environments.models import SETTINGS_FILE, load_settings

    ui, home = ctx.ui, ctx.home
    path = home / SETTINGS_FILE
    first = not path.exists()
    current = load_settings(home)
    monthly = LOCAL_MONTHLY_EUR if first else (current.monthly_eur or 0.0)
    hours = LOCAL_MAX_HOURS if first else current.max_hours
    live = LOCAL_MAX_LIVE if first else (current.max_live if current.max_live is not None else LOCAL_MAX_LIVE)
    ui.say("openajar refuses a VM past these limits, whatever an agent asks.")
    if not ctx.quick:
        monthly = ui.number("Monthly cap for VMs, in EUR (0: no cap)", monthly)
        hours = ui.number("Hours one VM may stay open, at most", hours)
        live = int(ui.number("VMs open at once, at most", live, whole=True))
    updates: dict[str, Any] = {"monthly_eur": monthly, "max_hours": hours, "max_live": live}
    updates.update(keep_updates(ctx, current))
    try:
        envcli._write_settings(updates, home)
    except ValueError as e:
        ui.say(f"Those limits were not saved: {str(e).splitlines()[0]}. Run `openajar setup limits` again.")
        ctx.todo.append("limits")
        return
    saved = load_settings(home)
    ctx.limits = limit_words(saved)
    ctx.keep = keep_words(saved)
    ui.say(f"Limits: {ctx.limits}.")
    ui.say(f"Environments: {ctx.keep}.")


# How an environment stays after the work (environments.toml default_keep and allow_keep_live).
# Saves when idle comes first and is the default: openajar is a computer that stays.
KEEP_OPTIONS = (
    ("save", ("Saves when idle (recommended): saved and the VM deleted when idle, back when an agent opens it "
              "again. Cents a month while saved.")),
    ("none", "Single use: the VM is deleted when the work is done. Nothing to pay after."),
    ("live", "Saves when idle, and an agent may keep one running when you ask. That one bills every hour."),
)
KEEP_LIVE_COST = ("A VM kept running bills every hour, also while no agent works: about EUR 7.80 a month "
                  "for the smallest. It is deleted only when you or the agent close it for good.")


def keep_updates(ctx: Ctx, current: Any) -> dict[str, Any]:
    """The one question about how environments stay: environments.toml's default_keep, and
    allow_keep_live only when the owner chose it here (the default saves when idle, never live)."""
    ui = ctx.ui
    keep = getattr(current, "default_keep", None) or "save"
    allowed = bool(getattr(current, "allow_keep_live", False))
    default = "live" if allowed else ("none" if keep == "none" else "save")
    choice = ui.choose("How should an environment stay after the work?", list(KEEP_OPTIONS), default)
    if choice == "live" and not allowed:
        ui.say(KEEP_LIVE_COST)
        if not ui.confirm("Let agents keep a VM running when you ask them to?", default=False):
            ui.say("Not allowed. Environments save when idle; run `openajar setup limits` to change it.")
            choice = "save"
    updates: dict[str, Any] = {"default_keep": "none" if choice == "none" else "save"}
    if choice == "live" or allowed:
        updates["allow_keep_live"] = choice == "live"
    return updates


def keep_words(s: Any) -> str:
    keep = getattr(s, "default_keep", None) or "save"
    words = "single use, deleted when the work is done" if keep == "none" else "saved when idle"
    if getattr(s, "allow_keep_live", False):
        words += ", kept running when you ask (it bills every hour)"
    return words


def limit_words(s: Any) -> str:
    cap = "no monthly cap" if not s.monthly_eur else f"EUR {s.monthly_eur:g} a month"
    live = "the VM limit" if s.max_live is None else str(s.max_live)
    return f"{cap}, {s.max_hours:g} hours per VM, {live} at once"


# ------------------------------------------------------------------ step: the service and the agents


def _service_state(ctx: Ctx, *, start: bool) -> Any:
    try:
        service = local("service")
    except ModuleNotFoundError:
        return None
    state = service.status(ctx.home)
    if start and not state.running:
        state = service.start(ctx.home)
    return state


def agents(ctx: Ctx) -> None:
    ui = ctx.ui
    state = _service_state(ctx, start=True)
    if state is None:
        ui.say("The local service is not part of this build of openajar yet. Update openajar and run "
               "`openajar setup agents`.")
        ctx.todo.append("the local service")
        return
    if not state.running:
        ui.say(f"The openajar service did not start{f': {state.note}' if state.note else ''}.")
        ui.say(f"Its log: {state.log_path}")
        ctx.todo.append("the local service")
        return
    ctx.service_url = state.url
    ui.say(f"The openajar service runs on this computer at {state.url}.")
    claude = codex = skills = True
    if not ctx.quick:
        claude = ui.confirm("Connect Claude Code?", default=True)
        codex = ui.confirm("Connect Codex?", default=True)
        skills = ui.confirm("Add the openajar skills (how to work in an environment)?", default=True)
    try:
        done = local("register").register_clients(ctx.home, state.port, claude=claude, codex=codex, skills=skills)
    except (OpenajarError, OSError) as e:
        ui.say(f"Connecting the agents failed: {getattr(e, 'message', e)}. Run `openajar setup agents` again.")
        ctx.todo.append("the agents")
        return
    # The grant lines are for `openajar local`; here the person wants to know which agents can reach it.
    ctx.agents = [name for name, state in (line.split(": ", 1) for line in done if ": " in line)
                  if name in CLIENT_NAMES and not state.startswith(("skipped", "failed", "not installed"))]
    for line in done:
        if not line.startswith(("Agent grant ", "  revoked ")):
            ui.say(f"  {line}")


def restart_if_running(ctx: Ctx) -> None:
    """A changed vms.toml or environments.toml is read when the service starts."""
    try:
        service = local("service")
    except ModuleNotFoundError:
        return
    if service.status(ctx.home).running:
        service.stop(ctx.home)
        state = service.start(ctx.home)
        ctx.ui.say("Restarted the openajar service, so it uses the new settings." if state.running else
                   f"The openajar service did not start again: {state.note}. Run `openajar setup agents`.")


# ------------------------------------------------------------------ step: ssh from the person's shell


def ssh(ctx: Ctx) -> None:
    """One line at the top of ~/.ssh/config, `Include ~/.openajar/ssh/config`, so `ssh openajar-<id>` works
    from any shell on this computer (openajar.environments.sshaccess writes an entry per environment).
    Asked once: when the line is there, nothing is asked."""
    from openajar.environments import sshaccess

    ui = ctx.ui
    target = sshaccess.include_target(ctx.home)
    if sshaccess.included(ctx.home):
        ui.say(f"Your shell reaches your environments already: ssh openajar-<id> (~/.ssh/config includes {target}).")
        ctx.ssh = "ssh openajar-<id> works from your shell"
        return
    ui.say("Your agents can also work on an environment from their own shell: ssh openajar-<id>, scp and rsync.")
    ui.say(f"For that, ~/.ssh/config needs one line at its top: Include {target}. Everything else in it stays.")
    if not ui.confirm("Add that line?", default=True):
        ui.say(f"Not added. Agents then use ssh -F {target} openajar-<id>. `openajar setup ssh` asks again.")
        return
    try:
        sshaccess.add_include(ctx.home)
    except OSError as e:
        ui.say(f"Could not change ~/.ssh/config: {e}. Agents use ssh -F {target} openajar-<id> instead.")
        ctx.todo.append("ssh")
        return
    ui.say("Added. Each environment your agents open gets its own entry, removed again when it closes.")
    ctx.ssh = "ssh openajar-<id> works from your shell"


# ------------------------------------------------------------------ step: health check


def _check_service(ctx: Ctx) -> tuple[bool, str]:
    state = _service_state(ctx, start=False)
    if state is None:
        return False, "not part of this build yet"
    if state.running:
        return True, f"running at {state.url}"
    return False, "not running: `openajar setup agents` starts it"


def _check_vms(ctx: Ctx) -> tuple[bool, str]:
    from openajar.vms.models import TOKEN_FILE, load_settings

    try:
        s = load_settings(ctx.home)
    except (ValueError, OSError):
        return False, "vms.toml cannot be read"
    if s.provider == "docker":
        _installed, running, built = docker_state(s.docker_bin, s.docker_image)
        if not running:
            return False, "Docker is not running"
        return (True, "Docker answers, and the image is there") if built else (
            False, "Docker answers, but the image is not there: openajar environments setup --pull")
    if s.provider == "own":
        from openajar.vms import own

        name = own.default_name(ctx.home, s)
        if name is None:
            return False, "no machine of yours is set up: openajar environments machine add NAME user@host"
        try:
            asyncio.run(own.OwnProvider(ctx.home, s).check())
        except OpenajarError as e:
            return False, e.message
        return True, f"your machine {name} answers" + ("" if own.is_whole(ctx.home, name)
                                                       else ", and the image is there")
    if s.provider == "morph":
        from openajar.setup.morph import morph_check
        from openajar.vms.models import MORPH_TOKEN_FILE

        key = ctx.home / MORPH_TOKEN_FILE
        problem = morph_check(key.read_text().strip(), s.morph_api_url) if key.exists() else "no API key"
        return (True, "Morph answers") if problem is None else (False, f"Morph: {problem} (openajar setup vms)")
    path = ctx.home / TOKEN_FILE
    if not path.exists():
        return False, "not set up: openajar setup vms"
    problem = hetzner_check(path.read_text().strip(), s.api_url)
    where = dict(LOCATIONS).get(s.location, s.location)
    return (True, f"Hetzner answers ({where})") if problem is None else (False, f"Hetzner: {problem}")


def _check_github(ctx: Ctx) -> tuple[bool | None, str]:
    saved = _saved_login(ctx)
    if saved is None:
        return None, "no login: public repositories only (openajar setup github)"
    reach = "any repository it can reach" if saved.any_repo else "repositories you connect one by one"
    via = "from gh" if saved.source == "gh" else "a pasted token"
    return True, f"{saved.login or 'a login'} ({via}): {reach}"


def _check_limits(ctx: Ctx) -> tuple[bool, str]:
    from openajar.environments.models import load_settings

    saved = load_settings(ctx.home)
    return True, f"{limit_words(saved)}; {keep_words(saved)}"


def check(ctx: Ctx) -> None:
    from openajar.setup.webstep import check_row

    rows = [("Service", *_check_service(ctx)), ("VMs", *_check_vms(ctx)), ("GitHub", *_check_github(ctx)),
            ("Limits", *_check_limits(ctx)), ("Web app", *check_row(ctx))]
    if ctx.agents:
        rows.append(("Agents", True, "connected: " + ", ".join(ctx.agents)))
    for what, ok, words in rows:
        mark, color = {True: ("ready", typer.colors.GREEN), False: ("to do", typer.colors.YELLOW),
                       None: ("note", None)}[ok]
        ctx.ui.say(f"  {mark:<6} {what:<8} {words}", **({"fg": color} if color else {}))
        if ok is False and what not in ctx.todo:
            ctx.todo.append(what)


# ------------------------------------------------------------------ step: a test VM, and the summary


def test_vm(ctx: Ctx) -> None:
    from openajar.vms.models import load_settings

    ui = ctx.ui
    try:
        s = load_settings(ctx.home)
    except (ValueError, OSError):
        return
    from openajar.vms.providers import configured

    if not configured(ctx.home, s):
        ui.say("No VMs are set up, so there is nothing to test yet.")
        return
    cost = {"docker": "free on Docker", "own": "free on your own machine",
            "morph": "about EUR 0.01 on Morph"}.get(s.provider, "about EUR 0.01")
    if not ui.confirm(f"Open a test VM now? It starts one, runs `uname -a` and closes it ({cost}).",
                      default=False):
        ui.say("No test VM. `openajar setup test-vm` runs one later.")
        return
    ui.say("Opening a test VM (a minute or two)...")
    try:
        said, spent = open_test_vm(ctx.home)
    except OpenajarError as e:
        ui.say(f"The test VM failed: {e.message}")
        ctx.todo.append("the test VM")
        return
    ui.say(f"It answered: {said}")
    ui.say("Closed." + (f" It cost EUR {spent:.3f}." if spent else ""))


def summary(ctx: Ctx) -> None:
    ui = ctx.ui
    ui.say()
    if ctx.todo:
        ui.say(f"Almost done. Still to do: {', '.join(dict.fromkeys(ctx.todo))} (see above).", bold=True)
    else:
        ui.say("All set. Nothing bills until an agent opens a VM.", bold=True)
    for what, words in (("VMs", ctx.vms), ("GitHub", ctx.github), ("Limits", ctx.limits),
                        ("Keeping", ctx.keep), ("Service", ctx.service_url), ("Web app", ctx.web), ("SSH", ctx.ssh)):
        if words:
            ui.say(f"  {what:<8} {words}")
    owner = ctx.github_login or "<you>"
    ui.say()
    ui.say("Next: start a new Claude Code session and ask:")
    ui.say(f'  "work in openajar on github.com/{owner}/<repo>: fix the failing test and open a pull request"')
    ui.say("Change one thing later: openajar setup vms, github, limits, agents, web, ssh, check or test-vm.")
