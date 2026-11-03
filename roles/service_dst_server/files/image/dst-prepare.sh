#!/bin/bash
# Install/update the DST server and its Workshop mods, before any shard starts.
#
# MANAGED BY ANSIBLE — roles/service_dst_server/files/image/dst-prepare.sh (baked into the image).
#
# Runs as the <instance>-prepare oneshot, which every shard unit Requires. Driven by environment
# set by that unit (from inventory, via the role):
#
#   DST_CLUSTER          cluster directory name
#   DST_UPDATE           true -> run steamcmd app_update (a missing install is installed regardless)
#   DST_VALIDATE         true -> add `validate`
#   DST_BETA             steamcmd beta branch, empty for public
#   DST_RETRIES          steamcmd attempts
#   DST_MODS_UPDATE      true -> download/update Workshop mods
#
# WHY THE MOD SETUP FILE IS COPIED IN HERE. dedicated_server_mods_setup.lua lives INSIDE the
# install (server/mods/), and steamcmd's app_update REPLACES it with Klei's empty template on every
# update. Written once by Ansible, it would silently revert after the first patch and the server
# would stop downloading every mod. So the managed copy lives in /config and is re-installed here,
# AFTER the update, every time.
set -uo pipefail

APP=343050
BIN=/server/bin64/dontstarve_dedicated_server_nullrenderer_x64
: "${DST_CLUSTER:?DST_CLUSTER is not set}"
DST_UPDATE="${DST_UPDATE:-true}"
DST_VALIDATE="${DST_VALIDATE:-false}"
DST_BETA="${DST_BETA:-}"
DST_RETRIES="${DST_RETRIES:-3}"
DST_MODS_UPDATE="${DST_MODS_UPDATE:-true}"

say() { echo "dst-prepare: $*"; }

# ------------------------------------------------------------------ server
if [ "$DST_UPDATE" = true ] || [ ! -x "$BIN" ]; then
    args=(+force_install_dir /server +login anonymous +app_update "$APP")
    [ -n "$DST_BETA" ] && args+=(-beta "$DST_BETA")
    [ "$DST_VALIDATE" = true ] && args+=(validate)
    args+=(+quit)
    ok=0
    for attempt in $(seq 1 "$DST_RETRIES"); do
        # RETRIES ADD `validate`. An update of an existing install can fail every time
        # with "Error! App '343050' state is 0x6 after update job", leaving the server a build
        # behind its clients — who then cannot see it in the LAN list at all.
        # 0x6 is EAppState UpdateRequired(2) | FullyInstalled(4) (Steamworks EAppState, checked
        # against SteamRE/open-steamworks AppsCommon.h): the install is intact and the update
        # simply DID NOT RUN — not FilesMissing(32) or FilesCorrupt(128). So `validate` is not a
        # known fix; it is kept as a cheap second attempt only. The most
        # likely cause is steamcmd's home — its depotcache of the old
        # manifests — being ephemeral; it is now a persistent volume (setup_vars.yml), and its
        # logs/content_log.txt on that volume is what to read if this ever recurs.
        try=("${args[@]}")
        how="plain"
        if [ "$attempt" -gt 1 ] && [ "$DST_VALIDATE" != true ]; then
            try=("${args[@]:0:${#args[@]}-1}" validate +quit)
            how="validate"
        fi
        say "steamcmd app_update $APP (attempt $attempt/$DST_RETRIES, $how)"
        if steamcmd "${try[@]}"; then
            ok=1
            break
        fi
        say "steamcmd exited non-zero; retrying in 5s"
        sleep 5
    done
    # THE MARKER makes a failed update VISIBLE. Starting the installed build when steamcmd fails
    # keeps a Steam outage from blocking play — but a server a build behind its clients is
    # INVISIBLE to them in the LAN list, so "running" is not "playable"
    # and the only trace would be this log. dst_action=status reports the marker loudly.
    if [ "$ok" -eq 1 ]; then
        rm -f /server/.iaac-update-failed
    else
        if [ -x "$BIN" ]; then
            say "WARNING: update FAILED after $DST_RETRIES attempts — starting the INSTALLED build."
            say "WARNING: clients on a newer build will NOT see this server until an update succeeds."
            printf '%s update failed after %s attempts; see the steamcmd volume logs/content_log.txt\n' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DST_RETRIES" > /server/.iaac-update-failed
        else
            say "FATAL: no server installed and steamcmd failed $DST_RETRIES times."
            exit 1
        fi
    fi
else
    say "update skipped (DST_UPDATE=$DST_UPDATE, server present)"
fi

[ -x "$BIN" ] || { say "FATAL: $BIN missing after install"; exit 1; }
if [ -f /server/version.txt ]; then
    say "server build: $(tr -d '[:space:]' < /server/version.txt)"
fi

# ------------------------------------------------------------------ mods
install -D -m 0644 /config/dedicated_server_mods_setup.lua /server/mods/dedicated_server_mods_setup.lua
if [ "$DST_MODS_UPDATE" = true ] && grep -qE '^Server(Mod|ModCollection)Setup\(' /config/dedicated_server_mods_setup.lua; then
    say "downloading/updating Workshop mods"
    cd /server/bin64 || exit 1
    # Doing it HERE, once, stops two shards racing to download the same mod into one ugc dir.
    #
    # -only_update_server_mods DOES NOT EXIT ON ITS OWN (seen on build 760049): it
    # logs "FinishDownloadingServerMods Complete! Process trying to quit nicely.." and then goes
    # on to load the MASTER WORLD and run it — binding the shard ports — inside this prepare
    # container, forever, so a start would never get past prepare. So the completion line is
    # watched for, and the process is ended the moment it appears. It is ended BEFORE the world
    # it then loads could be saved: nothing has changed in it, and a kill there writes nothing.
    MODLOG=/tmp/dst-mods-update.log
    DONE_LINE='FinishDownloadingServerMods Complete'
    "$BIN" -only_update_server_mods \
        -persistent_storage_root /data -conf_dir DoNotStarveTogether \
        -cluster "$DST_CLUSTER" -ugc_directory /ugc >"$MODLOG" 2>&1 &
    modpid=$!
    finished=0
    for _ in $(seq 1 "${DST_MODS_TIMEOUT:-600}"); do
        # A count from the FILE, never `... | grep -q` (PIPEQ-01).
        if [ "$(grep -c "$DONE_LINE" "$MODLOG")" -gt 0 ]; then
            finished=1
            break
        fi
        kill -0 "$modpid" 2>/dev/null || break
        sleep 1
    done
    kill -TERM "$modpid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$modpid" 2>/dev/null || break
        sleep 1
    done
    kill -KILL "$modpid" 2>/dev/null
    wait "$modpid" 2>/dev/null
    grep -E '\[Workshop\]|FinishDownloadingServerMods|ERROR' "$MODLOG" | sed 's/^/dst-prepare:   /'
    if [ "$finished" -eq 1 ]; then
        say "Workshop mods downloaded"
    else
        say "WARNING: mod download did not report completion within ${DST_MODS_TIMEOUT:-600}s — shards will retry it themselves."
    fi
else
    say "mod update skipped (DST_MODS_UPDATE=$DST_MODS_UPDATE or no mods declared)"
fi
say "ready"
