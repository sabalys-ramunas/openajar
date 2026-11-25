"""The owner's solving service (openajar.node.solver): what `openajar solver` stores and shows, what each built-in
service is sent and what it answers (a local fake with their documented shapes; no real service is called), the
command and webhook hooks end to end, and the log. The key never shows anywhere."""

from __future__ import annotations

import json
import stat
import sys

import pytest
from solver_fake import DUMMY_TOKEN, FakeService
from typer.testing import CliRunner

from openajar.node.solver import ledger, services, settings, widgets
from openajar.node.solver.settings import SolverEntry

# made up at run time, so the secret scan reads none of them as real
KEY = f"fake-solver-key-{0x8f3a:x}"
OTHER = f"fake-other-key-{0x11c2:x}"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(services, "FIRST_POLL_S", 0.0)
    monkeypatch.setattr(services, "POLL_S", 0.01)


def entry(provider="2captcha", *, url=None, key=KEY, command=None, name="main"):
    return SolverEntry(name=name, provider=provider, key=key, endpoint=url, command=command)


TURNSTILE = services.Job("turnstile", "0x4AAAAAAAexample00000", "https://shop.example.com/contact",
                         action="contact", cdata="c-123")
RECAPTCHA = services.Job("recaptcha_v2", "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI", "https://shop.example.com/form",
                         invisible=True)
HCAPTCHA = services.Job("hcaptcha", "10000000-ffff-ffff-ffff-000000000001", "https://shop.example.com/form")


# ---- settings -------------------------------------------------------------------------------------------------

def test_entries_are_stored_private_and_shown_without_their_key(tmp_path):
    settings.save(tmp_path, [entry(), entry("webhook", url="https://hook.example.net/solve?token=abc", name="mine")])
    p = settings.path(tmp_path)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    loaded = settings.load(tmp_path)
    assert [e.name for e in loaded] == ["main", "mine"] and loaded[0].key == KEY
    shown = json.dumps([e.public() for e in loaded])
    assert KEY not in shown and "token=abc" not in shown and '"has_key": true' in shown


def test_a_broken_file_names_the_entry_and_the_field_never_the_value(tmp_path):
    (tmp_path / "secrets").mkdir()
    settings.path(tmp_path).write_text(json.dumps([{"name": "main", "provider": "nope", "key": KEY}]))
    with pytest.raises(ValueError, match="solver 'main'") as e:
        settings.load(tmp_path)
    assert KEY not in str(e.value) and "provider" in str(e.value)
    assert settings.current(tmp_path) == []  # a file that does not load: no solver, nothing is sent


def test_current_reads_the_file_again_when_it_changes(tmp_path):
    assert settings.current(tmp_path) == []
    settings.save(tmp_path, [entry()])
    assert [e.name for e in settings.current(tmp_path)] == ["main"]
    settings.save(tmp_path, [])
    assert settings.current(tmp_path) == []


# ---- the three task APIs --------------------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["capsolver", "2captcha", "anticaptcha"])
async def test_each_service_gets_its_documented_turnstile_task_and_answers_with_a_token(provider):
    with FakeService(provider, key=KEY) as svc:
        answer = await services.solve(entry(provider, url=svc.url), TURNSTILE)
    assert answer.token == DUMMY_TOKEN
    assert answer.cost == (None if provider == "capsolver" else 0.00145)  # CapSolver reports no cost
    paths = [p for p, _, _ in svc.requests]
    assert paths[0] == "/createTask" and set(paths[1:]) == {"/getTaskResult"}
    task = svc.requests[0][1]["task"]
    assert task["websiteURL"] == "https://shop.example.com/contact" and task["websiteKey"] == "0x4AAAAAAAexample00000"
    if provider == "capsolver":
        assert task["type"] == "AntiTurnstileTaskProxyLess" and task["metadata"] == {"action": "contact",
                                                                                     "cdata": "c-123"}
    elif provider == "2captcha":
        assert task["type"] == "TurnstileTaskProxyless" and (task["action"], task["data"]) == ("contact", "c-123")
    else:
        assert task["type"] == "TurnstileTaskProxyless" and (task["action"], task["cData"]) == ("contact", "c-123")
    assert not any(k.lower().startswith("proxy") for k in task)  # never a proxy, its address or its credentials


