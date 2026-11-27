# Grant tokens: wire format v2

This is the specification of openajar grant tokens, precise enough to mint and verify them in
another implementation (the hosted cloud mints tokens that nodes verify). The reference
implementation is `openajar.policy.tokens` (format), `openajar.policy.grants` (block fields and
evaluation) and `openajar.policy.service` / `openajar.policy.issuers` (what a node checks).
Test vectors: `tests/fixtures/tokens/vectors.json`, checked by
`tests/unit/test_policy_token_vectors.py`.

Background: the security review items SEC-6, SEC-9 and SEC-10. Version 2 keeps v1's
cryptography and adds: revocation ids derived from signatures, issuer, audience and key id in
the authority block, canonical block bytes, and narrowed blocks that cannot carry a subject.

## Versions

| Version | Prefix | Minted by | Accepted |
|---|---|---|---|
| 1 | `agt1_` | nobody since v2 | during the migration window (below) |
| 2 | `agt2_` | nodes (`openajar grants issue`), the controller, the cloud | always |

Every block of one token has the token's version. A token never mixes versions.

## Framing

A token is the prefix followed by unpadded base64url (RFC 4648 section 5, no `=`) of:

```
token  = "AG" | u8 version | u8 nblocks | block{nblocks} | proof
block  = u32be len | data[len] | next_pub[32] | sig[64]
proof  = 0x01 | next_priv[32]      attenuable: the private key matching the last next_pub
       | 0x02 | seal_sig[64]       sealed: nobody can append
```

- `version` equals the prefix's version (`agt2_` and 2). `1 <= nblocks <= 12`, `len <= 8192`,
  the whole token is at most 32768 characters, and nothing follows the proof.
- v2: the base64url text must be exactly the encoding of the bytes (re-encoding the decoded
  bytes gives the same string), so one token has one string. Non-alphabet characters are refused.
- `next_pub` is a raw Ed25519 public key (32 bytes); `next_priv` a raw Ed25519 seed (32 bytes);
  signatures are raw Ed25519 (64 bytes). Verifiers must refuse `S >= L` (OpenSSL and libsodium
  do).

## Signatures

Block `i` (0 is the authority block) is signed by:
- block 0: the issuer's root key;
- block `i > 0`: the private key whose public half is block `i-1`'s `next_pub` (the previous
  holder's proof key, carried in the token).

The signed message for block `i` in v2 is:

```
"agentos.grant.block.v2" 0x00 | u32be i | u32be len(data) | data | next_pub | prev_sig
```

where `prev_sig` is block `i-1`'s `sig` (empty for block 0). A seal is a signature by the last
proof key over:

```
"agentos.grant.seal.v2" 0x00 | u32be len(data) | data | next_pub | sig      (all of the last block)
```

v1 used `"agentos.grant.block.v1" 0x00 | u32be i | data | next_pub | prev_sig` and
`"agentos.grant.seal.v1" 0x00 | data | next_pub | sig`. The context strings differ, so a
signature never transfers between versions.

To mint: sign the authority block with the root key, generate a fresh Ed25519 key as
`next_priv`, and emit proof `0x01 | next_priv`. To attenuate (anyone holding the token, offline):
sign the new block with the proof key, generate a fresh key, and replace the proof. To seal: sign
the last block with the proof key and replace the proof with `0x02 | seal_sig`.

## Canonical JSON (v2 block data)

`data` is the UTF-8 encoding of a JSON object in this canonical form, and verifiers refuse any
other bytes (SEC-10). A verifier parses, re-encodes, and compares bytes; it also refuses
duplicate keys and the `NaN` / `Infinity` extensions while parsing.

- No whitespace outside strings; `,` and `:` as separators; no byte order mark; no trailing
  newline.
