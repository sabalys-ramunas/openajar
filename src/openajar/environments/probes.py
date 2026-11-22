"""Short commands that ask an environment's VM about itself (its listening ports, its repositories
and their unsaved work), what they print read back, and how long such a check may take."""

from __future__ import annotations

import re

CHECK_TIMEOUT_S = 30.0  # one short command on the VM (a probe, mkdir, the setup state)

# One command over SSH that says, after its repositories, the files under /work that are in no git
# repository (`loose=`, at most 50, and `loose_count=`), past dependencies and caches: on a machine with
# no repository they are the work, and close must not delete them unseen. A file whose content is older
# than the file itself (mtime well before ctime: an upload or a restore put it there as it was) is a copy
# of something the agent has elsewhere, so it does not count; one written on the machine does.
#
# One command over SSH that says, for every repository under /work (four folders down, past
# node_modules and .venv), its branch, the commits on any local branch (or a detached HEAD) that no
# remote has, its stashes, and the files git status lists, untracked ones included. That is the
# work close and the reaper never delete without discard. A repository git cannot read says
# `error=`, which counts as unsaved. safe.directory: one made by another user still answers. Its
# checkpoints (openajar.environments.checkpoints) come along as `checkpoint=` lines, at no call of their own.
GIT_STATE = r"""
find /work -maxdepth 4 \( -name node_modules -o -name .venv \) -prune -o -name .git -print -prune 2>/dev/null | sort |
while IFS= read -r gitdir; do
  d=$(dirname "$gitdir")
  g() { git -c safe.directory='*' -C "$d" "$@"; }
  echo "repo=$d"
  if ! g status --porcelain=v1 >/dev/null 2>&1; then echo "error=git cannot read it"; continue; fi
  echo "branch=$(g rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "ahead=$(g rev-list --count HEAD --branches --not --remotes 2>/dev/null \
    || g rev-list --count --branches --not --remotes 2>/dev/null || echo 0)"
  echo "dirty_count=$(g status --porcelain=v1 --untracked-files=normal | wc -l | tr -d ' ')"
  g status --porcelain=v1 --untracked-files=normal | head -n 50 | sed 's/^/dirty=/'
  g diff HEAD --numstat 2>/dev/null | awk -F'\t' '$1 == "0" && $2 == "0" { print "samecontent=" $3 }' | head -n 50
  g diff HEAD --summary 2>/dev/null | sed -n 's/^ mode change [0-7]* => [0-7]* /modechange=/p' | head -n 50
  { g log --format='unpushed=%h %s' HEAD --branches --not --remotes 2>/dev/null \
    || g log --format='unpushed=%h %s' --branches --not --remotes 2>/dev/null; } | head -n 50
  echo "stash_count=$(g stash list 2>/dev/null | wc -l | tr -d ' ')"
  g stash list --format='stash=%gd %s' 2>/dev/null | head -n 20
  g for-each-ref --format='checkpoint=%(refname:lstrip=3)%09%(parent)%09%(committerdate:iso-strict)%09%(subject)' \
    refs/agentos/checkpoints 2>/dev/null | head -n 40
done
""" + r"""
{ find /work -maxdepth 4 \( -name node_modules -o -name .venv \) -prune -o -name .git -print -prune 2>/dev/null |
    sed 's|/\.git$||; s|^|R |'
  find /work \( -name node_modules -o -name .venv -o -name .git -o -name __pycache__ -o -name .pytest_cache \
    -o -name .mypy_cache -o -name .ruff_cache \) -prune -o -type f -printf 'F %T@ %C@ %p\n' 2>/dev/null; } |
awk -v max=50 '
  $1 == "R" { r[++n] = substr($0, 3); next }
  $1 == "F" {
    p = substr($0, length($1 " " $2 " " $3 " ") + 1)
    for (i = 1; i <= n; i++) if (index(p, r[i] "/") == 1) next
    if ($2 + 2 < $3) next
    c++; sub("^/work/", "", p); if (c <= max) print "loose=" p
  }
  END { print "loose_count=" c + 0 }'
"""


