"""`openajar environments machine ...`: bring a machine of your own in as the computer of environments
(openajar.vms.own). The owner runs these at the node; no tool adds or removes a machine.

    openajar environments machine add homebox me@192.168.1.20     # register it, check it, put the image there
    openajar environments machine add box root@203.0.113.7 --whole  # a fresh server, handed over as a whole
    openajar environments machine list
    openajar environments machine check homebox [--image]        # what is wrong, and the image again
    openajar environments machine use homebox                    # new environments open there
    openajar environments machine remove homebox                 # take away what openajar put there

No password is ever asked for by openajar or kept: `add` makes a key for the machine and installs it with
your own ssh (which may ask for your password itself), or uses a key file you name with --key.

Two ways to bring one in. With containers (the default): each environment is a container in the machine's
Docker, walled off from the machine, and `add` offers to install Docker when it is missing. As a whole
(`--whole`, openajar.vms.whole): a fresh Ubuntu 24.04 server with nothing of yours on it, on which the agent
is root, one environment at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.core import catalog
from openajar.core.config import home_from_env
from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.vms.models import provider_name

machine_app = typer.Typer(no_args_is_help=True, help=(
    "Bring your own machine: environments open on a Linux box of yours over ssh, as containers or whole."))

_state: dict = {"home": None}

RESTART = ("Restart openajar so it uses it: `openajar local restart` on your own computer, `sudo openajar node "
           "restart` on a server node.")
ISOLATION = ("An environment there is a container: it shares that machine's kernel, and is walled off from the "
             "machine's files, the machine itself, its network and the other environments. That wall is weaker "
             "than a rented VM's, so use your own machine for code you trust.")
WHOLE = ("An environment there has the whole machine: the agent is root on it, and it is on your network as itself, "
         "so it reaches whatever that machine reaches. Nothing walls it in. One environment is open on it at a "
         "time, and a save holds its files, not its disk.")
HAND_OVER = ("openajar takes this machine as a whole. It installs packages on it as root (git, Node, Python, a "
             "browser and a desktop), turns its automatic updates off, and empties its /work folder each time an "
             "environment closes. The agent is root there and can read and change everything on it.")
SETUP_S = 2700.0


def set_home(home: Path | None) -> None:
    _state["home"] = home


def _home() -> Path:
    return Path(_state["home"] or home_from_env())


def _fail(e: OpenajarError | str) -> typer.Exit:
    if isinstance(e, str):
        e = InvalidRequest(e)
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    return typer.Exit(1)


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except OpenajarError as e:
        raise _fail(e) from None


def _settings() -> Any:
    from openajar.vms.models import load_settings

    try:
        return load_settings(_home())
    except (ValueError, OSError) as e:
        raise _fail(f"vms.toml cannot be read ({type(e).__name__}); fix or remove {_home() / 'vms.toml'} first") \
            from None


def _docker(m: Any) -> Any:
    """The machine's side of the provider: its Docker, or the machine itself when it was handed over whole."""
    from openajar.vms.own import MachineDocker

    if m.mode == "whole":
        from openajar.vms.whole import WholeMachine

        return WholeMachine(_home(), m, _settings())
    return MachineDocker(_home(), m, _settings())


def _home_labels() -> dict[str, str]:
    """The label every container, network and image of this openajar home carries."""
    from openajar.vms.manager import VmManager

    return {"agentos-home": VmManager(_home()).home_id}


def _live_on(name: str) -> list[Any]:
    from openajar.vms.store import VmStore

    path = _home() / "vms.db"
    if not path.exists():
        return []
    return [r for r in VmStore(path).live() if r.provider == "own" and (r.provider_id or "").startswith(f"{name}:")]


def _live_elsewhere() -> list[Any]:
    """Machines open now on another provider: switching away would lose track of them."""
    from openajar.vms.store import VmStore

    path = _home() / "vms.db"
    if not path.exists():
        return []
    return [r for r in VmStore(path).live() if r.provider != "own"]


def _saves_on(name: str) -> list[Any]:
    from openajar.environments.saves import SaveStore
    from openajar.vms.own import IMAGE_SEP

    try:
        saves = SaveStore(_home()).all()
    except (OSError, ValueError):
        return []
    return [m for m in saves if m.snapshot is not None and m.snapshot.id.startswith(f"{name}{IMAGE_SEP}")]


