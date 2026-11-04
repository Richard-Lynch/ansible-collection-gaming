<!--
Copyright: (c) 2026, Richard Lynch
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

All notable changes to this collection are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the collection
uses [Semantic Versioning](https://semver.org/).

## [1.0.1] — 2026-11-04

### Changed

- The CI, release and disclose callers use ansible-devkit `v0.3.2`.

### Fixed

- `service_dst_server`: a real `--check` previews every write even with `dst_apply=true`. Each write's check mode was keyed on `dst_apply` alone, and a task-level check mode overrides the run's, so `--check` with the apply flag wrote files for real. The reads a deploy, status or probe depends on run under `--check`, and the read-backs of what a converge did (the image gate, the control endpoint's health check) are skipped under it.
- `steamdeck` `css_themes`: an apply with nothing to change no longer stops and restarts Decky Loader; the write, and the restart around it, run only when the plan has work.
- `steamdeck`: helper scripts are staged in a stable per-user directory, `~/.local/share/richard_lynch.gaming/steamdeck` under the target's HOME, instead of `/tmp`, which is emptied at every reboot (so each copy reported a change). The directory is derived when facts are gathered and created `0700`; `steamdeck_helper_dir` still overrides it, and a directory the caller names keeps its own mode. The `artwork` entrypoint's staged tree moves there too, so it persists between runs, and is synchronised with deletion so art removed from the source is no longer restored.
- `steamdeck`: the `emudeck`, `decky_settings`, `css_themes` and `shortcuts` entrypoints run under `--check`: their helper and report run (they only read), and `--check` with `steamdeck_apply=true` reports the plan without writing any settings or restarting anything (the role's own helper directory and scripts are still staged, because the report runs them).
- Both roles' molecule scenarios run the role under a real `--check` (nested `ansible-playbook`) and fail if any of the above regresses.

## [1.0.0] — 2026-11-03

### Added

- Role `steamdeck`: a Steam Deck managed in user space: library tiles (with removal by omission), artwork restore and prune from a caller-supplied tree, Moonlight, EmuDeck, Decky and CSS Loader settings, wake-on-LAN, a charge cap, and tiles that wake a host, restart its Steam and start, stop and join a Don't Starve Together server.
- Role `decky_plugin`: one Decky Loader plugin from a pinned GitHub release, with a verify entrypoint that answers loaded / failed / cannot tell.
- Roles `steam_workshop` (Workshop subscriptions through the Steam Web API) and `klei_dst_client` (Don't Starve Together client mods in the game's own mod state, native or under Proton).
- Role `service_dst_server`: an on-demand Don't Starve Together dedicated server under rootless podman, with an idle shutdown, backups and an allowlisted control endpoint; its INI and Lua renderers are the collection's filter plugins.
- Every site-specific value is an option the caller sets; nothing defaults to one.
