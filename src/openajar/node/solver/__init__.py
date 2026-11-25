"""The owner's own solving service for a site's "are you human" widget, in an environment's browser only.

Off by default: openajar ships no key and shares none. The owner turns it on with `openajar solver add` (their own
service, their own key, their own month's cap and site list), and an agent uses it only through one explicit tool,
browser_challenge (openajar.node.tools.challenge), whose steps are, in order: wait for the check to pass by itself,
the browser's own click, the owner's solver, the human (openajar.node.solver.steps).

- settings: what `openajar solver add` stores (secrets/solvers.json, chmod 600), and which sites it covers.
- services: CapSolver, 2Captcha and Anti-Captcha by their documented task APIs, and the command and webhook hooks.
- widgets: the widget's site key and kind, the sign-up, login and payment page guard, and handing the token over.
- ledger: the record of every try and the month's spend, checked against the cap before anything is sent.
- steps: the order above.

What it never does: solve on an account's browser (checks.allowed is the boundary, the same as the 0.6.1 click),
solve Cloudflare's full-page check, send a proxy or its credentials to a service, solve on a sign-up, login or
payment page, or pass the owner's cap. The key stays on this computer: it is in no tool result, log or trace, and
never reaches an environment's machine or a page.
"""
