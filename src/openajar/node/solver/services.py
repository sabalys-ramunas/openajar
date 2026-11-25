"""The owner's solving service, asked for one widget's token: what it is sent, what comes back.

Built in, all three with the same documented shape (createTask, then getTaskResult until it is ready; getBalance):

- CapSolver (api.capsolver.com): Turnstile (AntiTurnstileTaskProxyLess), reCAPTCHA v2 (ReCaptchaV2TaskProxyLess).
  Its result reports no cost, so its solves are logged with none.
- 2Captcha (api.2captcha.com, its API v2): Turnstile (TurnstileTaskProxyless), reCAPTCHA v2
  (RecaptchaV2TaskProxyless). Its result reports the cost.
- Anti-Captcha (api.anti-captcha.com): Turnstile (TurnstileTaskProxyless), reCAPTCHA v2
  (RecaptchaV2TaskProxyless). Its result reports the cost.

None of the three lists hCaptcha in its documentation any more (read 2026-11-25), so hCaptcha goes only to a hook.
Only the proxyless task types are used: openajar never sends a proxy, its address or its credentials to a service.

The hooks, for any other service or the owner's own script:

- command: a command line on this computer (split like a shell would, run with no shell). It gets the job as one
  JSON object on stdin and the key, when there is one, in the environment variable OPENAJAR_SOLVER_KEY (never on
  its command line). It prints one JSON object: {"token": "...", "cost": 0.002} ("cost" optional, in US
  dollars), or {"error": "why"}.
- webhook: an https URL (http only to this computer itself). It gets the job as the JSON body of a POST, the key
  as `Authorization: Bearer <key>`, and answers with the same JSON as the command. Redirects are not followed.

The job: {"kind": "turnstile" | "recaptcha_v2" | "hcaptcha", "site_key": "...", "page_url": "https://host/path",
"action": ..., "cdata": ..., "invisible": false}. The page's address goes without its query and fragment.

Every message that comes back from a service or a hook goes through `redact` with the key before anything shows it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import shlex
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from openajar.core.system import redact
from openajar.node.solver.settings import BUILT_IN, SolverEntry, safe_url

KINDS: tuple[str, ...] = ("turnstile", "recaptcha_v2", "hcaptcha")
KIND_WORDS = {"turnstile": "Cloudflare Turnstile", "recaptcha_v2": "reCAPTCHA v2", "hcaptcha": "hCaptcha"}
ENDPOINTS = {"capsolver": "https://api.capsolver.com", "2captcha": "https://api.2captcha.com",
             "anticaptcha": "https://api.anti-captcha.com"}
TASK_TYPES: dict[str, dict[str, str]] = {
    "capsolver": {"turnstile": "AntiTurnstileTaskProxyLess", "recaptcha_v2": "ReCaptchaV2TaskProxyLess"},
    "2captcha": {"turnstile": "TurnstileTaskProxyless", "recaptcha_v2": "RecaptchaV2TaskProxyless"},
    "anticaptcha": {"turnstile": "TurnstileTaskProxyless", "recaptcha_v2": "RecaptchaV2TaskProxyless"},
}
NAMES = {"capsolver": "CapSolver", "2captcha": "2Captcha", "anticaptcha": "Anti-Captcha", "command": "a command",
         "webhook": "a webhook"}
# For `openajar solver test --solve`: Cloudflare's Turnstile test key that forces the interactive check, on
# 2Captcha's own demo page (2Captcha refuses the always-pass key 1x00000000000000000000AA: ERROR_BAD_PARAMETERS).
TEST_SITE_KEY = "3x00000000000000000000FF"
TEST_PAGE = "https://2captcha.com/demo/cloudflare-turnstile"

SOLVE_TIMEOUT_S = 120.0    # a token takes 1 to 20 s by the services' own words; a stuck one is given up
FIRST_POLL_S = 5.0         # 2Captcha asks for at least 5 s before the first getTaskResult
POLL_S = 4.0
REQUEST_TIMEOUT_S = 30.0
MAX_TOKEN = 10_000         # a reCAPTCHA token is about 2 KB; anything far longer is not one
HOOK_OUTPUT = 64 << 10


@dataclass(frozen=True, slots=True)
class Job:
    kind: str
    site_key: str
    page_url: str
    action: str | None = None
    cdata: str | None = None
    invisible: bool = False


@dataclass(frozen=True, slots=True)
class Answer:
    token: str
    cost: float | None     # US dollars, when the service reports it


SHOWN = 300                # characters of a service's message that are shown, cut only after the key is redacted


class SolveError(Exception):
    """The service gave no token. Once out of `solve` the message is safe to show: the key was redacted from the
    whole message before it was cut."""


def _shown(text: str, key: str | None) -> str:
    return redact(text, key)[:SHOWN]


def supports(entry: SolverEntry, kind: str) -> bool:
    """The entry's service takes this kind of widget (a hook takes all three)."""
    if entry.provider in BUILT_IN:
        return kind in TASK_TYPES[entry.provider]
    return kind in KINDS


