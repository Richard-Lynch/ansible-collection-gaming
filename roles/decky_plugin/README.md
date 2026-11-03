# decky_plugin

Installs **one Decky Loader plugin** from a pinned GitHub release, writes its
config, and checks that its backend is running. Works on any device that runs
Decky Loader (SteamOS, or a Fedora Atomic gaming desktop). It is built for a
plugin you publish yourself as a GitHub release asset.

| Entrypoint | What it does                                                                                                          |
| ---------- | --------------------------------------------------------------------------------------------------------------------- |
| `main`     | Downloads the release and checks it against the pin, plans the change, applies it with `decky_plugin_apply`, verifies |
| `verify`   | Read-only. Is the plugin loaded and healthy? Answers loaded / failed / cannot tell                                    |

## Usage

```yaml
- name: Install my Decky plugin
  hosts: handhelds
  gather_facts: true
  tasks:
    - name: Install, configure and verify the plugin
      ansible.builtin.include_role:
        name: richard_lynch.gaming.decky_plugin
      vars:
        decky_plugin_directory: my-plugin
        decky_plugin_name: My Plugin
        decky_plugin_repo: example-org/example-plugin
        decky_plugin_version: v1.2.3
        decky_plugin_sha256: "<sha256 of the release zip>"
        decky_plugin_config:
          server_url: http://streaming-host.example.com:8091
```

Run it once without `decky_plugin_apply` to see the plan, then with
`decky_plugin_apply=true` to write. The `verify` entrypoint
(`tasks_from: verify`) is read-only and reports the plugin's state. Keep the pins
and the config in inventory: if several things on the device (a plugin and Game
Mode tiles, say) need the same URLs or addresses, derive both from the same
inventory values so they cannot point at different places.

## Supply chain: why this installs a plugin at all

`steamdeck`'s `decky_plugins` entrypoint only _removes_ plugins. It says that
installing from a third-party CDN is a supply-chain decision to make on purpose.
This role is that decision, and it is narrow:

- **Only a plugin you build yourself.** It is meant for a plugin built by your
  own CI (for example GitHub Actions in a repository you control, private or
  not) and published as a release asset.
- **Downloaded on the controller** with `gh release download`, using the GitHub
  CLI credential the controller already has. No new token is minted.
- **The sha256 pinned in inventory is the trust anchor.** The download is
  refused unless its digest equals the pin. A changed or re-published release
  therefore can't reach the device unless the pin changes in a reviewed commit.
  The release's own `.sha256` asset is also checked, but only as a cross-check.
  It is published by the same workflow as the zip, so a match shows the download
  is intact. It does not show that this is the build that was pinned.
- The archive must hold exactly one top-level directory, with the expected name,
  and a `plugin.json` whose `name` matches. Absolute paths, `..` members and
  symlinks are refused.
- The plugin's own validator (`decky_plugin_config_check`) runs **on the
  controller** against the rendered config before anything is written. This
  imports the plugin's Python from the _verified_ package. It is your own code,
  and this only happens after the pin check.

A release that doesn't exist (yet) is a clean `RELEASE vX NOT FOUND` report in
preview, and a failure in apply. Any _other_ `gh` error (auth, network, a typo
in the repo) is a failure in both modes. It never shows up as "not published".

## What it writes, and why the ownership looks odd

| Path                                      | Owner           | Mode        |
| ----------------------------------------- | --------------- | ----------- |
| `~/homebrew/plugins/<dir>/`               | root            | 0755        |
| `~/homebrew/plugins/<dir>/plugin.json`    | root            | 0644        |
| everything else in the archive            | the device user | 0755 / 0644 |
| `~/homebrew/settings/<dir>/<config_file>` | the device user | 0600        |

This is **Decky's own layout**. On every load, Decky (`plugin.py`, v3.2.9)
checks who owns the plugin's top directory. If
it isn't root, Decky chowns the whole tree to the backend's user, sets the top
directory back to root and runs `chmod -R 755`. Writing the layout Decky would
produce itself means the converge and Decky never fight.

The plugin directory converges to **exactly** the archive: files on the device
that aren't in the archive are removed. The exception is anything under
`__pycache__`. That is Python's byte cache, which the backend writes as the
device user. Removing it would make every run report a change.

