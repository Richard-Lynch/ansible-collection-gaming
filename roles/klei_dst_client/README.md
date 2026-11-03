# klei_dst_client

Switches **Don't Starve Together client mods** on or off in the game's own mod
state, on a device that plays DST. It does the job of ticking them in the game's
Mods screen.

It works on both kinds of install:

- **native Linux**, which keeps its state in `~/.klei/DoNotStarveTogether`;
- **the Windows build under Proton**, as on the Steam Deck. That keeps its state
  _inside the Proton prefix_:
  `…/compatdata/322330/pfx/drive_c/users/steamuser/Documents/Klei/DoNotStarveTogether`.
  Checking only the native location makes a played Proton client look as if it
  had never been launched.

## Usage

Run it after the account is subscribed to the mods (for example with
`richard_lynch.gaming.steam_workshop`). A subscription is what downloads a mod;
this role only switches it on.

```yaml
- name: Switch the DST client mods on
  hosts: dst_clients
  gather_facts: true
  tasks:
    - name: Converge the client's mod state
      ansible.builtin.include_role:
        name: richard_lynch.gaming.klei_dst_client
      vars:
        klei_dst_client_mods:
          - {id: "376333686", name: Combined Status}
```

The run reports the plan; `klei_dst_client_apply=true` writes it.

**Close the game on the device first.** DST rewrites its mod state when it
exits, so an edit made while it runs is lost. The role checks this and refuses
to apply while the game runs.

## The file it edits

`client_save/modindex`, in this format (decoded from a real client, not from
documentation):

```
KLEI     1D<base64( 16-byte header (1, 16, raw len, compressed len) + zlib( Lua ) )>
return {disable_special_event_warning=false,known_api_version=10,known_mods={...}}
```

The editor (`files/dst_modindex.py`):

- Changes **only** `known_mods["workshop-<id>"].enabled` for the declared mods.
  Every other field the game caches there, and every undeclared mod, is
  preserved. For a mod the game hasn't seen yet, it adds a minimal
  `{enabled=true}`.
- Re-encodes the result and checks that it decodes back to the same table before
  writing.
- Keeps the original as `modindex.iaac-bak`.

## Not done

Per-mod **configuration** (a client mod's settings, stored in
`mod_config_data/`). Mods run with their defaults until that is added.

## Cluster token (`tasks_from: cluster_token`)

Reads a Klei **cluster token**, for a server to run online. Tokens are minted
only at
[accounts.klei.com](https://accounts.klei.com/account/game/servers?game=DontStarveTogether)
("Add New Server"). There is no API. The game's console command
`TheNet:GenerateClusterToken()` is **deprecated**: it logs a pointer to that
page and writes nothing.

The account holder's step: on the site, "Download Settings", and unzip the
result into `~/Downloads`. This entrypoint finds `cluster_token.txt` there or in
either client home (native `~/.klei`, or the Proton prefix), picks the newest,
checks its shape, and sets `klei_dst_client_cluster_token` (no_log). It only
reads. Include it with `defaults_from: cluster_token`.

Storing the token is the caller's job, for example with
`richard_lynch.common.secrets_manager`, followed by a read-back.

## Crash marker (`tasks_from: reset_crash_marker`)

Resets the game's **crash marker**, `client_save/boot_modindex`. The game
writes `"loading"` when a load starts and `"done"` when it finishes. If it finds
`"loading"` at launch, it disables EVERY mod. Closing DST mid-load (in the
lobby, right after a join — which is where an automated join test closes it)
leaves the marker set, and the next launch turns every client mod off. This
entrypoint writes `"done"`, as the game would, keeping the file's header. It
does this only while no Steam game runs and only when the marker reads
`"loading"`. No gentler signal avoids it: what leaves the marker set is _when_
the game is closed, not how. It never closes the game (`steamdeck`'s
`close_game` entrypoint does). Include it with
`defaults_from: reset_crash_marker`.

Call it after anything that closes DST programmatically. A real player quits
from the menu and never needs it. Confirm afterwards with this role's plan:
every declared mod `before: True`.

## Joining from the command line

The client executable accepts `+connect <ip>:<port>` and `+password <pw>`. Its
strings also carry `+connect_lobby`, `+connect_steamid` and `+connect_netid`. No
Lua script reads launch arguments, so the engine handles them. The two ways to
pass them behave differently:

| Route                                            | Steam prompt?                                                                                                            | Joined?                                                         |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------- |
| `steam -applaunch 322330 +connect … +password …` | **No.** No `ShowGameArgs` step; the game process starts within seconds                                                   | **Yes**, with nobody touching the device                        |
| `steam://run/322330//+connect … +password …/`    | **Yes, every time.** `waiting for user response to ShowGameArgs`, the "… is trying to launch with custom options" dialog | Only when a person approves it. The approval is not remembered |

**An unanswered prompt blocks every later launch of DST.** While such a dialog
is pending, a later `steam://rungameid/322330` is accepted (`ExecuteSteamURL`)
and then does nothing: no GameAction, no process. A stray prompt is therefore
not cosmetic; it silently breaks every launcher until someone answers it on the
device. **Never use the `steam://run` form.**

DST's per-app Launch Options in Steam (`localconfig.vdf`) would apply to EVERY
DST launch, so they are no drop-in for a one-press join.

`steamdeck`'s `dst_play` tile (`templates/dst-play.sh.j2`) uses the
`-applaunch` route: it runs
`steam -applaunch 322330 +connect "$ip:$DST_PORT" +password "$DST_PASSWORD"` and
treats the join as done when the server's `/healthz` `players` rises above what
it read before the launch. No mod, no `modsettings.lua` edit.

**The password reaches the logs.** DST writes
`Command Line Arguments: +connect … +password <pw>` into `client_log.txt` and
keeps copies under `backup/client_log/`, which persist after the game closes.
The value is also in the argv of `steam` and every game process
(`/proc/<pid>/cmdline`), readable by any local process on the device while DST
runs. Use this route only for a low-value join password, and rotate it if it
matters: re-roll the item, redeploy the server, and reinstall the tile's copy.

`-applaunch` is not a way around a prompt. Steam does not raise one on that
route, a command run as the logged-in user on the device itself, and nothing
here suppresses or answers a dialog.
