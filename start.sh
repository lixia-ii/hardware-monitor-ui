#!/bin/sh
# Compatibility entry for the old launcher name; all commands and arguments go to start-linux.sh.
exec bash "$(dirname -- "$0")/start-linux.sh" "$@"
