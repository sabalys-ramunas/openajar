# Principles

These are the lines openajar is built on. They decide what goes in the core, what is a
paid-cloud concern, and what never ships. They come before features.

## 1. Offload, not impersonation

openajar exists to move work you already do onto machines that do not overload your
laptop. The browser engines, proxies and persistent profiles are there to reach parity
with normal use on your own accounts: log in and act the way you would from your own
home computer, just somewhere else. They are not there to defeat detection at scale or
to look like many different strangers.

In practice:

- Lanes are for your own accounts, or accounts whose owner has granted you access.
- One lane is one identity, kept stable over time, like one real person's computer.
- Anti-detect browser engines stay an optional, self-hosted plugin, never the center of
  the product and never part of the hosted cloud's core.

## 2. Own accounts and consented delegation only

- Automating your own accounts: yes.
- Acting for someone else: only through a route they consent to, and can revoke.
  Prefer the platform's own delegation (a Facebook Page role, an OAuth grant, a seller
  user invite) over anything that moves a password.
- Mass or fake account creation: no. When openajar reaches a hosted product, it will add
  controls specifically to stop it from being used that way.

## 3. The model never sees a secret

Passwords, API keys and one-time codes are resolved on the node, into the auth field or
the login form, at the moment of use. They are redacted from model context, logs,
screenshots and traces. A secret is bound to a domain and a lane, so a page cannot trick
an agent into using it somewhere else.

A password in the vault is the owner's permission to use it. Storing it is the consent, so a
login never asks: not on first use, and not after the agent read a page. What guards it is
the binding: the lane, the domain, https, the frame and the field. What the vault does not
hold still goes to a person: a captcha (unless a click or a solving service the owner set up
gets past it), a code it has no source for, a device
approval, a checkpoint.

## 4. Closed by default, encrypted, audited

- No public control plane. Nodes meet over a private mesh, not open ports.
- The vault is encrypted at rest, and in the hosted version it is end-to-end encrypted,
  so the cloud cannot read it.
- Every action an agent takes is recorded and replayable, tied to the grant that
  allowed it.

Security is the reason to run this yourself. It is the brand, not a setting.

## 5. Routines run on their own; a human approves what is hard to undo

The goal is automation. Each tool carries an effect class. Reading and drafting run on
their own, and so do posts, sends and other changes: a routine (a saved flow) or an
account asks a person first only when someone turned on "Ask me first" for it. The owner,
a member with the right role, or the agent that made the routine can turn it on; only a
person turns it off. Spending money and anything that cannot be undone always need a
person, by default from their phone, against the exact content. And when an agent acts
after reading text a stranger wrote (a web page, an email), it asks first and says why.

## 6. Permissions narrow as they pass down

When an agent hands work to a sub-agent, a CLI, a saved flow or a person, it can only
hand down a subset of what it holds, with a shorter life and a budget. Grants expire on
their own, revoking one revokes everything under it, and the node checks every call.

## 7. Say when it is an agent

Where a platform or the law asks for it (the EU AI Act's disclosure rule, a site's
agent policy), openajar labels its actions as automated rather than hiding them. Being
able to identify as an agent is treated as a feature.

## 8. Curated flows, not an open marketplace

Shared automations are a supply-chain risk (see the malicious-skill incidents in
RESEARCH.md). openajar starts with first-party, reviewed flows. If third-party sharing
ever opens, a flow must declare what it needs, run sandboxed in a lane with no raw
shell, come from a signed publisher, be scanned in full, and need an explicit approval
to install.

## 9. No phishing-shaped login pages

openajar will not serve a page that imitates a platform's login on a shared domain to
capture a credential. That is indistinguishable from a browser-in-the-middle phishing
kit, it gets domains blocklisted, and it breaks principle 2. Collaboration happens by
sharing a node or lane with a trusted person's own agent, or through official
delegation, never by harvesting their password through a link.