- Object keys are non-empty ASCII strings, sorted by code point (byte order), each once.
- Strings: the characters themselves in UTF-8, except `"` as `\"`, `\` as `\\`, and the controls
  U+0000 to U+001F: `\b \t \n \f \r` for 8, 9, 10, 12, 13 and `\u00XX` (lowercase hex) for the
  rest. No other escapes (`\/` and `A` are refused). No lone surrogates. This is what
  Python's `json.dumps(s, ensure_ascii=False)` and JavaScript's `JSON.stringify(s)` emit.
- Numbers: an integer in `[-(2^53 - 1), 2^53 - 1]` in plain decimal (no `+`, no leading zeros,
  `0` never `-0`, no fraction, no exponent: `1.0` and `1e3` are refused). A non-integer has
  magnitude below `10^15`, at most 6 decimals, and is written as the shortest decimal that
  round-trips to the same IEEE 754 double, in plain notation (`0.000001`, never `1e-06`). In
  practice only `budget.usd` and `specials.brain.quota_usd` are fractional.
- `true` and `false`. No `null` anywhere: leave an unset field out.

Beyond the JSON form, the block must be exactly what the node's model of it re-encodes to:
fields are typed (an integer field holds a JSON integer, never `"5"` or `5.0`), values are
already normalized (lane patterns and domains as the node writes them, lowercase), defaults
that the node writes are written (`sub.kind`, `sub.owner`), and unset optional fields are
absent. The reference check is
`canonical(GrantBlock.model_validate(data).to_data()) == data` (`grants.parse_block`).

## Revocation ids (v2)

Block data never carries a revocation id. Each block's id is derived from its signature:

```
rid = "rv_" + lowercase(base32_nopad(SHA-256("agentos.grant.revocation.v2" 0x00 | sig)[0:20]))
```

(32 base32 characters). A v2 block whose data contains `rid` is refused. Since the signature
covers the block's index, data, next key and the previous signature, a rid names exactly one
block of one chain, and the registry keeps it UNIQUE (SEC-6). A revocation names a grant id
or a rid (see Revocation).

## Key ids

```
kid = "ed25519:" + lowercase_hex(SHA-256(raw 32-byte public key)[0:16])
```

## Block fields

All blocks: `v` (2), `id` (`gr_` plus 8 to 40 of `[a-z0-9]`; the cloud mints `gr_` ids too;
`owner` is reserved), `iat` (issue time, Unix seconds). Caveats (every block may set them; a
call must satisfy every block, so a block can only narrow): `workspaces`, `lanes`, `domains`,
`egress`, `ceiling`, `specials`, `budget`, `nbf`, `exp`, `depth`, `fanout`, `pins`, `admin`,
`note`, and `aud` (below).

**Audience (`aud`).** 1 to 16 distinct values `node:<id>` or `org:<org id>`. Required in a v2
authority block; an optional caveat in every other block, v1 blocks included (additive: a v1
token without `aud` is valid on any node that trusts its issuer). A node accepts a token only when
every block that sets `aud` names one of its audiences, so a later block can narrow an org-wide
audience to one node (`node:<id>` is inside `org:<its org>`) but can never drop or move one an
earlier block set: that block stays in the chain and is checked on its own. A node narrowing a
token (`attenuate`) refuses a child `aud` outside its parent's with the widening `aud`. The
cloud's per-call tokens name the node they are for this way. Their meaning is in `grants.py` and ROUTING.md; unknown fields are refused (fail
closed).

**Authority block (block 0)** also has:

| Field | Required | Value |
|---|---|---|
| `iss` | yes | the issuer: `node:<id>` (a node's own grants) or `org:<org id>` (controller, cloud) |
| `aud` | yes | 1 to 16 distinct values `node:<id>` or `org:<org id>`: where the grant is valid |
| `kid` | yes | the root key's id (above): selects the verification key |
| `sub` | yes | `{"kind": "agent"|"human"|"flow"|"brain"|"panel", "id", "owner", "thread"?}` |
| `exp` | yes | expiry, Unix seconds |
| `parent` | never | |

`iss`/`aud` ids match `[A-Za-z0-9._-]{1,100}`.

**A person's grant** (a member) is an ordinary v2 token whose
authority `sub.kind` is `human` (`sub.id` and `sub.owner` both the member id, `m_...`). Only such
a grant may carry the specials `approve` (`{"effects": [...], "lanes"?: [...], "max_usd"?: n}`)
and `control` (`{"lanes"?: [...], "watch_only"?: bool, "max_session_s"?: n}`); a verifier refuses
either in a chain whose authority subject is not human (`invalid_token`). A node never accepts a
human grant as a bearer caller (MCP, the panel's bearer path) and never narrows one. Narrowing
rules, should a block narrow them: `effects` and `lanes` subsets, `max_usd` lower (absent means
no pay decisions), `watch_only` only turns on, `max_session_s` lower.

**A member's call from a controller** is the one human grant a node takes from a caller: a
controller's panel acts for a signed-in member (open a lane, a screenshot, a flow run, a post)
with a per-call token whose subject is that person (`{"kind": "human", "id": <member>, "owner":
<member>}`; their id is the controller's, not an `m_...` of this node), narrowed to one lane or
workspace and the call's effect. `resolve` accepts it only when all of this holds, else
`invalid_token`:

1. it is signed by a trusted foreign issuer (the org of an enrolled controller), never by this
   node: a member of this node signs in to its panel, so this node's own human grants stay
   refused everywhere;
2. it is the `grant_token` of the channel `invoke` being run: the channel wraps that call's every
   resolve (the invoke, the policy wrapper, the tool list) in `PolicyService.channel_call(token)`,
   a context mark that only a token equal to it satisfies. MCP over HTTP, the panel's bearer path
   and everything else never set it;
3. it lives at most `MEMBER_CALL_MAX_TTL_S` (360 s), judged on this node's own clock, never the
   controller-adjusted one (a welcome moves that by up to an hour): `exp` minus `iat` and `exp`
   minus now are both at most 360 s, `iat` is not in the future beyond `clock_skew_s`, and `exp`
   is not past (the skew other grants get never extends a member call). The cloud's token lapses
   10 s past the call's deadline, at most 300 s for a call retried after its approval. One that
   lives longer is a standing credential and is refused ("lives at most 6 minutes");
4. its subject after every block is the person: a block appended offline that narrows it into an
   agent, flow or brain (`<member>/label`) is `invalid_token`, as for any person's grant.

The mark lasts exactly as long as the call: it is a holder that `channel_call` empties on the way
out, so a task the call started (a timer, a background read) no longer resolves the token once the
call ends. `resolve` and the mark read a token the same way (spaces and a `Bearer ` scheme off).

Otherwise it is a controller grant like any other: the controller ceiling clamps it (`Grant.limit`),
its own scope, ceiling and specials apply, a revocation by grant id or rid stops it, and the
principal in traces and approvals is the member (kind human), so activity reads who did it. It is
not "in person": it never co-signs its own action (ROUTING.md, "Members and approvers"), so what
needs a person asks for an approval, recorded under the member and their grant family. `resolve`
marks the grant `member_call`, and every lane gate that lets a person at the node through
(`openajar.core.tools.in_person`: a take over, Stop agent, a freeze, another caller's claim, the
grant's egress list) holds it as it holds an agent. A retry of a call that waited for approval
may bring a fresh token (the first one often lapses first); the node resumes the call with it only
when it is the same principal under the same grant family root and covers the call's lane at the
tool's effect.

**Narrowed blocks (block i > 0)** also have `parent` (the previous block's `id`) and never `iss`,
`kid` or `sub` (they may set `aud`, above): the authority's subject stays authoritative (SEC-5).
A narrowed block may act as a sub-agent:

| Field | Value |
|---|---|
| `label` | appended to the parent's subject id: `claude-code` + `codex-worker` acts as `claude-code/codex-worker`; `[A-Za-z0-9._:@-]` in `/`-separated parts; the whole id is at most 120 characters |
| `kind` | `agent`, `flow` or `brain`; present exactly when `label` is |
| `thread` | pins a thread when the parent has none; it can never move to another thread |

The owner never changes, and no block can make the subject `human` or `panel`.

## Verifying (what a node does)

1. Decode the framing (all the rules above). Decode every block's data as canonical JSON.
2. Choose the root key from the authority block: if `iss` is the node's own issuer id, `kid` must
   be its active key or a rotated key inside its overlap; otherwise `iss` must be a trusted issuer
   (`policy/issuers.py`, the org the node enrolled with) and `kid` one of its keys.
   Reasons: `unknown_issuer`, `unknown_key`, `retired_key`.
3. Verify every block signature in order with the chain of keys, then the proof: for `0x01`, the
   proof key's public half equals the last `next_pub`; for `0x02`, the seal signature verifies
   with the last `next_pub`. Any failure: `invalid_token`.
4. Check each block against the schema and the model-canonical rule; the chain (`parent` ids,
   no repeated ids, one version, the subject rules, `depth`): `invalid_token` (or `depth`).
5. Every block's `aud` (the authority's always, any other block's when set) must contain one of
   the node's audiences: its own `node:<local id>`, and once enrolled `node:<node id>` and
   `org:<org id>` from the controller. Else `audience`.
6. Time window (`nbf`, `exp` of every block, with clock skew), revocation, and for the node's own
   grants the registry: the root id must be registered, and every registered block's id is bound
   to its parent and the SHA-256 of its exact bytes (the root's too), so a root signed elsewhere
   with a shared key is not accepted. Foreign grants are never `admin`.

Denial reasons are stable (`openajar.policy.errors`): `invalid_token`, `unknown_issuer`,
`unknown_key`, `retired_key`, `audience`, `unregistered`, `revoked`, `expired`, `not_yet_valid`,
`foreign_admin`, `degraded`.

## Revocation

A node refuses a token when any block of its chain is revoked:
- locally (`openajar grants revoke <grant id | rid>`): by grant id (the registry binds an id to one
  block, so this is safe) or by a block's v2 rid;
- by the controller or cloud (`revoke.revocation_ids`, `welcome.revoked_grants`): ids that match
  any block's grant id or v2 rid.

Because a narrowed token carries its parents' blocks byte for byte, revoking a parent revokes
everything narrowed from it, offline children included. The rid of a v1 block (data its author
chose) is never used to revoke locally. Re-signing a block with a new key gives it a new rid
but not a new grant id, so revoke grants by id when in doubt.

## Key rotation

A node: `openajar grants rotate-key [--overlap 7d]` makes a fresh key active. The previous key
stays in `secrets/policy-<kid hex>.key`, listed in `secrets/policy-ring.json` with its retirement
time: grants it signed keep verifying until then and are refused with `retired_key` after it
(when its private half is deleted). `openajar grants keys` lists them; `openajar grants
retire-key <kid>` ends an overlap early. The approval-link key is saved at the first rotation,
so Review and Deny links stay valid. A running daemon notices a rotation made from the CLI.

An org (the controller, the cloud): give nodes the new public key. `TrustedIssuers.trust(issuer,
key)` adds it and schedules the issuer's other keys to retire after an overlap (7 days by
default; kept in `grant_issuers.db`, so it survives restarts); `retire(issuer, kid, at)` ends
one sooner. Sign new grants with the new key and its `kid`.

## The v1 migration window

New tokens are v2. v1 tokens keep verifying while `accept_v1_tokens = true` in policy.toml (the
default), and for a node's own grants only until its key is first rotated: rotating ends every
v1 grant of the node at once (`rotate-key` says how many). A v1 chain narrows as v1. v1 blocks
carry `rid` as data and may carry a full `sub` in narrowed blocks; the node already evaluates
them with the v2 rules (derived subject, egress from the authority, revocation by grant id).

## Who holds which credential

| Credential | Held by | Where | Can |
|---|---|---|---|
| The node token (`mcp_token` in node.toml) | the node, and whoever has SSH to it | `/var/lib/agentos/node.toml` only | everything: it is the owner, and it may issue admin grants |
| A connection's agent grant (`agt2_...`, subject `<client>@<computer>`, kind agent) | the agents on the owner's computer (Claude Code, Codex) | Keychain service `openajar`, read by the `openajar-mcp-headers` helper | MCP tool calls and the panel's bearer routes inside its scope (every lane, up to its ceiling, default commit); never decides an approval |
| The owner's device credential (an owner panel session, `via = device`) | the owner's CLI tools on that computer: `openajar dash`, `openajar approvals`, `openajar open` | Keychain service `agentos-owner` (a different item), never read by the helper | what a signed-in owner does in the panel: decide approvals (its agents' too), control lanes, view links, revoke grants; never MCP |
| A member's session | a member's browser | a cookie | their roles |
| A controller's agent token (`aga_...`) | the owner's agents, through a controller | Keychain service `openajar` | the controller's org grant; the owner decides in the controller's panel |

`openajar connect <ssh-host>` reads the node token over SSH, keeps it in memory, and uses it
through the tunnel for exactly two calls: `POST /api/session/device` (the device credential) and
`POST /api/grants` (the agent grant). It is never written on the computer. `openajar disconnect`
revokes the agent grant and ends the device credential on the node. Connections made before
this held the node token in the `openajar` item: `openajar connect --upgrade` moves them, `openajar
doctor` on the computer warns while one is left, and `openajar node rotate-token` (then a restart
of agentosd) makes every old copy useless.

### Why the owner's credential is a device session, not a human grant

Two shapes were possible for the owner's credential on a computer: a human grant (subject kind
human, owner subject, `approve` and `control`) or a panel-session-like device credential. It is
the device credential, because:

- **MCP refuses it by construction.** /mcp and the panel's bearer path accept only bearer tokens,
  resolved by `PolicyService.resolve`; a device credential is a panel session id, which is no token
  format at all, so it is refused (401) with no special case to keep right. A human grant would be
  a valid signed token that every bearer path must remember to refuse.
- **No exception to the grant rules.** "A node never accepts a human grant as a bearer caller" stays
  true. A human grant for the CLI would be the one human grant that is a bearer caller (on /api but
  not on /mcp), and "owner" is not a member id, so it would also be the one human grant outside
  `issue_member_grant`.
- **It already means what it should.** The panel treats an owner session as the owner in person: it
  decides without the grant family rule (a person, not a token) and is traced as a session. A device
  credential is that, with the computer's name as its device label and a longer life (90 days by
  default, at most 366).
- **Revocable per computer, and listed.** `DELETE /api/session` with it (what `openajar disconnect`
  does) ends it; it shows with the owner's devices (`GET /api/me/devices`). Only a hash of it is
  stored on the node (panel.db), like every session.
- **Only the node token can make one.** `POST /api/session/device` refuses grants, admin grants and
  sessions, so an agent's grant can never mint itself an owner credential. (The node token is
  already the owner: whoever holds it could issue an admin grant and approve with that, which is
  why it no longer leaves the node.)

What it does not protect against: a program running as the owner on that computer can read the
owner's Keychain items with `security`, as it could read `~/.ssh` and log in to the node. The
split is against agents that act only through MCP and the panel: they hold a grant that cannot
decide approvals, and nothing they are given opens the owner's credential.

## Family key

Taint, the denial cooldown and the pending cap are keyed by `family:<root grant id>`, the same
for v1 and v2 roots. The computers a grant reaches (environments, saves, logins bound by title) are
keyed on `Grant.env_family` instead: the root id, or the family a local agent grant continues from the
one it replaced (the registry's `env_family` column, set only by `issue(continues=...)`;
openajar.policy.families). The token's bytes do not carry it.

## Test vectors

`tests/fixtures/tokens/vectors.json` holds:
- `keys`: Ed25519 seeds (hex; seed = SHA-256("agentos token vector " + name)), public keys
  (base64url) and kids;
- `signing`: the vector authority block, its canonical bytes, the exact v2 signature input in hex,
  and the vector narrowed block and its canonical bytes;
- `verifier`: the trusted (`iss`, `kid`, public key) and the node audiences to verify with;
- `vectors`: tokens with the expected outcome (`valid` or a denial reason) and, for valid ones,
  each block's canonical bytes, signature and derived rid.

Tokens are deterministic: Ed25519 signatures are, and the vectors use fixed next keys
(`next-0`, `next-1`). An implementation should mint the valid vectors byte for byte from the keys
and blocks, and reach the expected outcome for every vector.
