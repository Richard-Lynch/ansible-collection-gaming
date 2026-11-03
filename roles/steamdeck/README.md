# steamdeck

Manages a Steam Deck in USER SPACE: its library tiles and their artwork,
Moonlight and EmuDeck settings, Decky plugins, and the tiles (and their scripts)
that drive a separate streaming host on the same network: wake it, restart its
Steam, start/stop and join its Don't Starve Together server.

SteamOS replaces its rootfs on major updates and `sudo` needs a password, so
this role writes `$HOME` wherever it can. The few entrypoints that must write
`/etc` (`wol_persist`, `battery`) say so, and unseal and re-seal the rootfs
inside a guarded block.

## Usage

```yaml
- name: Steam Deck
  hosts: handhelds
  gather_facts: true
  roles:
    - role: richard_lynch.gaming.steamdeck
```

```
ansible-playbook steamdeck.yml -e steamdeck_action=<action>
```

The default action is the read-only `probe`. Writes are gated on
`steamdeck_apply=true`. Pass only the action and the gates on the command line;
everything else describes the device and belongs in inventory.

## Entrypoints

The authoritative list, with one line each, is the header of `tasks/main.yml`.
The groups:

| Group              | Actions                                                                                                                                                    |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Survey (read-only) | `probe`, `status`, `rootfs_report`, `shortcuts_report`, `moonlight_report`, `emudeck_report`, `wake_report`, `wol_report`                                  |
| Library            | `shortcuts` (tiles from `steamdeck_managed_shortcuts`, **stops Steam on the Deck**), `artwork`, `remove_app`                                               |
| Apps               | `moonlight`, `emudeck`, `decky_plugins`, `decky_settings`, `css_themes`                                                                                    |
| Streaming host     | `wake_host` (power it on), `steam_restart_host` (restart its Game Mode Steam), `dst_control` (its DST server), `dst_play` (start + join + stop-when-empty) |
| Device power       | `wol_persist`, `wol_verify`, `wol_disarm_wifi`, `wol_disarm_internal`, `battery`, `reboot`, `close_game`                                                   |

## The streaming-host tiles

Each installs a script into `~/.local/bin`. A tile in
`steamdeck_managed_shortcuts` launches it, and its outcome is logged to
`~/.local/share/`, because a Game Mode tile has no terminal. A tile is removed
by omitting it from `steamdeck_managed_shortcuts` (see below); the entrypoint
that installs its script stays available, so it comes back by declaring the
tile again (stream tiles: set `steamdeck_stream_host` and
`steamdeck_stream_apps`).

- **Wake** (`wake_host`) sends a magic packet. Docked on the target's segment it
  broadcasts directly; elsewhere (on WiFi, another VLAN) it asks a wake relay
  with a leg on the target's segment over HTTP (`steamdeck_wake_endpoint`).
- **Restart Steam** (`steam_restart_host`) POSTs to a small Steam guard service
  on the host (`steamdeck_steam_guard_url`), then waits for a _fresh_ Steam with
  Remote Play listening before it reports success. No credential is stored on
  the Deck; the guard's allowlist of the Deck's reserved addresses is the grant.
- **DST start / stop** (`dst_control`, `~/.local/bin/iaac-dst.sh`) call the
  host's Don't Starve Together control endpoint (the `service_dst_server` role
  in this collection). Start waits until every shard is listening, so the server
  is joinable when it returns. Stop waits until every shard has saved. The verb
  is the argument, and the server's `dst_control_allow` is the grant. **The play
  tile calls this script**, so install it before `dst_play`. The server also
  stops itself when it has been empty for its idle timeout.
- **DST: play** (`dst_play`) starts the server, launches DST straight into it,
  and stops the server when you quit and it is empty.
  - **The join** is DST's own launch arguments:
    `steam -applaunch 322330 +connect <ip>:<port> +password <pw>`. Steam raises
    no prompt on this route. Never `steam://run/322330//<args>/`: Steam asks
    about "custom options" every time, and a pending prompt silently blocks
    every later DST launch (see the `klei_dst_client` README). Joined means the
    SERVER counts a player: `/healthz` `players` rises above what it read before
    the launch.
  - **The join password is exposed in two places**, a deliberate trade-off for
    the simpler route: (a) DST writes its command line, `+password` included,
    into `client_log.txt` and `backup/client_log/`, which persist after the game
    closes; (b) it is in the argv of `steam` and every game process
    (`/proc/<pid>/cmdline`), readable by any local process on the Deck while DST
    runs. Use this only for a low-value join password. The Deck's own copy, for
    the tile, is `~/.config/iaac/dst-play.env` at 0600. To rotate it, change the
    1Password item (`steamdeck_dst_play_password_item`), redeploy the server,
    then re-run `dst_play` to reinstall the Deck's copy.
  - The server start runs INSIDE the tile, so Steam shows the tile as running
    for as long as the start takes and DST appears the moment the tile ends.
    (Handing off at once instead shows the tile "running" for a second and then
    nothing for over a minute, which invites a second launch by hand.) If DST
    was started by hand anyway, the job skips the join but still stops the
    server when DST exits.
  - To test it end to end, press it THROUGH STEAM
    (`steam://rungameid/<gameid>`, which launches the tile, not DST), the same
    path as a real tap, and check that the tile is tracked as running, DST is
    launched by `-applaunch` with no prompt, the server counts the player, and
    the tile stops the empty server. Expect the server start to dominate (tens
    of seconds cold), the server to count the player within about a minute of
    the launch, and the stop to follow `steamdeck_dst_play_stop_grace` after DST
    closes.
