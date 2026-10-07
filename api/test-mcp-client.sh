#!/bin/bash

# Simple MCP client for testing MCP servers
# Usage: ./test-mcp-client.sh [--verbose|-v] <method> <server-url> <token-file>

set -e

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'


_show_usage() {
    echo "Usage: ./test-mcp-client.sh [--verbose|-v] <method> <server-url> <token-file>"
    echo ""
    echo "Options:"
    echo "  --verbose, -v     Show HTTP status, headers, and raw response"
    echo ""
    echo "Required arguments:"
    echo "  method            MCP method to call"
    echo "  server-url        Full URL to the MCP server endpoint"
    echo "  token-file        Path to file containing the access token"
    echo ""
    echo "Available methods:"
    echo "  ping              - Test server connectivity"
    echo "  initialize        - Initialize MCP connection"
    echo "  tools/list        - List available tools"
    echo "  resources/list    - List available resources"
    echo "  current_time [tz] - Get current time (optional timezone)"
    echo ""
    echo "Token file formats supported:"
    echo "  - Plain JWT string"
    echo "  - JSON with .tokens.access_token"
    echo "  - JSON with .token_data.access_token"
    echo ""
    echo "Example:"
    echo "  ./test-mcp-client.sh ping https://mcpgateway.ddns.net/currenttime/mcp ./api/.token"
    echo "  ./test-mcp-client.sh --verbose initialize https://example.com/mcp/server/ /path/to/token"
    echo "  ./test-mcp-client.sh current_time https://example.com/mcp/server/ .token America/New_York"
}


# Parse --verbose flag
VERBOSE=false
if [ "$1" = "--verbose" ] || [ "$1" = "-v" ]; then
    VERBOSE=true
    shift
fi

# Required parameters (no defaults)
METHOD="$1"
SERVER_URL="$2"
TOKEN_FILE="$3"
SESSION_FILE="${SCRIPT_DIR}/.mcp-session"

# Validate required parameters
if [ -z "$METHOD" ] || [ -z "$SERVER_URL" ] || [ -z "$TOKEN_FILE" ]; then
    echo -e "${RED}Error: Missing required arguments${NC}"
    echo ""
    _show_usage
    exit 1
fi

# Check if token file exists
if [ ! -f "$TOKEN_FILE" ]; then
    echo -e "${RED}Error: Token file not found at $TOKEN_FILE${NC}"
    echo "Run get-m2m-token.sh first to generate a token"
    exit 1
fi

# Read and parse token from file
# Supports: plain JWT string, or JSON with .tokens.access_token or .token_data.access_token
TOKEN_CONTENT=$(cat "$TOKEN_FILE")

# Try to extract token from JSON structure first
ACCESS_TOKEN=$(echo "$TOKEN_CONTENT" | jq -r '.tokens.access_token // .token_data.access_token // empty' 2>/dev/null)

# If no JSON token found, assume the file contains a plain JWT string
if [ -z "$ACCESS_TOKEN" ]; then
    ACCESS_TOKEN="$TOKEN_CONTENT"
fi

# Validate token is not empty
if [ -z "$ACCESS_TOKEN" ]; then
    echo -e "${RED}Error: Could not extract access token from $TOKEN_FILE${NC}"
    exit 1
fi

# Read session ID if exists
SESSION_ID=""
if [ -f "$SESSION_FILE" ]; then
    SESSION_ID=$(cat "$SESSION_FILE")
fi

echo -e "${YELLOW}Calling MCP server...${NC}"
echo "  Method: $METHOD"
echo "  Server: $SERVER_URL"
if [ -n "$SESSION_ID" ]; then
    echo "  Session: $SESSION_ID"
fi
echo ""

# Build the request based on method
case "$METHOD" in
    ping)
        REQUEST_DATA='{
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping"
        }'
        ;;
    initialize)
        REQUEST_DATA='{
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-client",
                    "version": "1.0.0"
                }
            }
        }'
        ;;
    tools/list)
        REQUEST_DATA='{
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list"
        }'
        ;;
    resources/list)
        REQUEST_DATA='{
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/list"
        }'
        ;;
    current_time)
        TIMEZONE="${4:-America/New_York}"
        REQUEST_DATA="{
            \"jsonrpc\": \"2.0\",
            \"id\": 1,
            \"method\": \"tools/call\",
            \"params\": {
                \"name\": \"current_time_by_timezone\",
                \"arguments\": {
                    \"timezone\": \"$TIMEZONE\"
                }
            }
        }"
        ;;
    *)
        echo -e "${RED}Unknown method: $METHOD${NC}"
        echo ""
        _show_usage
        exit 1
        ;;
esac

# Make the request with proper headers for SSE support
# Include session ID in mcp-session-id header if available
# Use temporary file to capture response headers
HEADERS_FILE=$(mktemp)
RESPONSE=""
HTTP_CODE=""
if [ -n "$SESSION_ID" ]; then
    RESPONSE=$(curl -D "$HEADERS_FILE" -s -w "\n__HTTP_CODE__:%{http_code}" -X POST "$SERVER_URL" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "mcp-session-id: ${SESSION_ID}" \
        -d "$REQUEST_DATA")
else
    RESPONSE=$(curl -D "$HEADERS_FILE" -s -w "\n__HTTP_CODE__:%{http_code}" -X POST "$SERVER_URL" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d "$REQUEST_DATA")
fi

# Extract HTTP status code from response
HTTP_CODE=$(echo "$RESPONSE" | grep "^__HTTP_CODE__:" | sed 's/^__HTTP_CODE__://')
RESPONSE=$(echo "$RESPONSE" | grep -v "^__HTTP_CODE__:")

# Verbose output
if [ "$VERBOSE" = true ]; then
    echo -e "${YELLOW}--- HTTP Status Code ---${NC}"
    echo "$HTTP_CODE"
    echo ""
    echo -e "${YELLOW}--- Response Headers ---${NC}"
    cat "$HEADERS_FILE"
    echo ""
    echo -e "${YELLOW}--- Raw Response Body ---${NC}"
    echo "$RESPONSE"
    echo ""
    echo -e "${YELLOW}--- Parsed JSON ---${NC}"
fi

# Parse SSE response - extract JSON from "data:" lines
# SSE format is: "event: message\ndata: {json}"
JSON_RESPONSE=$(echo "$RESPONSE" | grep "^data: " | sed 's/^data: //' | head -1)

if [ -z "$JSON_RESPONSE" ]; then
    # No SSE format, assume plain JSON
    JSON_RESPONSE="$RESPONSE"
fi

# Display response - handle jq errors gracefully
if ! echo "$JSON_RESPONSE" | jq . 2>/dev/null; then
    echo -e "${RED}Error: Response is not valid JSON (HTTP $HTTP_CODE)${NC}"
    echo "$JSON_RESPONSE"
fi

# Extract session ID from response headers (mcp-session-id header)
NEW_SESSION_ID=$(grep -i "^mcp-session-id:" "$HEADERS_FILE" | sed 's/^mcp-session-id: *//i' | tr -d '\r\n')

# Save session ID if present
if [ -n "$NEW_SESSION_ID" ]; then
    echo "$NEW_SESSION_ID" > "$SESSION_FILE"
    echo -e "${GREEN}Session ID saved to $SESSION_FILE: $NEW_SESSION_ID${NC}"
fi

# Clean up temporary headers file
rm -f "$HEADERS_FILE"

echo ""
echo -e "${GREEN}Done!${NC}"
