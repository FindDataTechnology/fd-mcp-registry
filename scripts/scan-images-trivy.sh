#!/bin/bash
# Scan Docker images for vulnerabilities using Trivy
# Requires Trivy to be installed: https://aquasecurity.github.io/trivy/

set -e

echo "Scanning Docker images with Trivy..."
echo "===================================="

# Check if Trivy is installed
if ! command -v trivy &> /dev/null; then
    echo "❌ ERROR: Trivy is not installed"
    echo "Install Trivy: https://aquasecurity.github.io/trivy/latest/getting-started/installation/"
    exit 1
fi

# List of images to scan
IMAGES=(
    "mcp-gateway-registry-registry:latest"
    "mcp-gateway-registry-auth-server:latest"
    "mcp-gateway-registry-metrics-service:latest"
    "mcp-gateway-registry-metrics-db:latest"
)

# Severity levels to report (CRITICAL, HIGH, MEDIUM, LOW, UNKNOWN)
SEVERITY="CRITICAL,HIGH"

# Exit code tracking
EXIT_CODE=0

echo "Trivy version: $(trivy --version)"
echo "Scanning for: $SEVERITY"
echo ""

for image in "${IMAGES[@]}"; do
    echo "=================================================="
    echo "Scanning: $image"
    echo "=================================================="

    # Check if image exists locally
    if ! docker image inspect "$image" &> /dev/null; then
        echo "⚠ WARNING: Image $image not found locally, skipping..."
        echo ""
        continue
    fi

    # Scan the image
    echo "Running Trivy scan..."
    if trivy image \
        --severity "$SEVERITY" \
        --no-progress \
        --timeout 5m \
        "$image"; then
        echo "✅ $image: No vulnerabilities found at $SEVERITY level"
    else
        echo "❌ $image: Vulnerabilities found"
        EXIT_CODE=1
    fi

    echo ""
done

echo "===================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ All scans completed successfully"
else
    echo "❌ Some images have vulnerabilities"
fi

exit $EXIT_CODE