- **Both DST scripts `unset LD_PRELOAD`.** Steam starts a tile with its overlay
  library (`gameoverlayrenderer.so`) preloaded into every child. With it
  inherited, a `tee` in the script could spin at 100% CPU and hold Steam's IPC
  shared memory, so the start hung and the tile stayed "running" for good.

## Who owns each shortcut

The role manages only shortcuts tagged `iaac`. `shortcuts_report`, and the
`shortcuts` plan's "left alone" list, label every other shortcut with its owner.
That owner comes from evidence on the entry, not a guess:

- `managed: yes — iaac`: tagged by this role.
- `managed: no — owned by Steam ROM Manager (EmuDeck)`: the shortcut's Exe is
  one of EmuDeck's launchers, `…/Emulation/tools/launchers/<emu>.sh`. SRM points
  every entry it generates there. Its tags are just the parser's category
  (`Emulation`, `Nintendo Game Boy Advance`), so they cannot identify it. The
  match ignores the install root, which can be the home directory or the SD
  card.
- `managed: no — origin unknown (not tagged by iaac)`: no matcher fits. The role
  cannot tell whether a person or some other tool added it.

The matchers are `steamdeck_shortcut_owners`, a list of `{owner, exe_search}`
entries. They only change the labels and never change what is written. Do not
adopt SRM's tiles into `steamdeck_managed_shortcuts`: SRM rewrites them every
time it runs.

## Artwork

The role ships no artwork. Tile art is the caller's data (usually third-party
images), kept on the controller in a directory named by `steamdeck_artwork_dir`:
one `<slug(NAME)>/` directory per shortcut, plus a `MANIFEST.json` recording
which shortcut each set belongs to. `artwork` restores each set to the
shortcut's current appid, and `steamdeck_artwork_action=backup` pulls the
device's grid directory into `<steamdeck_artwork_dir>/_device/`.

Restore only ever ADDS files: it never overwrites or deletes. So removing a tile
leaves its files in Steam's grid directory, keyed to an appid nothing references
any more.

**Prune** (`steamdeck_artwork_action=prune`) is the cleanup half. It previews by
default; add `steamdeck_apply=true` to delete. A file is an orphan only if all
three hold:

1. Its name is `<appid><variant>.<ext>` with a known variant (capsule, `p`,
   `_hero`, `_logo`, `_icon`, `.json`).
2. The appid is in the non-Steam-shortcut range: Steam sets the high bit on
   every generated shortcut appid, so it is ≥ 2³¹ and < 2³².
3. No shortcut currently in `shortcuts.vdf` has that appid, whoever owns it.
   Steam ROM Manager's tiles are protected too.

Everything else is kept and listed:

- real Steam games' custom art (appid < 2³¹);
- names it doesn't recognise, such as 64-bit game ids.

**Safety gates.** Before deleting, the role asserts that:

- every shortcut contributed exactly one protected appid;
- no orphan overlaps that set or is a Steam game;
- an empty protected set deletes nothing.

**Backup.** Orphans not byte-identical to art in `steamdeck_artwork_dir` are
fetched to `steamdeck_artwork_backup_dir` (default
`$HOME/.cache/iaac/steamdeck-grid-backup/<host>/<timestamp>/`) on the
controller, and each backup is verified by sha256. That directory is outside the
artwork tree on purpose.

**Deletion and read-back.** The delete removes only the previewed set; if the
grid changed in between, it deletes nothing. A read-back then requires zero
orphans and every kept file unchanged (size, mtime and sha256).

`files/test_steam_artwork_prune.py` tests this, including that a sabotaged
protect check fails the suite. The rule behind it: a role that creates things
must be able to remove them. `files/test_steam_artwork.py` checks that every set
in an artwork tree is findable by restore; point `STEAMDECK_ARTWORK_DIR` at your
tree to run its real-tree cases.

## Variables

Every option, its default and its rationale are in `meta/argument_specs.py`.
`defaults/main.yml` and `meta/argument_specs.yml` are generated from it.
