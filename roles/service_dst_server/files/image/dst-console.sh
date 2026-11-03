#!/bin/bash
# Send ONE line to a running shard's console.
#
# MANAGED BY ANSIBLE — roles/service_dst_server/files/image/dst-console.sh (baked into the image).
#
#   podman exec <shard container> dst-console 'c_announce("hello")'
#
# The command's OUTPUT is not returned here: the server prints to its log
# (<cluster>/<shard>/server_log.txt). Callers that need an answer print a unique marker and read
# the log for it — see dst_control.py's player count.
set -euo pipefail
FIFO=/tmp/dst-console
[ "$#" -gt 0 ] || { echo "usage: dst-console <lua>" >&2; exit 2; }
[ -p "$FIFO" ] || { echo "dst-console: no console FIFO — is the shard running?" >&2; exit 1; }
# A newline inside the command would split it into two console lines.
case "$*" in *$'\n'*) echo "dst-console: refusing a multi-line command" >&2; exit 2 ;; esac
# timeout: a FIFO write blocks forever if nothing holds the read end.
timeout 5 bash -c 'printf "%s\n" "$1" > "$2"' _ "$*" "$FIFO"
