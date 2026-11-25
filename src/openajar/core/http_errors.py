"""One error shape and one status map for every HTTP surface: the node's API, its MCP endpoint and
the controller (docs/ERRORS.md).

The body is always `{"error": {"code", "message", "fix"?, ...extra}}` (openajar.core.catalog adds the
person's fix). The status comes from the code, else from the error's class, else 500: an error nobody
classified is the server's fault, never the caller's. `/v1/sites` keeps its own problem+json shape
(openajar.runner.siteapis.problems), as that API specifies.
"""

from __future__ import annotations

from typing import Any

from openajar.core import catalog
from openajar.core.errors import InvalidRequest, NotFound, OpenajarError, PolicyDenied, ServiceUnavailable

STATUS_BY_CODE: dict[str, int] = {
    "invalid": 400,
    "unauthorized": 401,
    "denied": 403,
    "csrf_failed": 403,
    "step_up_required": 403,
    "presence_required": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "lane_not_found": 404,
    "lane_exists": 409,
    "busy": 409,
    "quarantined": 409,
    "frozen": 409,
    "ref_not_found": 409,
    "conflict": 409,
    "already_decided": 409,
    "confirm_required": 409,
    "approval_mismatch": 409,  # the approved card no longer covers what would run: nothing ran
    "lane_cold": 409,
    "login_step_changed": 409,
    # what a node answers about the lane's state: expected, the caller waits or tells a person
    "claim_lost": 409,
    "human_in_control": 409,
    "agent_paused": 409,
    "cancelled": 409,
    "source_retired": 409,  # a run from the removed engagement engine: cancelled, nothing sent
    "form_changed": 409,
    "publish_changed": 409,
    "too_large": 413,
    "clipboard_too_large": 413,  # what the lane copied is over the live view's limit
    "misdirected": 421,
    "rate_limited": 429,
    "internal": 500,
    "engine_error": 502,
    "unavailable": 503,
    "deadline_exceeded": 504,
    "timeout": 504,
    # Not a failure: the request was accepted and waits for a human co-sign.
    "approval_pending": 202,
    # ---- every other catalog code, each placed on purpose (tests/unit/test_http_errors.py) ----
    # 400: what was sent cannot be used as it is
    "bad_archive": 400, "bad_evidence": 400, "bad_export": 400, "bundle_invalid": 400,
    "connector_invalid": 400, "edit_mismatch": 400, "invalid_pattern": 400, "mail_confirm_hosts": 400,
    "mail_connect_failed": 400, "mail_id_taken": 400, "repo_connect_failed": 400, "missing_param": 400, "profile_invalid": 400,
    "secret_in_memory": 400, "secret_in_profile": 400, "template_invalid": 400, "vault_entry_invalid": 400, "vault_secret_too_long": 400,
    # 403: a rule refuses it, and asking again the same way will not change that. A run's grant that no
    # longer counts is 403 too, never 401: the request's own sign-in is fine, and a 401 signs the panel out
    "grant_expired": 403, "grant_not_yet_valid": 403, "grant_revoked": 403, "grant_unknown": 403,
    "bundle_rejected": 403, "connector_broken": 403, "egress_refused": 403, "lane_paced": 403,
    "mail_daily_limit": 403, "mail_suppressed": 403, "memory_refused": 403, "method_unavailable": 403,
    "not_a_read": 403, "out_of_scope": 403, "owner_only": 403, "owner_trust_required": 403,
    "passkey_invalid": 403, "pilot_lane_uncovered": 403, "pilot_refused": 403,
    "plugin_person_only": 403, "privacy": 403, "proof_too_weak": 403, "publish_unbound": 403,
    "trust_too_low": 403, "type_not_allowed": 403, "url_outside_domain": 403,
    "use_a_list": 403, "vault_code_refused": 403, "vault_denied": 403, "vault_wrong_passphrase": 403,
    # 404: it is not there (any more)
    "file_not_found": 404, "login_closed": 404, "mail_job_ended": 404, "mail_message_gone": 404,
    "mail_servers_not_found": 404, "share_invalid": 404, "vault_code_request_closed": 404,
    "vault_entry_not_found": 404, "workflow_missing": 404, "no_environment": 404,
    # 409: the state of the lane, the page or the run stands in the way; a person or a wait moves it
    "agenda_state": 409, "already_current": 409, "ambiguous_locator": 409, "clipboard_unreadable": 409, "approval_expired": 409, "approval_loop": 409,
    "approval_used": 409, "check_unknown": 409, "connector_contract_broken": 409, "deferred": 409,
    "dialog_pending": 409, "element_not_found": 409, "export_refused": 409, "identity_unknown": 409,
    "in_flight_unknown": 409, "mail_in_doubt": 409, "pilot_gave_up": 409, "pilot_held": 409,
    "pilot_interrupted": 409, "pilot_lane_off": 409, "pilot_needs_help": 409, "paused": 409, "plugin_requirements_unmet": 409,
    "preflight_mismatch": 409, "see_whole_card": 409, "readback_failed": 409, "refused": 409, "signal_blocked": 409,
    "signal_captcha": 409, "signal_checkpoint": 409, "signal_logged_out": 409, "stopped_by_answer": 409,
    "studio_blank_page": 409, "studio_dev_error": 409, "studio_state_failed": 409, "studio_theme_not_applied": 409,
    "target_covered": 409, "target_not_visible": 409, "target_unreachable": 409, "vault_busy": 409,
    "vault_code_declined": 409, "vault_entry_exists": 409, "vault_exists": 409, "vault_typing_aborted": 409,
    "vm_limit": 409, "wrong_account": 409, "wrong_element": 409, "wrong_surface": 409,
    "environment_limit": 409, "environment_full": 409, "unsaved_work": 409, "save_limit": 409, "keep_live_not_allowed": 409, "environment_in_use": 409, "environment_already_open": 409, "network_not_allowed": 409,
    "repo_not_connected": 409, "which_environment": 409, "environment_choice": 409, "repo_not_found": 404,
    "no_desktop": 409,  # no desktop on this environment (yet): the browser tools and terminal_exec still work
    "machine_gone": 409,  # the environment's machine stopped for good: it is closed, or opened again on the same title
    "checkpoint_failed": 409, "rollback_refused": 409, "checkpoint_not_found": 404,
    # 429: too many tries, or the pace is used up; it opens again by itself
    "pace_limited": 429, "vault_locked_out": 429, "pilot_day_limit": 429,
    # 502: something past the node (the browser driver, a site, a mail or storage server) failed
    "connection": 502, "driver_error": 502, "json_empty": 502, "json_invalid": 502, "json_status": 502,
    "json_too_large": 502, "json_unreadable": 502, "mail_not_sent": 502, "output_contract_violation": 502,
    "readback_error": 502, "storage_error": 502, "studio_http_error": 502, "studio_page_not_loaded": 502,
    "vm_error": 502, "environment_failed": 502, "save_failed": 502, "restore_failed": 502, "pull_request_failed": 502, "recording_failed": 502,
    "web_app_missing": 502, "web_app_refused": 502,
    # 503: not set up, not ready, or short of something on this node; a person sets it right
    "audit_unwritable": 503, "brain_unconfined": 503, "xclip_missing": 503, "decision_unavailable": 503, "identity_error": 503,
    "lane_format": 503, "lane_unavailable": 503, "mail_unavailable": 503, "no_sandbox": 503, "plugin_change_not_recorded": 503,
    "plugin_disabled": 503, "plugin_state_broken": 503, "policy_key": 503, "resources_low": 503,
    "unsupported": 503, "vault_key_insecure": 503, "vault_key_missing": 503, "vault_not_initialized": 503,
    "vault_sealed": 503, "vault_wrong_key": 503, "environment_not_ready": 503,
    # 504: the site did not answer in time
    "json_not_seen": 504, "json_slow": 504, "studio_timeout": 504,
}