@pytest.mark.parametrize("provider", ["capsolver", "2captcha", "anticaptcha"])
async def test_recaptcha_v2_goes_as_the_proxyless_task_and_comes_back_from_its_field(provider):
    with FakeService(provider, key=KEY, token="03AFcWeA-token") as svc:
        answer = await services.solve(entry(provider, url=svc.url), RECAPTCHA)
    task = svc.requests[0][1]["task"]
    assert task["type"] in ("ReCaptchaV2TaskProxyLess", "RecaptchaV2TaskProxyless") and task["isInvisible"] is True
    assert answer.token == "03AFcWeA-token"


@pytest.mark.parametrize("provider", ["capsolver", "2captcha", "anticaptcha"])
async def test_hcaptcha_is_not_sent_to_a_built_in_that_no_longer_takes_it(provider):
    assert not services.supports(entry(provider), "hcaptcha")
    with FakeService(provider, key=KEY) as svc, pytest.raises(services.SolveError, match="does not take hCaptcha"):
        await services.solve(entry(provider, url=svc.url), HCAPTCHA)
    assert svc.requests == []


async def test_a_service_error_says_its_code_and_never_the_key():
    with FakeService("anticaptcha", key=KEY, unsolvable=True) as svc, \
            pytest.raises(services.SolveError, match="ERROR_CAPTCHA_UNSOLVABLE") as e:
        await services.solve(entry("anticaptcha", url=svc.url), TURNSTILE)
    assert KEY not in str(e.value)
    with FakeService("2captcha", key=OTHER) as svc, pytest.raises(services.SolveError,  # a wrong key
                                                                  match="ERROR_KEY_DOES_NOT_EXIST"):
        await services.solve(entry("2captcha", url=svc.url), TURNSTILE)


async def test_a_service_that_echoes_the_key_has_it_redacted(monkeypatch):
    async def echo(entry_, method, body):
        raise services.SolveError(f"bad clientKey {entry_.key}")
    monkeypatch.setattr(services, "_post", echo)
    with pytest.raises(services.SolveError) as e:
        await services.solve(entry("capsolver"), TURNSTILE)
    assert KEY not in str(e.value) and "***" in str(e.value)


async def test_the_balance_costs_nothing_and_a_hook_has_none():
    with FakeService("capsolver", key=KEY, balance=12.25) as svc:
        assert await services.balance(entry("capsolver", url=svc.url)) == 12.25
    assert [p for p, _, _ in svc.requests] == ["/getBalance"]
    assert await services.balance(entry("command", command="true")) is None


async def test_a_key_goes_only_over_https_or_to_this_computer():
    assert services.check_address("https://api.example.net") is None
    assert services.check_address("http://127.0.0.1:9000") is None
    assert "https" in services.check_address("http://api.example.net")
    assert "user or password" in services.check_address("https://u:p@api.example.net")
    with pytest.raises(services.SolveError, match="refused"):
        await services.solve(entry("2captcha", url="http://api.example.net"), TURNSTILE)


async def test_a_token_that_is_not_one_is_refused(monkeypatch):
    async def bad(entry_, job):
        return services.Answer("line one\nline two", None)
    monkeypatch.setattr(services, "_task_api", bad)
    with pytest.raises(services.SolveError, match="no usable token"):
        await services.solve(entry("2captcha"), TURNSTILE)


def test_the_page_address_keeps_its_query_and_fragment_here():
    assert services.page_address("https://u:p@shop.example.com:8443/a/b?order=123&email=x#frag") == \
        "https://shop.example.com:8443/a/b"


# ---- the hooks ------------------------------------------------------------------------------------------------

HOOK = """
import json, os, sys
job = json.load(sys.stdin)
assert os.environ.get("OPENAJAR_SOLVER_KEY") == sys.argv[1][::-1], "the key comes in the environment"
assert not any(os.environ.get("OPENAJAR_SOLVER_KEY") in a for a in sys.argv), "never on the command line"
if job["site_key"] == "fail":
    print("it broke with " + os.environ["OPENAJAR_SOLVER_KEY"], file=sys.stderr); sys.exit(3)
print(json.dumps({"token": "tok-" + job["kind"] + "-" + job["site_key"], "cost": 0.002, "seen": job}))
"""


