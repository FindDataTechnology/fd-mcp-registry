#!/bin/bash
#
# Peer Federation Test Script
#
# Sets up 2 registry instances and tests federation sync between them.
#
# Usage:
#   ./scripts/test-peer-federation.sh
#
# This script will:
#   1. Start Registry A on port 7860
#   2. Start Registry B on port 7861
#   3. Register test servers/agents on Registry A
#   4. Configure Registry B to peer with Registry A
#   5. Trigger sync and verify data was replicated
#   6. Clean up when done (Ctrl+C)

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
REGISTRY_A_PORT=7860
REGISTRY_B_PORT=7861
REGISTRY_A_DATA="/tmp/registry-a-data-$$"
REGISTRY_B_DATA="/tmp/registry-b-data-$$"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Auth headers for testing (simulates nginx-proxied authentication)
AUTH_HEADERS='-H "X-Username: test-admin" -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" -H "X-Auth-Method: keycloak"'

# PIDs for cleanup
REGISTRY_A_PID=""
REGISTRY_B_PID=""

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Cleaning up...${NC}"

    if [ -n "$REGISTRY_A_PID" ]; then
        echo "Stopping Registry A (PID: $REGISTRY_A_PID)"
        kill $REGISTRY_A_PID 2>/dev/null || true
    fi

    if [ -n "$REGISTRY_B_PID" ]; then
        echo "Stopping Registry B (PID: $REGISTRY_B_PID)"
        kill $REGISTRY_B_PID 2>/dev/null || true
    fi

    # Clean up data directories
    rm -rf "$REGISTRY_A_DATA" "$REGISTRY_B_DATA" 2>/dev/null || true

    echo -e "${GREEN}Cleanup complete${NC}"
    exit 0
}

# Set up trap for cleanup
trap cleanup EXIT INT TERM

# Wait for a service to be ready
wait_for_service() {
    local port=$1
    local name=$2
    local max_attempts=30
    local attempt=1

    echo -n "Waiting for $name (port $port) to be ready"
    while [ $attempt -le $max_attempts ]; do
        if curl -s "http://localhost:$port/health" > /dev/null 2>&1; then
            echo -e " ${GREEN}Ready${NC}"
            return 0
        fi
        echo -n "."
        sleep 1
        attempt=$((attempt + 1))
    done

    echo -e " ${RED}Failed${NC}"
    return 1
}

# Print section header
print_section() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

# Print success message
print_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

# Print error message
print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Print info message
print_info() {
    echo -e "${YELLOW}[INFO]${NC} $1"
}

