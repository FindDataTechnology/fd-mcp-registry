# mcpgw MCP Server Test Script

## Overview

This script comprehensively tests the mcpgw MCP server by exercising all 5 tools through the FastMCP streamable-http protocol. **It also demonstrates WHY the `Mcp-Session-Id` header forwarding in nginx is absolutely necessary.**

## What It Tests

The script performs the following operations in order:

1. **Initialize MCP Session** - Establishes a session and captures the `Mcp-Session-Id`
2. **Send Initialized Notification** - Completes the MCP handshake
3. **List Available Tools** - Discovers all 5 tools provided by mcpgw
4. **Test Each Tool**:
   - `list_services` - Lists all MCP servers in registry
   - `list_agents` - Lists all agents in registry
   - `list_skills` - Lists all skills in registry
   - `intelligent_tool_finder` - Semantic search for tools
   - `healthcheck` - Gets registry health status
5. **Verify Session Persistence** - Calls a tool again using the SAME session ID to prove session continuity

## Why Mcp-Session-Id Header Is Required

### The Problem Without Header Forwarding

FastMCP's streamable-http transport uses **stateful sessions**:

```
Client                    Nginx                   mcpgw Server
  |                         |                          |
  |-- POST /mcp ----------->|-- forward -------------->|
  |    (initialize)         |                          |
  |<------------------------|<-- Mcp-Session-Id: abc --|
  |                         |                          |
  |-- POST /mcp ----------->|-- forward (MISSING ID!)->|
  |    tools/list           |                          |
  |<-- 404 Session Not Found|<-------------------------|
```

**Without nginx forwarding `Mcp-Session-Id`**, the mcpgw server receives requests without session context and returns `404 Session not found` errors.

### The Fix

