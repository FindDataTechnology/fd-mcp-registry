# A2A agent backend addressing

**What this answers:** is a listed A2A agent's backend a real host, or is it this gateway
pointing at itself?

## Why it matters

The nginx generator picks the backend with `proxy_pass_url or url`. When
`proxy_pass_url` is empty and `url` is the gateway's own public entry
(`https://<gateway>/agent/<path>/`), the backend becomes the gateway. A forwarded
request then carries `Authorization` into `/validate`, passes, and is proxied to the
gateway's own public entry — where it starts over.

Nothing looks broken from the caller's side: the answer is plausible, and the registry's
health check is green (it probes the gateway, which is always up). The only evidence is
on the executor: **the runner logs the A2A method zero times.** Measured 2026-11-21, on
every A2A agent in this deployment.

## The three classes

`GET /api/agents` carries `backend: { status, url, reason }` for every A2A agent:

| `status` | meaning | what to do |
|---|---|---|
| `ok` | the backend is a non-gateway host | nothing |
| `missing` | no backend address at all (neither `proxy_pass_url` nor `url`) | point the backend, or finish the registration |
| `self_loop` | the backend **is** this gateway | set `proxy_pass_url` to the runner's agent address |

They are not a boolean because the remedy differs. `missing` is unfinished
configuration; `self_loop` is a known value filled in wrong.

Agents registered before this check existed are **labelled, not dropped**: the read face
returns them with `self_loop` and a reason. Silently hiding them would turn a registry
problem into "this agent does not exist", while it is still routed to the wrong place.

## The judgment

A hostname comparison on `host[:port]`, against `registry_url` plus any
`gateway_alias_hosts`:

- **The port is part of the identity.** The runner publishes many agents on one host
  (8891..8922); comparing hostnames alone would call every one of them the gateway.
  Verified: a first implementation that dropped the port flagged `localhost:9000` as a
  self-loop at a gateway on `localhost:8000`.
- **Default ports are normalized**: `https://gw` and `https://gw:443` are the same.
- **No DNS resolution.** A self-loop is exactly the thing that looks like an
  unreachable host from outside; the judgment must not need the network.
- **Health status is never consulted.** The health probe is built from the same
  fallback, so using it to judge a self-loop is circular.

## Write-side refusal

Registration and any update that touches `url` / `proxy_pass_url` refuse a
`self_loop` address with **409** and `code: BACKEND_SELF_LOOP`. `missing` is allowed on
write — a registrant may register the card first and point the backend later.

Updates that do NOT touch those fields (health status, enable/disable) are unaffected:
judging them would reject an unrelated write because of an address the operator has not
fixed yet, which is how a guard like this gets reverted.

## Backfilling an existing deployment

```bash
# 1) Plan. Writes nothing.
python scripts/backfill-a2a-agent-backends.py --runner-endpoints /tmp/runner-endpoints.json

# 2) Apply, saving old values.
python scripts/backfill-a2a-agent-backends.py --runner-endpoints /tmp/runner-endpoints.json \
    --apply --backup /tmp/a2a-backfill.json

# 3) Undo.
python scripts/backfill-a2a-agent-backends.py --revert /tmp/a2a-backfill.json
```

`--runner-endpoints` is a JSON file `{ "<agent path>": "<backend base URL>" }` built from
the runner's own `/health` face (`agents[].path` → `podIP:port`). It is a file rather
than something the script derives itself: the mapping lives in the deployment, and a
script that quietly queried a different source would produce "the fix ran but nothing
changed".

Matching is by **full path**, never by name similarity — production has two different
`.../d1da519d4` paths on different machines, and tail-matching would cross-assign them.
Rows with no runner endpoint are left untouched with a named reason; an all-or-nothing
failure would let one underivable row block everyone else.