def parse_ports(text: str) -> list[dict]:
    """`ss -ltnpH` lines -> [{port, process}], each port once, without the node's own (ssh, the
    gateway's reverse tunnel)."""
    out: dict[int, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] != "LISTEN":
            continue
        local = parts[3]
        try:
            port = int(local.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        m = re.search(r'users:\(\("([^"]+)"', line)
        proc = m.group(1) if m else None
        if proc in ("sshd", "sshd-session") or port in (22, 3129):
            continue
        out.setdefault(port, {"port": port, "process": proc, "link": None})
    return [out[p] for p in sorted(out)]


def _loose(files: list[str], count: int) -> dict:
    """The files in no repository, as one more entry of parse_git_state (`loose`: True, dir /work)."""
    return {"dir": "/work", "loose": True, "branch": None, "ahead": 0, "dirty": count, "dirty_files": files,
            "unpushed": [], "stash_count": 0, "stashes": [], "error": None}


def parse_git_state(text: str) -> list[dict]:
    """Each repository under /work, and last, when there are any, the files in none (`loose`: True)."""
    repos: list[dict] = []
    cur: dict | None = None
    loose: list[str] = []
    loose_count = 0
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "loose":
            loose.append(value)
            continue
        if key == "loose_count":
            loose_count = int(value) if value.isdigit() else len(loose)
            cur = None
            continue
        if key == "repo":
            cur = {"dir": value, "branch": None, "ahead": 0, "dirty": 0, "dirty_files": [], "unpushed": [],
                   "stash_count": 0, "stashes": [], "error": None}
            repos.append(cur)
        elif cur is None:
            continue
        elif key == "branch":
            cur["branch"] = value or None
        elif key in ("ahead", "dirty_count", "stash_count"):
            cur["dirty" if key == "dirty_count" else key] = int(value) if value.isdigit() else 0
        elif key == "dirty":
            cur["dirty_files"].append(value[3:] if len(value) > 3 else value)
        elif key == "unpushed":
            cur["unpushed"].append(value)
        elif key == "stash":
            cur["stashes"].append(value)
        elif key == "checkpoint":
            cur.setdefault("checkpoints", []).append(value)
        elif key == "error":
            cur["error"] = value or "git cannot read it"
        elif key in ("samecontent", "modechange"):
            cur.setdefault("_" + key, []).append(value)
    for r in repos:  # a mode change (chmod +x) with the content as committed: `mode_only`, only when there are any
        same, modes = r.pop("_samecontent", []), r.pop("_modechange", [])
        if only := [f for f in modes if f in same]:
            r["mode_only"] = only
    if loose or loose_count:
        repos.append(_loose(loose, max(loose_count, len(loose))))
    return repos


def main_repo(repos: list[dict] | None, workdir: str) -> dict | None:
    """The repository an environment was opened with (its workdir), else the first one."""
    repos = [r for r in repos or [] if not r.get("loose")]
    if not repos:
        return None
    return next((r for r in repos if r["dir"] == workdir), repos[0])


# The machine's load and memory, for a call that was slow or stuck there (openajar.node.lanes slow_hints).
LOAD = "cat /proc/loadavg; nproc; awk '/^MemTotal:|^MemAvailable:/ {print $2}' /proc/meminfo"
OVERLOADED_PER_CPU = 1.5    # a load above this many per CPU: the machine is overloaded
LOW_MEMORY = 0.1            # less than this share of memory available: it is short of memory


def load_words(stdout: str, size: str | None, bigger: str | None) -> str | None:
    """`cat /proc/loadavg; nproc; MemTotal and MemAvailable in kB` as words for the agent, with a bigger
    size when the machine is overloaded. None when the output is not that."""
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    try:
        load1 = float(lines[0].split()[0])
        cpus = max(1, int(lines[1]))
        total_kb, avail_kb = int(lines[2]), int(lines[3])
    except (IndexError, ValueError):
        return None
    used_gb, total_gb = (total_kb - avail_kb) / 2**20, total_kb / 2**20
    facts = f"load {load1:.1f} on {cpus} CPU{'s' if cpus != 1 else ''}, memory {used_gb:.1f} of {total_gb:.1f} GB used"
    over = load1 / cpus >= OVERLOADED_PER_CPU or (total_kb and avail_kb / total_kb < LOW_MEMORY)
    if not over:
        return f"The machine is not overloaded ({facts})."
    fix = ("Stop what you do not need (terminal_exec \"ps aux --sort=-%cpu | head\" shows it; terminal_job_cancel ends a job)")
    if bigger:
        fix += (f", or move to a bigger machine: push your work, environment_close(), then environment_open with "
                f"the same title and size=\"{bigger}\"")
    return f"The machine is overloaded ({facts}{', size ' + size if size else ''}). {fix}."
