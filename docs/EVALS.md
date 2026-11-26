# Evals: is openajar getting better for the agents that use it?

The environments eval runs the loop an agent really does in openajar (open a machine, change code,
run tests, start a dev server, look at the page, push, open the pull request, close or save and
come back) against a node on this computer, and measures three things: did the work get done (the
experience), how long it took (speed), and how many tokens of tool output the agent had to read
(token efficiency). Run it before and after a change to a tool, a result's shape, a description,
the skill or the machine's setup, and compare.

It lives in `tests/env_eval/`. It needs Docker and the environment image
(`uv run openajar environments setup --pull`, once). Nothing in a run reaches a real repository or
a cloud: the repositories are local bare repositories, GitHub's pull request API is a stand-in, and
every machine is a container. The one thing that leaves this computer is task 5's `npm install`
of one small public package. The free suite costs nothing; a model run costs what its model
reads and writes (see "Paid runs").

Since 0.5.0 it is the one eval suite: the second one, `evals/` (built on 2026-11-20 while 0.4.0 was
released elsewhere), was merged into it ("What came from evals/" at the end). The studio has its own
eval of the same kind (`tests/studio_eval/`, for `ui_check` and friends), and the lanes have their
benchmarks ([BENCH.md](BENCH.md)).

## Run it

```sh
make eval                                                     # the free suite: the perfect and careless scripts
uv run python tests/env_eval/run.py                           # the same
uv run python tests/env_eval/run.py --task t01 t03            # some tasks (ids or prefixes)
uv run python tests/env_eval/run.py --agent claude --model haiku --task t01 --repeat 3
uv run python tests/env_eval/run.py compare                   # the newest run against the one before
uv run python tests/env_eval/run.py compare --against 5693c8e # ... or against a commit's run
uv run python tests/env_eval/run.py list                      # the tasks
uv run python tests/env_eval/run.py clean                     # what a killed run left in Docker
uv run python tests/env_eval/run.py smoke                     # the budgets: no node, no Docker, no model
make eval-smoke                                               # the same
```

A run prints each task as it goes, then a table, then what changed against the previous run of the
same kind. `--out DIR` keeps the results (`results.json`), every call with its whole result
(`<agent>/<task>-r<n>/calls.jsonl`), each node's log and a model's transcripts
(`session<n>.jsonl`); without it a new temporary folder is used and printed. `--keep` also keeps
each node's home and repositories there. Ports: the node on 7467, the proxy on 7468, the GitHub
stand-in on 7469 (`--port` moves all three).

The free suite takes about 17 minutes on a Mac with Docker Desktop (15 tasks, both scripts). It exits 1 unless `perfect`
finishes every task and `careless` none (that proves the checkers), or when a model agent errored.

## What a run does

For each task (and each repeat) the harness, `tests/env_eval/node.py`:

1. Makes a scratch home with openajar's local node (`openajar local serve`'s composition, with the
   environment's browser on the machine), the docker provider, the task's `environments.toml`, and
   an agent grant as `openajar local` gives Claude Code. It is never your `~/.openajar`, and it never
   registers anything with your Claude Code or Codex.
2. Commits the task's fixture (`tests/env_eval/repos/<name>/`) into a new bare repository and
   declares it for environments as `github.com/evals/<name>` through the git gateway
   (`openajar git remote allow env ... --url <path>`, as an owner does), with a made-up token in the
   vault. Pushes go to that bare repository; `environment_pull_request` goes to the stand-in
   (`ghstub.py`), which checks the branch is really there.
3. Starts the node and runs the agent, one session per prompt, each in a fresh empty folder with a
   new MCP session. Every agent reaches the node through the recording proxy (`proxy.py`), so every
   agent is measured the same way, from the MCP side.
4. Runs the task's checker over the real end state, then stops the node and removes every container,
   network and image it made (also after an error or Ctrl-C), and the scratch home.

The docker provider's image is tagged `openajar-eval-<id>:base` for each node, so every save, snapshot
and prepared machine of a run is an image of that repository alone and goes with it.

## The agents

| Agent | What it is | Costs |
|---|---|---|
| `perfect` | A script that does what the `openajar-environment` skill teaches. It must finish every task, and it is a repeatable benchmark of the canonical loop: its time and the tokens of tool output it reads. | nothing |
| `careless` | A script that takes a plausible wrong turn per task (leaves the machine running, sleeps instead of waiting on the port, retries a refused host, opens a new machine instead of resuming). It must finish none. | nothing |
| `claude` | `claude -p` with your own Claude Code sign-in (a subscription or an API key), only the run's node as MCP server (`--mcp-config` with `--strict-mcp-config`: none of yours), no built-in tools (`--tools ""`) and only that server's tools allowed, the `openajar-environment` skill appended to its system prompt, none of your skills, commands, hooks or settings (`--disable-slash-commands`, `--setting-sources project` in an empty folder), no session saved (`--no-session-persistence`), `--max-turns` (60), `--model`, `--output-format stream-json`, and `--max-budget-usd` as each session's hard cap. It writes nothing into `~/.claude`'s skills or settings. When the harness runs with a scratch HOME, `OPENAJAR_EVAL_AGENT_HOME` names the HOME `claude` gets (where it is signed in). | the model's tokens (or your subscription's usage) |
| `codex` | `codex exec --json` with a scratch `CODEX_HOME` whose `config.toml` names only the openajar server (the token in `OPENAJAR_EVAL_TOKEN`, `bearer_token_env_var`), the skill as `AGENTS.md` in its folder, `--sandbox read-only`, and your Codex sign-in linked in. Built to Codex's documented CLI and not yet run against a real Codex. | the model's tokens |
| `cmd` | Any other command, with `--agent-cmd 'mytool --mcp {mcp_config} {prompt}'`. It gets {prompt}, {mcp_config} (a Claude-style MCP JSON file), {url}, {token_file}, {skill}, {model} and {transcript}; stdout is read as stream-json when it is that. | yours |

