<!--
Copyright: (c) 2026, Richard Lynch
SPDX-License-Identifier: GPL-3.0-or-later
-->

# richard_lynch.gaming

Gaming roles: a Steam Deck managed in user space (library tiles, artwork, Moonlight, EmuDeck, Decky plugins, wake-on-LAN), Decky Loader plugin installs, Steam Workshop subscriptions, Don't Starve Together client mods, and an on-demand Don't Starve Together dedicated server under rootless podman.

## Roles

| Role | Description |
| ---- | ----------- |
| `decky_plugin` | Install a Decky plugin from a pinned release, configure it, verify it |
| `klei_dst_client` | Enable/disable client mods in a Don't Starve Together client |
| `service_dst_server` | Don't Starve Together dedicated server — rootless podman, on demand |
| `steam_workshop` | Reconcile a Steam account's Workshop subscriptions over the Web API |
| `steamdeck` | Manage the Steam Deck (SteamOS) — reporting and user-level config |

## Install

```sh
ansible-galaxy collection install richard_lynch.gaming
```

or pin it in `requirements.yml`:

```yaml
collections:
  - name: richard_lynch.gaming
    version: "1.0.0"
```

Requires ansible-core >=2.19.0.

## Development

```sh
# from a checkout at ansible_collections/richard_lynch/gaming
ansible-test sanity --docker
ansible-test units --docker
ansible-galaxy collection build
devkit conform                  # the repository standard (ansible-devkit)
devkit check all                # the static checks, configured by devkit.toml
```

CI, the disclosure gate and releases are the shared workflows of
[ansible-devkit](https://github.com/Richard-Lynch/ansible-devkit), called from
`.github/workflows/` at tag `v0.2.3`. A release is a merged pull
request that bumps `version` in `galaxy.yml` and adds the matching
`CHANGELOG.md` section. `export.manifest` lists every tracked path; update it in
the same commit as any file added or removed.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
