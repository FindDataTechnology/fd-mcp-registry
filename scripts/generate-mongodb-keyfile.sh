#!/bin/bash
# Generate MongoDB keyfile for replica set authentication
# This is required when running MongoDB with --replSet and authentication enabled

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
KEYFILE_PATH="$SCRIPT_DIR/../.mongodb-keyfile"

# Generate a random keyfile if it doesn't exist
if [ ! -f "$KEYFILE_PATH" ]; then
    echo "Generating MongoDB keyfile..."
    openssl rand -base64 756 > "$KEYFILE_PATH"
    chmod 400 "$KEYFILE_PATH"
    echo "Keyfile generated at: $KEYFILE_PATH"
else
    echo "Keyfile already exists at: $KEYFILE_PATH"
fi
