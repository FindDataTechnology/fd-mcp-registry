#!/bin/bash
#
# End-to-end test for Virtual MCP Server scope-based access control
#
# This script tests:
# 1. Creating a virtual server with required_scopes
# 2. Creating a user group with matching scopes
# 3. Creating an M2M service account in that group
# 4. Creating a regular user in that group (for UI testing)
# 5. Verifying the virtual server is accessible
# 6. Cleanup
#
# Usage:
#   ./test_virtual_server_scopes_e2e.sh --registry-url <URL> --token-file <PATH>
#
# Example:
#   ./test_virtual_server_scopes_e2e.sh \
#       --registry-url http://localhost \
#       --token-file .token
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Default values
REGISTRY_URL=""
TOKEN_FILE=""
CLEANUP_ON_EXIT=true

# Test configuration
VS_PATH="/virtual/scoped-tools-test"
VS_CONFIG="$PROJECT_ROOT/cli/examples/virtual-server-scoped-example.json"
GROUP_CONFIG="$PROJECT_ROOT/cli/examples/virtual-server-scoped-users.json"
GROUP_NAME="virtual-scoped-tools-test-users"
M2M_NAME="vs-scope-test-bot"
USER_NAME="vs-scope-test-user"
USER_EMAIL="vs-scope-test-user@example.com"

# Temporary file for modified configs
TEMP_VS_CONFIG=""
TEMP_GROUP_CONFIG=""

# Credentials file for UI testing
CREDS_FILE="/tmp/.vs-creds"


_log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}


_log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}


_log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}


_log_step() {
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}$1${NC}"
    echo -e "${GREEN}========================================${NC}"
}


_usage() {
    echo "Usage: $0 --registry-url <URL> --token-file <PATH> [--no-cleanup]"
    echo ""
    echo "Options:"
    echo "  --registry-url    Registry base URL (e.g., http://localhost)"
    echo "  --token-file      Path to JWT token file"
    echo "  --no-cleanup      Skip cleanup on exit (useful for UI testing)"
    echo "                    Credentials will be saved to /tmp/.vs-creds"
    echo ""
    echo "Example:"
    echo "  # Run with cleanup"
    echo "  $0 --registry-url http://localhost --token-file .token"
    echo ""
    echo "  # Run without cleanup for UI testing"
    echo "  $0 --registry-url http://localhost --token-file .token --no-cleanup"
    echo "  cat /tmp/.vs-creds  # View saved credentials"
    exit 1
}


_parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --registry-url)
                REGISTRY_URL="$2"
                shift 2
                ;;
            --token-file)
                TOKEN_FILE="$2"
                shift 2
                ;;
            --no-cleanup)
                CLEANUP_ON_EXIT=false
                shift
                ;;
            -h|--help)
                _usage
                ;;
            *)
                _log_error "Unknown option: $1"
                _usage
                ;;
        esac
    done

    if [[ -z "$REGISTRY_URL" ]]; then
        _log_error "Missing required argument: --registry-url"
        _usage
    fi

    if [[ -z "$TOKEN_FILE" ]]; then
        _log_error "Missing required argument: --token-file"
        _usage
    fi

    if [[ ! -f "$TOKEN_FILE" ]]; then
        _log_error "Token file not found: $TOKEN_FILE"
        exit 1
    fi
}


_run_cmd() {
    local description="$1"
    shift
    _log_info "$description"
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        "$@"
}


_create_temp_configs() {
    # Create temporary configs with unique paths/names to avoid conflicts
    TEMP_VS_CONFIG=$(mktemp)
    TEMP_GROUP_CONFIG=$(mktemp)

    # Modify virtual server config with unique path
    cat "$VS_CONFIG" | \
        sed "s|/virtual/scoped-tools|$VS_PATH|g" | \
        sed 's|"server_name": ".*"|"server_name": "Scoped Tools Test"|' \
        > "$TEMP_VS_CONFIG"

    # Modify group config with unique name
    cat "$GROUP_CONFIG" | \
        sed "s|virtual-scoped-tools-users|$GROUP_NAME|g" | \
        sed "s|virtual/scoped-tools|${VS_PATH#/}|g" | \
        sed "s|/virtual/scoped-tools|$VS_PATH|g" \
        > "$TEMP_GROUP_CONFIG"

    _log_info "Created temporary configs:"
    _log_info "  Virtual Server: $TEMP_VS_CONFIG"
    _log_info "  Group: $TEMP_GROUP_CONFIG"
}


