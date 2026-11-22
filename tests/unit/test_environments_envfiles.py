"""`openajar environments env push`: a repository's local env files on an environment's machine, plain
settings copied, secrets bound as placeholders, and no secret's value leaving the node
(openajar.environments.envfiles and envfiles_cli)."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from test_vault_support import make_vault, private_home
from typer.testing import CliRunner

from openajar.environments import appsecrets, repoconfig
from openajar.environments import envfiles as ef
from openajar.vault.models import ENV_VAR_RE

# ------------------------------------------------------------------ parsing


# Made-up secrets, put together at run time so the source holds nothing a secret scanner (gitleaks,
# GitHub push protection) takes for a real one.
FAKE_STRIPE = "sk_" + "test_" + "51Habcdefghijklmnop1234567890"
FAKE_STRIPE_2 = "sk_" + "test_" + "51Hzzzzzzzzzzzzzzzzz1234567890"
FAKE_WHSEC = "whsec" + "_abcdefabcdef1234567890"
FAKE_PASSWORD = "Hunter2" + "-Very-Private"


def test_dotenv_syntax_quotes_export_comments_and_empty_values():
    text = (
        "# a comment\n"
        "\n"
        "export NODE_ENV=development\n"
        "PORT = 3000   # the dev server\n"
        "EMPTY=\n"
        "QUOTED_EMPTY=\"\"\n"
        "SINGLE='keep $this # and this'\n"
        'DOUBLE="a\\nb \\"q\\""\n'
        "BACKTICK=`tick`\n"
        'MULTI="line one\nline two"\n'
        "AFTER=1\n"
    )
    p = ef.parse(text)
    got = {e.key: e.value for e in p.entries}
    assert got == {"NODE_ENV": "development", "PORT": "3000", "EMPTY": "", "QUOTED_EMPTY": "",
                   "SINGLE": "keep $this # and this", "DOUBLE": 'a\nb "q"', "BACKTICK": "tick",
                   "MULTI": "line one\nline two", "AFTER": "1"}
    by = {e.key: e for e in p.entries}
    assert by["NODE_ENV"].export and not by["PORT"].export
    assert by["SINGLE"].raw == "'keep $this # and this'" and by["PORT"].raw == "3000"  # raw: as written, no comment
    assert by["AFTER"].line == 12 and p.bad_lines == []


def test_a_line_that_is_not_key_value_is_left_out_by_number_and_the_last_of_a_key_wins():
    p = ef.parse("A=1\nnot a line\nB='never closed\nA=2\n=x\n")
    assert [(e.key, e.value) for e in p.entries] == [("A", "2")]
    assert p.bad_lines == [2, 3, 5]  # the unclosed quote swallows nothing after it
    p = ef.parse("A=1\nB='never closed\n")
    assert p.bad_lines == [2]


# ------------------------------------------------------------------ classifying


@pytest.mark.parametrize(("key", "value"), [
    ("NODE_ENV", "production"), ("PORT", "3000"), ("DEBUG", "true"), ("TIMEOUT", "1.5"), ("JWT_EXPIRES_IN", "7d"),
    ("API_URL", "http://localhost:3000/api"), ("NEXTAUTH_URL", "https://shop.example.com"),
    ("REDIS_URL", "redis://localhost:6379"), ("AWS_REGION", "eu-west-1"), ("FEATURES", "search,cart, checkout"),
    ("SUPPORT_EMAIL", "help@example.com"), ("UPLOAD_DIR", "./uploads"), ("APP_NAME", "My Shop"),
    ("SESSION_COOKIE_NAME", "sid"), ("OPENAI_MODEL", "gpt-4o-mini"), ("EMPTY_SECRET", ""),
])
def test_settings_are_plain(key, value):
    assert ef.classify(key, value).cls == ef.PLAIN


@pytest.mark.parametrize(("key", "value", "use"), [
    ("STRIPE_SECRET_KEY", FAKE_STRIPE, ef.HEADER),
    ("OPENAI_API_KEY", "anything", ef.HEADER),
    ("DATAFORSEO_PASSWORD", "hunter2", ef.HEADER),
    ("MY_TOKEN", "short", ef.HEADER),
    ("STRIPE_WEBHOOK_SECRET", FAKE_WHSEC, ef.LOCAL),
    ("JWT_SECRET", "changeme", ef.LOCAL),
    ("SESSION_SECRET", "changeme", ef.LOCAL),
    ("SECRET_KEY", "django-insecure", ef.LOCAL),
    ("AWS_SECRET_ACCESS_KEY", "abcdEFGHijklMNOPqrstUVWXyz0123456789abcd", ef.LOCAL),
    ("GITHUB_APP_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", ef.LOCAL),
    ("ZOHO_REFRESH_TOKEN", "1000.abc.def", ef.BODY),
    ("GOOGLE_CLIENT_SECRET", "GOCSPX-abc", ef.BODY),
    ("DATABASE_URL", "postgres://app:pw@localhost:5432/app", ef.CONNECTION),
    ("DB_PASSWORD", "postgres", ef.CONNECTION),
    ("SMTP_PASS", "pw", ef.CONNECTION),
    ("SENTRY_DSN", "https://abc123@o1.ingest.sentry.io/1", ef.IN_URL),
    ("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T000/B000/XXXX", ef.IN_URL),
])
def test_secrets_by_name_or_value_and_how_they_are_used(key, value, use):
    v = ef.classify(key, value)
    assert (v.cls, v.use) == (ef.SECRET, use)


@pytest.mark.parametrize(("key", "value"), [
    ("SOME_ID", "550e8400-e29b-41d4-a716-446655440000"),   # a UUID may be a key
    ("UPSTREAM", "x9Kq2mPz7LwR4tYb"),                        # letters and digits mixed
    ("BLOB", "a long value with $ and ; in it"),
    ("MULTI", "line one\nline two"),
    ("RAW", "%%weird%%"),
])
def test_a_value_it_is_not_sure_about_is_a_secret(key, value):
    v = ef.classify(key, value)
    assert v.cls == ef.SECRET and "not sure" in v.why


# ------------------------------------------------------------------ the plan

SAMPLE = f"""\
NODE_ENV=development
API_URL=http://localhost:3000
STRIPE_SECRET_KEY={FAKE_STRIPE}
STRIPE_WEBHOOK_SECRET={FAKE_WHSEC}
ZOHO_REFRESH_TOKEN=1000.f00dbabe.deadbeef1234
DATAFORSEO_LOGIN=me@example.com
DATAFORSEO_PASSWORD={FAKE_PASSWORD}
DATAFORSEO_API_URL=https://api.dataforseo.com/v3
DATABASE_URL=postgres://app:Sup3rS3cretDb@localhost:5432/app
MYSTERY=x9Kq2mPz7LwR4tYb
export JWT_SECRET="jwt-signing-secret-value"
"""


def plan(text: str = SAMPLE, **kw):
    kw.setdefault("overrides", ef.Overrides())
    kw.setdefault("bound", {})
    kw.setdefault("live", set())
    return ef.plan_file(ef.parse(text).entries, bindable=ENV_VAR_RE, **kw)


def by_key(rows):
    return {r.entry.key: r for r in rows}


def test_the_plan_copies_settings_binds_what_a_placeholder_carries_and_holds_the_rest():
    rows = by_key(plan())
    assert rows["NODE_ENV"].action == ef.COPY and rows["API_URL"].action == ef.COPY
    assert rows["STRIPE_SECRET_KEY"].action == ef.BIND and rows["STRIPE_SECRET_KEY"].hosts == ("api.stripe.com",)
    assert rows["DATAFORSEO_PASSWORD"].action == ef.BIND  # its host from DATAFORSEO_API_URL
    assert rows["DATAFORSEO_PASSWORD"].hosts == ("api.dataforseo.com",) and "DATAFORSEO_API_URL" in \
        rows["DATAFORSEO_PASSWORD"].note
    for key in ("STRIPE_WEBHOOK_SECRET", "ZOHO_REFRESH_TOKEN", "DATABASE_URL", "JWT_SECRET"):
        assert rows[key].action == ef.HOLD and "not supported as a placeholder" in rows[key].note, key
        assert f"--plain {key}" in rows[key].fix
    assert rows["MYSTERY"].action == ef.HOLD and "needs a host" in rows[key := "MYSTERY"].note
    assert f"--host {key}=" in rows[key].fix


def test_a_secret_already_bound_is_a_placeholder_and_says_whether_the_machine_has_it():
    rows = by_key(plan(bound={"STRIPE_SECRET_KEY": ("api.stripe.com",), "JWT_SECRET": ("x.example.com",)},
                       live={"STRIPE_SECRET_KEY"}))
    assert rows["STRIPE_SECRET_KEY"].action == ef.PLACEHOLDER and "on the machine now" in rows["STRIPE_SECRET_KEY"].note
    assert rows["JWT_SECRET"].action == ef.PLACEHOLDER and "next open" in rows["JWT_SECRET"].note
    assert "may not work" in rows["JWT_SECRET"].note  # bound by the person, though a placeholder cannot carry it


def test_the_person_overrides_per_key_with_patterns():
    ov = ef.Overrides(plain=["DATABASE_URL"], skip=["JWT_*"], hosts={"MYSTERY": ["api.mystery.example"]},
                      secret=["NODE_ENV"])
    rows = by_key(plan(overrides=ov))
    assert rows["DATABASE_URL"].action == ef.COPY and rows["JWT_SECRET"].action == ef.SKIP
    assert rows["MYSTERY"].action == ef.BIND and rows["MYSTERY"].hosts == ("api.mystery.example",)
    assert rows["NODE_ENV"].action == ef.HOLD and rows["NODE_ENV"].cls == ef.SECRET  # a secret with no host


def test_one_value_per_name_across_files():
    a, b, c = (plan(f"STRIPE_SECRET_KEY={FAKE_STRIPE}\n") for _ in range(3))
    c[0] = plan(f"STRIPE_SECRET_KEY={FAKE_STRIPE_2}\n")[0]
    ef.across_files([a, b, c])
    assert [a[0].action, b[0].action, c[0].action] == [ef.BIND, ef.PLACEHOLDER, ef.HOLD]


# ------------------------------------------------------------------ no secret leaves


def _secret_values(rows):
    return [r.entry.value for r in rows if r.cls == ef.SECRET or r.action == ef.SKIP]


def test_no_secret_value_reaches_the_machines_file_the_table_or_the_call_log():
    rows = plan()
    secrets = _secret_values(rows)
    assert len(secrets) == 7
    machine = ef.render(rows)
    shown = "\n".join(ef.table(rows) + ef.fixes(rows) + [ef.summary("apps/api/.env", rows)])
    for value in secrets:
        for piece in {value, value[:12], value[-12:]}:
            assert piece not in machine and piece not in shown, value
    assert "STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY}" in machine and "DATAFORSEO_PASSWORD=${DATAFORSEO_PASSWORD}" in machine
    assert "# STRIPE_WEBHOOK_SECRET: a secret, not sent by openajar" in machine
    assert "export NODE_ENV=development" not in machine and "NODE_ENV=development" in machine
    assert "export JWT_SECRET" not in machine and "DATAFORSEO_LOGIN=me@example.com" in machine
    assert "—" not in machine + shown


def test_a_value_the_person_said_is_plain_is_copied_but_never_shown():
    rows = plan(overrides=ef.Overrides(plain=["DATABASE_URL"]))
    assert "Sup3rS3cretDb" in ef.render(rows)  # the person's choice: the machine gets it
    assert "Sup3rS3cretDb" not in "\n".join(ef.table(rows))


def test_comments_and_bad_lines_never_reach_the_machine():
    fake = "sk_" + "live_abcdefghijklmnopqrstuvwx"  # made up, and split so no scanner reads it as a real key
    text = f"# OLD_KEY={fake}\nGOOD=1 # {fake}\nnonsense sk_live_x\n"
    out = ef.render(plan(text))
    assert "sk_live" not in out and "GOOD=1" in out


# ------------------------------------------------------------------ environment.toml


def test_env_files_in_environment_toml_names_paths_in_the_repository():
    assert repoconfig.parse('env_files = ["apps/api/.env", "./web/.env.local"]').env_files == [
        "apps/api/.env", "web/.env.local"]
    with pytest.raises(repoconfig.ConfigError, match="env_files"):
        repoconfig.parse('env_files = ["../../.ssh/id_rsa"]')


# ------------------------------------------------------------------ writing on the machine


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_write_script_puts_each_file_exactly_under_the_workdir(tmp_path):
    from openajar.environments.envfiles_cli import write_script

    content = ef.render(plan())
    tricky = "A='$HOME `x`'\nEOF\nOPENAJAR_ENV_\n"
    script = write_script(str(tmp_path), [("apps/api/.env", content), (".env", tricky)])
    r = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "apps/api/.env").read_text() == content
    assert (tmp_path / ".env").read_text() == tricky
    assert oct((tmp_path / ".env").stat().st_mode & 0o777) == "0o600"


# ------------------------------------------------------------------ the command


@pytest.fixture
async def vault(tmp_path):
    (tmp_path / "vault").mkdir()
    v = make_vault(private_home(tmp_path / "vault"))
    await v.start(control=False)
    yield v
    await v.stop()


@pytest.fixture
def pushed(tmp_path, monkeypatch, vault):
    from openajar.environments import envfiles_cli

    repo = tmp_path / "shop"
    (repo / "apps/api").mkdir(parents=True)
    (repo / "apps/api/.env").write_text(SAMPLE)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/me/shop.git"], check=True)
    monkeypatch.chdir(repo)
    calls: list[dict] = []
    env = SimpleNamespace(id="e7k2", title="Shop fix", repo="github.com/me/shop", workdir="/work/shop", open=True,
                          state="ready", secrets=[], terminal_lane="env/e7k2-t", vm_id="v1")
    mgr = SimpleNamespace(get=lambda i: env if i == "e7k2" else (_ for _ in ()).throw(ValueError(i)),
                          store=SimpleNamespace(open=lambda: [env]),
                          record_call=lambda lane, tool, **kw: calls.append({"lane": lane, "tool": tool, **kw}))
    monkeypatch.setattr(envfiles_cli, "_managers", lambda: (mgr, None))
    monkeypatch.setattr(envfiles_cli, "_vault", lambda: vault)
    written: list = []
    monkeypatch.setattr(envfiles_cli, "write_files", lambda vms, e, files: written.extend(files))
    return SimpleNamespace(env=env, written=written, calls=calls, vault=vault, cli=envfiles_cli)


def run(*args, input=None):
    from openajar.environments.cli import app

    return CliRunner().invoke(app, ["env", "push", *args], input=input)


def test_dry_run_shows_the_table_and_sends_nothing(pushed):
    r = run("e7k2", "apps/api/.env", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "apps/api/.env -> /work/shop/apps/api/.env on e7k2 (Shop fix)" in r.output
    assert "not supported as a placeholder" in r.output and "Nothing was sent (--dry-run)." in r.output
    assert not pushed.written and not pushed.calls
    assert not pushed.cli.asyncio.run(appsecrets.all_bound(pushed.vault))
    for value in _secret_values(plan()):
        assert value not in r.output


def test_without_a_terminal_it_asks_for_yes(pushed):
    r = run("e7k2", "apps/api/.env")
    assert r.exit_code == 1 and "run again with --yes" in r.output and not pushed.written


def test_push_binds_secrets_in_the_vault_and_writes_only_placeholders(pushed):
    r = run("Shop fix", "apps/api/.env", "--yes")
    assert r.exit_code == 0, r.output
    [(rel, content)] = pushed.written
    assert rel == "apps/api/.env" and "STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY}" in content
    bound = {b.var: (b.hosts, b.repo) for b in pushed.cli.asyncio.run(appsecrets.all_bound(pushed.vault))}
    assert bound == {"STRIPE_SECRET_KEY": (("api.stripe.com",), "github.com/me/shop"),
                     "DATAFORSEO_PASSWORD": (("api.dataforseo.com",), "github.com/me/shop")}
    [call] = pushed.calls
    assert call["tool"] == "env_push" and "not sent: STRIPE_WEBHOOK_SECRET" in call["summary"]
    everything = r.output + content + json.dumps(pushed.calls)
    for value in _secret_values(plan()):
        assert value not in everything
    assert "Bound STRIPE_SECRET_KEY to api.stripe.com" in r.output and "opens again" in r.output

    again = run("e7k2", "apps/api/.env", "--yes", "--json")  # bound now: placeholders, nothing bound twice
    keys = {k["key"]: k for k in json.loads(again.output)["files"][0]["keys"]}
    assert keys["STRIPE_SECRET_KEY"]["action"] == ef.PLACEHOLDER and "value" not in keys["STRIPE_SECRET_KEY"]
    assert keys["NODE_ENV"]["value"] == "development"


def test_files_of_another_repository_are_refused(pushed):
    pushed.env.repo = "github.com/me/blog"
    r = run("e7k2", "apps/api/.env", "--dry-run")
    assert r.exit_code == 1 and "these files are from github.com/me/shop" in r.output


def test_env_files_from_environment_toml_are_the_default(pushed, tmp_path):
    (tmp_path / "shop/.openajar").mkdir()
    (tmp_path / "shop/.openajar/environment.toml").write_text('env_files = ["apps/api/.env"]\n')
    r = run("e7k2", "--dry-run")
    assert r.exit_code == 0 and "apps/api/.env -> /work/shop/apps/api/.env" in r.output