def whole_default(home: Path, s: Any) -> bool:
    """Do new environments open on a machine of the owner's handed over whole (openajar.vms.whole)? For
    `openajar environments setup`."""
    if s.provider != "own":
        return False
    from openajar.vms import own

    return own.is_whole(home, own.default_name(home, s))


def own_image(home: Path, *, build: bool) -> None:
    """`openajar environments setup --pull` and `--build` with provider = "own": the image on the machine new
    environments open on (a machine handed over whole has none: its setup runs again)."""
    from openajar.vms import own

    name = own.default_name(home)
    if name is None:
        typer.echo("No machine is the default: openajar environments machine use NAME")
        return
    set_home(home)
    m = own.load(home, name)
    if not (_set_up(m) if m.mode == "whole" else _get_image(m, build=build)):
        raise typer.Exit(1)


def use_machine(home: Path, name: str) -> None:
    """vms.toml: provider = "own" and location = the machine, keeping the rest of the file."""
    import tomllib

    import tomli_w

    from openajar.core.config import write_private
    from openajar.vms.models import SETTINGS_FILE, VmSettings

    path = Path(home) / SETTINGS_FILE
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data.update(provider="own", location=name)
    VmSettings(**data)
    write_private(path, tomli_w.dumps(data))


def _say_facts(f: Any, *, whole: bool = False) -> None:
    from openajar.vms.own import ARCHES

    cpu = {"x86": "x86-64", "arm": "arm64"}.get(ARCHES.get(f.arch, ""), f.arch or "?")
    if whole:
        system = f"{f.os_id.capitalize()} {f.os_version}".strip() or f.system or "?"
        free = f", {f.root_free_gb:g} GB free" if f.root_free_gb is not None else ""
        typer.echo(f"  {system} on {cpu}, {f.host_cores} CPU, {f.host_memory_gb:g} GB{free}")
        return
    docker = f"Docker {f.docker_version}" + (" (through sudo)" if f.docker[:1] == ["sudo"] else "") if f.docker else \
        "no Docker it can use"
    size = f", {f.cores} CPU, {f.memory_gb:g} GB" if f.cores else ""
    free = f", {f.free_gb:g} GB free for Docker" if f.free_gb is not None else ""
    typer.echo(f"  {f.system or '?'} on {cpu}{size}, {docker}{free}")


def _examine_whole(m: Any) -> tuple[Any, list[str], list[str]]:
    """A machine to hand over whole: (what it said, why it cannot be, the signs that it is in use)."""
    from openajar.vms import own, whole

    facts = _run(own.read_facts(_home(), m))
    got = _run(whole.read_fresh(_home(), m, facts))
    wrong = whole.problems(facts, got, _home_labels()["agentos-home"])
    if not wrong:
        refused = own.forwarding_refused(_home(), m, facts.sshd_port or m.port)
        if refused:
            wrong.append(refused)
    m.system, m.arch, m.cores, m.memory_gb = facts.system, facts.arch, facts.host_cores, facts.host_memory_gb
    m.disk_gb, m.sudo = facts.disk_gb, facts.uid != 0
    return facts, wrong, whole.not_fresh(facts, got)


def _install_docker(m: Any, facts: Any, install: bool | None) -> bool:
    """Docker is missing there: install it with Docker's own script when the machine's owner says yes
    (asked at a terminal; never on --yes alone). True when it was installed."""
    from openajar.vms import own

    why = own.cannot_install_docker(facts)
    if why is not None:
        if install:
            typer.echo(f"Docker was not installed: {why}.", err=True)
        return False
    if install is None:
        if not sys.stdin.isatty():
            return False
        typer.echo(f"Docker is not installed on {m.name}. openajar can install it now with Docker's own script "
                   "(https://get.docker.com), run as root there. It stays on the machine when you remove the "
                   "machine from openajar.")
        install = typer.confirm("Install Docker there?", default=False)
    if not install:
        return False
    typer.echo(f"Installing Docker on {m.name} (a few minutes)...")
    try:
        said = asyncio.run(own.install_docker(_home(), m, facts))
    except OpenajarError as e:
        typer.echo(f"It did not install: {e.message}", err=True)
        return False
    m.installed_docker = True
    own.save(_home(), m)
    typer.echo(f"Installed: {said}.")
    return True


