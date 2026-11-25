"""A local stand-in for a solving service, speaking the documented shapes of CapSolver, 2Captcha and Anti-Captcha
(createTask, getTaskResult, getBalance; openajar.node.solver.services) and the webhook hook's. No real service is
called anywhere in the tests: this answers on 127.0.0.1 only.

It checks what a real service would refuse (the key, the task type for its provider, the fields it needs) and keeps
every request it got, so a test can see exactly what left this computer.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

# The documented task types each provider takes (no proxy task types: openajar never sends one)
TASK_TYPES = {
    "capsolver": {"AntiTurnstileTaskProxyLess": "token", "ReCaptchaV2TaskProxyLess": "gRecaptchaResponse",
                  "ReCaptchaV2EnterpriseTaskProxyLess": "gRecaptchaResponse"},
    "2captcha": {"TurnstileTaskProxyless": "token", "RecaptchaV2TaskProxyless": "gRecaptchaResponse",
                 "RecaptchaV2EnterpriseTaskProxyless": "gRecaptchaResponse"},
    "anticaptcha": {"TurnstileTaskProxyless": "token", "RecaptchaV2TaskProxyless": "gRecaptchaResponse",
                    "RecaptchaV2EnterpriseTaskProxyless": "gRecaptchaResponse"},
}
REPORTS_COST = {"capsolver": False, "2captcha": True, "anticaptcha": True}
DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"  # Cloudflare's documented dummy token for its test keys


class FakeService:
    """`with FakeService("2captcha", key="k") as svc:` serves on svc.url until the block ends."""

    def __init__(self, provider: str, *, key: str = "test-key", token: str = DUMMY_TOKEN, cost: str = "0.00145",
                 processing: int = 1, unsolvable: bool = False, balance: float = 3.5, webhook: bool = False):
        self.provider, self.key, self.token, self.cost = provider, key, token, cost
        self.processing, self.unsolvable, self.balance, self.webhook = processing, unsolvable, balance, webhook
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.tasks: dict[int, dict[str, Any]] = {}
        self._next = 1000
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw or b"null")
                except ValueError:
                    body = None
                fake.requests.append((self.path, body if isinstance(body, dict) else {}, dict(self.headers)))
                status, answer = fake.answer(self.path, body, dict(self.headers))
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ---- the API ----

    def answer(self, path: str, body: Any, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if self.webhook:
            return self._webhook(body, headers)
        if not isinstance(body, dict):
            return 400, {"errorId": 1, "errorCode": "ERROR_BAD_REQUEST"}
        if body.get("clientKey") != self.key:
            return 200, {"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST", "errorDescription": "bad key"}
        if path == "/getBalance":
            return 200, {"errorId": 0, "balance": self.balance}
        if path == "/createTask":
            task = body.get("task") or {}
            kinds = TASK_TYPES[self.provider]
            if task.get("type") not in kinds:
                return 200, {"errorId": 2, "errorCode": "ERROR_TASK_NOT_SUPPORTED",
                             "errorDescription": f"no task type {task.get('type')}"}
            if not task.get("websiteURL") or not task.get("websiteKey"):
                return 200, {"errorId": 3, "errorCode": "ERROR_BAD_PARAMETERS"}
            if any(k.startswith("proxy") for k in task):
                return 200, {"errorId": 4, "errorCode": "ERROR_PROXY_NOT_EXPECTED"}
            self._next += 1
            self.tasks[self._next] = {"task": task, "polls": 0}
            return 200, {"errorId": 0, "taskId": self._next}
        if path == "/getTaskResult":
            job = self.tasks.get(body.get("taskId"))  # type: ignore[arg-type]
            if job is None:
                return 200, {"errorId": 16, "errorCode": "ERROR_NO_SUCH_CAPCHA_ID"}
            job["polls"] += 1
            if job["polls"] <= self.processing:
                return 200, {"errorId": 0, "status": "processing"}
            if self.unsolvable:
                return 200, {"errorId": 12, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                             "errorDescription": "Workers could not solve the Captcha"}
            field = TASK_TYPES[self.provider][job["task"]["type"]]
            out: dict[str, Any] = {"errorId": 0, "status": "ready", "solution": {field: self.token,
                                                                                    "userAgent": "Mozilla/5.0"}}
            if REPORTS_COST[self.provider]:
                out.update({"cost": self.cost, "ip": "127.0.0.1", "createTime": 1, "endTime": 2, "solveCount": 1})
            return 200, out
        return 404, {"errorId": 1, "errorCode": "ERROR_NO_SUCH_METHOD"}

    def _webhook(self, body: Any, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if headers.get("Authorization") != f"Bearer {self.key}":
            return 401, {"error": "bad key"}
        if not isinstance(body, dict) or not body.get("site_key") or not body.get("page_url"):
            return 200, {"error": "no job"}
        if self.unsolvable:
            return 200, {"error": "could not solve it"}
        return 200, {"token": self.token, "cost": float(self.cost)}