Added to [nginx_service.py:1110](../registry/core/nginx_service.py#L1110):
```nginx
proxy_set_header Mcp-Session-Id $http_mcp_session_id;
```

This ensures:
```
Client                    Nginx                   mcpgw Server
  |                         |                          |
  |-- POST /mcp ----------->|-- forward -------------->|
  |    (initialize)         |                          |
  |<------------------------|<-- Mcp-Session-Id: abc --|
  |                         |                          |
  |-- POST /mcp ----------->|-- forward + Session ✓ -->|
  |    Mcp-Session-Id: abc  |    Mcp-Session-Id: abc   |
  |<-- tools list -----------|<-------------------------|
```

## Usage

### Prerequisites

1. **Token file**: Create `.token` file in project root with your bearer token
   ```bash
   # Extract token from roo's config (already done)
   cat /home/ubuntu/.vscode-server/data/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json | \
     jq -r '.mcpServers["mcp-gateway-tools"].headers.Authorization' | \
     cut -d' ' -f2 > .token
   ```

2. **Dependencies**: Requires `jq` and `curl`
   ```bash
   sudo apt-get install -y jq curl
   ```

### Run the Test

```bash
# From project root
./scripts/test-mcpgw-tools.sh

# Or with custom URLs
MCPGW_URL=https://mcpgateway.ddns.net/mcpgw/mcp \
TOKEN_FILE=.token \
./scripts/test-mcpgw-tools.sh
```

### Expected Output

```
=== MCP Gateway Tools Test Script ===
MCPGW URL: https://mcpgateway.ddns.net/mcpgw/mcp
Registry URL: https://mcpgateway.ddns.net

✓ Token loaded from .token

=== Step 1: Initialize MCP Session ===
→ Request: initialize (id=init-1)
{
  "jsonrpc": "2.0",
  "method": "initialize",
  "params": {...},
  "id": "init-1"
}
  Session created: abc123def456...
← Response:
{
  "jsonrpc": "2.0",
  "id": "init-1",
  "result": {...}
}

✓ Session initialized successfully

=== Step 3: List Available Tools ===
✓ Found 5 tools: list_services, list_agents, list_skills, intelligent_tool_finder, healthcheck

=== Step 4: Test All Tools ===
--- Testing: list_services ---
✓ list_services: Found 12 services

--- Testing: list_agents ---
✓ list_agents: Found 3 agents

--- Testing: list_skills ---
✓ list_skills: Found 8 skills

--- Testing: intelligent_tool_finder ---
✓ intelligent_tool_finder: Found 3 results

--- Testing: healthcheck ---
✓ healthcheck: Status=success

=== Step 5: Verify Session Persistence ===
Calling list_services again with the SAME session ID...
This proves that Mcp-Session-Id must be forwarded by nginx!

✓ Session persistence verified: Found 12 services

=== Test Summary ===
✓ Session ID: abc123def456...
✓ All 5 tools tested successfully
✓ Session persistence verified

Key Insight:
Without the Mcp-Session-Id header being forwarded by nginx,
the FastMCP streamable-http transport cannot maintain sessions.
Each request would create a NEW session, causing 404 errors
when clients try to reuse session IDs.

This proves the nginx configuration change is NECESSARY!
```

## Troubleshooting

### Error: Session Not Found (404)

If you see `404 Session not found` errors, it means:
1. The nginx configuration is NOT forwarding `Mcp-Session-Id` header
2. Run `docker exec mcp-gateway-registry-registry-1 grep -i "mcp-session" /etc/nginx/conf.d/nginx_rev_proxy.conf` to verify
3. Expected: `proxy_set_header Mcp-Session-Id $http_mcp_session_id;`

### Error: 401 Unauthorized

If you see `401 Unauthorized` errors:
1. Check your `.token` file contains a valid bearer token
2. Verify token hasn't expired
3. Test token directly: `curl -H "Authorization: Bearer $(cat .token)" https://mcpgateway.ddns.net/api/servers`

### Connection Refused

If connection fails:
1. Verify mcpgw container is running: `docker ps | grep mcpgw`
2. Check container logs: `docker logs mcp-gateway-registry-mcpgw-server-1`
3. Verify nginx is forwarding correctly: `docker logs mcp-gateway-registry-registry-1 | grep mcpgw`

## mcpgw Server Architecture

### Tools Overview

| Tool | Registry API | Description |
|------|-------------|-------------|
| `list_services` | `GET /api/servers` | Lists all registered MCP servers |
| `list_agents` | `GET /api/agents` | Lists all registered agents |
| `list_skills` | `GET /api/skills` | Lists all registered skills |
| `intelligent_tool_finder` | `POST /api/search/semantic` | Semantic search for tools |
| `healthcheck` | `GET /api/servers/health` | Registry health statistics |

### Token Flow

```
1. User stores token in .token file
2. Script reads token: cat .token
3. Script sends: Authorization: Bearer <token>
4. Nginx forwards to mcpgw: Authorization: Bearer <token>
5. mcpgw extracts from Context: _extract_bearer_token(ctx)
6. mcpgw forwards to registry APIs: Authorization: Bearer <token>
7. Registry validates and processes request
```

### Session Management Flow

```
1. Client: POST /mcp (initialize)
   → mcpgw: Creates session, returns Mcp-Session-Id

2. Client: POST /mcp (tools/list) + Mcp-Session-Id
   → nginx: MUST forward Mcp-Session-Id header
   → mcpgw: Looks up session, processes request

3. Client: POST /mcp (tools/call) + Mcp-Session-Id
   → nginx: MUST forward Mcp-Session-Id header
   → mcpgw: Reuses same session, maintains context
```

## Related Files

- [mcpgw server.py](../servers/mcpgw/server.py) - MCP server implementation
- [nginx_service.py](../registry/core/nginx_service.py#L1110) - Nginx config with Mcp-Session-Id forwarding
- [Issue #583](https://github.com/agentic-community/mcp-gateway-registry/issues/583) - mcpgw rewrite
- [PR #584](https://github.com/agentic-community/mcp-gateway-registry/pull/584) - Implementation PR

## Proof of Necessity

This script **empirically proves** that the `Mcp-Session-Id` header forwarding is not optional:

1. **Step 1 (Initialize)**: Creates session, receives `Mcp-Session-Id` in response
2. **Step 3 (List Tools)**: Sends `Mcp-Session-Id` in request - nginx MUST forward it
3. **Step 4 (Tool Calls)**: Each tool call reuses the same session ID
4. **Step 5 (Persistence)**: Calls same tool again - proves session is maintained

**Without nginx forwarding this header**, FastMCP's session manager in mcpgw would be unable to match incoming requests to existing sessions, resulting in `404 Session not found` errors.

The architectural change from the old mcpgw (which managed its own sessions internally) to the new mcpgw (stateless HTTP client where FastMCP manages sessions) made this header forwarding **absolutely necessary**.