A model agent's prompt is the task's prompt as a human would type it, and one line asking it to end
with `{"status": ..., "summary": ...}`.

## The tasks

One Python file each in `tests/env_eval/tasks/` (`tNN_<what>.py`, with `TASK = Task(...)`): the
prompt of each session, the fixture repository, the node's settings, the checker, and the two
scripts.

| Task | The human asks | Finished when (the checker reads the end state) |
|---|---|---|
| `t01_fix_push_pr` | fix the failing tests, push the fix on a branch, open the pull request | a new branch is in the bare repository; its tests pass in a clean checkout here; the stand-in has a pull request for it; main is unchanged; every environment is closed and its container gone |
| `t02_dev_server_proof` | start the dev server, check the home page in the browser, keep a screenshot as proof | the node keeps a proof with a title, of `localhost:3000`; the environment's browser showed the app's page; closed and gone |
| `t03_continue_tomorrow` | day one: start, keep the machine, leave a note; day two: "continue Todo export in openajar", push, open the pull request | day two's open resumed the save (no second machine); the note came back naming what was next (the second prompt does not say it); every test passes at the pushed branch; a pull request; closed and gone |
| `t04_prepared_warm` | three times: open the storefront (it has `.openajar/environment.toml`) and say its first price | the app listened at each open; the second booted from the prepared machine (the harness waits for the build); the third took the warm machine (the harness turns `warm = 1` on and waits for one); every answer names the price; closed and gone. The three open times are the point: `open1_s`, `open2_s`, `open3_s` |
| `t05_blocked_host` | on a `limited` network: install the dependencies (npm is allowed) and run a script that calls a host that is not | the script got past its dependency; the node refused the host; the script ran at most twice (no retry loop); the answer names the host and the owner's command (`openajar environments allow --host ...`); closed and gone |
| `t06_checkpoint_rollback` | a risky change: checkpoint first, roll back if the tests fail | a checkpoint before the first edit; a rollback; the tests ran and passed after it; nothing broken pushed; the answer says the tests failed. Needs `environment_checkpoint`: skipped on an openajar without it |
| `t07_video_proof` | on a `limited` network: change the heading, record the page in the browser, push and open the pull request with the recording | a recording kept as proof with a title; the browser showed the new heading; a branch with it; a pull request for it; the recording's files reached the stand-in's `agentos-proofs` branch and the body shows its GIF; closed and gone. Needs `browser_record`. A machine from an image without ffmpeg installs it on the first recording, through the proxy |

