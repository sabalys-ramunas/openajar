"""The bash scripts a save and a restore run on an environment's machine (openajar.environments.saving),
over its SSH, with their input on stdin. Every path in them is relative to the machine's root (the
first argument), so the same script runs on a VM ("/") and in tests (a folder).

SAVE writes a save's file part into a staging folder and prints what it found, one `key=value` line
each: the repositories under /work with their branch, HEAD, stashes and the commits no remote has (a
git bundle each, checked with `git bundle verify`), the changed and new files git does not ignore,
staged or not, and every other file on the disk that `git status` lists (never .env files or node_modules,
never anything outside /work and the keep_paths), the deleted paths (only those not on the disk), each
repository's `git status` paths (r<n>.status, which the node checks the file part against:
openajar.environments.savecheck), the docker containers that run, and a sha256 of each file it wrote. A line `error=...` and a non-zero exit say what failed (exit 7: the file part is past the size cap).
"""

from __future__ import annotations

from openajar.environments.machine import SWAP_OFF, UPGRADE_WAIT

# What listens on the machine (round 3, T2: the node had lost track of a dev server's job when it
# restarted, so the save recorded no services and the resume started none): each TCP port that
# listens, from a process whose folder is under /work, with that process and its parents up to 10
# deep, one `proc=<port> <depth> <cwd, base64> <argv, NUL-separated, base64>` line each (depth 0 is
# the listener). openajar.environments.comeback.listening reads the command that was started from it.
# Part of SAVE, so it costs no call of its own.
LISTENING = r"""
if command -v ss >/dev/null 2>&1; then
  ss -ltnpH 2>/dev/null | awk '{n=split($4,a,":"); if (match($0,/pid=[0-9]+/)) print a[n], substr($0,RSTART+4,RLENGTH-4)}' \
    | sort -un | head -n 20 | while read -r port pid; do
    c=$(readlink "/proc/$pid/cwd" 2>/dev/null) || continue
    case "$c" in "/${W#/}"|"/${W#/}"/*) ;; *) continue ;; esac
    p=$pid; d=0
    while [ -n "$p" ] && [ "$p" -gt 1 ] && [ "$d" -lt 10 ] && [ -r "/proc/$p/cmdline" ]; do
      echo "proc=$port $d $(readlink "/proc/$p/cwd" 2>/dev/null | tr -d '\n' | base64 -w0) $(head -c 4096 "/proc/$p/cmdline" | base64 -w0)"
      p=$(sed -n 's/^PPid:[[:space:]]*//p' "/proc/$p/status" 2>/dev/null); d=$((d + 1))
    done
  done
fi
"""

