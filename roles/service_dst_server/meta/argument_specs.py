# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# One shard of the cluster. A DST "cluster" is one world save made of one or more SHARDS (the
# overworld, the caves, ...), each its own server process with its own ports. Exactly one shard
# is the MASTER; the others connect to it over the [SHARD] link declared in cluster.ini.
_SHARD_OPTIONS = {
    "name": OptionSpec(
        type="str",
        required=True,
        description=[
            "Shard directory name inside the cluster (Master, Caves, ...). Also names the",
            "shard's systemd unit and container, lower-cased. Letters, digits and _ only:",
            "it becomes part of a unit name and a path.",
        ],
    ),
    "is_master": OptionSpec(
        type="bool",
        required=False,
        default=False,
        description="True for exactly ONE shard. The master owns the cluster's player list.",
    ),
    "enabled": OptionSpec(
        type="bool",
        required=False,
        default=True,
        description=[
            "False keeps the shard declared (its world save is untouched) but does not run",
            "it. The master cannot be disabled.",
        ],
    ),
    "server_port": OptionSpec(
        type="int",
        required=True,
        description=[
            "UDP game port clients connect to. EVERY shard's port must be reachable by",
            "clients, not only the master's: a player who walks into the caves is handed",
            "the caves shard's address and connects to it directly. Klei's LAN discovery only",
            "scans 10998-11018, so a port outside that range is reported (not refused).",
        ],
    ),
    "master_server_port": OptionSpec(
        type="int",
        required=True,
        description="UDP port for Steam's master-server (server browser) traffic. Unique per shard.",
    ),
    "authentication_port": OptionSpec(
        type="int",
        required=True,
        description="UDP port for Steam authentication. Unique per shard.",
    ),
    "id": OptionSpec(
        type="int",
        required=False,
        description=[
            "Optional fixed shard id ([SHARD] id). Omit to let the master assign one; set it",
            "only when migrating a save whose shard ids are already fixed.",
        ],
    ),
    "worldgen_preset": OptionSpec(
        type="str",
        required=False,
        default="",
        description=[
            "worldgenoverride.lua preset, e.g. SURVIVAL_TOGETHER (forest) or DST_CAVE (caves).",
            "A non-master shard with neither a preset nor a leveldata_override generates a",
            "FOREST, which is almost never what a 'Caves' shard was meant to be — reported.",
        ],
    ),
    "worldgen_overrides": OptionSpec(
        type="dict",
        required=False,
        default={},
        description=[
            "worldgenoverride.lua `overrides` table: setting name -> value, exactly as the",
            "game spells them (e.g. {season_start: autumn, world_size: huge}).",
            "APPLIES ONLY WHEN THE WORLD IS GENERATED. Changing it on an existing save",
            "changes the file on disk and nothing in the running world; regenerate to apply.",
        ],
    ),
    "leveldata_override": OptionSpec(
        type="dict",
        required=False,
        default={},
        description=[
            "A complete leveldataoverride.lua table, for settings worldgenoverride cannot",
            "express. When non-empty it REPLACES worldgenoverride.lua for this shard (the two",
            "files are mutually exclusive; the stale one is removed). Same generation-only",
            "caveat as worldgen_overrides.",
        ],
    ),
    "server_ini_extra": OptionSpec(
        type="dict",
        required=False,
        default={},
        description=[
            "Extra server.ini keys as {SECTION: {key: value}}, merged OVER the generated",
            "file. For a setting this role does not model; prefer a typed option.",
        ],
    ),
}