def page_address(url: str) -> str:
    """What a service is told of the page: scheme, host, port and path. The query and fragment stay here."""
    return safe_url(url)


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_address(url: str) -> str | None:
    """Why `url` may not carry a key (None when it may): https, or http to this computer itself."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "it does not parse"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "it is not an http(s) address"
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        return "a key goes only over https (plain http only to this computer itself)"
    if parts.username or parts.password:
        return "it holds a user or password: put the secret in the key instead"
    return None


async def solve(entry: SolverEntry, job: Job) -> Answer:
    """The token for `job` from `entry`'s service. Raises SolveError with a message safe to show."""
    try:
        if entry.provider in BUILT_IN:
            answer = await _task_api(entry, job)
        elif entry.provider == "command":
            answer = await _command(entry, job)
        else:
            answer = await _webhook(entry, job)
    except SolveError as e:
        raise SolveError(_shown(str(e), entry.key)) from None
    except Exception as e:  # noqa: BLE001 - any other failure (a malformed answer included) is one with no token
        raise SolveError(_shown(f"{type(e).__name__}: {e}", entry.key)) from None
    token = answer.token
    if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN or any(c in token for c in "\r\n\0"):
        raise SolveError("the service answered with no usable token")
    return answer


async def balance(entry: SolverEntry) -> float | None:
    """The account's balance in US dollars (a built-in's getBalance, which costs nothing); None for a hook."""
    if entry.provider not in BUILT_IN:
        return None
    try:
        data = await _post(entry, "getBalance", {})
    except SolveError as e:
        raise SolveError(_shown(str(e), entry.key)) from None
    except Exception as e:  # noqa: BLE001 - shown as a failed check
        raise SolveError(_shown(f"{type(e).__name__}: {e}", entry.key)) from None
    try:
        return float(data.get("balance"))
    except (TypeError, ValueError):
        raise SolveError("the service's balance answer had no balance") from None


# ---- the three task APIs ---------------------------------------------------------------------------------------

def task_body(provider: str, job: Job) -> dict[str, Any]:
    """The `task` object of createTask for this provider, by its documentation."""
    task: dict[str, Any] = {"type": TASK_TYPES[provider][job.kind], "websiteURL": job.page_url,
                            "websiteKey": job.site_key}
    if job.kind == "turnstile":
        if provider == "capsolver":
            meta = {k: v for k, v in (("action", job.action), ("cdata", job.cdata)) if v}
            if meta:
                task["metadata"] = meta
        elif provider == "2captcha":
            task.update({k: v for k, v in (("action", job.action), ("data", job.cdata)) if v})
        else:
            task.update({k: v for k, v in (("action", job.action), ("cData", job.cdata)) if v})
    elif job.invisible:
        task["isInvisible"] = True
    return task