SAVE = r"""
set -u
ROOT=$1; S=$2; W=$3; CAP=$4; KEEP_CAP_KB=$5; shift 5
KEEP=$*
umask 077
cd "$ROOT" || { echo "error=the machine has no folder $ROOT"; exit 3; }
case "$S" in /*) ;; *) S="${PWD%/}/$S";; esac
rm -rf "$S" && mkdir -p "$S" || { echo "error=could not make the save folder (is the disk full?)"; exit 3; }
LIST="$S/files.list"
: > "$LIST" || { echo "error=could not write to the disk (is it full?)"; exit 3; }
i=0
find "$W" -maxdepth 4 \( -name node_modules -o -name .venv \) -prune -o -name .git -print -prune 2>/dev/null \
  | sort > "$S/repos.txt"
while IFS= read -r gitdir; do
  D=${gitdir%/.git}; i=$((i+1)); R=r$i
  gx() { git -c safe.directory='*' -C "$D" "$@"; }
  echo "repo=$R $D"
  if ! gx status --porcelain >/dev/null 2>&1; then echo "error=git cannot read /$D"; exit 4; fi
  echo "branch=$R $(gx rev-parse --abbrev-ref HEAD 2>/dev/null)"
  H=$(gx rev-parse -q --verify HEAD 2>/dev/null) && echo "head=$R $H" || H=
  n=0
  gx stash list --format='%H %gs' 2>/dev/null > "$S/$R.stashes"
  while IFS=' ' read -r s msg; do
    n=$((n+1)); echo "stash=$R $s $msg"; gx update-ref "refs/agentos-save/stash-$n" "$s"
  done < "$S/$R.stashes"
  rm -f "$S/$R.stashes"
  if [ -n "$H" ]; then set -- --branches --glob=refs/agentos-save HEAD; else set -- --branches --glob=refs/agentos-save; fi
  c=$(gx rev-list --count "$@" --not --remotes 2>/dev/null || echo 0)
  if [ -n "$H" ]; then own=$(gx rev-list --count --branches HEAD --not --remotes 2>/dev/null || echo 0)
  else own=$(gx rev-list --count --branches --not --remotes 2>/dev/null || echo 0); fi
  if [ "${c:-0}" -gt 0 ]; then
    if ! gx bundle create "$S/$R.bundle" "$@" --not --remotes >/dev/null 2>"$S/$R.err"; then
      echo "error=could not bundle the commits of /$D: $(tail -n 1 "$S/$R.err")"; exit 5
    fi
    gx bundle verify "$S/$R.bundle" >/dev/null 2>>"$S/$R.err" || { echo "error=the bundle of /$D does not verify"; exit 5; }
    echo "commits=$R ${own:-0}"
  fi
  rm -f "$S/$R.err"
  for ref in $(gx for-each-ref --format='%(refname)' refs/agentos-save); do gx update-ref -d "$ref"; done
  # what differs from HEAD, staged or not, and what git does not track yet (the 0.7.0 audit, S1-02: ls-files -m
  # left out what was staged with git add); with no commit yet, everything in the index
  if [ -n "$H" ]; then
    gx diff --name-only -z --no-renames --diff-filter=D HEAD > "$S/$R.gone" \
      || { echo "error=could not write to the disk (is it full?)"; exit 3; }
    { gx diff --name-only -z --no-renames --diff-filter=d HEAD; gx ls-files -z -o --exclude-standard; }
  else
    gx ls-files -z -d > "$S/$R.gone" || { echo "error=could not write to the disk (is it full?)"; exit 3; }
    gx ls-files -z -c -o --exclude-standard
  fi | while IFS= read -r -d '' f; do
    p="$D/$f"
    [ -e "$p" ] || [ -L "$p" ] || continue
    case "/$f" in */node_modules/*|*/.env|*/.env.*) continue;; esac
    printf '%s\0' "$p"
  done >> "$LIST"
  # deleted is only what is not on the disk: `git rm --cached` leaves the file there, and a resume that deleted
  # it would lose it (it is in the file part instead)
  while IFS= read -r -d '' f; do
    [ -e "$D/$f" ] || [ -L "$D/$f" ] || printf '%s\0' "$f"
  done < "$S/$R.gone" > "$S/$R.deleted" || { echo "error=could not write to the disk (is it full?)"; exit 3; }
  rm -f "$S/$R.gone"
  # each path git status lists, c (on the disk) or d (not): the node checks that the file part holds them. Each
  # file on the disk goes into the file part too (a path untracked and ignored after `git rm --cached`, a staged
  # change the work tree took back), so the file part holds what git reports
  gx status --porcelain=v1 -z --untracked-files=all --no-renames 2>/dev/null | while IFS= read -r -d '' e; do
    f=${e:3}; p="$D/$f"
    case "/$f" in */node_modules/*|*/.env|*/.env.*) continue;; esac
    if [ -e "$p" ] || [ -L "$p" ]; then
      printf 'c%s\0' "$f"
      if [ -f "$p" ] || [ -L "$p" ]; then printf '%s\0' "$p" >> "$LIST"; fi  # status-list
    else printf 'd%s\0' "$f"; fi
  done > "$S/$R.status" || { echo "error=could not write to the disk (is it full?)"; exit 3; }
done < "$S/repos.txt"
find "$W" -mindepth 1 \( -name node_modules -o -name .venv -o -name __pycache__ -o -name .git \) -prune -o \
  -type d -exec test -e '{}/.git' \; -prune -o \( -type f -o -type l \) ! -name .env ! -name '.env.*' -print0 \
  2>/dev/null >> "$LIST"
KB=0
for k in $KEEP; do
  [ -e "$k" ] || { echo "keep_missing=/$k"; continue; }
  kb=$(du -sk "$k" 2>/dev/null | cut -f1)
  if [ $((KB + ${kb:-0})) -gt "$KEEP_CAP_KB" ]; then echo "keep_skipped=/$k ${kb:-0}"; continue; fi
  KB=$((KB + ${kb:-0})); printf '%s\0' "$k" >> "$LIST"; echo "keep=/$k"
done
if [ -s "$LIST" ] && sort -zu "$LIST" > "$LIST.u" 2>/dev/null; then mv -f "$LIST.u" "$LIST"; else rm -f "$LIST.u"; fi
echo "files=$(tr -cd '\000' < "$LIST" | wc -c | tr -d ' ')"
if [ -s "$LIST" ]; then
  tar -czf "$S/files.tgz" --null -T "$LIST" 2>"$S/tar.err"
else
  tar -czf "$S/files.tgz" -T /dev/null 2>"$S/tar.err"
fi
rc=$?
if [ "$rc" -gt 1 ] || [ ! -s "$S/files.tgz" ]; then echo "error=could not pack the files: $(tail -n 1 "$S/tar.err")"; exit 6; fi
rm -f "$LIST" "$S/repos.txt" "$S/tar.err"
size=$(wc -c < "$S/files.tgz" | tr -d ' ')
echo "bytes=$size"
if [ "$size" -gt "$CAP" ]; then echo "error=the files come to $size bytes, past the cap of $CAP"; exit 7; fi
if command -v docker >/dev/null 2>&1; then docker ps -q 2>/dev/null | sed 's/^/container=/'; fi
""" + LISTENING.strip("\n") + r"""
cd "$S" || exit 3
for f in *; do
  [ -f "$f" ] || continue
  h=$( (sha256sum "$f" 2>/dev/null || shasum -a 256 "$f") | cut -d' ' -f1)
  echo "sum=$h $f"
done
echo "ok=1"
"""