def _examine(m: Any) -> tuple[Any, list[str]]:
    """(what the machine said, what is wrong with it), and the record brought up to date."""
    from openajar.vms import own

    facts = _run(own.read_facts(_home(), m))
    wrong = own.problems(facts)
    if not wrong:
        refused = own.forwarding_refused(_home(), m, facts.sshd_port or m.port)
        if refused:
            wrong.append(refused)
    m.system, m.arch, m.cores, m.memory_gb = facts.system, facts.arch, facts.cores, facts.memory_gb
    m.docker_version = facts.docker_version
    if facts.docker:
        m.docker = facts.docker
    own.save(_home(), m)
    return facts, wrong


def _get_image(m: Any, *, build: bool) -> bool:
    """Put this openajar's image on the machine: the prebuilt one when it downloads there, else built there."""
    from openajar.vms import own
    from openajar.vms.docker import image_folder

    docker = _docker(m)
    source = own.image_source(_settings())
    if source and not build:
        typer.echo(f"Downloading the environments image on {m.name} ({source}, about 4 GB: several minutes)...")
        why = _run(docker.pull(source))
        if why is None:
            _note_image(m)
            typer.echo(f"The image is on {m.name}.")
            return True
        typer.echo(f"It did not download there ({why}).")
    folder = image_folder()
    if folder is None:
        typer.echo("This installation of openajar has no copy of the image's Dockerfile, so it cannot build it "
                   "there. Install openajar again, then `openajar environments machine check "
                   f"{m.name} --image`.", err=True)
        return False
    typer.echo(f"Building the image on {m.name} (10 to 15 minutes the first time)...")
    try:
        asyncio.run(docker.build(own.build_context(folder, Path(__file__).resolve().parent)))
    except OpenajarError as e:
        typer.echo(f"It did not build there: {e.message}", err=True)
        return False
    _note_image(m)
    typer.echo(f"The image is on {m.name}.")
    return True


def _note_image(m: Any) -> None:
    """The record says openajar put the image there, so `machine remove` takes it away again (an image that
    was there before, another openajar's say, is left)."""
    from openajar.vms import own

    m.installed_image = True
    own.save(_home(), m)


def _confirm_host_key(name: str, host: str, port: int, keys: list[str], prints: list[str], *, yes: bool,
                      fingerprint: str | None) -> None:
    from openajar.vms import own

    typer.echo(f"The host key of {host}" + (f" port {port}" if port != 22 else "") + ":")
    for p in prints:
        typer.echo(f"  {p}")
    if fingerprint:
        if not any(p.split()[0] == fingerprint.strip() for p in prints):
            own.forget(_home(), name)
            raise _fail(f"none of them is {fingerprint}, the fingerprint you gave: this may not be your machine. "
                        "Nothing was added")
        typer.echo("It is the fingerprint you gave.")
        return
    if own.trusted_already(host, port, keys):
        typer.echo("It is the key your own ssh already trusts for this machine.")
        return
    if yes:
        typer.echo("Pinned as it is (--yes). Every later connection must show this key.")
        return
    typer.echo("Compare it with the machine's own: `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` there.")
    if not sys.stdin.isatty() or not typer.confirm("Is this your machine's key?", default=False):
        own.forget(_home(), name)
        raise _fail("the host key was not confirmed, so nothing was added. Give it with --fingerprint SHA256:..., "
                    "or --yes to pin what the machine shows now")