async def test_the_command_hook_gets_the_job_on_stdin_and_the_key_in_its_environment(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text(HOOK)
    e = entry("command", command=f"{sys.executable} {script} {KEY[::-1]}")
    answer = await services.solve(e, HCAPTCHA)  # a hook takes hCaptcha
    assert (answer.token, answer.cost) == ("tok-hcaptcha-10000000-ffff-ffff-ffff-000000000001", 0.002)
    with pytest.raises(services.SolveError, match="exited 3") as err:
        await services.solve(e, services.Job("turnstile", "fail", "https://x.example/"))
    assert KEY not in str(err.value) and "***" in str(err.value)


async def test_the_webhook_gets_the_job_and_the_key_as_a_bearer_token():
    with FakeService("webhook", key=KEY, webhook=True, cost="0.004") as svc:
        answer = await services.solve(entry("webhook", url=f"{svc.url}/solve"), TURNSTILE)
        assert (answer.token, answer.cost) == (DUMMY_TOKEN, 0.004)
        path, body, headers = svc.requests[0]
        assert path == "/solve" and headers["Authorization"] == f"Bearer {KEY}"
        assert body == {"kind": "turnstile", "site_key": "0x4AAAAAAAexample00000", "invisible": False,
                        "page_url": "https://shop.example.com/contact", "action": "contact", "cdata": "c-123"}
        with pytest.raises(services.SolveError, match="HTTP 401"):
            await services.solve(entry("webhook", url=svc.url, key=OTHER), TURNSTILE)


# ---- the log --------------------------------------------------------------------------------------------------

def test_the_log_is_a_log_that_never_stands_in_the_way(tmp_path, caplog):
    line = ledger.Solve("2026-11-02T10:00:00Z", "env/e1-browser", "example.com", "turnstile", "main", "2captcha",
                        ledger.PASSED, 0.00145)
    ledger.write(tmp_path, line)
    ledger.write(tmp_path, ledger.Solve("2026-10-30T10:00:00Z", "b", "s", "turnstile", "main", "capsolver",
                                        ledger.PASSED))  # CapSolver reports no cost
    p = ledger.record_path(tmp_path)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    p.write_bytes(p.read_bytes() + b"not json\n\xff\xfe\n" + json.dumps({"at": 5, "cost": "1"}).encode() + b"\n")
    assert ledger.read(tmp_path, month="2026-11") == [line]  # lines that do not read are skipped, with a warning
    assert ledger.spent(ledger.read(tmp_path)) == 0.00145
    assert "does not read" in caplog.text
    p.unlink()
    p.symlink_to(tmp_path / "elsewhere")
    ledger.write(tmp_path, line)  # a log that cannot be written: a warning, and nothing raises
    assert ledger.read(tmp_path) == [] and "could not write the solve log" in caplog.text
    ledger.write(None, line)  # no home: nothing to write


# ---- the command line -----------------------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    from openajar.cli import app

    assert CliRunner().invoke(app, ["node", "init"]).exit_code == 0
    return home


def _cli(*args: str, input: str | None = None):
    from openajar.cli import app

    return CliRunner().invoke(app, list(args), input=input)


def test_solver_add_stores_the_key_and_never_prints_it(home):
    assert "No solver" in _cli("solver", "list").output
    out = _cli("solver", "add", "main", "2captcha", input=f"{KEY}\n")
    assert out.exit_code == 0, out.output
    assert "Added solver main: 2Captcha, with a key." in out.output
    assert "your choice" in out.output and KEY not in out.output
    assert settings.load(home)[0].key == KEY
    out = _cli("solver", "add", "main", "capsolver", KEY)
    assert out.exit_code == 1 and "--yes" in out.output
    out = _cli("solver", "add", "main", "capsolver", KEY, "--yes")
    assert out.exit_code == 0 and "Replaced solver main: CapSolver, with a key." in out.output
    listed = _cli("solver", "list").output
    assert "main  CapSolver" in listed and "key stored" in listed and "0 solves (0 passed), 0 USD reported" in listed
    assert KEY not in listed


def test_solver_add_checks_the_hooks(home):
    assert "--command" in _cli("solver", "add", "s", "command").output
    assert "--url" in _cli("solver", "add", "s", "webhook").output
    out = _cli("solver", "add", "s", "webhook", "--url", "http://hook.example.net/")
    assert out.exit_code == 1 and "https" in out.output
    out = _cli("solver", "add", "s", "webhook", "--url", "https://hook.example.net/x")
    assert out.exit_code == 0, out.output


def test_solver_test_asks_the_balance_and_a_hook_for_a_test_token(home, tmp_path):
    with FakeService("anticaptcha", key=KEY) as svc:
        _cli("solver", "add", "anti", "anticaptcha", KEY, "--url", svc.url)
        out = _cli("solver", "test", "anti")
        assert out.exit_code == 0 and "Anti-Captcha answers: balance 3.5000 USD" in out.output
        assert [p for p, _, _ in svc.requests] == ["/getBalance"]  # the balance only: no token is bought
        out = _cli("solver", "test", "anti", "--solve")
        assert out.exit_code == 0 and "gave a token for Cloudflare's test widget" in out.output, out.output
        task = svc.requests[2][1]["task"]  # after a second balance
        # Cloudflare's key that forces the interactive check, on 2Captcha's demo page (it refuses the always-pass key)
        assert (task["websiteKey"], task["websiteURL"]) == ("3x00000000000000000000FF",
                                                            "https://2captcha.com/demo/cloudflare-turnstile")
    assert ledger.spent(ledger.read(home)) == 0.00145
    assert "1 solves (1 passed), 0.00145 USD reported" in _cli("solver", "list").output
    script = tmp_path / "hook.py"
    script.write_text(HOOK)
    _cli("solver", "add", "mine", "command", "--command",
         f"{sys.executable} {script} {KEY[::-1]}", KEY)
    out = _cli("solver", "test", "mine")
    assert out.exit_code == 0 and "gave a token" in out.output, out.output
    log = _cli("solver", "log").output
    assert "anti" in log and "mine" in log and KEY not in log and "tok-" not in log
    out = _cli("solver", "remove", "anti")
    assert out.exit_code == 0 and [e.name for e in settings.load(home)] == ["mine"]
    assert _cli("solver", "remove", "anti").exit_code == 1


def test_solver_test_fails_plainly_with_a_wrong_key(home):
    with FakeService("2captcha", key=OTHER) as svc:
        _cli("solver", "add", "main", "2captcha", KEY, "--url", svc.url)
        out = _cli("solver", "test")
    assert out.exit_code == 1 and "ERROR_KEY_DOES_NOT_EXIST" in out.output and KEY not in out.output


def test_a_widget_is_read_from_its_frames_address_when_its_element_says_nothing():
    w = widgets.from_frame_url("https://www.google.com/recaptcha/api2/anchor?ar=1&k=6LeIxAcTAAAAAJcZVRqyHh71UMIEGN"
                               "Q_MXjiZKhI&co=aHR0cHM6&size=normal")
    assert (w.kind, w.site_key, w.invisible) == ("recaptcha_v2", "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI", False)
    assert widgets.from_frame_url("https://www.google.com/recaptcha/enterprise/anchor?k=6Lxyz") is None
    w = widgets.from_frame_url("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox&id=0"
                               "&host=shop.example.com&sitekey=10000000-ffff-ffff-ffff-000000000001&theme=light")
    assert (w.kind, w.site_key) == ("hcaptcha", "10000000-ffff-ffff-ffff-000000000001")
    assert widgets.from_frame_url("https://newassets.hcaptcha.com/x#frame=checkbox-invisible&sitekey=k") is None
    w = widgets.from_frame_url("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/"
                               "rcv/abc12/0x4AAAAAAADnPIDROrmt1Wwj/light/fbE/new/normal/auto/")
    assert (w.kind, w.site_key) == ("turnstile", "0x4AAAAAAADnPIDROrmt1Wwj")
    w = widgets.from_frame_url("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/"
                               "rcv/abc12/3x00000000000000000000FF/light/normal/")
    assert w.site_key == "3x00000000000000000000FF"
    assert widgets.from_frame_url("https://example.com/recaptcha/api2/anchor?k=x") is None
