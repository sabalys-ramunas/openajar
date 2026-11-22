"""The toolsets table: the node's tools in named groups (openajar.mcp.toolsets serves them).

It sits in core because node.toml's `mcp_toolsets` is checked against these names when the config
loads (openajar.core.config), `openajar connect` reads them without the MCP server, and the server
decides with them what one connection is shown. How a tool lands in a toolset, and what a
connection is shown, is in openajar.mcp.toolsets.
"""

from __future__ import annotations

from dataclasses import dataclass

CORE = "core"
FULL = "full"
OTHER = "other"
FULL_ALIASES = frozenset({FULL, "all"})


@dataclass(frozen=True)
class Toolset:
    name: str
    summary: str  # one short line an agent reads in tools_more's description
    tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()  # a tool no toolset names lands in the first toolset sharing a tag
    hint: str = ""  # a few words for tools_more's description, when the name alone does not say it


TOOLSETS: tuple[Toolset, ...] = (
    # core: the loop of an agent on its own computer (docs/design/tool-surface-review.md, the owner's
    # decision of 2026-11-19): open, keep, close; move work in and out; the terminal and its files; the
    # browser and its proof; the pull request; an undo point; a port for the human; signing in. Everything
    # else is one tools_more away, the owner's account browsers in `accounts`.
    Toolset(CORE, "your own computer: open, save, close, its terminal, files and browser, proof, the pull request", (
        "environment_open", "environment", "environment_save", "environment_close",
        "environment_upload", "environment_download",
        # its terminal: what nearly every chat with one runs
        "terminal_exec", "terminal_job_start", "terminal_job_read", "terminal_job_cancel",
        # its files: without these an agent writes files through shell quoting (heredocs, sed), which breaks
        # on the first quote in the code
        "file_read", "file_write", "file_edit",
        "browser_navigate", "browser_snapshot", "browser_act", "browser_wait", "browser_screenshot",
        "browser_tabs", "browser_dialog", "browser_wait_handback", "browser_record",
        # the end of nearly every coding task, and the undo before a risky step: testers went through
        # tools_more and guessed arguments blind when these were a toolset away (2026-11-18)
        "environment_pull_request", "environment_checkpoint", "environment_rollback", "environment_port",
        "login", "approval_wait",
    )),
    # the owner's account browsers (signed in to real accounts on the node), which `core` held until
    # 2026-11-19: an agent driving them loads this first (the openajar-lanes skill says so)
    Toolset("accounts", "the owner's account browsers: list, open, claim and release one, its live view, "
            "files to upload, asking a person, workspace memory, a run's status", (
        "whoami", "howto", "browsers_list", "browser_open", "browser_close", "browser_claim", "browser_release",
        "browser_view", "browser_file_put", "browser_upload", "approval_confirm", "human_ask",
        "memory_context", "run_status",
    ), (), "the owner's signed-in browsers"),
    Toolset("browser", "more for a browser: its inbox and downloads, scrolling a feed", (
        "browser_files", "browser_file_get", "browser_file_delete", "browser_scroll",
    ), ("files",), "files, scroll"),
    Toolset("devtools", "why a page broke: its console messages and page errors, its requests and which "
            "failed, and one response's body", (
        "browser_console", "browser_network",
    ), ("devtools",)),
    Toolset("control", "sharing account browsers: who has one, asking a person for it, the queue, a safety check, "
            "a freeze", (
        "browser_control", "browser_request_control", "browser_queue", "browser_check", "browser_freeze",
    ), ("control",), "who has a browser, asking for it, queue, freeze"),
    Toolset("vault", "stored logins: what the vault holds, filling one login field, asking for a log in", (
        "vault_list", "vault_fill", "browser_login_request",
    ), ("vault",), "stored logins"),
    Toolset("flows", "saved flows and their runs: list, read, run, record, save, cancel, Ask me first", (
        "flows_list", "flow_get", "flow_run", "flow_save", "flow_record_start", "flow_record_stop", "run_cancel",
        "ask_first_on",
    ), ("flows",), "saved flows and runs"),
    Toolset("workflows", "routines (workflows): a trigger such as a schedule, a webhook or an email, then "
            "steps; save, propose, run, retry, and the leads and opt-outs their mail steps use", (
        "workflow_catalog", "workflow_list", "workflow_get", "workflow_save", "workflow_propose",
        "workflow_run", "workflow_runs", "workflow_retry", "workflow_discard", "leads_propose", "send_suppress",
    ), ("workflows",), "routines on a schedule or a trigger"),
    Toolset("connectors", "installed connector operations, one tool per site operation (like "
            "instagram_post_photo), and their health", (
        "connectors_list", "connector_status",
    ), ("connectors",), "site operations"),
    Toolset("site_apis", "a site's data as typed endpoints: find what a page repeats, try a draft, ask a "
            "person to turn it on, list them", (
        "site_api_discover", "site_api_try", "site_api_propose", "site_apis_list",
    ), ("site_apis",), "a page's data as an API"),
    Toolset("mail", "mailboxes: find yours, list, read, mark, send, and asking a person to connect one", (
        "mail_mailboxes", "mail_list", "mail_read", "mail_mark", "mail_send", "mail_connect",
    ), ("mail",)),
    Toolset("memory", "workspace memory: search, read, write and correct (forget erases, and needs an "
            "irreversible grant and the owner)", (
        "memory_search", "memory_get", "memory_write", "memory_forget",
    ), ("memory",)),
    Toolset("profiles", "who the work is for: a person's or business's bios and voice, a product's brand and "
            "rules (profile_use)", (
        "profile_list", "profile_get", "profile_use", "profile_propose", "profile_save", "profile_update",
        "profile_attach", "profile_detach", "profile_sync_status", "profile_mark_synced", "profile_photo_put",
        "profile_learn_start", "profile_learn_finish", "profile_learn_brand",
    ), ("profiles",)),
    Toolset("studio", "is a page all right: ui_check (layout, contrast, targets, names, health, design system), "
            "ui_diff (what a change did), system_check, baselines, a finding or an image on request", (
        "ui_check", "ui_diff", "system_check", "ui_baseline", "finding_get", "ui_image", "studio_status",
    ), ("studio",), "check a page you build"),
    Toolset("workspaces", "workspaces: list, plan and apply changes, whether an account browser is signed in", (
        "workspace_list", "workspace_queue", "workspace_plan", "workspace_apply", "browser_set_readiness",
    ), ("workspaces",)),
    Toolset("worker", "the node's workers: hand a browser job to the cheap worker on the node (browser_task), "
            "and which workers are on", (
        "browser_task", "plugins_list",
    ), ("plugins",), "hand a browser job to the node"),
    Toolset("admin", "your grant and the call trail", (
        "grant_info", "traces_query",
    ), ("policy", "approvals", "traces"), "grant, call trail"),
    Toolset("terminals", "terminals and services the node keeps: shells, background jobs, synced files, installs, "
            "images, disposable cloud VMs; forgetting an environment's saves", (
        "terminal_create", "terminals_list", "terminal_remove", "terminal_install", "terminal_upload_check",
        "terminal_upload", "terminal_jobs", "shell_open",
        "shell_read", "shell_write", "shell_close", "service_start", "service_stop", "service_status",
        "service_logs", "image_build", "images_list", "vm_create", "vm_destroy", "vm_extend", "vm_clone",
        "vms_list", "environment_forget", "environment_app_restart",
    ), ("env",), "shells, services, raw VMs"),
    Toolset("screen", "your environment's desktop, for apps that are not web pages: a picture of the screen and its "
            "windows, real clicks, keys and typing, starting an app", (
        "screen_snapshot", "screen_act", "screen_open",
    ), ("screen",), "a desktop app's windows"),
    Toolset("brains", "agent tasks on the node's brains: run, follow, resume, cancel; the git remotes they reach", (
        "brains_list", "brain_run", "brain_status", "brain_tail", "brain_tasks", "brain_resume",
        "brain_cancel", "brain_use", "git_remotes",
    ), ("brains", "git"), "agent tasks on the node"),
    Toolset("automation", "events, triggers and the agenda "
            "(planned one-off actions, their approval, runs and proof)", (
        "events_list", "triggers_list", "trigger_propose",
        "agenda_list", "agenda_get", "agenda_draft", "agenda_update", "agenda_propose", "agenda_cancel",
    ), ("events", "agenda"), "events, triggers, agenda"),
)
NAMES: tuple[str, ...] = tuple(t.name for t in TOOLSETS)
CORE_TOOLS: frozenset[str] = frozenset(next(t for t in TOOLSETS if t.name == CORE).tools)
# a toolset's name before 2026-11-19 -> its name now (node.toml's mcp_toolsets, a URL's ?toolsets=)
TOOLSET_ALIASES: dict[str, str] = {"files": "browser"}