| `t08_probes` | (the scripts only) one call each, for what testers found wrong: a command cut off by its time limit, a command with no `cwd`, a relative `cwd`, a `cwd` that does not exist, two million characters of output, a command that reads input with none given | each call came back as the 2026-11-20 review's fixes say it must (the next step named, no wrapper's line; `/work`; `/work/probe-dir`; an error with a fix line; cut and marked; a quick failure); closed and gone |
| `t09_slow_command` | run the release build (about 80 s, longer than a command waits by default) and say its build id | the build's last line reached the agent through `terminal_exec` or `terminal_job_read`; the answer has the id; closed and gone |
| `t10_checkout_form` | start a shop on the machine and order with its browser: a quantity, text, an email, a select, radio buttons and check boxes; leave the machine open | the shop itself recorded exactly one order with the right products, quantities, buyer, delivery and gift wrap (read on the machine the agent left open, `ctx.on_machine`); the answer has its number |
| `t11_desktop_app` | type a command into the desktop's own Terminal window, where the human watches; leave the machine open | the file the command writes is on the machine; the line went in through `screen_act`; the agent's terminal tool did not write it. Needs `screen_act` |
| `t12_machine_dies` | run the release build and say its build id; if anything goes wrong with the machine, sort it out; leave the machine open | the harness removed the machine 12 s into the build (`docker rm -f`, a lost host, a session's `during` step; a container that only stopped is started again since 0.8.0); the agent took another machine; the build's `dist/build-id.txt` is on the machine left open; the answer has the id |
| `t13_files_in_out` | `sales.csv` in this folder: put it on a machine, run the repository's script on it there, bring `summary.json` back into this folder; leave the machine open | `summary.json` is in the person's folder (the harness's, `Task.local`); its totals are right; it names the machine's host name; it is byte for byte the file the machine made (`ctx.on_machine`). A model runs the links' commands with only `curl`, `tar`, `git`, `ls`, `cat`, `mkdir`, `cp`, `mv` in that folder (`host_tools`), and only with `--host-shell`: without it the task is skipped for a model |
| `t14_wait_dialog` | on the shop: build the monthly report and say its reference; delete one draft (it asks to confirm); leave the machine open | the shop recorded the report built and exactly that draft deleted; the answer has the reference (it shows about 5 s after the click: `browser_wait`, and `browser_dialog(accept=true)` for the confirm) |
| `t15_find_price` | on the shop: what does one product cost in its catalogue | a browser result showed the product with its price (never on the catalogue's first page: a consent overlay, pages, a search box); the answer has the price; closed and gone |

A task can name tools it needs (`requires`); on a node without them it is skipped and says so. A task
the scripts alone can run (`scripted_only`, the probes) is skipped for a model agent. The
stand-in answers the Git Data API calls the proofs make (blobs, trees, commits, the ref) in memory.
A session's `during` is the harness's own step while the agent works (t12 kills the machine), stopped when
the session ends. A task's `local` writes the person's folder: a model agent works in it instead of an empty
one, with the shell commands in `host_tools` and nothing else (Claude Code's permission rules; Codex gets
`--sandbox workspace-write` there). The four tasks from `evals/` (t12 to t15) are hermetic: the shop and
the files never leave this computer. A task that touches the public internet says so (`internet = True`,
t05's `npm install`).

To add a task: a fixture under `repos/` if none fits, then one file in `tasks/`. Run the scripts
(`--task tNN`) and see `perfect` finish it and `careless` fail it before a model runs it. Every wrong
turn seen in real use should become a task or a rule here.

## The metrics

Per task run (`results.json`), and per task in the history as the median over the repeats:

| Metric | What |
|---|---|
| `finished` | the checker passed and no session errored (in the history: the rate over the repeats) |
| `seconds` | the task's wall time, every session, from the first prompt to the checker (the harness's own waits between sessions in t04 are inside it) |
| `ready_s` | from the agent's start to the first result that said its environment is `ready` |
| `first_navigate_s` | the first `browser_navigate`'s own time: on a fresh machine it waited for the browser to start |
| `opens` | each `environment_open`: the time until ready, `new` or `resumed`, the app listening, prepared, warm |
| `calls`, `calls_by_tool`, `seconds_by_tool` | the agent's tool calls (a `tools_call` counts as the tool it ran), and where the time went |
| `tool_errors`, `errors_by_code` | calls that came back as errors, by their code |
| `tokens_read` | the text of every tool result the agent got back, in tokens (characters / 4, the estimate the toolset budget test uses). The same measure for a script and a model |
| `surface_tokens`, `surface_tools` | the tool list the agent's session starts with, estimated the same way (`tests/unit/test_toolsets.py`'s budget measures the same list) |
| `instructions_tokens` | the server's instructions at `initialize` |
| `wrong_turns` | what an agent following the skill would not have done, one line each (below) |
| model: `turns`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd` | from Claude Code's stream-json `result` event (Codex: its `turn.completed` events), summed over the sessions |

The wrong turns (`metrics.wrong_turns`), each one a rule the skill states:

- a call refused for want of the `on` its `environment_open` gave (`which_environment`,
  `no_environment`), whether it passed none or another value;
- the name of an environment's terminal or browser as `on` (`env/<id>`, `env/<id>-browser`, `<id>-browser`), refused or not (Haiku passed
  the open's `lanes.browser` once; a chat whose session holds the machine now reaches it so);
- a file read or written through `terminal_exec` (`cat`, `head`, `sed -n`, `sed -i`, a heredoc, `echo >`)
  instead of `file_read`, `file_edit` or `file_write` (a `tail` at the end of a pipe
  reads no file and is fine);
- a `sleep` of 2 s or more in a command (in a loop it counts ten times), or the same job read three
  times without `until_exit` or `until_port`: waiting by hand where the machine waits for you;
- the same call repeated unchanged right after it failed (an error, or a command's non-zero exit), in
  the same chat;
- a new environment opened past what the task needs.

## Reading the compare

Every run appends one line per agent to `bench/evals.jsonl`: when, the commit (and whether the tree
was dirty), a hash of the host's name, the agent and model, the repeats, the tasks run, and per
task the medians. `run.py` then compares it with the latest earlier line of the same agent, model,
host and task set, and `run.py compare` does the same for the newest line (or `--against <commit>`):

```
perfect: 0bd3c1a (dirty) against 5693c8e: 2 better, 1 worse, regressed: t02_dev_server_proof.tokens_read
  t02_dev_server_proof.tokens_read           2216 -> 2790       +25.9%  REGRESSION
  t04_prepared_warm.open2_s                   5.21 -> 3.9        -25.1%  better
```

A metric regresses when it is worse by more than its threshold and by more than its noise floor,
so a run that is one second or a hundred tokens off is not flagged:

| Metric | Threshold | Noise floor |
|---|---|---|
| `finished` | any drop | |
| `wrong_turns`, `tool_errors` | any rise | 0.5 (a median) |
| `*_s` (seconds) | 25% | 3 s |
| `tokens_read` | 10% | 300 tokens |
| `surface_tokens` | 3% | 60 tokens |
| `instructions_tokens` | 5% | 40 tokens |
| `calls` | 20% | 1.5 calls |
| model `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens` | 15% / 20% / 20% / 20% | 2,000 / 500 / 20,000 / 5,000 |
| `turns` | 20% | 2 |
| `cost_usd` | 20% | $0.02 |

`compare` exits 1 on a regression; `run.py --fail-on-regression` does the same for a run. Times on
Docker move with what else the computer does: for a speed claim, `--repeat 3` both sides.

### How sure a number is

The scripts are deterministic, so one run a side shows a broken task. A model is not: with
`--repeat`, a task's `finished` rate changes for real only when Fisher's exact test gives p below
0.05, and "likely" below 0.15 (`env_eval.stats`); anything else is "same", with the numbers. The
compare prints the strength and p beside the change. With 3 runs a side, 3 of 3 against 0 of 3 is only
likely (p = 0.10); with 5 a side it is real (p = 0.008), which is why a release compares 5 runs.
Seconds and tokens keep their thresholds and noise floors above; on a loaded computer read the times
as provisional.

## The smoke checks

`run.py smoke` (and `make eval-smoke`) reads the product's own definitions, with no node, no Docker
and no model, and holds them to `tests/env_eval/budgets.toml`: every error in the catalog has a step
for the agent and a fix for the person; the environment skill's length; the everyday tool list's
count, its longest description, all of them together, and its arguments with no description (none).
They are read by every agent in every chat, so their size is a cost. The limits are ratchets, a little
above the product today: lower one when the product shrinks, raise one only with a reason in the
commit. They run in CI with the unit tests (`tests/unit/test_env_eval.py`), next to the default list's
token budget in `tests/unit/test_toolsets.py`.

## Gate a release with it

1. On the release candidate, on a quiet computer: `make eval-smoke`, then `make eval`. `perfect`
   finishes every task, `careless` none, and the compare against the last release's run shows no
   regression.
2. `--agent claude --model <the day-to-day model> --repeat 5` on the tasks a change touched (all of
   them for a minor release), compared with the same against the release before: no task regresses,
   real or likely. Read the transcript of a task that sometimes passes.
3. Every real bug a tester finds becomes a check: a probe in `t08_probes` when one call shows it, a
   task when it needs an agent's judgement.

## A/B between two commits

`--ref <commit>` runs the suite on another commit's openajar under this harness: a temporary
worktree of that commit (removed afterwards), whose `src/` the node, the owner commands and the
skill come from. The tasks, checkers and metrics stay this checkout's, so both sides are measured
the same way:

```sh
uv run python tests/env_eval/run.py --ref main --repeat 3       # before
uv run python tests/env_eval/run.py --repeat 3                  # after (this checkout)
uv run python tests/env_eval/run.py compare --against main      # what changed
```

A ref needs the same Python dependencies as this checkout (`uv sync` here first when they differ).

## Paid runs

A model run prints its estimate before it starts, refuses to start when the estimate is above
`--max-usd` (default $2), and gives each session `--max-budget-usd` of `--max-usd` divided by the
sessions, so the whole run cannot spend more. The estimate is a long session of these tasks
(about 400,000 tokens read from the cache, 50,000 written to it, 10,000 more input and 6,000 output)
priced at the dearest model an alias may mean: about $0.14 a session for `haiku` at Haiku 4.5's
prices, $0.43 for `sonnet`, $0.71 for `opus`. The suite is 18 sessions, so `--model haiku` on all
of it estimates about $2.56. The real cost comes from the transcript (`cost_usd` in the table), and
is far lower: on 2026-11-18 `haiku` meant Haiku 5.5, and t01 and t02 cost $0.015 together.
For a model the table does not know (a Codex model), give your own estimate of one session with
`--session-usd`; Codex has no hard cap of its own, so keep its runs small.

Model runs use your Claude Code or Codex account and its usage limits: one or two tasks, once, is
enough to see a change; a whole-suite `--repeat 3` is for a release.

Before any node starts, a model run checks its sign-in (`claude auth status`; for Codex its `auth.json` or
`OPENAI_API_KEY`). Without one it stops with exit 2 and one line that says what to provide and where:

```
env eval: the claude agent cannot run: Claude Code is not signed in for HOME=... (`claude auth status`): run
`claude auth login` once in this shell, or set ANTHROPIC_API_KEY to an Anthropic API key, then run the same
command again. Nothing ran and nothing was spent.
```

### The standard model baseline

The default route is your own `claude` CLI, signed in as you use it every day (`claude auth status` says
`loggedIn`): no API key, no other config. One command, from your own shell, on a quiet computer:

```sh
OPENAJAR_EVAL_IMAGE=openajar-env:0.5.0-eval make eval \
  EVAL_ARGS="--agent claude --model claude-sonnet-5-5 --repeat 3 --max-usd 40"
```

Every task three times with Sonnet 5.5, the model an owner's agent uses by default, one task run at a time:
13 tasks (t08, the probes, is for the scripts only; t13 needs a shell on this computer and runs for a model
only with `--host-shell`). It appends a `claude (claude-sonnet-5-5)` line to `bench/evals.jsonl` and compares
it with the one before. The estimate is $23.09 for its 54 sessions (hence `--max-usd 40`: each session is
capped at $0.74); what the CLI reported for the run of 2026-11-21 was $3.96, about 40 minutes. On a
subscription that is usage, not money. The image is a build of this tree (below); without
`OPENAJAR_EVAL_IMAGE` it is `agentos-env:latest`.

The alternative is an Anthropic API key: `export ANTHROPIC_API_KEY=...` in that shell, then the same command
(`claude` uses the key, and then the CLI's cost is what is billed, about $4 for this baseline). A quick check
of a change is one task, once: `EVAL_ARGS="--agent claude --model claude-sonnet-5-5 --task t01"` (about
$0.08). For a release gate, `--repeat 5 --max-usd 60`.

The image, once, from the top of a checkout (Docker's cache makes it seconds after the first build):

```sh
mkdir -p /tmp/openajar-env && cp deploy/environments/* src/openajar/environments/setup.sh \
  src/openajar/environments/browser.sh src/openajar/environments/browser-requirements.txt \
  src/openajar/environments/desktop.sh /tmp/openajar-env/ && docker build -t openajar-env:0.5.0-eval /tmp/openajar-env
```

## The baseline

The free suite on a Mac with Docker Desktop, 2026-11-18 (`perfect`; `careless` finished none):

| Task | Seconds | Ready s | Opens s | Calls | Tokens read |
|---|---|---|---|---|---|
| t01_fix_push_pr | 18.8 | 6.2 | 5.7 | 8 | 2,161 |
| t02_dev_server_proof | 29.8 | 4.8 | 4.8 | 8 | 2,220 |
| t03_continue_tomorrow | 26.1 | 5.1 | 5.1 new, 4.6 resumed | 11 | 4,030 |
| t04_prepared_warm | 86.5 | 17.9 | 17.9 cold, 5.3 prepared, 3.3 warm | 12 | 4,687 |
| t05_blocked_host | 17.2 | 5.5 | 5.4 | 5 | 2,678 |
| t06_checkpoint_rollback | skipped (no checkpoints on this branch) | | | | |

Each session starts with 38 tools of about 9,480 tokens and instructions of about 900. The first
`browser_navigate` of a machine takes 10 to 14 s (its browser starting), the largest single wait
after a cold open. `--agent claude --model haiku` (Haiku 5.5) finished t01 and t02 in 32 s and 47 s
for $0.015, with 5 wrong turns between them: files read with `cat` through `lane_exec`, and a
`browser_navigate` with `on` set to the environment's `lanes.browser` instead of its `on`.

After the fixes for those (2026-11-18, `envs/axfixes`): `perfect` and `careless` still finish all and
none; t05 reads 1,668 tokens instead of 2,992 and makes 4 calls instead of 5 (the refused host is in
the command's own result, no `environment()` needed); every other metric within its noise. The
browser starts during the open, so it is ready by the time an agent that thinks between calls
navigates: Haiku's first `browser_navigate` took 3.3 s instead of 15.6 s, and t01 and t02 took 30 s
and 30 s for the same $0.015, with 3 wrong turns (the `cat`s, before the hint) and no tool error. A
script that navigates a second after the open still waits for most of the start (t02: 6.8 s, was
7.7 s). `t07_video_proof` finishes in 45 s, 25 of them installing ffmpeg on this image.

The tool surface of 2026-11-19 (docs/design/tool-surface-review.md, as the owner decided it): the
default list is the environment loop, the tools have their new names (`terminal_exec`, `file_read`,
...), `file_read` takes several paths, and t06 counts a change that passes. The history counts a call
by an old name as its new tool (`lane_exec` is `terminal_exec`), so older runs stay comparable.

| | Before (2026-11-18) | The prototype | Now |
|---|---:|---:|---:|
| tools at the start of a session (local node) | 38 | 29 | 30 |
| their tokens | 9,485 | 7,799 | 8,201 |
| `perfect`: finished, calls, tokens read, wrong turns | 7 of 7, 62, 19,850, 0 | 7 of 7, 62, 19,887, 0 | 7 of 7, 62, 19,678, 0 |
| `careless`: finished | 0 of 7 | 0 of 7 | 0 of 7 |
| Haiku t01: finished, calls, errors | yes, 9, 1 | yes, 7, 0 | yes, 7, 0 |
| Haiku t06: finished, calls, tokens read | no, 18, 7,765 | no, 16, 3,512 | yes, 10, 3,396 |
| Haiku: files read through the terminal | 3 | 3 | 3 (each one `cat` inside a git status command) |
| Haiku cost, both tasks | $0.022 | $0.017 | $0.015 |

One live run each, so a direction. Haiku read the repository with `file_read(paths=[...])` in both
tasks instead of a `cat` loop; what is left is a `cat package.json` inside its first `git status`
command. It called the pull request, the checkpoint and the rollback directly. In t06 it made the
change, the tests failed, it rolled back and said what broke. The scripts' times moved with Docker's
noise (the compare calls several tasks 20 to 40% slower against the prototype's run; no tool on
those paths changed).

The review's phase 2, the same day: three merges (`browser_tabs(select=...)`, `browser_dialog(accept=...)`,
`approval_wait(wait_s=0)`), `wait_s` for every wait in the list, and descriptions without "Effect:" lines,
"Errors:" lists or lists of returned fields. The first live run of it, with `on` described as
"From environment_open.", went backwards: Haiku left `on` out of `file_write` and `file_edit` three times,
passed the open's id as `on` once, and t06 did not finish (its last test run was hidden in a command that
also committed). `on` went back to "The on environment_open gave" and the open says to keep its whole handle
(50 tokens); the run after that is the "Now" column.

| | Before (the surface above) | `on` as "From environment_open." | Now |
|---|---:|---:|---:|
| their tokens | 8,201 | 6,961 | 7,013 |
| `perfect`: finished, calls, tokens read, wrong turns | 7 of 7, 62, 19,678, 0 | 7 of 7, 62, 19,802, 0 | 7 of 7, 62, 19,745, 0 |
| `careless`: finished | 0 of 7 | 0 of 7 | 0 of 7 |
| Haiku t01: finished, calls, errors | yes, 7, 0 | yes, 8, 1 | yes, 10, 1 |
| Haiku t06: finished, calls, tokens read | yes, 10, 3,396 | no, 16, 3,513 | yes, 9, 3,384 |
| Haiku: `on` left out or wrong | 0 | 4 | 1 (`file_edit` without it) |
| Haiku: files read through the terminal | 3 | 3 | 3 |
| Haiku cost, both tasks | $0.015 | $0.018 | $0.015 |

One run each, so a direction: the trims cost nothing measurable once `on` keeps its words, and the
words of `on` matter more than their tokens.
The scripts' times of the last free run are slower across the board because the whole unit suite ran
on the same machine at the time; no tool on their paths changed.

## What came from evals/

A second suite, `evals/`, was built on 2026-11-20 beside the private line's desktop work, while the
released 0.4.0 shipped this one. 0.5.0 keeps one: this one, because people run it (`make eval`,
`run.py` and its flags, `bench/evals.jsonl`). From `evals/` it took what this one lacked:

| From evals/ | Here |
|---|---|
| the static checks and their budgets (in CI) | `run.py smoke`, `tests/env_eval/budgets.toml`, `tests/unit/test_env_eval.py` |
| how sure a change is: Wilson's interval, Fisher's exact test, real and likely | `env_eval.stats`, used by the compare for `finished` over repeats |
| probes, one call each, for what testers found | `t08_probes` |
| `recover-slow-command`, `browse-checkout-form` and its shop fixture | `t09_slow_command`, `t10_checkout_form`, `tests/env_eval/repos/shopsite/` |
| `machine-dies`, `files-in-out`, `browse-wait-dialog`, `browse-find-price` (ported 2026-11-21) | `t12_machine_dies`, `t13_files_in_out` with `tests/env_eval/repos/report/`, `t14_wait_dialog`, `t15_find_price` (the same shop) |
| the desktop and its apps (planned there, never built) | `t11_desktop_app` |
| a checker that reads the machine the agent left open | `Ctx.on_machine` |

Left behind, with why: its own runner, drivers, MCP client and results folder (this harness has its
recording proxy, its agents and `bench/evals.jsonl`); its fixture image built on the machine image (the
fixtures here are repositories cloned like a user's); the tasks that repeat this suite's
(`open-run-close`, `build-test-fix`, `git-branch-push` are t01's loop, `save-resume` is t03,
`devserver-fix-ui` is t02); and the two that need the live web (`repo-open-live`, `repo-install-test-live`).
Its fixture image's `/opt/evals` is not needed: the slow build and the shop are fixture repositories here,
and t13's script names the machine by its host name instead of an image's stamp. Its first smoke baseline
of 2026-11-21 measured `evals/`'s own checks; the baseline here is this harness's run below.

## The 0.5.0 baseline

The free suite on the merged 0.5.0 tree, 2026-11-21, on a Mac with Docker Desktop, from a machine image
built from this tree (`OPENAJAR_EVAL_IMAGE=openajar-env:0.5.0-rc`: the desktop and ffmpeg in one image).
`perfect` finished 11 of 11, `careless` none. The computer was busy (a load average far above its
CPUs), so read the seconds as provisional.

| Task | Seconds | Ready s | Opens s | Calls | Tokens read |
|---|---|---|---|---|---|
| t01_fix_push_pr | 13.5 | 4.1 | 3.7 | 8 | 2,147 |
| t02_dev_server_proof | 20.4 | 3.5 | 3.5 | 8 | 2,265 |
| t03_continue_tomorrow | 19.8 | 3.7 | 3.7 new, 3.7 resumed | 11 | 4,055 |
| t04_prepared_warm | 64.4 | 16.6 | 16.6 cold, 4.0 prepared, 2.5 warm | 12 | 4,699 |
| t05_blocked_host | 11.5 | 3.6 | 3.5 | 4 | 1,700 |
| t06_checkpoint_rollback | 12.3 | 3.7 | 3.7 | 7 | 2,175 |
| t07_video_proof | 24.6 | 3.3 | 3.2 | 12 | 3,005 |
| t08_probes | 11.9 | 2.7 | 2.6 | 9 | 5,929 |
| t09_slow_command | 91.2 | 3.6 | 3.5 | 4 | 1,681 |
| t10_checkout_form | 60.7 | 3.7 | 3.7 | 19 | 3,877 |
| t11_desktop_app | 19.7 | 2.6 | 2.6 | 7 | 3,459 |

Each session starts with 30 tools of about 7,140 tokens and instructions of about 950. The desktop
adds nothing a script waits for: opens stay at 3 to 4 s, as in 0.4.0's runs. The `perfect` wrong turns
(3) are the probes' own refused `cwd` and the desktop task's check of its file with `cat`.

## The baseline with the four ported tasks

2026-11-21, on the 0.5.0 tree with t12 to t15, a Mac with Docker Desktop (10 CPUs), from a machine image built
from this tree (`OPENAJAR_EVAL_IMAGE=openajar-env:0.5.0-eval`, the commands above). The computer was busy
with other work: during the free run its 1-minute load was 10 at the start, 40 in the median and 188 at its
peak (load sampled every 30 s); during the model run 6, 21 and 259. Read the seconds as provisional. Each
session starts with 30 tools of about 7,140 tokens and instructions of about 950.

The free suite (`make eval`): `perfect` finished 14 of 15, `careless` none.

| Task | Finished | Seconds | Ready s | Opens s | Calls | Tokens read |
|---|---|---|---|---|---|---|
| t01_fix_push_pr | yes | 12.9 | 3.8 | 3.5 | 8 | 2,173 |
| t02_dev_server_proof | yes | 21.0 | 3.7 | 3.6 | 8 | 2,242 |
| t03_continue_tomorrow | yes | 20.1 | 3.5 | 3.5 new, 4.1 resumed | 11 | 4,052 |
| t04_prepared_warm | yes | 77.7 | 17.2 | 17.2 cold, 4.3 prepared, 2.4 warm | 12 | 4,696 |
| t05_blocked_host | yes | 11.6 | 3.2 | 3.2 | 4 | 1,699 |
| t06_checkpoint_rollback | yes | 16.7 | 5.8 | 5.7 | 7 | 2,210 |
| t07_video_proof | yes | 29.2 | 3.4 | 3.4 | 12 | 2,982 |
| t08_probes | yes | 13.8 | 2.7 | 2.7 | 9 | 5,928 |
| t09_slow_command | yes | 94.5 | 3.9 | 3.9 | 4 | 1,665 |
| t10_checkout_form | yes | 64.3 | 4.2 | 4.2 | 19 | 3,861 |
| t11_desktop_app | no (below) | 32.0 | 2.2 | 2.2 | 16 | 4,445 |
| t12_machine_dies | yes | 131.6 | 4.2 | 4.2, 7.3 the second machine | 7 | 3,282 |
| t13_files_in_out | yes | 10.1 | 3.6 | 3.5 | 4 | 2,039 |
| t14_wait_dialog | yes | 34.5 | 3.4 | 3.3 | 11 | 3,251 |
| t15_find_price | yes | 25.3 | 3.4 | 3.3 | 7 | 3,878 |

The four new tasks, each with `careless` failing for its own reason: t12's reads the lost job again and
reports the failure (no second machine, no build id); t13's runs the download's commands on the machine with
`terminal_exec` (nothing comes back to the folder); t14's reads the page right after the click (no reference)
and leaves the dialog unanswered (the draft stays); t15's answers from the catalogue's first page.

The model baseline (the command above): `claude-sonnet-5-5` on the owner's Claude Code sign-in, every task
three times, medians. It finished 31 of 39 runs; after the two checker fixes below, t05 and t06 ran three times
again (their own line in the history), so the suite's real figure is 34 of 39 (t05 and t06 rows: the rerun). In all, the baseline and the rerun: 459 turns, 786 input,
74,251 output, 7.9 million cache-read and 709,034 cache-write tokens, $4.38 as the CLI reports it (on the
subscription: usage), and 2,642 s of task time.

| Task | Finished | Seconds | Calls | Tokens read | Wrong turns | Turns | Cost |
|---|---|---|---|---|---|---|---|
| t01_fix_push_pr | 3 of 3 | 31.9 | 7 | 2,684 | 2 | 8 | $0.078 |
| t02_dev_server_proof | 0 of 3 | 33.9 | 5 | 1,936 | 1 | 6 | $0.074 |
| t03_continue_tomorrow | 3 of 3 | 84.7 | 12 | 5,142 | 2 | 14 | $0.150 |
| t04_prepared_warm | 3 of 3 | 91.4 | 9 | 4,694 | 0 | 12 | $0.153 |
| t05_blocked_host | 3 of 3 (0 of 3 before its checker fix) | 21.0 | 3 | 1,710 | 0 | 4 | $0.055 |
| t06_checkpoint_rollback | 2 of 3 (2 of 3 before) | 29.8 | 8 | 3,001 | 3 | 9 | $0.087 |
| t07_video_proof | 3 of 3 | 38.9 | 11 | 3,199 | 2 | 12 | $0.098 |
| t09_slow_command | 3 of 3 | 99.2 | 4 | 1,686 | 0 | 5 | $0.056 |
| t10_checkout_form | 3 of 3 | 101.5 | 21 | 5,538 | 0 | 22 | $0.149 |
| t11_desktop_app | 3 of 3 | 50.0 | 13 | 4,617 | 2 | 15 | $0.124 |
| t12_machine_dies | 3 of 3 | 145.5 | 11 | 5,172 | 2 | 12 | $0.114 |
| t14_wait_dialog | 3 of 3 | 55.1 | 12 | 3,472 | 0 | 13 | $0.101 |
| t15_find_price | 2 of 3 | 34.5 | 6 | 2,899 | 0 | 7 | $0.076 |

t08 is for the scripts only; t13 was skipped (`--host-shell` was not given: no shell on the owner's Mac).

What it found:

- The eval's own checkers, fixed: t05 counted no run of the script when it was in the same command as
  `npm install` (Sonnet's single, right run failed it three times out of three); t06 took a file written
  through the terminal for no change, and a test run in the same command as the change for one before it.
- The product, open: a machine that dies is not noticed. After the harness kills t12's container the job
  reads `state: "lost"` with the note "the node could not reach the machine over ssh to start it" (it had
  started; it died), `environment()` still says `working` with no error, `terminal_exec` comes back `ok` with
  exit 255 and ssh's "Connection refused", and `environment_open` with the same title gives back the dead
  machine as `attached`, `ready`. `environment_close` is the one call that says it ("its machine does not
  answer ... close it with discard=true"). Sonnet got there each time by opening a second title, after
  starting the build again, running commands and trying a save on the dead machine. `perfect` closes with
  `discard=true` and opens again.
- The product, open: on the desktop, the browser's window (it starts with the environment) comes to the
  front after `screen_open` opened LXTerminal, so the script's `screen_act` typed into Camoufox (`"front":
  "camoufox"` in its result) and t11 failed for `perfect` three runs out of three this day (it passed on the
  0.5.0 baseline above). Sonnet finished t11 three times: it looks at the screen and clicks the window first.
- The agent, open: Sonnet leaves the machine open when the work is done and nobody asked for it (t02 three
  times, t06 and t15 once): it gives no `keep`, so the default save holds it, and it never calls
  `environment_close`, though the skill and the server's instructions say to. In t12 it first asked for
  `keep="live"` because the prompt says "leave the machine open". The commonest wrong turn is still reading
  files with `cat` through `terminal_exec`.

## 0.5.1: the dead machine, the desktop's focus, and the defaults

2026-11-21, the fixes for what the baseline above found, on the same Mac and image
(`openajar-env:0.5.0-eval`), the computer less busy (1-minute load 8 to 26).

- **The dead machine (t12).** The node asks the provider when a command's ssh cannot reach the machine,
  the tunnel drops, `environment()` finds git silent, or an open names the title (docs/ENVIRONMENTS.md,
  "Costs and the reaper"). After the harness's `docker kill` the job read came back `lost` about 3 s later
  with "the job ended with its machine, which stopped while it ran" and the next step, and
  `environment_open` with the same title ended the dead environment and started a new machine
  (`replaced`). `perfect` now does just that (no close with `discard=true`): its second open took 3.6 s
  instead of 7.3 s. `careless` still fails.
- **The desktop's focus (t11).** The browser starts in the background when the environment opens, and its
  first window came up while `screen_act` typed, so openbox gave it the keyboard (`perfect` failed 3 of 3,
  the last one here with `"opened": [camoufox]` in the type's result). openbox now gives a new browser window
  no focus and the desk helper puts the app back in front during the browser's start; `screen_act` says
  `keys_to`. `perfect` finished t11 3 of 3 alone and in the suite.
- **The defaults (the skill, the instructions, the open's `next`).** Close when the task is done and nobody
  asked to keep it, whatever the `keep`; files only with the file tools; `machine_gone` means the same title.

The free suite: `perfect` 15 of 15 (it was 14), `careless` 0 of 15. Against the run before: t11 finished, 16
calls to 7, 4,445 tokens read to 3,603, 11 wrong turns to 2; the suite's wrong turns 12 to 3.

`--agent claude --model claude-sonnet-5-5 --task t11 t12 t02 --repeat 3` (t02 for the close), medians:

| Task | Before: finished, seconds, calls, wrong turns, cost | Now |
|---|---|---|
| t02_dev_server_proof | 0 of 3, 33.9, 5, 1, $0.074 | 3 of 3, 31.6, 7, 1, $0.085 |
| t11_desktop_app | 3 of 3, 50.0, 13, 2, $0.124 | 3 of 3, 29.1, 7, 1, $0.078 |
| t12_machine_dies | 3 of 3, 145.5, 11, 2, $0.114 | 3 of 3, 118.8, 7, 0, $0.085 |

In t02 Sonnet now ends with `environment_close(note=...)` each time (it left the machine open 3 of 3
before). In t12 it reads the lost job, opens the same title once and builds again: no retry on the dead
machine, no command on it, no save tried, no second title. In t11 it types with `window=` set to the
Terminal's id. The one error left in t11 and t12 is `keep_live_not_allowed`: both prompts say "leave the
machine open" and Sonnet asks for `keep="live"` first, then opens with the default. The wrong turns left are
`cat` through `terminal_exec`: `cat /work/desktop.txt` to check t11's file, and `ls; cat package.json` as t02's
first look, once a run each (t02 also one `sleep 2` before a curl). The nine runs cost $0.79 (on the
subscription: usage).