def _install_key(name: str, user: str, host: str, port: int, public: str) -> bool:
    """openajar's key into the login's authorized_keys, by the person's own ssh. False when that did not work."""
    from openajar.vms import own

    typer.echo(f"openajar made a key for this machine. Adding it to {user}@{host} with your own ssh: it may ask for "
               "your password or your key's passphrase, which openajar neither sees nor keeps.")
    argv = own.install_key_argv(_home(), name, user, host, port)
    if not sys.stdin.isatty():
        argv[1:1] = ["-o", "BatchMode=yes"]
    try:
        done = subprocess.run(argv, input=(own.authorized_line(public) + "\n").encode(), timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _reach(name: str, target: str, key: Path | None, fingerprint: str | None, yes: bool) -> Any:
    """The first part of `add`, the same for both ways: the machine's host key pinned, and a key it takes.
    Returns the machine (not saved yet); raises typer.Exit with nothing left behind when it cannot sign in."""
    from openajar.vms import own
    from openajar.vms.ssh import probe

    home = _home()
    try:
        d = own.folder(home, name)
        user, host, port = own.parse_target(target)
    except OpenajarError as e:
        raise _fail(e) from None
    if (d / own.RECORD).exists():
        raise _fail(f"a machine named {name} is set up already; `openajar environments machine check {name}` looks "
                    f"at it again, and `machine remove {name}` takes it out")
    if key is not None and not key.expanduser().is_file():
        raise _fail(f"no key file at {key}")
    user, host, port, hop = own.resolve_target(user, host, port)
    if hop:
        raise _fail(hop)

    try:
        keys = own.scan_host_keys(host, port)
    except OpenajarError as e:
        raise _fail(e) from None
    _confirm_host_key(name, host, port, keys, own.pin(home, name, keys), yes=yes, fingerprint=fingerprint)

    m = own.Machine(name=name, host=host, port=port, user=user, key=str(key.expanduser()) if key else "")
    if key is None and own.public_key(home, name) is None:
        own.make_key(home, name)
    ok, said = asyncio.run(probe(own.remote(home, m)))
    if not ok and key is not None:
        own.forget(home, name)
        raise _fail(f"{user}@{host} did not take the key {key} ({said}). openajar's ssh never asks for a passphrase, "
                    "so the key must have none; or leave --key out and openajar makes one. Nothing was added")
    if not ok:
        public = own.public_key(home, name) or ""
        if _install_key(name, user, host, port, public):
            m.installed_key = True
            ok, said = asyncio.run(probe(own.remote(home, m)))
        if not ok:
            typer.echo(f"The key could not be added there ({said or 'your ssh did not sign in'}). Add this line to "
                       f"~/.ssh/authorized_keys of {user} on the machine yourself, then run the same command again:",
                       err=True)
            typer.echo(own.authorized_line(public))
            raise typer.Exit(1)
    typer.echo(f"ssh reaches {user}@{host} with " + ("your key." if key is not None else "the key for this machine."))
    return m


def _take_back(m: Any) -> None:
    """An `add` that stops before the machine is registered leaves nothing: openajar's key line there goes
    again, and the machine's folder on this computer."""
    from openajar.vms import own

    public = own.public_key(_home(), m.name)
    if m.installed_key and public:
        with contextlib.suppress(OpenajarError):
            asyncio.run(own.run(_home(), m, "sh -s", stdin=own.remove_key_script(public).encode(), timeout_s=60))
    own.forget(_home(), m.name)


def _setup_script() -> str:
    """What a rented VM's image has, for a machine handed over whole: the setup, the browser and the desktop
    (openajar.environments.machine.dev_image_script without an image's cleanup)."""
    from openajar.environments import machine
    from openajar.environments.vmbrowser import desktop_install_script, install_script

    return machine.UPDATES_OFF + machine.setup_install() + install_script() + desktop_install_script()


def _set_up(m: Any) -> bool:
    from openajar.vms import own

    typer.echo(f"Setting {m.name} up (git, Node, Python, the browser and the desktop: 5 to 15 minutes)...")
    try:
        code, out, err = asyncio.run(own.run(_home(), m, own.as_root(m), stdin=_setup_script().encode(),
                                             timeout_s=SETUP_S))
    except OpenajarError as e:
        typer.echo(f"The setup did not finish: {e.message}", err=True)
        return False
    if code != 0:
        said = (err or out).strip().splitlines()[-3:]
        typer.echo("The setup did not finish there" + (":" if said else f" (exit {code})."), err=True)
        for line in said:
            typer.echo(f"  {line[:300]}", err=True)
        return False
    typer.echo(f"{m.name} is set up.")
    return True


def _add_whole(m: Any, *, yes: bool, setup: bool) -> None:
    """The second part of `add --whole`: is it a fresh Ubuntu 24.04 the login is root on, does its owner
    hand it over, and its setup."""
    from openajar.vms import own

    facts, wrong, signs = _examine_whole(m)
    _say_facts(facts, whole=True)
    if wrong:
        _take_back(m)
        typer.echo(f"{m.name} cannot be handed over as a whole:", err=True)
        for w in wrong:
            typer.echo(f"  - {w}", err=True)
        raise _fail("nothing was added")
    typer.echo(HAND_OVER)
    if signs:
        typer.echo("This machine does not look fresh:")
        for sign in signs:
            typer.echo(f"  - {sign}")
        typer.echo("Hand over a machine with nothing of yours on it, or bring this one in with containers (leave "
                   "out --whole): then the agent works inside a container and does not see the machine.")
        if not sys.stdin.isatty() or not typer.confirm("Hand it over as it is anyway?", default=False):
            _take_back(m)
            raise _fail("the machine does not look fresh and was not handed over (this question is asked at a "
                        "terminal only, never answered by --yes). Nothing was added")
    elif not yes:
        if not sys.stdin.isatty() or not typer.confirm(f"Hand {m.name} over to openajar as a whole?", default=False):
            _take_back(m)
            raise _fail("not confirmed, so nothing was added. --yes says yes to this for a machine that looks fresh")
    m.mode = "whole"
    own.save(_home(), m)
    if setup and not _set_up(m):
        typer.echo(f"{m.name} is registered, and its setup runs again with: openajar environments machine check "
                   f"{m.name} --image", err=True)
        raise typer.Exit(1)


def _add_containers(m: Any, *, image: bool, build: bool, install_docker: bool | None) -> None:
    """The second part of `add` for a machine of containers: Docker there (installed when its owner says
    so), and the environments image."""
    from openajar.vms import own

    own.save(_home(), m)
    facts, wrong = _examine(m)
    if facts.system == "Linux" and not facts.docker_found and _install_docker(m, facts, install_docker):
        facts, wrong = _examine(m)
    _say_facts(facts)
    if wrong:
        typer.echo(f"{m.name} is registered, but it cannot hold environments yet:", err=True)
        for w in wrong:
            typer.echo(f"  - {w}", err=True)
        typer.echo(f"When that is done: openajar environments machine check {m.name} --image", err=True)
        raise typer.Exit(1)

    have = _run(_docker(m).has_image())
    if have is not None and image:
        if not _get_image(m, build=build):
            typer.echo(f"{m.name} is registered without the image. Try again: openajar environments machine check "
                       f"{m.name} --image", err=True)
            raise typer.Exit(1)
    elif have is not None:
        typer.echo(f"The image is not there yet ({have}): openajar environments machine check {m.name} --image")
    else:
        typer.echo(f"The environments image is on {m.name} already.")


@machine_app.command("add")
def add(name: Annotated[str, typer.Argument(metavar="NAME", help="What you call it, like homebox.")],
        target: Annotated[str, typer.Argument(metavar="USER@HOST", help="How ssh reaches it, like me@192.168.1.20 or "
                                              "root@box.example.com:2222. A host from your ~/.ssh/config works too.")],
        whole: Annotated[bool, typer.Option("--whole", help="Hand the whole machine over, with no Docker: the agent "
                                            "is root on it, one environment at a time. For a fresh Ubuntu 24.04 "
                                            "server with nothing of yours on it.")] = False,
        key: Annotated[Path | None, typer.Option("--key", help="A private key file of yours that the machine already "
                                                 "takes (no passphrase). Default: openajar makes a key for this "
                                                 "machine and adds it there with your own ssh.")] = None,
        fingerprint: Annotated[str | None, typer.Option("--fingerprint", help="The machine's host key fingerprint "
                                                        "(SHA256:...), so nothing is asked.")] = None,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Pin the host key the machine shows without asking, "
                                          "and make this the machine new environments open on. With --whole: "
                                          "also hand over a machine that looks fresh without asking.")] = False,
        image: Annotated[bool, typer.Option("--image/--no-image", help="Put the environments image there now "
                                            "(about 4 GB). With --whole: run the machine's setup now.")] = True,
        build: Annotated[bool, typer.Option("--build", help="Build the image there instead of downloading it.")] = False,
        install_docker: Annotated[bool | None, typer.Option(
            "--install-docker/--no-install-docker", help="When Docker is missing there: install it with Docker's "
            "own script, as root. Default: asked.")] = None,
        use: Annotated[bool | None, typer.Option("--use/--no-use", help="Open new environments on this machine "
                                                 "(vms.toml provider = \"own\"). Default: asked.")] = None) -> None:
    """Bring a machine of your own in: openajar checks it over ssh, and from then on agents' environments
    open there.

    With containers (the default): it needs Linux (x86-64 or arm64), Docker (openajar offers to install it)
    and an ssh login that may run Docker. openajar puts there one line in that login's authorized_keys and
    the environments image, and later a container per environment, walled off from the machine.

    As a whole (--whole): a fresh Ubuntu 24.04 server and a root login (or one with passwordless sudo).
    The agent is root on the machine itself, and one environment is open on it at a time.

    openajar never restarts, powers off or wipes the machine; `machine remove` takes away what it put there."""
    m = _reach(name, target, key, fingerprint, yes)
    if whole:
        _add_whole(m, yes=yes, setup=image)
    else:
        _add_containers(m, image=image, build=build, install_docker=install_docker)
    _offer_use(name, use if use is not None else (True if yes else None))
    typer.echo(WHOLE if whole else ISOLATION)


