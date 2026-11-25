"""The owner's own solving service for a site's "are you human" widget, in any browser (an environment's or an
account's).

Off by default: openajar ships no key and shares none. The owner turns it on with `openajar solver add` (their own
service and key), and an agent uses it only through one explicit tool, browser_challenge
(openajar.node.tools.challenge), whose steps are, in order: wait for the check to pass by itself, the browser's own
click, the owner's solver, the human (openajar.node.solver.steps).

- settings: what `openajar solver add` stores (secrets/solvers.json, chmod 600).
- services: CapSolver, 2Captcha and Anti-Captcha by their documented task APIs, and the command and webhook hooks.
- widgets: the widget's site key and kind, Cloudflare's full-page markers, and handing the token over.
- ledger: the log of every request to a service, with the cost it reported.
- steps: the order above.

What it never does: send Cloudflare's full-page check or a widget inside another site's frame to a service (no
token from one works there), or send a proxy or its credentials anywhere. The key stays on this computer: it is in
no tool result, log or trace, and never reaches an environment's machine or a page.
"""
