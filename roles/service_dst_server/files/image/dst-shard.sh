#!/bin/bash
# Run ONE Don't Starve Together shard in the foreground, and save it on stop.
#
# MANAGED BY ANSIBLE — roles/service_dst_server/files/image/dst-shard.sh (baked into the image).
#
#   dst-shard <ShardName>
#
# THE CONSOLE FIFO. The server reads console commands from stdin. Its stdin is a FIFO at
# /tmp/dst-console, held open read-write on fd 3 so it never sees EOF (a server whose stdin closes
# treats that as a quit). `dst-console <lua>` writes one line into it; that is how the control
# endpoint asks for the player count and how a stop asks the shard to save.
#
# A STOP SAVES. podman stop sends SIGTERM to this script (PID 1); the trap turns it into
# `c_shutdown(true)` — save, then exit — and waits for the server to finish. podman's
# --stop-timeout is the ceiling before SIGKILL, set from dst_stop_timeout. Killing the server
# directly instead would lose everything since the last autosave (one in-game day).
set -uo pipefail

SHARD="${1:?usage: dst-shard <ShardName>}"
: "${DST_CLUSTER:?DST_CLUSTER is not set}"
BIN=/server/bin64/dontstarve_dedicated_server_nullrenderer_x64
FIFO=/tmp/dst-console

if [ ! -x "$BIN" ]; then
    echo "dst-shard: $BIN is missing — the prepare unit has not installed the server." >&2
    exit 1
fi

rm -f "$FIFO"
mkfifo "$FIFO"
exec 3<>"$FIFO"

cd /server/bin64 || exit 1
echo "dst-shard: starting $SHARD of cluster $DST_CLUSTER"
"$BIN" \
    -persistent_storage_root /data \
    -conf_dir DoNotStarveTogether \
    -cluster "$DST_CLUSTER" \
    -shard "$SHARD" \
    -ugc_directory /ugc \
    <&3 &
pid=$!

stopping=0
on_stop() {
    if [ "$stopping" -eq 0 ]; then
        stopping=1
        echo "dst-shard: stop requested — saving $SHARD (c_shutdown(true))"
        printf 'c_shutdown(true)\n' >&3
    fi
}
trap on_stop TERM INT

# `wait` returns early (status > 128) whenever a trapped signal arrives, so loop until the server
# has really exited, then collect its real status.
while kill -0 "$pid" 2>/dev/null; do
    wait "$pid"
done
wait "$pid"
rc=$?
echo "dst-shard: $SHARD exited with status $rc"
exit "$rc"