def _offer_use(name: str, use: bool | None) -> None:
    from openajar.vms import own

    settings = _settings()
    current = own.default_name(_home(), settings) if settings.provider == "own" else None
    if current == name:
        typer.echo(f"New environments open on {name}.")
        return
    if use is None:
        if not sys.stdin.isatty():
            use = settings.provider == "own" and current is None
        else:
            now = f"on {current}" if current else {"docker": "in Docker on this computer"}.get(
                settings.provider, f"at {provider_name(settings.provider)}")
            use = typer.confirm(f"Open new environments on {name} from now on? (They open {now} today.)", default=True)
    if not use:
        typer.echo(f"{name} is ready. To open new environments there: openajar environments machine use {name}")
        return
    _use(name)


def _use(name: str) -> None:
    settings = _settings()
    others = _live_elsewhere()
    if settings.provider != "own" and others:
        raise _fail(f"{len(others)} machine{'s are' if len(others) != 1 else ' is'} open at "
                    f"{provider_name(settings.provider)} now, and openajar works with one provider at a time: close "
                    "them first (`openajar environments list`), or they would run on with nobody watching them. "
                    f"Then: openajar environments machine use {name}")
    was = settings.provider
    use_machine(_home(), name)
    typer.echo(f"New environments open on {name} (vms.toml: provider = \"own\", location = \"{name}\").")
    if was != "own":
        typer.echo(RESTART)