async def _task_api(entry: SolverEntry, job: Job) -> Answer:
    if not supports(entry, job.kind):
        raise SolveError(f"{NAMES[entry.provider]} does not take {KIND_WORDS.get(job.kind, job.kind)}")
    created = await _post(entry, "createTask", {"task": task_body(entry.provider, job)})
    task_id = created.get("taskId")
    if task_id in (None, ""):
        raise SolveError("the service gave no task id")
    deadline = time.monotonic() + SOLVE_TIMEOUT_S
    await asyncio.sleep(FIRST_POLL_S)
    while True:
        result = await _post(entry, "getTaskResult", {"taskId": task_id})
        if result.get("status") == "ready":
            solution = result.get("solution")
            if not isinstance(solution, dict):
                raise SolveError("the service said ready with no solution object")
            token = solution.get("token") if job.kind == "turnstile" else solution.get("gRecaptchaResponse")
            token = token or solution.get("token") or solution.get("gRecaptchaResponse")
            return Answer(str(token or ""), _cost(result.get("cost")))
        if time.monotonic() + POLL_S > deadline:
            raise SolveError(f"no token within {SOLVE_TIMEOUT_S:.0f} s")
        await asyncio.sleep(POLL_S)


async def _post(entry: SolverEntry, method: str, body: dict[str, Any]) -> dict[str, Any]:
    base = (entry.endpoint or ENDPOINTS[entry.provider]).rstrip("/")
    why = check_address(base)
    if why is not None:
        raise SolveError(f"the service's address is refused: {why}")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, follow_redirects=False, trust_env=False) as client:
        response = await client.post(f"{base}/{method}", json={"clientKey": entry.key or "", **body})
    if response.status_code != 200:
        raise SolveError(f"{method} answered HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise SolveError(f"{method} answered with no JSON object")
    if data.get("errorId") not in (0, None, "0"):
        code = str(data.get("errorCode") or data.get("errorId"))
        text = str(data.get("errorDescription") or "")
        raise SolveError(f"{code}: {text}" if text else code)
    return data


def _cost(value: Any) -> float | None:
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    return cost if 0 <= cost <= 1000 else None


# ---- the hooks -------------------------------------------------------------------------------------------------

def job_json(job: Job) -> dict[str, Any]:
    return asdict(job)


def _hook_answer(raw: bytes, who: str) -> Answer:
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "null")
    except ValueError:
        raise SolveError(f"{who} printed no JSON") from None
    if not isinstance(data, dict):
        raise SolveError(f"{who} printed no JSON object")
    if data.get("error"):
        raise SolveError(f"{who} said: {data['error']}")
    return Answer(str(data.get("token") or ""), _cost(data.get("cost")))


async def _command(entry: SolverEntry, job: Job) -> Answer:
    argv = shlex.split(entry.command or "")
    if not argv:
        raise SolveError("the command hook has no command")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")}
    if entry.key:
        env["OPENAJAR_SOLVER_KEY"] = entry.key
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(job_json(job)).encode()), SOLVE_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise SolveError(f"the command gave no answer within {SOLVE_TIMEOUT_S:.0f} s") from None
    if proc.returncode != 0:
        tail = redact(err.decode("utf-8", errors="replace"), entry.key).strip()[-SHOWN:]  # redacted, then cut
        raise SolveError(f"the command exited {proc.returncode}" + (f": {tail}" if tail else ""))
    return _hook_answer(out[:HOOK_OUTPUT], "the command")


async def _webhook(entry: SolverEntry, job: Job) -> Answer:
    url = entry.endpoint or ""
    why = check_address(url)
    if why is not None:
        raise SolveError(f"the webhook's address is refused: {why}")
    headers = {"Authorization": f"Bearer {entry.key}"} if entry.key else {}
    async with httpx.AsyncClient(timeout=SOLVE_TIMEOUT_S, follow_redirects=False, trust_env=False) as client:
        response = await client.post(url, json=job_json(job), headers=headers)
    if response.status_code != 200:
        raise SolveError(f"the webhook answered HTTP {response.status_code}")
    return _hook_answer(response.content[:HOOK_OUTPUT], "the webhook")