# One Steam Workshop mod. Each mod is configured case by case: the role owns the mechanism
# (download, enable, per-shard overrides), the inventory owns which mods and their options.
_MOD_OPTIONS = {
    "id": OptionSpec(
        type="str",
        required=True,
        description=[
            "Steam Workshop id, as digits ('378160973') or as the game spells it",
            "('workshop-378160973'). Quote it in YAML so it stays a string.",
        ],
    ),
    "name": OptionSpec(
        type="str",
        required=False,
        default="",
        description="Human label, for reports and the rendered files' comments. Not used by the game.",
    ),
    "enabled": OptionSpec(
        type="bool",
        required=False,
        default=True,
        description=[
            "False keeps the declaration (and its config) without downloading or loading the",
            "mod — the way to park a mod rather than delete its settings.",
        ],
    ),
    "shards": OptionSpec(
        type="list",
        elements="str",
        required=False,
        default=[],
        description=[
            "Shard names to enable it on. Empty means every shard, which is right for nearly",
            "every mod: a mod enabled on one shard only splits the cluster's rules.",
        ],
    ),
    "config": OptionSpec(
        type="dict",
        required=False,
        default={},
        description=[
            "The mod's configuration_options, name -> value, using the option NAMES and",
            "VALUES from the mod's modinfo.lua (dst_action=mods_report prints them).",
            "YAML types are preserved into Lua: true/false, numbers, strings, nested tables.",
        ],
    ),
}

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Don't Starve Together dedicated server — rootless podman, on demand",
            description=[
                "Runs a DST dedicated cluster (one or more shards) in rootless podman under the",
                "invoking user's systemd --user manager, with nothing installed into the host",
                "image — so it suits an immutable OS. The server is stopped by default and",
                "started on demand: by this role, or by an allowlisted client (a Steam Deck",
                "tile) through a small control endpoint, which also stops it again once the",
                "cluster has been empty for dst_idle_shutdown_minutes.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                # ---------------------------------------------------------------- dispatch
                "dst_action": OptionSpec(
                    type="str",
                    required=False,
                    default="probe",
                    choices=[
                        "probe",
                        "status",
                        "deploy",
                        "start",
                        "stop",
                        "restart",
                        "update",
                        "backup",
                        "restore",
                        "regenerate",
                        "purge",
                        "players",
                        "mods_report",
                        "uninstall",
                    ],
                    description=[
                        "Which entrypoint to run. Defaults to the read-only probe, so a bare run",
                        "against an unfamiliar host changes nothing.",
                    ],
                ),
                "dst_apply": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "deploy/update/backup/uninstall: actually make changes. Default false",
                        "renders every file in check mode and prints the diff, then stops.",
                    ],
                ),
                "dst_restore_confirm": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "restore: the backup archive's FILE NAME (as listed by",
                        "dst_action=backup). Typing the name is both the selection and the",
                        "confirmation. The current cluster is moved aside, never deleted.",
                    ],
                ),
                "dst_regenerate_confirm": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "regenerate: must equal dst_cluster_dir_name. Moves every shard's world",
                        "aside (never deletes it) so the next start generates a fresh one from",
                        "the declared world settings.",
                    ],
                ),
                "dst_purge_confirm": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "purge: must equal dst_instance. DELETES the instance's whole data root —",
                        "install, steamcmd state, worlds, mods, backups — for a clean start.",
                    ],
                ),
                "dst_uninstall_confirm": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "uninstall: must equal dst_instance. Removes units, the control endpoint",
                        "and the image. World saves, backups and the installed server are KEPT.",
                    ],
                ),
                # ---------------------------------------------------------------- layout
                "dst_instance": OptionSpec(
                    type="str",
                    required=False,
                    default="dst",
                    pattern=r"^[a-z][a-z0-9-]{0,30}$",
                    description=[
                        "Instance name. Prefixes every unit and container and names the data",
                        "directory, so two clusters can coexist on one host.",
                    ],
                ),
                "dst_root": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Root directory for the install, saves, mods and backups. Empty derives",
                        "$HOME/.local/share/iaac-dst/<instance> from the connecting user's facts.",
                    ],
                ),
                "dst_cluster_dir_name": OptionSpec(
                    type="str",
                    required=False,
                    default="Cluster_1",
                    pattern=r"^[A-Za-z0-9_-]+$",
                    description=[
                        "The cluster folder under DoNotStarveTogether/ (the game's own default",
                        "is Cluster_1). Renaming it points the server at a DIFFERENT, new world.",
                    ],
                ),
                "dst_python": OptionSpec(
                    type="str",
                    required=False,
                    default="/usr/bin/python3",
                    description="Interpreter for the control endpoint (absolute; systemd ExecStart).",
                ),
                "dst_podman": OptionSpec(
                    type="str",
                    required=False,
                    default="/usr/bin/podman",
                    description="podman binary (absolute; systemd ExecStart).",
                ),
                # ---------------------------------------------------------------- image + install
                "dst_base_image": OptionSpec(
                    type="str",
                    required=False,
                    default="docker.io/steamcmd/steamcmd:ubuntu-24",
                    description=[
                        "Base image providing steamcmd. The server binaries are NOT baked in:",
                        "they live on a volume and are updated by steamcmd at start, so the",
                        "image changes only when this or the packages change.",
                    ],
                ),
                "dst_image_packages": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=["libcurl3t64-gnutls", "ca-certificates"],
                    description=[
                        "apt packages added to the base image. The 64-bit server links",
                        "libcurl-gnutls.so.4; on an Ubuntu 22.04 base the package is",
                        "libcurl3-gnutls instead (24.04 renamed it in the t64 transition).",
                    ],
                ),
                "dst_image_name": OptionSpec(
                    type="str",
                    required=False,
                    default="localhost/iaac-dst-server",
                    description=[
                        "Local image name. Tagged with a hash of its build inputs, so a changed",
                        "Containerfile is a new tag and an unchanged one is never rebuilt.",
                    ],
                ),
                "dst_beta_branch": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "steamcmd -beta branch (e.g. 'updatebeta'). Empty = the public release.",
                        "Clients must be on the SAME branch to join.",
                    ],
                ),
                "dst_update_on_start": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Update the server through steamcmd every time it starts. Clients cannot",
                        "join a server on an older build, so after any game patch a server that",
                        "skipped this would be unjoinable. A failed update starts the installed",
                        "build with a warning rather than refusing to start.",
                    ],
                ),
                "dst_update_validate": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description="Add steamcmd `validate` (re-hash every file). Slower; for repairing an install.",
                ),
                "dst_steamcmd_retries": OptionSpec(
                    type="int",
                    required=False,
                    default=3,
                    min_value=1,
                    max_value=10,
                    description=[
                        "steamcmd attempts per update. steamcmd routinely exits non-zero on the",
                        "run where it updates ITSELF, and succeeds on the next.",
                    ],
                ),
                # ---------------------------------------------------------------- cluster.ini
                "dst_cluster_name": OptionSpec(
                    type="str",
                    required=True,
                    min_length=1,
                    description="Server name in the browser ([NETWORK] cluster_name). Required: no default.",
                ),
                "dst_cluster_description": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description="[NETWORK] cluster_description.",
                ),
                "dst_game_mode": OptionSpec(
                    type="str",
                    required=False,
                    default="survival",
                    choices=[
                        "survival",
                        "endless",
                        "wilderness",
                        "lavaarena",
                        "quagmire",
                    ],
                    description=[
                        "[GAMEPLAY] game_mode. lavaarena/quagmire are Klei's EVENT modes and only",
                        "work while the event is live.",
                    ],
                ),
                "dst_max_players": OptionSpec(
                    type="int",
                    required=False,
                    default=6,
                    min_value=1,
                    max_value=64,
                    description="[GAMEPLAY] max_players.",
                ),
                "dst_pvp": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description="[GAMEPLAY] pvp.",
                ),
                "dst_pause_when_empty": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "[GAMEPLAY] pause_when_empty. Also the idle detector's FALLBACK signal:",
                        "when the console cannot be read, 'every shard paused' means empty.",
                    ],
                ),
                "dst_vote_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description="[GAMEPLAY] vote_enabled (rollback/regenerate votes).",
                ),
                "dst_vote_kick_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description="[GAMEPLAY] vote_kick_enabled.",
                ),
                "dst_cluster_intention": OptionSpec(
                    type="str",
                    required=False,
                    default="cooperative",
                    choices=["cooperative", "competitive", "social", "madness"],
                    description="[NETWORK] cluster_intention (the browser's playstyle tag).",
                ),
                "dst_lan_only": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "[NETWORK] lan_only_cluster: list the server on the LAN only, never in",
                        "Klei's public browser. Clients on another VLAN do not see LAN",
                        "broadcasts and must connect by address.",
                    ],
                ),
                "dst_offline": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "[NETWORK] offline_cluster: no Klei authentication, so no cluster token is",
                        "needed — and no Klei items/skins, and LAN only. Workshop mods STILL work:",
                        "the server downloads them anonymously (an offline cluster downloads and",
                        "loads them).",
                    ],
                ),
                "dst_tick_rate": OptionSpec(
                    type="int",
                    required=False,
                    default=15,
                    choices=[10, 15, 20, 30, 60],
                    description="[NETWORK] tick_rate. Higher is smoother and costs CPU on every shard.",
                ),
                "dst_whitelist_slots": OptionSpec(
                    type="int",
                    required=False,
                    default=0,
                    min_value=0,
                    max_value=64,
                    description="[NETWORK] whitelist_slots: slots reserved for dst_whitelist players.",
                ),
                "dst_cluster_language": OptionSpec(
                    type="str",
                    required=False,
                    default="en",
                    description="[NETWORK] cluster_language.",
                ),
                "dst_autosaver_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description="[NETWORK] autosaver_enabled (save at each day's dawn).",
                ),
                "dst_console_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "[MISC] console_enabled. REQUIRED for idle shutdown's primary player",
                        "count, which asks the master's console; with it off, idle detection",
                        "falls back to pause_when_empty or cannot tell.",
                    ],
                ),
                "dst_max_snapshots": OptionSpec(
                    type="int",
                    required=False,
                    default=6,
                    min_value=1,
                    max_value=100,
                    description="[MISC] max_snapshots: in-game rollback points kept per shard.",
                ),
                "dst_steam_group_id": OptionSpec(
                    type="int",
                    required=False,
                    default=0,
                    description="[STEAM] steam_group_id. 0 = no group, and the [STEAM] group keys are omitted.",
                ),
                "dst_steam_group_only": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description="[STEAM] steam_group_only (needs dst_steam_group_id).",
                ),
                "dst_steam_group_admins": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description="[STEAM] steam_group_admins (needs dst_steam_group_id).",
                ),
                "dst_shard_bind_ip": OptionSpec(
                    type="str",
                    required=False,
                    default="127.0.0.1",
                    description=[
                        "[SHARD] bind_ip / master_ip. Loopback, because every shard runs on this",
                        "host — the inter-shard link never needs to leave it.",
                    ],
                ),
                "dst_shard_master_port": OptionSpec(
                    type="int",
                    required=False,
                    default=10888,
                    description="[SHARD] master_port: the master's listener for its secondary shards.",
                ),
                "dst_cluster_ini_extra": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    description=[
                        "Extra cluster.ini keys as {SECTION: {key: value}}, merged OVER the",
                        "generated file. For a setting this role does not model.",
                    ],
                ),
                # ---------------------------------------------------------------- secrets
                # Two ways to supply each credential: the VALUE itself (dst_cluster_token /
                # dst_cluster_password, e.g. from an Ansible Vault variable), or a 1Password ITEM
                # resolved through richard_lynch.common.onepassword_cached. A value wins; the
                # item is looked up only when the value is empty.
                "dst_cluster_token": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "The Klei cluster token itself (minted at accounts.klei.com -> Games ->",
                        "DST Servers), for a caller that keeps it in Ansible Vault or any other",
                        "source. Wins over dst_cluster_token_item; empty = resolve that item.",
                        "One of the two is required unless dst_offline.",
                    ],
                ),
                "dst_cluster_password": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    no_log=True,
                    description=[
                        "The join password itself. Wins over dst_cluster_password_item; empty =",
                        "resolve that item, or no password when the item is empty too.",
                    ],
                ),
                "dst_secret_vault": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "1Password vault holding the cluster token / password items. Required",
                        "whenever either item is resolved (an item declared and its value option",
                        "empty).",
                    ],
                ),
                "dst_cluster_token_item": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "1Password item holding the Klei cluster token, resolved through",
                        "richard_lynch.common.onepassword_cached when dst_cluster_token is",
                        "empty. One of the two is required unless dst_offline.",
                    ],
                ),
                "dst_cluster_token_field": OptionSpec(
                    type="str",
                    required=False,
                    default="credential",
                    description="Field of dst_cluster_token_item holding the token.",
                ),
                "dst_cluster_password_item": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "1Password item holding the join password, resolved when",
                        "dst_cluster_password is empty. Both empty = no password.",
                    ],
                ),
                "dst_cluster_password_field": OptionSpec(
                    type="str",
                    required=False,
                    default="password",
                    description="Field of dst_cluster_password_item holding the password.",
                ),
                # ---------------------------------------------------------------- player lists
                "dst_admins": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description="Klei user ids (KU_...) written to adminlist.txt.",
                ),
                "dst_whitelist": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description="Klei user ids written to whitelist.txt (see dst_whitelist_slots).",
                ),
                "dst_blocklist": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description="Klei user ids written to blocklist.txt (banned).",
                ),
                # ---------------------------------------------------------------- shards
                "dst_shards": OptionSpec(
                    type="list",
                    elements="dict",
                    required=False,
                    options=_SHARD_OPTIONS,
                    default=[
                        {
                            "name": "Master",
                            "is_master": True,
                            "server_port": 10999,
                            "master_server_port": 27016,
                            "authentication_port": 8766,
                            "worldgen_preset": "SURVIVAL_TOGETHER",
                        },
                        {
                            "name": "Caves",
                            "server_port": 11000,
                            "master_server_port": 27017,
                            "authentication_port": 8767,
                            "worldgen_preset": "DST_CAVE",
                        },
                    ],
                    description=[
                        "The cluster's shards. Default is the game's standard forest + caves",
                        "pair on Klei's conventional ports. Exactly one must be is_master.",
                    ],
                ),
                # ---------------------------------------------------------------- mods
                "dst_mods": OptionSpec(
                    type="list",
                    elements="dict",
                    required=False,
                    options=_MOD_OPTIONS,
                    default=[],
                    description=[
                        "Workshop mods, one entry per mod. Enabled mods are listed in",
                        "dedicated_server_mods_setup.lua (so the server downloads them) and",
                        "enabled with their config in every applicable shard's",
                        "modoverrides.lua. Clients download them on join.",
                    ],
                ),
                "dst_mod_collections": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Workshop COLLECTION ids to download (ServerModCollectionSetup). A",
                        "collection only DOWNLOADS mods; each still needs a dst_mods entry to be",
                        "enabled, because enabling is per mod in modoverrides.lua.",
                    ],
                ),
                "dst_mods_update_on_start": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Download/update Workshop mods (-only_update_server_mods) before the",
                        "shards start, so two shards never race to download the same mod.",
                    ],
                ),
                # ---------------------------------------------------------------- lifecycle
                "dst_autostart": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Start the cluster when the user manager starts (boot/login). Default",
                        "false: the server runs only on demand.",
                    ],
                ),
                "dst_linger": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Enable systemd lingering for the user (needs become), so user units —",
                        "the server and its control endpoint — run with nobody logged in. Not",
                        "needed on a host that auto-logs-in; probe reports the current state.",
                    ],
                ),
                "dst_stop_timeout": OptionSpec(
                    type="int",
                    required=False,
                    default=120,
                    min_value=10,
                    description=[
                        "Seconds a shard gets to save and exit after c_shutdown(true) before",
                        "podman kills it. A big world on a slow disk needs the long end.",
                    ],
                ),
                "dst_start_timeout": OptionSpec(
                    type="int",
                    required=False,
                    default=900,
                    min_value=60,
                    description=[
                        "start: seconds to wait for every enabled shard to listen. The FIRST",
                        "start downloads the whole server (~1 GB), hence the generous default.",
                    ],
                ),
                "dst_backup_on_start": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Archive the cluster (stopped, so consistent) before every start. Cheap",
                        "insurance against an update or a mod corrupting the world.",
                    ],
                ),
                "dst_backup_keep": OptionSpec(
                    type="int",
                    required=False,
                    default=10,
                    min_value=1,
                    description="Archives kept; older ones are pruned after each new backup.",
                ),
                "dst_firewalld_manage": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description=[
                        "Open the shards' UDP ports in firewalld (needs become). Leave false on",
                        "a host whose zone already admits them — probe reports which.",
                    ],
                ),
                "dst_firewalld_zone": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description="firewalld zone for dst_firewalld_manage. Empty = the default zone.",
                ),
                # ---------------------------------------------------------------- on-demand control
                "dst_control_enabled": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Run the control endpoint: start/stop over HTTP for allowlisted clients,",
                        "and idle shutdown. False leaves the server controllable only by this role.",
                    ],
                ),
                "dst_control_port": OptionSpec(
                    type="int",
                    required=False,
                    default=8091,
                    min_value=1025,
                    max_value=65535,
                    description="TCP port of the control endpoint.",
                ),
                "dst_control_allow": OptionSpec(
                    type="list",
                    elements="str",
                    required=False,
                    default=[],
                    description=[
                        "Exact IPv4 addresses allowed to call the control endpoint, besides",
                        "127.0.0.1. Empty (the default) means only the host itself can call it. The",
                        "endpoint listens on every interface and has no credential: this",
                        "allowlist IS the access control — the grant is start/stop, nothing else.",
                    ],
                ),
                "dst_control_interval": OptionSpec(
                    type="int",
                    required=False,
                    default=60,
                    min_value=10,
                    description="Seconds between idle checks.",
                ),
                "dst_idle_shutdown_minutes": OptionSpec(
                    type="int",
                    required=False,
                    default=30,
                    min_value=0,
                    description=[
                        "Stop the cluster after it has been EMPTY this long. 0 disables. An",
                        "unreadable player count never counts as empty: the endpoint reports",
                        "'cannot tell' and leaves the server running.",
                    ],
                ),
                "dst_idle_startup_grace_minutes": OptionSpec(
                    type="int",
                    required=False,
                    default=15,
                    min_value=0,
                    description=[
                        "No idle shutdown within this long of a start, so pressing Start and",
                        "then taking a few minutes to join does not race the idle timer.",
                    ],
                ),
                "dst_idle_shutdown_mode": OptionSpec(
                    type="str",
                    required=False,
                    default="enforce",
                    choices=["observe", "enforce"],
                    description=[
                        "observe logs WOULD STOP and acts on nothing; enforce stops the cluster.",
                        "Stopping saves (c_shutdown(true)), so enforce is the default.",
                    ],
                ),
            },
        ),
    }
)