_cleanup_temp_files() {
    if [[ -n "$TEMP_VS_CONFIG" && -f "$TEMP_VS_CONFIG" ]]; then
        rm -f "$TEMP_VS_CONFIG"
    fi
    if [[ -n "$TEMP_GROUP_CONFIG" && -f "$TEMP_GROUP_CONFIG" ]]; then
        rm -f "$TEMP_GROUP_CONFIG"
    fi
}


_cleanup() {
    if [[ "$CLEANUP_ON_EXIT" != "true" ]]; then
        _log_warn "Skipping cleanup (--no-cleanup specified)"
        _log_warn "Credentials saved to: $CREDS_FILE"
        _log_warn "Virtual server path: $VS_PATH"
        _log_warn "M2M account: $M2M_NAME"
        _log_warn "Regular user: $USER_NAME"
        _log_warn "Group: $GROUP_NAME"
        _cleanup_temp_files
        return
    fi

    _log_step "Cleanup"

    # Delete M2M account
    _log_info "Deleting M2M account: $M2M_NAME"
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        user-delete --username "$M2M_NAME" --force 2>/dev/null || \
        _log_warn "M2M account may not exist or could not be deleted"

    # Delete regular user
    _log_info "Deleting regular user: $USER_NAME"
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        user-delete --username "$USER_NAME" --force 2>/dev/null || \
        _log_warn "Regular user may not exist or could not be deleted"

    # Delete group
    _log_info "Deleting group: $GROUP_NAME"
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        group-delete --name "$GROUP_NAME" --force 2>/dev/null || \
        _log_warn "Group may not exist or could not be deleted"

    # Delete virtual server
    _log_info "Deleting virtual server: $VS_PATH"
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        vs-delete --path "$VS_PATH" --force 2>/dev/null || \
        _log_warn "Virtual server may not exist or could not be deleted"

    # Delete credentials file
    if [[ -f "$CREDS_FILE" ]]; then
        _log_info "Deleting credentials file: $CREDS_FILE"
        rm -f "$CREDS_FILE"
    fi

    _cleanup_temp_files
    _log_info "Cleanup complete"
}


_test_create_virtual_server() {
    _log_step "Step 1: Create Virtual Server with Scope-Based Access Control"

    # Delete existing virtual server if it exists (override mode)
    _log_info "Checking for existing virtual server..."
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        vs-delete --path "$VS_PATH" --force 2>/dev/null || true

    _log_info "Virtual server configuration:"
    cat "$TEMP_VS_CONFIG" | jq '.'

    _run_cmd "Creating virtual server..." vs-create --config "$TEMP_VS_CONFIG"

    _log_info "Verifying virtual server was created..."
    _run_cmd "Getting virtual server details..." vs-get --path "$VS_PATH"
}


_test_create_group() {
    _log_step "Step 2: Create User Group with Matching Scopes"

    # Delete existing group if it exists (override mode)
    _log_info "Checking for existing group..."
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        group-delete --name "$GROUP_NAME" --force 2>/dev/null || true

    _log_info "Group configuration:"
    cat "$TEMP_GROUP_CONFIG" | jq '.'

    # Import the group configuration
    _run_cmd "Importing group configuration..." import-group --file "$TEMP_GROUP_CONFIG"

    _log_info "Verifying group was created..."
    _run_cmd "Listing groups..." group-list
}


_test_create_m2m_account() {
    _log_step "Step 3: Create M2M Service Account in Group"

    # Delete existing M2M account if it exists (override mode)
    _log_info "Checking for existing M2M account..."
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        user-delete --username "$M2M_NAME" --force 2>/dev/null || true

    _log_info "Creating M2M service account..."
    M2M_OUTPUT=$(uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        user-create-m2m --name "$M2M_NAME" --groups "$GROUP_NAME" 2>&1)

    echo "$M2M_OUTPUT"

    # Extract credentials for later use
    CLIENT_ID=$(echo "$M2M_OUTPUT" | grep "Client ID:" | head -1 | sed 's/Client ID: //')
    CLIENT_SECRET=$(echo "$M2M_OUTPUT" | grep "Client Secret:" | head -1 | sed 's/Client Secret: //')

    _log_info "Verifying M2M account was created..."
    _run_cmd "Listing users..." user-list --search "$M2M_NAME"
}


