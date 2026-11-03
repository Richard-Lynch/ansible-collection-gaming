# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Enable/disable client mods in a Don't Starve Together client",
            description=[
                "Edits the DST CLIENT's own mod state (client_save/modindex) on a device that",
                "plays the game — native Linux (~/.klei) or the Windows build under Proton (the",
                "Steam Deck). Only the declared mods' `enabled` field changes; everything else",
                "the game keeps there is preserved. The mods must already be downloaded, which",
                "a Workshop subscription does (roles/steam_workshop). Refuses while the game is",
                "running: it rewrites that file on exit. Generate-don't-apply.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "klei_dst_client_apply": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description="Actually write the modindex. Default false reports the plan only.",
                ),
                "klei_dst_client_mods": OptionSpec(
                    type="list",
                    elements="dict",
                    required=False,
                    default=[],
                    options={
                        "id": OptionSpec(type="str", required=True, description="Workshop id."),
                        "name": OptionSpec(
                            type="str",
                            required=False,
                            default="",
                            description="Label for reports.",
                        ),
                        "enabled": OptionSpec(
                            type="bool",
                            required=False,
                            default=True,
                            description="false switches it OFF; omitted mods are untouched.",
                        ),
                    },
                    description="Client mods to switch on (or off) in the game's Mods screen.",
                ),
                "klei_dst_client_home": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Home directory to look for the game under. Empty derives it from the",
                        "connecting user's facts (ansible_env.HOME); set only by tests.",
                    ],
                ),
                "klei_dst_client_python": OptionSpec(
                    type="str",
                    required=False,
                    default="python3",
                    description="Interpreter on the device for the modindex editor.",
                ),
            },
        ),
        "cluster_token": ArgumentSpec(
            short_description="Read the Klei cluster token a DST client generated",
            description=[
                "Finds cluster_token.txt — downloaded from accounts.klei.com into ~/Downloads, or",
                "left by an older client in either install's home (native ~/.klei or the Proton",
                "prefix) — checks its shape, and sets the fact klei_dst_client_cluster_token",
                "(no_log) for the caller to store. Reads only; fails with where to make one.",
            ],
            version_added="1.1",
            author=DEFAULT_AUTHOR,
            options={
                "klei_dst_client_home": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Home directory to look for the game under. Empty derives it from the",
                        "connecting user's facts (ansible_env.HOME); set only by tests.",
                    ],
                ),
            },
        ),
        "reset_crash_marker": ArgumentSpec(
            short_description="Reset DST's crash marker after the game was closed mid-load",
            description=[
                "The game writes client_save/boot_modindex = 'loading' when a load starts and",
                "'done' when it ends; a launch that finds 'loading' disables EVERY mod. A close",
                "while the client is still loading (a join test closing it in the lobby) leaves",
                "it set. This rewrites 'loading' to 'done', keeping the header, only while no",
                "Steam game runs. Proton (Steam Deck) install. Safe to run any time.",
            ],
            version_added="1.3",
            author=DEFAULT_AUTHOR,
            options={
                "klei_dst_client_home": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Home directory to look for the game under. Empty derives it from the",
                        "connecting user's facts (ansible_env.HOME); set only by tests.",
                    ],
                ),
            },
        ),
    }
)