# One repository back: its saved commits and stashes from the bundle, its branch at its HEAD, and
# the paths it had deleted. Args: root, the repository, the bundle ("" for none), the branch, HEAD,
# the file of deleted paths (NUL-separated; "" for none), then each stash, oldest first, as its commit
# and its message. (stdin is the script itself: `bash -s`.)
RESTORE_REPO = r"""
set -u
ROOT=$1; D=$2; B=$3; BR=$4; H=$5; DEL=$6; shift 6
cd "$ROOT" || { echo "error=the machine has no folder $ROOT"; exit 3; }
case "$B" in /*|"") ;; *) B="${PWD%/}/$B";; esac
case "$DEL" in /*|"") ;; *) DEL="${PWD%/}/$DEL";; esac
gx() { git -c safe.directory='*' -C "$D" "$@"; }
if [ ! -d "$D/.git" ]; then mkdir -p "$D" && git init -q "$D" || { echo "error=could not make /$D"; exit 3; }; fi
before=$(gx rev-parse -q --verify HEAD 2>/dev/null || true)
if [ -n "$B" ]; then
  gx fetch -q --update-head-ok "$B" '+refs/heads/*:refs/heads/*' 2>/dev/null || true
  gx fetch -q "$B" '+refs/agentos-save/*:refs/agentos-restore/*' 2>/dev/null || true
  gx fetch -q "$B" HEAD 2>/dev/null || true
fi
if [ -n "$H" ] && [ "$before" != "$H" ]; then
  gx cat-file -e "$H^{commit}" 2>/dev/null || gx fetch -q origin "$H" 2>/dev/null || true
  if gx cat-file -e "$H^{commit}" 2>/dev/null; then
    if [ -n "$BR" ] && [ "$BR" != HEAD ]; then gx checkout -q -f -B "$BR" "$H"; else gx checkout -q -f --detach "$H"; fi \
      && echo "head=ok" || echo "missing=the checkout of $BR at ${H:0:12}"
  else
    echo "missing=commit ${H:0:12} of /$D (in no bundle and on no remote)"
  fi
fi
have=$(gx stash list --format=%H 2>/dev/null || true)
while [ $# -gt 1 ]; do
  s=$1; msg=$2; shift 2
  case "$have" in *"$s"*) continue;; esac
  gx stash store -m "${msg:-restored by openajar}" "$s" 2>/dev/null && echo "stash=ok" || echo "missing=a stash of /$D"
done
for r in $(gx for-each-ref --format='%(refname)' refs/agentos-restore 2>/dev/null); do gx update-ref -d "$r"; done
gone=0
if [ -n "$DEL" ] && [ -f "$DEL" ]; then
  while IFS= read -r -d '' f; do rm -f -- "$D/$f" && gone=$((gone+1)); done < "$DEL"
fi
echo "deleted=$gone"
echo "ok=1"
"""

# Before a disk snapshot: what must never persist leaves the machine (the gateway token's git config,
# a git credential store, a swapfile with process memory in it: machine.SWAP_OFF), and the disk is
# synced, after a security upgrade that runs ended (machine.UPGRADE_WAIT). The SSH keys stay: the node forgets this machine's key pair once it is deleted, and a machine
# booted from the snapshot gets new ones at its first boot.
SCRUB = f"{UPGRADE_WAIT}; rm -f ~/.config/agentos/gitconfig ~/.git-credentials; {SWAP_OFF}; sync; echo ok=1"


def parse(text: str) -> dict:
    """SAVE's or RESTORE_REPO's lines: {repos: {r1: {...}}, files, bytes, keep, sums, containers,
    procs (LISTENING's), missing, error, ok}."""
    out: dict = {"repos": {}, "sums": {}, "containers": [], "keep": [], "keep_skipped": [], "missing": [],
                 "error": None, "ok": False, "files": 0, "bytes": 0, "deleted": 0, "stashes": 0, "head": False,
                 "procs": []}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("repo", "branch", "head", "stash", "commits") and " " in value:
            name, _, rest = value.partition(" ")
            if key == "repo":
                out["repos"][name] = {"dir": "/" + rest.lstrip("/"), "branch": None, "head": None, "stashes": [],
                                      "commits": 0}
            elif name in out["repos"]:
                r = out["repos"][name]
                if key == "stash":
                    sha, _, msg = rest.partition(" ")
                    r["stashes"].append(sha)
                    r.setdefault("stash_messages", []).append(msg)
                elif key == "commits":
                    r["commits"] = int(rest) if rest.isdigit() else 0
                else:
                    r[key] = rest or None
        elif key in ("files", "bytes", "deleted"):
            out[key] = int(value) if value.isdigit() else 0
        elif key == "head":
            out["head"] = value == "ok"
        elif key == "stash":
            out["stashes"] += value == "ok"
        elif key == "sum":
            h, _, name = value.partition(" ")
            out["sums"][name] = h
        elif key == "container" and value:
            out["containers"].append(value)
        elif key == "proc" and value:
            out["procs"].append(value)
        elif key in ("keep", "keep_skipped", "missing"):
            out[key].append(value)
        elif key == "error":
            out["error"] = value
        elif key == "ok":
            out["ok"] = True
    return out
