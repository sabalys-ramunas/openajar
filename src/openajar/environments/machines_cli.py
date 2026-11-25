"""`openajar environments machine ...`: bring a machine of your own in as the computer of environments
(openajar.vms.own). The owner runs these at the node; no tool adds or removes a machine.

    openajar environments machine add homebox me@192.168.1.20     # register it, check it, put the image there
    openajar environments machine list
    openajar environments machine check homebox [--image]        # what is wrong, and the image again
    openajar environments machine use homebox                    # new environments open there
    openajar environments machine remove homebox                 # take away what openajar put there

No password is ever asked for by openajar or kept: `add` makes a key for the machine and installs it with
your own ssh (which may ask for your password itself), or uses a key file you name with --key.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.core import catalog
from openajar.core.config import home_from_env
from openajar.core.errors import InvalidRequest, OpenajarError

machine_app = typer.Typer(no_args_is_help=True, help=(
    "Bring your own machine: environments open as containers on a Linux box of yours, over ssh."))

_state: dict = {"home": None}

RESTART = ("Restart openajar so it uses it: `openajar local restart` on your own computer, `sudo openajar node "
           "restart` on a server node.")
ISOLATION = ("An environment there is a container: it shares that machine's kernel, and is walled off from the "
             "machine's files, the machine itself, its network and the other environments. That wall is weaker "
             "than a rented VM's, so use your own machine for code you trust.")


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
    from openajar.vms.own import MachineDocker

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


def _say_facts(f: Any) -> None:
    from openajar.vms.own import ARCHES

    cpu = {"x86": "x86-64", "arm": "arm64"}.get(ARCHES.get(f.arch, ""), f.arch or "?")
    docker = f"Docker {f.docker_version}" + (" (through sudo)" if f.docker[:1] == ["sudo"] else "") if f.docker else \
        "no Docker it can use"
    size = f", {f.cores} CPU, {f.memory_gb:g} GB" if f.cores else ""
    free = f", {f.free_gb:g} GB free for Docker" if f.free_gb is not None else ""
    typer.echo(f"  {f.system or '?'} on {cpu}{size}, {docker}{free}")


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


@machine_app.command("add")
def add(name: Annotated[str, typer.Argument(metavar="NAME", help="What you call it, like homebox.")],
        target: Annotated[str, typer.Argument(metavar="USER@HOST", help="How ssh reaches it, like me@192.168.1.20 or "
                                              "root@box.example.com:2222. A host from your ~/.ssh/config works too.")],
        key: Annotated[Path | None, typer.Option("--key", help="A private key file of yours that the machine already "
                                                 "takes (no passphrase). Default: openajar makes a key for this "
                                                 "machine and adds it there with your own ssh.")] = None,
        fingerprint: Annotated[str | None, typer.Option("--fingerprint", help="The machine's host key fingerprint "
                                                        "(SHA256:...), so nothing is asked.")] = None,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Pin the host key the machine shows without asking, "
                                          "and make this the machine new environments open on.")] = False,
        image: Annotated[bool, typer.Option("--image/--no-image", help="Put the environments image there now "
                                            "(about 4 GB).")] = True,
        build: Annotated[bool, typer.Option("--build", help="Build the image there instead of downloading it.")] = False,
        use: Annotated[bool | None, typer.Option("--use/--no-use", help="Open new environments on this machine "
                                                 "(vms.toml provider = \"own\"). Default: asked.")] = None) -> None:
    """Bring a machine of your own in: openajar checks it over ssh, puts the environments image in its
    Docker, and from then on agents' environments open there as containers.

    It needs Linux (x86-64 or arm64), Docker, and an ssh login that may run Docker. openajar puts there one
    line in that login's authorized_keys and the image, and later a container per environment. It never
    stops, restarts or wipes the machine; `machine remove` takes all of it away again."""
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

    own.save(home, m)
    facts, wrong = _examine(m)
    _say_facts(facts)
    if wrong:
        typer.echo(f"{name} is registered, but it cannot hold environments yet:", err=True)
        for w in wrong:
            typer.echo(f"  - {w}", err=True)
        typer.echo(f"When that is done: openajar environments machine check {name} --image", err=True)
        raise typer.Exit(1)

    have = _run(_docker(m).has_image())
    if have is not None and image:
        if not _get_image(m, build=build):
            typer.echo(f"{name} is registered without the image. Try again: openajar environments machine check "
                       f"{name} --image", err=True)
            raise typer.Exit(1)
    elif have is not None:
        typer.echo(f"The image is not there yet ({have}): openajar environments machine check {name} --image")
    else:
        typer.echo(f"The environments image is on {name} already.")

    _offer_use(name, use if use is not None else (True if yes else None))
    typer.echo(ISOLATION)


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
                settings.provider, f"at {settings.provider.capitalize()}")
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
                    f"{settings.provider.capitalize()} now, and openajar works with one provider at a time: close "
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
             "saves": len(_saves_on(m.name)), "key": "yours" if m.key else "openajar's"}
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
        typer.echo(f"{r['name']:<16} {r['address']:<32} {size:<18} {r['open']} open, {r['saves']} saved  {mark}".rstrip())
    if settings.provider != "own":
        where = {"docker": "in Docker on this computer"}.get(settings.provider, f"at {settings.provider.capitalize()}")
        typer.echo(f"New environments open {where} now. To open them on one of these: openajar environments "
                   "machine use NAME")


@machine_app.command("check")
def check(name: Annotated[str, typer.Argument(metavar="NAME")],
          image: Annotated[bool, typer.Option("--image", help="Put this openajar's image there when it is missing or "
                                              "older (after an upgrade of openajar).")] = False,
          build: Annotated[bool, typer.Option("--build", help="With --image: build it there instead of downloading "
                                              "it.")] = False) -> None:
    """Look at a machine again: is it reachable, can it hold environments, and does it have the image."""
    from openajar.vms import own

    try:
        m = own.load(_home(), name)
    except OpenajarError as e:
        raise _fail(e) from None
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
    and its key's line in authorized_keys. Nothing else on the machine is touched, and it is never stopped."""
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

    async def clean() -> dict | None:
        docker = _docker(m)
        try:
            found = await docker.remove_mine(_home_labels())
            if m.installed_image and not keep_image:
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
    if found is None:
        typer.echo(f"Removed {name} from openajar. What openajar made is still on it: when you reach it, `docker ps "
                   "-a --filter label=agentos-home` and `docker images --filter label=agentos-home` list it"
                   + (f", and the line ending in openajar-machine-{name} in ~/.ssh/authorized_keys is its key."
                      if m.installed_key else "."))
    else:
        bits = [f"{len(found['containers'])} container{'s' if len(found['containers']) != 1 else ''}",
                f"{len(found['images'])} saved image{'s' if len(found['images']) != 1 else ''}"]
        if m.installed_image and not keep_image:
            bits.append("the environments image")
        if m.installed_key:
            bits.append("openajar's key")
        typer.echo(f"Removed {name}, and from it: {', '.join(bits)}. Nothing else there was touched.")
    settings = _settings()
    if settings.provider == "own":
        left = own.every(home)
        if not left:
            typer.echo("No machine is left, so agents cannot open an environment until you add one or choose "
                       "another provider (`openajar setup vms`).")
        elif own.default_name(home, settings) is None:
            typer.echo("Pick where new environments open: openajar environments machine use "
                       + " | ".join(x.name for x in left))
