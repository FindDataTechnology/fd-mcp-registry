#!/bin/bash

# Download AWS DocumentDB global-bundle.pem certificate
# This certificate is required for TLS connections to Amazon DocumentDB

set -e

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

# Configuration
CA_BUNDLE_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
CA_BUNDLE_FILE="${DOCUMENTDB_TLS_CA_FILE:-global-bundle.pem}"
DOWNLOAD_PATH="${PARENT_DIR}/${CA_BUNDLE_FILE}"

# Allow override via environment variable
if [ -n "$DOCUMENTDB_CA_BUNDLE_PATH" ]; then
    DOWNLOAD_PATH="$DOCUMENTDB_CA_BUNDLE_PATH"
fi

echo "Downloading AWS DocumentDB CA bundle..."
echo "Source: ${CA_BUNDLE_URL}"
echo "Destination: ${DOWNLOAD_PATH}"

# Download the certificate bundle
if command -v wget &> /dev/null; then
    wget -O "$DOWNLOAD_PATH" "$CA_BUNDLE_URL"
elif command -v curl &> /dev/null; then
    curl -o "$DOWNLOAD_PATH" "$CA_BUNDLE_URL"
else
    echo "Error: Neither wget nor curl is available. Please install one of them."
    exit 1
fi

# Verify download
if [ -f "$DOWNLOAD_PATH" ]; then
    FILE_SIZE=$(stat -f%z "$DOWNLOAD_PATH" 2>/dev/null || stat -c%s "$DOWNLOAD_PATH" 2>/dev/null)
    if [ "$FILE_SIZE" -gt 0 ]; then
        echo "Successfully downloaded CA bundle (${FILE_SIZE} bytes)"
        echo "Certificate bundle location: ${DOWNLOAD_PATH}"
        exit 0
    else
        echo "Error: Downloaded file is empty"
        rm -f "$DOWNLOAD_PATH"
        exit 1
    fi
else
    echo "Error: Failed to download CA bundle"
    exit 1
fi