# Besides `internal`, the codes that are the node's own failure, so 500 is right: a bug, or a store that broke.
SERVER_ERRORS: frozenset[str] = frozenset({
    "error", "backup_failed", "crash_loop", "export_failed", "host_error", "import_failed",
    "sandbox_error", "vault_code_catcher", "vault_corrupt", "vault_error", "vault_tampered"})

# Packages raise their own codes (vault_sealed, vault_entry_not_found, ...); those map by class.
STATUS_BY_CLASS: tuple[tuple[type[OpenajarError], int], ...] = (
    (InvalidRequest, 400), (PolicyDenied, 403), (NotFound, 404), (ServiceUnavailable, 503))

# What a bearer-token surface sends with a 401, so an MCP client knows to (re)authenticate.
WWW_AUTHENTICATE = {"WWW-Authenticate": 'Bearer realm="openajar"'}


def status_for(exc: OpenajarError) -> int:
    if exc.code in STATUS_BY_CODE:
        return STATUS_BY_CODE[exc.code]
    return next((status for cls, status in STATUS_BY_CLASS if isinstance(exc, cls)), 500)


def status_of(code: str) -> int:
    """The status for a bare code (no exception at hand); an unknown one, and SERVER_ERRORS, are 500."""
    return STATUS_BY_CODE.get(code, 500)


def error_body(code: str, message: str, *, surface: str | None = None, **extra: Any) -> dict[str, Any]:
    """The envelope, with the error catalog's fix for a person when the code has one (`surface`: the
    fix for the node's /mcp, the controller or the controller's /mcp, when it differs from the panel's)."""
    return catalog.error_body(code, message, surface=surface, **extra)