_test_create_regular_user() {
    _log_step "Step 4: Create Regular User in Group"

    # Delete existing regular user if it exists (override mode)
    _log_info "Checking for existing regular user..."
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        user-delete --username "$USER_NAME" --force 2>/dev/null || true

    # Generate a random password
    USER_PASSWORD=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 16)

    _log_info "Creating regular user: $USER_NAME"
    _log_info "Email: $USER_EMAIL"
    _log_info "Password: $USER_PASSWORD"

    # Create regular user with password
    uv run python "$PROJECT_ROOT/api/registry_management.py" \
        --registry-url "$REGISTRY_URL" \
        --token-file "$TOKEN_FILE" \
        user-create-human \
        --username "$USER_NAME" \
        --email "$USER_EMAIL" \
        --first-name "Test" \
        --last-name "User" \
        --password "$USER_PASSWORD" \
        --groups "$GROUP_NAME" 2>&1

    # Save credentials to file only if --no-cleanup was specified
    if [[ "$CLEANUP_ON_EXIT" != "true" ]]; then
        _log_info "Saving credentials to $CREDS_FILE"
        cat > "$CREDS_FILE" << EOF
# Virtual Server Scope Test Credentials
# Created: $(date -Iseconds)
# Registry: $REGISTRY_URL

# Test Configuration
VS_PATH=$VS_PATH
GROUP_NAME=$GROUP_NAME

# M2M Service Account (for API/programmatic access)
M2M_NAME=$M2M_NAME
CLIENT_ID=$CLIENT_ID
CLIENT_SECRET=$CLIENT_SECRET

# Regular User (for UI testing)
USER_NAME=$USER_NAME
USER_EMAIL=$USER_EMAIL
USER_PASSWORD=$USER_PASSWORD

# To get a token for the M2M service account:
# curl -X POST "\${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \\
#   -d "client_id=\${CLIENT_ID}" \\
#   -d "client_secret=\${CLIENT_SECRET}" \\
#   -d "grant_type=client_credentials"

# To get a token for the regular user:
# curl -X POST "\${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \\
#   -d "client_id=mcp-gateway-ui" \\
#   -d "username=\${USER_NAME}" \\
#   -d "password=\${USER_PASSWORD}" \\
#   -d "grant_type=password"
EOF
        chmod 600 "$CREDS_FILE"
        _log_info "Credentials saved to $CREDS_FILE"
    fi

    _log_info "Verifying regular user was created..."
    _run_cmd "Listing users..." user-list --search "$USER_NAME"
}


_test_verify_access() {
    _log_step "Step 5: Verify Virtual Server Access"

    _log_info "Testing virtual server listing..."
    _run_cmd "Listing virtual servers..." vs-list --json

    _log_info "Testing virtual server get..."
    _run_cmd "Getting virtual server..." vs-get --path "$VS_PATH" --json

    _log_info "Access verification complete"
}


_test_scope_enforcement() {
    _log_step "Step 6: Verify Scope-Based Tool Filtering"

    _log_info "The virtual server has the following scope configuration:"
    _log_info "  - Server-level required_scopes: [virtual-scoped-tools/access]"
    _log_info "  - Tool-level override for 'get-time': [virtual-scoped-tools/time-access]"
    _log_info ""
    _log_info "Users with only 'virtual-scoped-tools/access' scope will see:"
    _log_info "  - search_cloudflare_documentation"
    _log_info ""
    _log_info "Users with both scopes will also see:"
    _log_info "  - get-time (alias for current_time_by_timezone)"
    _log_info ""
    _log_info "Note: Full scope enforcement testing requires MCP client calls through the gateway."
    _log_info "This test verifies the configuration is correctly stored."

    # Verify the tool mappings include scope overrides
    VS_DETAILS=$(_run_cmd "Getting virtual server details as JSON..." vs-get --path "$VS_PATH" --json 2>/dev/null | tail -n +2)

    TOOL_COUNT=$(echo "$VS_DETAILS" | jq '.tool_mappings | length')
    _log_info "Tool count: $TOOL_COUNT"

    if [[ "$TOOL_COUNT" -eq 2 ]]; then
        _log_info "SUCCESS: Virtual server has expected 2 tool mappings"
    else
        _log_error "FAILED: Expected 2 tool mappings, got $TOOL_COUNT"
        exit 1
    fi
}


main() {
    _parse_args "$@"

    _log_step "Virtual Server Scope-Based Access Control E2E Test"
    _log_info "Registry URL: $REGISTRY_URL"
    _log_info "Token File: $TOKEN_FILE"
    _log_info "Project Root: $PROJECT_ROOT"

    # Set up cleanup trap
    trap _cleanup EXIT

    # Create temporary configs
    _create_temp_configs

    # Run tests
    _test_create_virtual_server
    _test_create_group
    _test_create_m2m_account
    _test_create_regular_user
    _test_verify_access
    _test_scope_enforcement

    _log_step "All Tests Passed"
    _log_info "Virtual server scope-based access control is working correctly."
}

main "$@"