The config is meant for **non-secret** settings (URLs, a MAC, addresses, ports,
timeouts), and the role does not `no_log` it; do not put a credential in it. It is still written 0600, because the backend
runs as its owner (`plugin.json` has no `root` flag) and nothing else needs to
read it.

## Reload: when Decky is restarted, and when it isn't

From Decky Loader v3.2.9's `loader.py`:

- The file watcher only reacts to `main.py` and `dist/index.js` events.
- For a plugin that is **already loaded**, it _refuses_ to reload unless
  `plugin.json` has the `debug` flag. It logs: "already loaded and has requested
  to not be re-loaded". So an upgrade is never picked up live.
- A **first** install would be hot-loaded, but by whichever of those two files
  lands first. At that point `py_modules/` may not exist yet, and the failed
  attempt then blocks the retry.

So when the plugin's **files** change, the role **stops `plugin_loader` for the
write and starts it after**, in a `block`/`always`. The loader comes back even
if the write fails. This is the same pattern the `steamdeck` role uses for its
Decky edits. A restart reloads the Decky UI. It doesn't stop a running game.

A **config-only** change restarts nothing. Decky doesn't watch `settings/`, so
this suits a plugin that reads its config file again on every action.

## Verify: judged from the running system

`files/decky_plugin_probe.py`, run as the device user. It writes nothing.

| Evidence                                              | Source                                                                                                                                                    |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Decky is active, and when it started                  | `systemctl is-active` / `ActiveEnterTimestamp`                                                                                                            |
| the backend process exists, and which user it runs as | `/proc`: Decky retitles it `"<name> (<dir>/main.py)"`                                                                                                     |
| the backend started cleanly                           | the newest `~/homebrew/logs/<dir>/<YYYY-MM-DD HH.MM.SS>.log` _opened since that process started_, holding `decky_plugin_ready_pattern` and no error lines |
| not disabled                                          | `~/homebrew/settings/loader.json` `disabled_plugins`                                                                                                      |
| config accepted                                       | the plugin's own validator, imported from the **installed** `py_modules`                                                                                  |
| load failures Decky logged                            | the `plugin_loader` journal since it started. Supporting evidence only (see below)                                                                        |

Outcomes: **loaded**, **failed** (with the reason), or **cannot tell**. "Cannot
tell" means the process table or Decky's state couldn't be read, or the backend
is running but no log of its _current_ start shows the ready line. Old log files
never count. After an apply that restarted Decky, the role waits up to
`decky_plugin_verify_timeout` for **loaded**, then asserts it.

A Steam Deck's journal for `plugin_loader` is heavily rotated: a whole boot can
leave a few dozen lines, none of them about plugins loading. So when the journal
says nothing, that is not taken as evidence either way.

What it **cannot** see: the frontend panel. That renders inside Steam's CEF.
Open the Quick Access Menu to check it.

## Options

See `meta/argument_specs.py`. Your values are passed in from inventory:
`decky_plugin_repo`, `decky_plugin_version`, `decky_plugin_sha256` and
`decky_plugin_config`. The paths and the file owner come from the connecting
user's facts. `decky_plugin_root_owner`, `decky_plugin_become`,
`decky_plugin_restart`, `decky_plugin_home`, `decky_plugin_archive` and
`decky_plugin_staging_dir` exist so the molecule scenario can run on localhost.

## Tests

- `files/test_decky_plugin.py` (stdlib `unittest`, run by molecule) covers:
  - the pin and sidecar refusals
  - the archive-shape refusals (wrong or extra top-level directory, `..`,
    symlink, missing `plugin.json`, wrong name)
  - that the unpack is idempotent
  - the config verdicts
  - that a probe on a home with no Decky never says "loaded"
- `molecule/default`:
  - The real role runs against a fixture zip and a fixture home.
  - A wrong sha256 is refused, and a config the plugin rejects is refused.
    Neither one writes to the device tree; this is compared file by file before
    and after.
  - The plugin directory converges to exactly the archive: a stale file is
    removed and `__pycache__` is kept.
  - config.json is rendered with numbers as numbers, at 0600.
  - A second converge changes nothing.
- Not covered by tests: `gh release download` (needs a GitHub credential) and
  the Decky restart with a live **loaded** verdict (needs a device running
  Decky).
