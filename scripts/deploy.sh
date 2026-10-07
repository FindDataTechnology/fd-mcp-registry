#!/bin/bash
# Deploy services to ECS (build, push, force new deployment)
#
# Usage:
#   ./scripts/deploy.sh [--service registry|auth|both] [--no-cache] [--skip-monitor]
#
# Examples:
#   ./scripts/deploy.sh                          # Deploy both registry and auth server
#   ./scripts/deploy.sh --service registry       # Deploy registry only
#   ./scripts/deploy.sh --service auth           # Deploy auth server only
#   ./scripts/deploy.sh --service both           # Deploy both (default)
#   ./scripts/deploy.sh --no-cache               # Deploy both without Docker cache
#   ./scripts/deploy.sh --service auth --no-cache  # Deploy auth without cache
#   ./scripts/deploy.sh --skip-monitor           # Deploy without monitoring step

# Exit on error
set -e

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Configuration
AWS_REGION="${AWS_REGION:-us-east-1}"
ECS_CLUSTER="mcp-gateway-ecs-cluster"

# Service configuration mapping
# Format: IMAGE_NAME:ECS_SERVICE_NAME
REGISTRY_IMAGE="registry"
REGISTRY_ECS_SERVICE="mcp-gateway-v2-registry"

AUTH_IMAGE="auth_server"
AUTH_ECS_SERVICE="mcp-gateway-v2-auth"

# Defaults
SERVICE="both"
NO_CACHE=""
SKIP_MONITOR="false"


_print_usage() {
    echo "Usage: $0 [--service registry|auth|both] [--no-cache] [--skip-monitor]"
    echo ""
    echo "Options:"
    echo "  --service   Service to deploy: registry, auth, or both (default: both)"
    echo "  --no-cache  Build Docker images without cache"
    echo "  --skip-monitor  Skip the deployment monitoring step"
    echo ""
    echo "Examples:"
    echo "  $0                              # Deploy both services"
    echo "  $0 --service registry           # Deploy registry only"
    echo "  $0 --service auth               # Deploy auth server only"
    echo "  $0 --no-cache --service auth    # Deploy auth without cache"
}


_parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --service)
                SERVICE="$2"
                # Accept auth_server as alias for auth
                if [[ "$SERVICE" == "auth_server" ]]; then
                    SERVICE="auth"
                fi
                if [[ "$SERVICE" != "registry" && "$SERVICE" != "auth" && "$SERVICE" != "both" ]]; then
                    echo "Error: --service must be 'registry', 'auth', 'auth_server', or 'both'"
                    _print_usage
                    exit 1
                fi
                shift 2
                ;;
            --no-cache)
                NO_CACHE="true"
                shift
                ;;
            --skip-monitor)
                SKIP_MONITOR="true"
                shift
                ;;
            --help|-h)
                _print_usage
                exit 0
                ;;
            *)
                echo "Error: Unknown option: $1"
                _print_usage
                exit 1
                ;;
        esac
    done
}


_build_and_push() {
    local image_name="$1"
    local display_name="$2"

    echo "Building and pushing ${display_name} image..."
    echo "----------------------------------------"

    cd "$REPO_ROOT"
    if [[ "$NO_CACHE" == "true" ]]; then
        echo "Building without cache (--no-cache)"
        NO_CACHE=true make build-push IMAGE="$image_name"
    else
        make build-push IMAGE="$image_name"
    fi

    echo "${display_name} image built and pushed successfully"
    echo ""
}


_force_new_deployment() {
    local ecs_service="$1"
    local display_name="$2"

    echo "Forcing new deployment for ${display_name} (${ecs_service})..."
    echo "----------------------------------------"

    aws ecs update-service \
        --cluster "$ECS_CLUSTER" \
        --service "$ecs_service" \
        --force-new-deployment \
        --region "$AWS_REGION" \
        --output json | jq '{service: .service.serviceName, status: .service.status, desiredCount: .service.desiredCount}'

    echo "${display_name} deployment triggered"
    echo ""
}


_monitor_deployment() {
    local ecs_services="$1"

    echo "Monitoring deployment status..."
    echo "----------------------------------------"
    echo "Press Ctrl+C to exit monitoring"
    echo ""
    sleep 2

    watch -n 5 'aws ecs describe-services \
      --cluster '"$ECS_CLUSTER"' \
      --services '"$ecs_services"' \
      --region '"$AWS_REGION"' \
      --query "services[*].{Service:serviceName,Status:status,Desired:desiredCount,Running:runningCount,Pending:pendingCount,Deployments:deployments[*].{Status:status,Running:runningCount,Desired:desiredCount,RolloutState:rolloutState}}" \
      --output table'
}


_deploy_services() {
    local step=1
    local total_steps=0
    local monitor_services=""

    # Calculate total steps
    case "$SERVICE" in
        registry)
            total_steps=2
            if [[ "$SKIP_MONITOR" == "false" ]]; then
                total_steps=3
            fi
            ;;
        auth)
            total_steps=2
            if [[ "$SKIP_MONITOR" == "false" ]]; then
                total_steps=3
            fi
            ;;
        both)
            total_steps=4
            if [[ "$SKIP_MONITOR" == "false" ]]; then
                total_steps=5
            fi
            ;;
    esac

    # Build and push
    if [[ "$SERVICE" == "registry" || "$SERVICE" == "both" ]]; then
        echo "Step ${step}/${total_steps}: Building Registry"
        _build_and_push "$REGISTRY_IMAGE" "Registry"
        step=$((step + 1))
    fi

    if [[ "$SERVICE" == "auth" || "$SERVICE" == "both" ]]; then
        echo "Step ${step}/${total_steps}: Building Auth Server"
        _build_and_push "$AUTH_IMAGE" "Auth Server"
        step=$((step + 1))
    fi

    # Force new deployments
    if [[ "$SERVICE" == "registry" || "$SERVICE" == "both" ]]; then
        echo "Step ${step}/${total_steps}: Deploying Registry"
        _force_new_deployment "$REGISTRY_ECS_SERVICE" "Registry"
        monitor_services="$REGISTRY_ECS_SERVICE"
        step=$((step + 1))
    fi

    if [[ "$SERVICE" == "auth" || "$SERVICE" == "both" ]]; then
        echo "Step ${step}/${total_steps}: Deploying Auth Server"
        _force_new_deployment "$AUTH_ECS_SERVICE" "Auth Server"
        if [[ -n "$monitor_services" ]]; then
            monitor_services="$monitor_services $AUTH_ECS_SERVICE"
        else
            monitor_services="$AUTH_ECS_SERVICE"
        fi
        step=$((step + 1))
    fi

    # Monitor
    if [[ "$SKIP_MONITOR" == "false" ]]; then
        echo "Step ${step}/${total_steps}: Monitoring"
        _monitor_deployment "$monitor_services"
    else
        echo "Skipping deployment monitoring (--skip-monitor)"
        echo ""
        echo "To check status manually:"
        echo "  aws ecs describe-services --cluster $ECS_CLUSTER --services $monitor_services --region $AWS_REGION --query 'services[*].{Service:serviceName,Running:runningCount,Desired:desiredCount}' --output table"
    fi
}


# Main
_parse_args "$@"

echo "=========================================="
echo "ECS Deployment Script"
echo "=========================================="
echo "Service:    $SERVICE"
echo "Region:     $AWS_REGION"
echo "Cluster:    $ECS_CLUSTER"
echo "No Cache:   ${NO_CACHE:-false}"
echo "=========================================="
echo ""

_deploy_services