# Main script
main() {
    cd "$PROJECT_DIR"

    print_section "Peer Federation Test"
    echo "Registry A: http://localhost:$REGISTRY_A_PORT (data: $REGISTRY_A_DATA)"
    echo "Registry B: http://localhost:$REGISTRY_B_PORT (data: $REGISTRY_B_DATA)"

    # Create data directories
    mkdir -p "$REGISTRY_A_DATA" "$REGISTRY_B_DATA"

    # Start Registry A
    print_section "Starting Registry A"
    STORAGE_BACKEND=file \
    SERVERS_DIR_OVERRIDE="$REGISTRY_A_DATA" \
    uv run uvicorn registry.main:app --host 127.0.0.1 --port $REGISTRY_A_PORT \
        > /tmp/registry-a.log 2>&1 &
    REGISTRY_A_PID=$!
    print_info "Registry A started with PID $REGISTRY_A_PID"

    # Start Registry B
    print_section "Starting Registry B"
    STORAGE_BACKEND=file \
    SERVERS_DIR_OVERRIDE="$REGISTRY_B_DATA" \
    uv run uvicorn registry.main:app --host 127.0.0.1 --port $REGISTRY_B_PORT \
        > /tmp/registry-b.log 2>&1 &
    REGISTRY_B_PID=$!
    print_info "Registry B started with PID $REGISTRY_B_PID"

    # Wait for both services
    wait_for_service $REGISTRY_A_PORT "Registry A" || { print_error "Registry A failed to start"; cat /tmp/registry-a.log; exit 1; }
    wait_for_service $REGISTRY_B_PORT "Registry B" || { print_error "Registry B failed to start"; cat /tmp/registry-b.log; exit 1; }

    # Register test server on Registry A (uses Form data, not JSON)
    print_section "Registering Test Server on Registry A"
    REGISTER_RESPONSE=$(curl -s -X POST "http://localhost:$REGISTRY_A_PORT/api/servers/register" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" \
        -F "name=Test Server from Registry A" \
        -F "description=A server for testing federation sync" \
        -F "path=/test-server" \
        -F "proxy_pass_url=http://localhost:8000" \
        -F "tags=production,test")

    if echo "$REGISTER_RESPONSE" | grep -q "test-server"; then
        print_success "Server registered on Registry A"
        echo "$REGISTER_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$REGISTER_RESPONSE"
    else
        print_error "Failed to register server"
        echo "$REGISTER_RESPONSE"
    fi

    # Register another test server
    print_info "Registering second test server..."
    curl -s -X POST "http://localhost:$REGISTRY_A_PORT/api/servers/register" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" \
        -F "name=Another Test Server" \
        -F "description=Second server for testing" \
        -F "path=/another-server" \
        -F "proxy_pass_url=http://localhost:8001" \
        -F "tags=development" > /dev/null
    print_success "Second server registered"

    # Enable the servers (they're disabled by default)
    print_info "Enabling test servers..."
    curl -s -X POST "http://localhost:$REGISTRY_A_PORT/api/servers/toggle" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" \
        -F "path=/test-server" \
        -F "new_state=true" > /dev/null
    curl -s -X POST "http://localhost:$REGISTRY_A_PORT/api/servers/toggle" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" \
        -F "path=/another-server" \
        -F "new_state=true" > /dev/null
    print_success "Servers enabled"

    # List servers on Registry A
    print_section "Servers on Registry A"
    curl -s "http://localhost:$REGISTRY_A_PORT/api/servers" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" | python3 -m json.tool 2>/dev/null || true

    # Configure Registry B to peer with Registry A
    print_section "Configuring Peer on Registry B"
    PEER_RESPONSE=$(curl -s -X POST "http://localhost:$REGISTRY_B_PORT/api/peers" \
        -H "Content-Type: application/json" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" \
        -d "{
            \"peer_id\": \"registry-a\",
            \"name\": \"Registry A\",
            \"endpoint\": \"http://localhost:$REGISTRY_A_PORT\",
            \"enabled\": true,
            \"sync_mode\": \"all\",
            \"sync_interval_minutes\": 5
        }")

    if echo "$PEER_RESPONSE" | grep -q "registry-a"; then
        print_success "Peer configured on Registry B"
        echo "$PEER_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$PEER_RESPONSE"
    else
        print_error "Failed to configure peer"
        echo "$PEER_RESPONSE"
    fi

    # Trigger sync
    print_section "Triggering Sync from Registry A to Registry B"
    SYNC_RESPONSE=$(curl -s -X POST "http://localhost:$REGISTRY_B_PORT/api/peers/registry-a/sync" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak")

    if echo "$SYNC_RESPONSE" | grep -q '"success"'; then
        print_success "Sync completed"
        echo "$SYNC_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$SYNC_RESPONSE"
    else
        print_error "Sync failed"
        echo "$SYNC_RESPONSE"
    fi

    # Verify servers were synced to Registry B
    print_section "Servers on Registry B (After Sync)"
    SERVERS_B=$(curl -s "http://localhost:$REGISTRY_B_PORT/api/servers" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak")
    echo "$SERVERS_B" | python3 -m json.tool 2>/dev/null || echo "$SERVERS_B"

    # Check for federated servers
    if echo "$SERVERS_B" | grep -q "registry-a/test-server"; then
        print_success "Federation test PASSED - servers synced correctly"
    else
        print_error "Federation test FAILED - servers not found on Registry B"
    fi

    # Show peer status
    print_section "Peer Sync Status"
    curl -s "http://localhost:$REGISTRY_B_PORT/api/peers/registry-a/status" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" | python3 -m json.tool 2>/dev/null || true

    # Test federation export endpoint
    print_section "Testing Federation Export Endpoint (Registry A)"
    print_info "GET /api/federation/servers"
    curl -s "http://localhost:$REGISTRY_A_PORT/api/federation/servers" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" | python3 -m json.tool 2>/dev/null || true

    # Test whitelist mode
    print_section "Testing Whitelist Mode"
    print_info "Adding peer with whitelist mode..."
    curl -s -X POST "http://localhost:$REGISTRY_B_PORT/api/peers" \
        -H "Content-Type: application/json" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak" \
        -d "{
            \"peer_id\": \"registry-a-whitelist\",
            \"name\": \"Registry A (Whitelist)\",
            \"endpoint\": \"http://localhost:$REGISTRY_A_PORT\",
            \"enabled\": true,
            \"sync_mode\": \"whitelist\",
            \"whitelist_servers\": [\"/test-server\"],
            \"sync_interval_minutes\": 5
        }" | python3 -m json.tool 2>/dev/null || true

    print_info "Syncing with whitelist mode..."
    WHITELIST_SYNC=$(curl -s -X POST "http://localhost:$REGISTRY_B_PORT/api/peers/registry-a-whitelist/sync" \
        -H "X-Username: test-admin" \
        -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" \
        -H "X-Auth-Method: keycloak")
    echo "$WHITELIST_SYNC" | python3 -m json.tool 2>/dev/null || echo "$WHITELIST_SYNC"

    if echo "$WHITELIST_SYNC" | grep -q '"servers_synced": 1'; then
        print_success "Whitelist mode test PASSED - only whitelisted server synced"
    else
        print_info "Whitelist sync result (check servers_synced count)"
    fi

    # Summary
    print_section "Test Summary"
    echo "Registry A: http://localhost:$REGISTRY_A_PORT"
    echo "Registry B: http://localhost:$REGISTRY_B_PORT"
    echo ""
    echo "Auth headers (add to all requests):"
    echo '  -H "X-Username: test-admin" -H "X-Scopes: mcp-servers-unrestricted/read mcp-servers-unrestricted/execute federation-service" -H "X-Auth-Method: keycloak"'
    echo ""
    echo "Useful commands (with auth):"
    echo "  List peers:    curl http://localhost:$REGISTRY_B_PORT/api/peers -H 'X-Username: test-admin' ..."
    echo "  List servers:  curl http://localhost:$REGISTRY_B_PORT/api/servers -H 'X-Username: test-admin' ..."
    echo "  Trigger sync:  curl -X POST http://localhost:$REGISTRY_B_PORT/api/peers/registry-a/sync -H 'X-Username: test-admin' ..."
    echo "  Fed export:    curl http://localhost:$REGISTRY_A_PORT/api/federation/servers"
    echo ""
    echo -e "${YELLOW}Press Ctrl+C to stop both registries and clean up${NC}"

    # Keep running until interrupted
    while true; do
        sleep 1
    done
}

main "$@"