@machine_app.command("list")
def list_cmd(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Your machines, which one new environments open on, and how many are open on each."""
    from openajar.vms import own

    settings = _settings()
    current = own.default_name(_home(), settings) if settings.provider == "own" else None
    rows = [{"name": m.name, "address": f"{m.user}@{m.host}" + (f":{m.port}" if m.port != 22 else ""),
             "default": m.name == current, "system": m.system, "arch": m.arch, "cores": m.cores,
             "memory_gb": m.memory_gb, "docker": m.docker_version, "open": len(_live_on(m.name)),
             "saves": len(_saves_on(m.name)), "key": "yours" if m.key else "openajar's", "mode": m.mode}
            for m in own.every(_home())]
    if json_out:
        typer.echo(json.dumps({"machines": rows, "provider": settings.provider}, indent=2))
        return
    if not rows:
        typer.echo("No machine of your own is set up. Add one: openajar environments machine add NAME user@host")
        return
    for r in rows:
        size = f"{r['cores']} CPU, {r['memory_gb']:g} GB" if r["cores"] else "not checked yet"
        mark = "new environments open here" if r["default"] else ""
        held = (("in use" if r["open"] else "free") if r["mode"] == "whole"
                else f"{r['open']} open, {r['saves']} saved")
        typer.echo(f"{r['name']:<16} {r['address']:<32} {size:<18} {r['mode']:<11} {held}  {mark}".rstrip())
    if settings.provider != "own":
        where = {"docker": "in Docker on this computer"}.get(settings.provider, f"at {provider_name(settings.provider)}")
        typer.echo(f"New environments open {where} now. To open them on one of these: openajar environments "
                   "machine use NAME")


def _check_whole(m: Any, *, setup: bool, free: bool) -> None:
    from openajar.vms import own

    facts, wrong, _signs = _examine_whole(m)
    own.save(_home(), m)
    typer.echo(f"ssh reaches {m.user}@{m.host}.")
    _say_facts(facts, whole=True)
    if wrong:
        typer.echo(f"{m.name} cannot hold an environment now:", err=True)
        for w in wrong:
            typer.echo(f"  - {w}", err=True)
        raise typer.Exit(1)
    side = _docker(m)
    held = _run(side.held())
    if held is not None:
        vm = (held.get("labels") or {}).get("agentos-vm") or ""
        known = [r for r in _live_on(m.name) if r.id == vm]
        if known:
            typer.echo(f"An environment is open on it ({known[0].lane_id}).")
        elif free:
            _run(side.delete_server(str(held.get("id") or "")))
            typer.echo(f"{m.name} was held by an environment this openajar no longer has. It is free now, and what "
                       "that environment left in /work is gone.")
        else:
            typer.echo(f"{m.name} is held by an environment this openajar no longer has ({held.get('name') or '?'}). "
                       f"Free it, which empties its /work: openajar environments machine check {m.name} --free",
                       err=True)
            raise typer.Exit(1)
    else:
        typer.echo("No environment is open on it.")
    if setup and not _set_up(m):
        raise typer.Exit(1)
    typer.echo(f"{m.name} is ready.")


@machine_app.command("check")
def check(name: Annotated[str, typer.Argument(metavar="NAME")],
          image: Annotated[bool, typer.Option("--image", help="Put this openajar's image there when it is missing or "
                                              "older (after an upgrade of openajar). A whole machine: run its "
                                              "setup again.")] = False,
          build: Annotated[bool, typer.Option("--build", help="With --image: build it there instead of downloading "
                                              "it.")] = False,
          install_docker: Annotated[bool, typer.Option("--install-docker", help="When Docker is missing there: "
                                                       "install it with Docker's own script, as root.")] = False,
          free: Annotated[bool, typer.Option("--free", help="A whole machine held by an environment this openajar "
                                             "no longer has: end that hold and empty its /work.")] = False) -> None:
    """Look at a machine again: is it reachable, can it hold environments, and does it have the image."""
    from openajar.vms import own

    try:
        m = own.load(_home(), name)
    except OpenajarError as e:
        raise _fail(e) from None
    if m.mode == "whole":
        _check_whole(m, setup=image, free=free)
        return
    facts, wrong = _examine(m)
    if facts.system == "Linux" and not facts.docker_found and install_docker and _install_docker(m, facts, True):
        facts, wrong = _examine(m)
    typer.echo(f"ssh reaches {m.user}@{m.host}.")
    _say_facts(facts)
    if wrong:
        typer.echo(f"{name} cannot hold environments yet:", err=True)
        for w in wrong:
            typer.echo(f"  - {w}", err=True)
        raise typer.Exit(1)
    have = _run(_docker(m).has_image())
    if have is not None and (image or build):
        if not _get_image(m, build=build):
            raise typer.Exit(1)
    elif have is not None:
        typer.echo(f"The image is not ready there ({have}): openajar environments machine check {name} --image",
                   err=True)
        raise typer.Exit(1)
    else:
        typer.echo("The environments image is there.")
    found = _run(_docker(m).mine(_home_labels()))
    typer.echo(f"openajar has {len(found['containers'])} container{'s' if len(found['containers']) != 1 else ''} and "
               f"{len(found['images'])} saved image{'s' if len(found['images']) != 1 else ''} there.")
    typer.echo(f"{name} is ready.")


@machine_app.command("use")
def use_cmd(name: Annotated[str, typer.Argument(metavar="NAME")]) -> None:
    """Open new environments on this machine. Environments and saves on another of your machines stay
    there and keep working."""
    from openajar.vms import own

    try:
        own.load(_home(), name)
    except OpenajarError as e:
        raise _fail(e) from None
    _use(name)


@machine_app.command("remove")
def remove(name: Annotated[str, typer.Argument(metavar="NAME")],
           force: Annotated[bool, typer.Option("--force", help="Also when environments are open or saved there "
                                               "(their containers and saved images are deleted), or when the "
                                               "machine no longer answers.")] = False,
           keep_image: Annotated[bool, typer.Option("--keep-image", help="Leave the environments image in its "
                                                    "Docker.")] = False) -> None:
    """Take a machine out, and take away what openajar put there: its containers, networks and images,
    and its key's line in authorized_keys. Nothing else on the machine is touched, and it is never stopped.

    A whole machine: an environment still open on it is ended (--force) and its /work emptied, and
    openajar's key goes. What its setup installed stays (packages, Node, the browser under /opt)."""
    from openajar.vms import own

    home = _home()
    try:
        m = own.load(home, name)
    except OpenajarError as e:
        raise _fail(e) from None
    live, saves = _live_on(name), _saves_on(name)
    if (live or saves) and not force:
        what = []
        if live:
            what.append(f"{len(live)} environment{'s are' if len(live) != 1 else ' is'} open there")
        if saves:
            what.append(f"{len(saves)} save{'s keep their' if len(saves) != 1 else ' keeps its'} disk there")
        raise _fail(f"{' and '.join(what)}. Close and forget them first (`openajar environments list`, `openajar "
                    "environments saves`), or --force deletes them there: the saved files stay on this computer, so "
                    "a save still resumes from them on another machine")
    whole = m.mode == "whole"

    async def clean() -> dict | None:
        docker = _docker(m)
        try:
            found = await docker.remove_mine(_home_labels())
            if whole:
                await docker.forget()
            elif m.installed_image and not keep_image:
                source = own.image_source(_settings())
                await docker._run("rmi", docker.image, *([source] if source and source != docker.image else []),
                                  ok_codes=(0, 1))
            public = own.public_key(home, name)
            if m.installed_key and public:
                await own.run(home, m, "sh -s", stdin=own.remove_key_script(public).encode(), timeout_s=60)
            return found
        except OpenajarError as e:
            if not force:
                raise
            typer.echo(f"{name} could not be cleaned ({e.message}).", err=True)
            return None

    found = _run(clean())
    own.forget(home, name)
    key_words = (f"the line ending in openajar-machine-{name} in ~/.ssh/authorized_keys is its key"
                 if m.installed_key else "")
    if found is None and whole:
        typer.echo(f"Removed {name} from openajar. It was not reached, so nothing on it changed: /var/lib/"
                   "openajar-machine there is openajar's folder" + (f", and {key_words}." if key_words else "."))
    elif found is None:
        typer.echo(f"Removed {name} from openajar. What openajar made is still on it: when you reach it, `docker ps "
                   "-a --filter label=agentos-home` and `docker images --filter label=agentos-home` list it"
                   + (f", and {key_words}." if key_words else "."))
    elif whole:
        ended = " The environment open on it was ended and its /work emptied." if found["containers"] else ""
        typer.echo(f"Removed {name}." + ended + (" openajar's key is gone from it." if m.installed_key else "")
                   + " What the setup installed stays on it (packages, Node, the browser under /opt/agentos-browser); "
                   "install its system again for a machine as it was.")
    else:
        bits = [f"{len(found['containers'])} container{'s' if len(found['containers']) != 1 else ''}",
                f"{len(found['images'])} saved image{'s' if len(found['images']) != 1 else ''}"]
        if m.installed_image and not keep_image:
            bits.append("the environments image")
        if m.installed_key:
            bits.append("openajar's key")
        typer.echo(f"Removed {name}, and from it: {', '.join(bits)}. Nothing else there was touched."
                   + (" Docker, which openajar installed there when you said so, stays." if m.installed_docker else ""))
    settings = _settings()
    if settings.provider == "own":
        left = own.every(home)
        if not left:
            typer.echo("No machine is left, so agents cannot open an environment until you add one or choose "
                       "another provider (`openajar setup vms`).")
        elif own.default_name(home, settings) is None:
            typer.echo("Pick where new environments open: openajar environments machine use "
                       + " | ".join(x.name for x in left))
