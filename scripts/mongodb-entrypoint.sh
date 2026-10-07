#!/bin/bash
# MongoDB entrypoint that ensures keyfile has correct permissions
# MongoDB requires keyfile to be owned by mongodb user with 400 permissions

set -e

# Copy keyfile to a location where we can change ownership
if [ -f /data/mongodb-keyfile ]; then
    cp /data/mongodb-keyfile /tmp/mongodb-keyfile
    chown mongodb:mongodb /tmp/mongodb-keyfile
    chmod 400 /tmp/mongodb-keyfile
else
    echo "ERROR: Keyfile not found at /data/mongodb-keyfile"
    exit 1
fi

# Run the standard MongoDB docker-entrypoint script with keyfile
# This ensures MONGO_INITDB_ROOT_USERNAME/PASSWORD are processed correctly
exec docker-entrypoint.sh mongod --replSet rs0 --bind_ip_all --keyFile /tmp/mongodb-keyfile "$@"
