# service_dst_server

A **Don't Starve Together** dedicated server, run **on demand**: rootless podman
under the host user's `systemd --user` manager, with nothing installed into the
host image. That makes it a fit for an immutable OS such as Fedora Atomic. The
server is **stopped by default**. It starts when asked, either by this role or
by an allowlisted client (for example a Steam Deck tile, see the `steamdeck`
role's `dst_control` and `dst_play`) through a small control endpoint, and it
**stops itself** once the cluster has been empty for
`dst_idle_shutdown_minutes`.

Every option, with its type and default, is in
[`meta/argument_specs.py`](meta/argument_specs.py). That file is the interface;
this README explains how the parts fit.

## Usage

```yaml
- name: Don't Starve Together dedicated server
  hosts: dst_servers
  gather_facts: true
  tasks:
    - name: Run the requested action
      ansible.builtin.include_role:
        name: richard_lynch.gaming.service_dst_server
```

An online cluster needs a Klei cluster token, and a join password is optional.
Pass each one either as a value (`dst_cluster_token`, `dst_cluster_password`,
for example from an Ansible Vault variable) or as a 1Password item
(`dst_cluster_token_item`, `dst_cluster_password_item`, with `dst_secret_vault`)
resolved through `richard_lynch.common.onepassword_cached`. A value wins over an
item, and the item is looked up only when the value is empty.

The cluster, its shards, mods and allowlist are inventory; only the action and
its gates change per run:

```
dst_action=probe                  # can the host run it? rootless podman, user manager, linger, SELinux, free ports, disk
dst_action=deploy                 # PREVIEW: prints the diff, writes nothing
dst_action=deploy dst_apply=true  # files + image + units + endpoint
dst_action=start                  # waits until EVERY shard is listening (joinable)
dst_action=status                 # state, players, build, config-vs-running, mods, backups
dst_action=stop                   # every shard SAVES; gate proves each exited 0
```

| action                       | does                                                                                                                                                                 | writes?          |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `probe`                      | host readiness                                                                                                                                                       | no               |
| `status`                     | live state, player count (and how it was read), build, **config newer than the running server?**, mods downloaded, backups                                           | no               |
| `deploy`                     | cluster files, image, units, control endpoint. **Never starts or restarts the server.**                                                                              | with `dst_apply` |
| `start` / `stop` / `restart` | the cluster target; start waits for readiness, stop gates on clean exits                                                                                             | yes              |
| `update`                     | steamcmd update + mod download now, with the server stopped (pre-update backup)                                                                                      | with `dst_apply` |
| `backup`                     | list the archives; with `dst_apply`, take one (server must be stopped)                                                                                               | with `dst_apply` |
| `restore`                    | restore the archive **named by** `dst_restore_confirm`; moves the current cluster aside                                                                              | yes              |
| `regenerate`                 | discard the **world** (moved to `discarded/`, backup first) so the next start generates one from the declared world settings; `dst_regenerate_confirm=<cluster dir>` | yes              |
| `players`                    | the master's raw client table, to check the idle count's assumption                                                                                                  | no               |
| `mods_report`                | each declared mod: downloaded?, its **real option names**, defaults, allowed values; flags unknown keys                                                              | no               |
| `uninstall`                  | units, endpoint, image. **Keeps saves, backups and the install.** `dst_uninstall_confirm=<instance>`                                                                 | yes              |

From a Steam Deck: the `steamdeck` role's `dst_control` entrypoint installs
`iaac-dst.sh` (start / stop / status) against this endpoint, and its `dst_play`
tile starts the server, joins it and stops it again when the game exits.

## How it fits together

```
 client   ──HTTP──▶ dst-control (:8091, allowlist) ──systemctl --user──▶ dst.target
                         │  idle loop: players==0 for N min ─▶ stop            │ Wants
                         ▼                                                     ▼
                    master console ◀── podman exec dst-console      dst-master / dst-caves
                    (nonce'd count, read from server_log.txt)        (podman, --network host)
                                                                            │ Requires
                                                                       dst-prepare (oneshot)
                                                     backup ─▶ steamcmd update ─▶ mod download
```

- **Shards** are one container each, in `--network host` so the UDP ports are
  the host's. The shards share an install volume, the save data and a Workshop
  (`ugc`) volume. Every volume is `:z`, the shared SELinux label, because `:Z`
  would make each container's label private and lock out the next one.
- **The image is the runtime, not the game.** It holds steamcmd, the libraries
  the 64-bit server links, and three scripts. The game installs onto a volume
  and is updated at **every start**, because clients cannot join a server on an
  older build. The image tag is a hash of its build inputs, so it is rebuilt
  exactly when those change.
- **A stop saves.** `podman stop` sends SIGTERM to `dst-shard`, which turns it
  into `c_shutdown(true)` written to the server's console FIFO. `stop` then
  gates on each shard's exit status, because "inactive" is also what a killed,
  unsaved shard looks like.
- **The control endpoint** (`files/dst_control.py`, a `--user` unit) is the only
  part that runs all the time. It exposes `GET /healthz`, plus `POST /start`,
  `/stop` and `/restart`. It listens on every interface and takes no credential;
  requests are allowed from `127.0.0.1` and `dst_control_allow` only, and that
  list is empty by default, so out of the box only the host itself can call it.
  The allowlist is the access control (a firewall zone that admits high ports
  does not limit it): list exactly the client addresses that may start and stop
  the server.

### Idle shutdown: three outcomes, not two

The endpoint stops the cluster after `dst_idle_shutdown_minutes` of
**continuous, positive** evidence that it is empty, and never within
`dst_idle_startup_grace_minutes` of a start. A player count it **cannot read**
never counts as empty, and the endpoint reports `cannot_tell`. Stopping a server
with people on it is the expensive mistake. Leaving an empty one running only
costs RAM.

1. **Console (primary).** The endpoint prints a nonce-tagged count of
   `TheNet:GetClientTable()` entries into the master's console, then reads it
   back from `server_log.txt`. The table includes the server's own entry, which
   is excluded by its `performance` field. `dst_action=players` prints the raw
   table, so this can be checked with someone connected.
2. **Pause (fallback).** With `pause_when_empty`, every shard logs
   `Sim paused`/`Sim unpaused`. The cluster counts as empty only if **every**
   shard is paused. Someone in the caves leaves the master paused.

It does **not** stop the server because another game is running: the host may be
playing DST itself.

## Mods, case by case

The role owns the mechanism; inventory owns which mods to run and how they're
configured. To add one:

1. Add `- id: "<workshop id>"` and a `name:` label to `dst_mods`, with no config
   yet.
2. `deploy dst_apply=true`, then `update dst_apply=true` with the server stopped
   (or just start it). The server downloads the mod.
3. Run `dst_action=mods_report`. It prints the mod's options with their defaults
   and allowed values, read from its downloaded `modinfo.lua`.
4. Copy the options you want into the mod's `config:` and deploy. Run
   `mods_report` again: **unknown keys and values the mod does not offer are
   flagged**, because DST ignores both silently.

YAML types carry into Lua (`true`, numbers, strings, nested tables). `shards:`
restricts a mod to some shards (rarely right). `enabled: false` parks a mod
without losing its config. `dst_mod_collections` downloads a whole collection,
but each mod still needs its own entry to be **enabled**. Clients download the
mods themselves when they join.

## Things the game does to its own files (observed, and handled)

- **`server.ini` is rewritten** in the game's own layout, and a secondary
  shard's generated `[SHARD] id` is appended to it. The deploy compares
  **meaning**, not bytes, and carries that id over. Stripping it would re-key
  the shard that the master's cave entrances link to.
- **`worldgenoverride.lua` is rewritten** at world generation (`preset` becomes
  `worldgen_preset`/`settings_preset`). World-gen settings only apply when a
  world is generated, so the file is written only for a shard **with no world
  yet**. Changing `worldgen_overrides` on an existing save does nothing until
  the world is regenerated (in game: `c_regenerateworld()`).
- **`dedicated_server_mods_setup.lua` lives inside the install**, and steamcmd
  replaces it on every update. The managed copy lives in `config/` and
  `dst-prepare` re-installs it after each update.
- **`-only_update_server_mods` does not exit.** It logs
  `FinishDownloadingServerMods Complete!` and then loads and runs the master
  world. `dst-prepare` watches for that line and ends the process.

## Validated on a real host

On a rootless-podman host with SELinux enforcing, the role has been run through
its whole life cycle, and each of these was observed rather than assumed:

- The first start downloads the server; every shard listens, and the console
  count reads `0`.
- Stop saves (`RemoteCommandInput: "c_shutdown(true)"` → `Shutting down`), and
  every shard exits 0.
- Deploy is idempotent (`changed=0`), including after a full run cycle in which
  the game rewrites its files.
- **Idle shutdown** fires against a running cluster: it goes idle, then stops,
  and every shard saves and exits 0.
- **A remote client** on another host starts and stops the server through the
  control endpoint; an address not on `dst_control_allow` is refused with 403.
- **Mod pipeline:** a declared Workshop mod downloads (on an **offline**
  cluster, too), the game logs `modoverrides.lua enabling …` / `Loading mod: …`
  / `applying configuration_options`, and `mods_report` parses its options and
  flags a deliberately wrong key.
- Backup, restore (to a named archive), update, `regenerate` and `mods_report`
  all run against a real host; after `regenerate`, the caves load `DST_CAVE`
  through `worldgen_preset`/`settings_preset` and the Caves shard id is kept.
- `mods_report` reads real `modinfo.lua` files; two parser hangs found that way
  (a function inside a table, and a function-call value) are pinned in
  `test_dst_mods_report.py`.

Molecule (`molecule/default`) covers the translation layer offline: every option
shape rendered through the real tasks, the validation gates exercised in both
directions, idempotence, and a Lua compile check when an interpreter is
available.

## Deliberately not done

- **Server metrics/alerts.** Nothing ships the player count to Prometheus yet;
  `/healthz` is the source if that is wanted.
- **Local (non-Workshop) mods.** Only Workshop ids are modelled.
- **Multi-host clusters.** The shard link is bound to loopback; every shard runs
  on one host.
- **Deleting worlds.** No action here deletes a save. Restore and uninstall move
  things aside or keep them.
