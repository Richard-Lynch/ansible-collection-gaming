<!--
Copyright: (c) 2026, Richard Lynch
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

All notable changes to this collection are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the collection
uses [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-11-03

### Added

- Role `steamdeck`: a Steam Deck managed in user space: library tiles (with removal by omission), artwork restore and prune from a caller-supplied tree, Moonlight, EmuDeck, Decky and CSS Loader settings, wake-on-LAN, a charge cap, and tiles that wake a host, restart its Steam and start, stop and join a Don't Starve Together server.
- Role `decky_plugin`: one Decky Loader plugin from a pinned GitHub release, with a verify entrypoint that answers loaded / failed / cannot tell.
- Roles `steam_workshop` (Workshop subscriptions through the Steam Web API) and `klei_dst_client` (Don't Starve Together client mods in the game's own mod state, native or under Proton).
- Role `service_dst_server`: an on-demand Don't Starve Together dedicated server under rootless podman, with an idle shutdown, backups and an allowlisted control endpoint; its INI and Lua renderers are the collection's filter plugins.
- Every site-specific value is an option the caller sets; nothing defaults to one.
