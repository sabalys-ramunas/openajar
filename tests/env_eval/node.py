"""One task's node: openajar's local node with the docker provider, in a scratch home, and its cleanup.

`EvalNode` is a context manager. On the way in it:

- tags the docker provider's image (vms.toml `docker_image`, default agentos-env:latest) as
  `openajar-eval-<id>:base` and points vms.toml at that tag, so every save, snapshot and prepared
  machine this node makes is an image of that repository alone (and nothing is pulled or built);
- writes vms.toml (docker, never a cloud), environments.toml (the task's settings), the fixture
  repositories as local bare repositories declared through the git gateway as
  `github.com/evals/<name>` (`openajar git remote allow env ... --url <path>`, as an owner does),
  and one token for `github.com/evals/*` in the vault (a made-up one: the gateway pushes to a local
  path, and the GitHub stand-in takes any token);
- prepares the home and an agent grant (node_main.py prepare), then starts the node
  (node_main.py serve) with OPENAJAR_GIT_ALLOW_FILE=1 on its port.

On the way out, always (an error, Ctrl-C), it stops the node and removes every container and
network labelled with this node's name, every image of its repository, and its scratch home
(unless `keep`). Nothing it does reaches a real repository or a cloud.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

REPO = Path(__file__).resolve().parents[2]
NODE_MAIN = Path(__file__).with_name("node_main.py")
FIXTURES = Path(__file__).with_name("repos")
# the machine image a run starts from; OPENAJAR_EVAL_IMAGE names another (a build of this tree under its own tag)
BASE_IMAGE = os.environ.get("OPENAJAR_EVAL_IMAGE", "agentos-env:latest")
HOST, OWNER = "github.com", "evals"
GIT_ENV = {"HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Eval Fixture",
           "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Eval Fixture",
           "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
START_TIMEOUT_S = 90


class NodeFailed(RuntimeError):
    pass


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    env = {"PATH": os.environ["PATH"], **GIT_ENV}
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise NodeFailed(f"git {' '.join(args)}: {done.stderr.strip()[-400:]}")
    return done.stdout


def docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def docker_ready(image: str = BASE_IMAGE) -> str | None:
    """None when docker answers and has `image`; else what is missing, with the fix."""
    if shutil.which("docker") is None:
        return "docker is not installed"
    try:
        done = docker("image", "inspect", "--format", "{{.Id}}", image, timeout=30)
    except subprocess.TimeoutExpired:
        return "docker does not answer (is Docker Desktop running?)"
    if done.returncode != 0:
        if "Cannot connect" in done.stderr or "daemon" in done.stderr:
            return "docker does not answer (is Docker Desktop running?)"
        return f"docker has no {image}: run `uv run openajar environments setup --pull` once"
    return None


@dataclass
class Repo:
    name: str          # shop
    bare: Path         # <root>/repos/shop.git

    @property
    def slug(self) -> str:
        return f"{HOST}/{OWNER}/{self.name}"

    @property
    def full_name(self) -> str:
        return f"{OWNER}/{self.name}"


class EvalNode:
    def __init__(self, root: Path, *, port: int, github_api: str, settings: dict[str, Any] | None = None,
                 src: Path | None = None, keep: bool = False):
        self.root = Path(root).resolve()
        self.port = port
        self.github_api = github_api
        self.settings = dict(settings or {})
        self.src = src                      # an openajar checkout's src/ to run instead of this one's
        self.keep = keep
        self.id = secrets.token_hex(4)
        self.name = f"eval-{self.id}"
        self.image_repo = f"openajar-eval-{self.id}"
        self.home = root / "home"
        self.repos: dict[str, Repo] = {}
        self.token = ""                     # the agent grant's
        self.owner_token = ""               # the node's own (the owner): the harness's checks only
        self.proc: subprocess.Popen | None = None
        self.log = root / "node.log"

    # ---- the way in ----

    def __enter__(self) -> Self:
        try:
            self._start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tagged = docker("tag", BASE_IMAGE, f"{self.image_repo}:base")
        if tagged.returncode != 0:
            raise NodeFailed(f"could not tag {BASE_IMAGE}: {tagged.stderr.strip()[-300:]}")
        self._python("prepare", str(self.home), str(self.port), self.name, capture=True)
        self.token = json.loads(self._last_out)["token"]
        self.owner_token = self._config()["mcp_token"]
        self._write_settings()
        self.owner("git", "key", "add", "--host", HOST, "--repo", f"{OWNER}/*", "--token", "--stdin",
                   "--name", "evals-token", stdin="ghp_" + "e" * 36 + "\n")

    def add_repo(self, name: str, files: Path | None = None) -> Repo:
        """A fixture repository: <files> (default repos/<name>/) committed on main in a new bare
        repository, declared for environments as github.com/evals/<name>."""
        files = files or FIXTURES / name
        bare = self.root / "repos" / f"{name}.git"
        seed = self.root / "repos" / f"{name}-seed"
        shutil.rmtree(bare, ignore_errors=True)
        shutil.rmtree(seed, ignore_errors=True)
        shutil.copytree(files, seed)
        git("init", "-q", "--bare", "-b", "main", str(bare))
        git("init", "-q", "-b", "main", cwd=seed)
        git("add", "-A", cwd=seed)
        git("commit", "-qm", "The fixture as it starts", cwd=seed)
        git("push", "-q", str(bare), "main", cwd=seed)
        shutil.rmtree(seed, ignore_errors=True)
        repo = Repo(name, bare)
        self.owner("git", "remote", "allow", "env", repo.slug, "--url", str(bare))
        self.repos[name] = repo
        return repo

    def serve(self) -> None:
        """Start the node (after the repositories are added) and wait until MCP listens."""
        env = {**self._env(), "OPENAJAR_GIT_ALLOW_FILE": "1"}
        out = self.log.open("w")
        self.proc = subprocess.Popen([sys.executable, "-P", str(NODE_MAIN), "serve", str(self.home), str(self.port),
                                      self.github_api], env=env, stdout=out, stderr=subprocess.STDOUT, cwd=self.root,
                                     start_new_session=True)
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if "READY " in self.log.read_text(errors="replace"):
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        raise NodeFailed(f"the node did not start; see {self.log}")

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def _write_settings(self) -> None:
        import tomli_w

        vms = {"provider": "docker", "docker_image": f"{self.image_repo}:base", "docker_pull": "",
               "ssh_from": ["127.0.0.1/32"], "max_live": 4}
        (self.home / "vms.toml").write_text(tomli_w.dumps(vms))
        envs = {"browser": "vm", "max_live": 3, **self.settings}
        (self.home / "environments.toml").write_text(tomli_w.dumps(envs))

    def set_settings(self, **changes: Any) -> None:
        """Change environments.toml on the running node (what the node reads again, like warm)."""
        import tomli_w

        self.settings.update(changes)
        (self.home / "environments.toml").write_text(tomli_w.dumps({"browser": "vm", "max_live": 3,
                                                                     **self.settings}))

    def _config(self) -> dict[str, Any]:
        import tomllib

        return tomllib.loads((self.home / "node.toml").read_text())

    def _env(self) -> dict[str, str]:
        env = {**os.environ, "OPENAJAR_HOME": str(self.home)}
        env.pop("OPENAJAR_URL", None)
        env.pop("OPENAJAR_TOKEN", None)
        if self.src is not None:
            env["PYTHONPATH"] = str(self.src) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return env

    def _python(self, *args: str, capture: bool = False) -> None:
        done = subprocess.run([sys.executable, "-P", str(NODE_MAIN), *args], env=self._env(), capture_output=True,
                              text=True, timeout=120, cwd=self.root, check=False)
        if done.returncode != 0:
            raise NodeFailed(f"node_main {args[0]} failed: {done.stderr.strip()[-600:]}")
        self._last_out = done.stdout.strip().splitlines()[-1] if capture else ""

    def owner(self, *args: str, stdin: str | None = None, timeout: float = 120) -> str:
        """An owner's `openajar ...` command on this node's home (never the user's ~/.openajar)."""
        argv = [sys.executable, "-P", "-c", "from openajar.cli import app; app()", *args]
        done = subprocess.run(argv, env=self._env(), input=stdin, capture_output=True, text=True, timeout=timeout,
                              cwd=self.root, check=False)
        if done.returncode != 0:
            raise NodeFailed(f"openajar {' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()[-400:]}")
        return done.stdout

    # ---- reading the end state ----

    def environments(self) -> list[Any]:
        """Every environment this node opened, newest first (the node's own records)."""
        from openajar.environments.store import EnvironmentStore

        db = self.home / "environments.db"
        if not db.exists():
            return []
        store = EnvironmentStore(db)
        try:
            return store.recent(200)
        finally:
            with contextlib.suppress(Exception):
                store.close()

    def prepared(self) -> list[Any]:
        from openajar.environments.prepared import PreparedStore

        return PreparedStore(self.home).all()

    def vms(self) -> list[Any]:
        """Every machine this node rented (openajar.vms records), newest first."""
        from openajar.vms.store import VmStore

        db = self.home / "vms.db"
        return VmStore(db).recent(200) if db.exists() else []

    def machines_left(self, env_ids: set[str]) -> list[str]:
        """The containers of these environments that still exist (their VM records' provider ids)."""
        left = []
        for rec in self.vms():
            if (rec.environment in env_ids and rec.provider_id
                    and docker("container", "inspect", "--format", "{{.Id}}", rec.provider_id).returncode == 0):
                left.append(rec.provider_id[:12])
        return left

    def exec_in(self, env_id: str, cmd: str) -> tuple[int, str]:
        """`cmd` in the container of environment `env_id`, as root: (exit code, stdout); (-1, "") without one."""
        rec = next((r for r in self.vms() if r.environment == env_id and r.provider_id), None)
        if rec is None:
            return -1, ""
        done = docker("exec", rec.provider_id, "sh", "-c", cmd)
        return done.returncode, done.stdout

    def kill(self, env_id: str) -> str | None:
        """Lose the container of environment `env_id` as a lost host would (`docker rm -f`: no shutdown, and its
        disk goes with it): its id, or None when it has none. The harness's own step (t12's dying machine), never
        the agent's. A container that only stopped (`docker kill`) is no longer a dead machine: the node starts it
        again with its disk (the 0.7.0 audit, S1-04)."""
        rec = next((r for r in self.vms() if r.environment == env_id and r.provider_id), None)
        if rec is None or docker("rm", "-f", rec.provider_id).returncode != 0:
            return None
        return rec.provider_id[:12]

    def warm_ready(self) -> bool:
        """A warm machine is booted and waiting (openajar.environments.warmpool)."""
        return any(str(r.lane_id).startswith("env/warm-") and r.state == "ready" for r in self.vms())

    def containers(self, *, running_only: bool = False) -> list[dict[str, str]]:
        """This node's containers: id, name, state, and the openajar labels."""
        args = ["ps", "--format", "{{json .}}", "--filter", f"label=agentos-node={self.name}"]
        if not running_only:
            args.insert(1, "-a")
        done = docker(*args)
        rows = []
        for line in done.stdout.splitlines():
            with contextlib.suppress(ValueError):
                d = json.loads(line)
                rows.append({"id": d.get("ID", ""), "name": d.get("Names", ""), "state": d.get("State", ""),
                             "labels": d.get("Labels", "")})
        return rows

    def proofs(self, env_id: str) -> list[dict[str, Any]]:
        folder = self.home / "environments" / env_id / "proofs"
        out = []
        for f in sorted(folder.glob("*.json")) if folder.exists() else []:
            with contextlib.suppress(ValueError, OSError):
                png, mp4 = f.with_suffix(".png"), f.with_suffix(".mp4")  # a screenshot; a recording
                out.append({**json.loads(f.read_text()), "png_bytes": png.stat().st_size if png.exists() else 0,
                            "mp4_bytes": mp4.stat().st_size if mp4.exists() else 0})
        return out

    # ---- the way out ----

    def __exit__(self, *exc: object) -> None:
        try:
            self._stop_node()
        finally:
            self.cleanup_docker()
            if not self.keep:
                shutil.rmtree(self.root / "home", ignore_errors=True)
                shutil.rmtree(self.root / "repos", ignore_errors=True)

    def _stop_node(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(10)

    def cleanup_docker(self) -> dict[str, int]:
        """Remove every container, network and image this node made. Safe to call twice."""
        return sweep(f"label=agentos-node={self.name}", self.image_repo)


def sweep(label_filter: str | None, image_repo: str | None) -> dict[str, int]:
    """Containers and networks matching `label_filter`, and every tag of `image_repo` (a prefix ending
    in '*' matches several repositories)."""
    removed = {"containers": 0, "networks": 0, "images": 0}
    if label_filter:
        ids = docker("ps", "-aq", "--filter", label_filter).stdout.split()
        if ids:
            docker("rm", "-f", *ids, timeout=300)
            removed["containers"] = len(ids)
        nets = docker("network", "ls", "-q", "--filter", label_filter).stdout.split()
        for n in nets:
            if docker("network", "rm", n).returncode == 0:
                removed["networks"] += 1
    if image_repo:
        tags = [t for t in docker("images", "--format", "{{.Repository}}:{{.Tag}}").stdout.split()
                if _matches(t.rsplit(":", 1)[0], image_repo)]
        for t in tags:
            if docker("rmi", "-f", t, timeout=300).returncode == 0:
                removed["images"] += 1
    return removed


def _matches(repo: str, pattern: str) -> bool:
    return repo.startswith(pattern[:-1]) if pattern.endswith("*") else repo == pattern


def sweep_stale() -> dict[str, int]:
    """What earlier runs left behind (a killed run): containers and networks whose openajar-node label
    starts with eval-, and images of openajar-eval-* repositories. Nothing else is touched."""
    removed = {"containers": 0, "networks": 0, "images": 0}
    for kind in ("ps", "network"):
        args = ["ps", "-a"] if kind == "ps" else ["network", "ls"]
        out = docker(*args, "--filter", "label=agentos-node", "--format", '{{.ID}} {{.Label "agentos-node"}}').stdout
        for line in out.splitlines():
            ident, _, node = line.partition(" ")
            if node.startswith("eval-"):
                if kind == "ps":
                    docker("rm", "-f", ident, timeout=300)
                    removed["containers"] += 1
                elif docker("network", "rm", ident).returncode == 0:
                    removed["networks"] += 1
    removed["images"] = sweep(None, "openajar-eval-*")["images"]
    return removed
