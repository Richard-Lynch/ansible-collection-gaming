# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

# Options both entrypoints read. Declared once so the two specs cannot drift.
_COMMON = {
    "decky_plugin_directory": OptionSpec(
        type="str",
        required=True,
        pattern=r"^[A-Za-z0-9._-]+$",
        description=[
            "Directory name under ~/homebrew/plugins/. Decky derives the plugin's settings,",
            "data and log directories from THIS name (not the display name), so it is also",
            "~/homebrew/settings/<directory>/. Must equal the release archive's single",
            "top-level directory.",
        ],
    ),
    "decky_plugin_name": OptionSpec(
        type="str",
        required=True,
        min_length=1,
        description=[
            "The plugin's display name — plugin.json `name`. Decky keys its process title,",
            "its disabled list and its log lines on it. The archive's plugin.json must match.",
        ],
    ),
    "decky_plugin_home": OptionSpec(
        type="str",
        required=False,
        default="",
        description=[
            "Home directory holding ~/homebrew. Empty derives it from the connecting user's",
            "facts (ansible_env.HOME); set only by tests.",
        ],
    ),
    "decky_plugin_service": OptionSpec(
        type="str",
        required=False,
        default="plugin_loader",
        description="Decky Loader's systemd unit (a SYSTEM unit, running as root).",
    ),
    "decky_plugin_python": OptionSpec(
        type="str",
        required=False,
        default="python3",
        description="Interpreter on the device for the read-only probe.",
    ),
    "decky_plugin_ready_pattern": OptionSpec(
        type="str",
        required=False,
        default="",
        description=[
            "Python regex the plugin's backend writes to its decky.logger log once it has",
            "started (e.g. 'My Plugin backend loaded'). Empty: a running backend",
            "process alone counts as loaded — weaker, and the report says which was used.",
        ],
    ),
    "decky_plugin_config_file": OptionSpec(
        type="str",
        required=False,
        default="config.json",
        description="File name of the plugin's config in its settings directory.",
    ),
    "decky_plugin_config_check": OptionSpec(
        type="str",
        required=False,
        default="",
        pattern=r"^([A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*)?$",
        description=[
            "'module:function' importable from the plugin's OWN py_modules, called with the",
            "config path and returning (config, error) — error None means parsed. Lets the",
            "plugin's own validator judge the config, on the controller before it is written",
            "and on the device after. A returned config carrying a non-empty `errors` dict",
            "(per-section problems) is reported as invalid. Empty: JSON syntax only.",
        ],
    ),
}

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Install a Decky plugin from a pinned release, configure it, verify it",
            description=[
                "Downloads a release asset on the CONTROLLER with the authenticated GitHub CLI,",
                "refuses it unless its sha256 equals the pinned value, unpacks it, then converges",
                "~/homebrew/plugins/<directory>/ on the device to EXACTLY the archive's files and",
                "writes the plugin's config.json from the caller's dict. A change to the plugin's",
                "files restarts Decky (its watchdog does not reload an already-loaded plugin);",
                "a config-only change does not. Then verifies the RUNNING backend (process + its",
                "own log), not the files. Generate-don't-apply: reports the plan unless",
                "decky_plugin_apply.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "decky_plugin_apply": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description="Actually write to the device. Default false reports the plan only.",
                ),
                "decky_plugin_repo": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    pattern=r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?$",
                    description=[
                        "GitHub OWNER/REPO publishing the release. Required unless",
                        "decky_plugin_archive names a local archive.",
                    ],
                ),
                "decky_plugin_version": OptionSpec(
                    type="str",
                    required=True,
                    pattern=r"^v\d+\.\d+\.\d+$",
                    description="Release tag to install, vX.Y.Z. Pin it in inventory.",
                ),
                "decky_plugin_sha256": OptionSpec(
                    type="str",
                    required=True,
                    pattern=r"^[0-9a-f]{64}$",
                    description=[
                        "sha256 of the release ZIP. THE TRUST ANCHOR: the download is refused",
                        "unless it matches. The release's own .sha256 asset is only a cross-check",
                        "(it is published by the same hand as the zip). Pin it in inventory.",
                    ],
                ),
                "decky_plugin_asset": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description="Release asset name. Empty derives <directory>-<version>.zip.",
                ),
                "decky_plugin_archive": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Path ON THE CONTROLLER of an already-downloaded archive. Set, the GitHub",
                        "download is skipped (the sha256 is still enforced). For tests.",
                    ],
                ),
                "decky_plugin_staging_dir": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "Controller directory for downloads and unpacked packages. Empty derives",
                        "~/.cache/iaac/decky_plugin from the controller user's HOME.",
                    ],
                ),
                "decky_plugin_config": OptionSpec(
                    type="dict",
                    required=False,
                    default={},
                    description=[
                        "The plugin's config document, written as JSON. Empty writes none. Build",
                        "it from inventory so the plugin and anything else reading the same",
                        "values cannot disagree. NOT for secrets as written: the file is",
                        "readable by the device user and the role does not no_log it.",
                    ],
                ),
                "decky_plugin_config_mode": OptionSpec(
                    type="str",
                    required=False,
                    default="0600",
                    description=[
                        "Mode of the config file. Owned by the device user, whom the backend",
                        "runs as (plugin.json without the `root` flag).",
                    ],
                ),
                "decky_plugin_root_owner": OptionSpec(
                    type="str",
                    required=False,
                    default="root",
                    description=[
                        "Owner (and group) of the plugin's top directory and plugin.json. Every",
                        "other file, and the config, belong to the CONNECTING user — Decky's",
                        "HOST_USER, the account it runs unprivileged backends as. Decky re-chowns",
                        "the WHOLE plugin tree on load whenever the top directory is not owned",
                        "by the user it runs as (root), so this is what keeps a converge and",
                        "Decky from fighting. Tests set it to the test user.",
                    ],
                ),
                "decky_plugin_become": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Use privilege escalation for writes: ~/homebrew/plugins is root-owned",
                        "and the unit is a system unit. The CALLER supplies the become password.",
                    ],
                ),
                "decky_plugin_restart": OptionSpec(
                    type="bool",
                    required=False,
                    default=True,
                    description=[
                        "Stop Decky around a change to the plugin's files and start it after,",
                        "then require the backend to come up. False writes the files with Decky",
                        "running and skips the post-apply verify (tests; no systemd).",
                    ],
                ),
                "decky_plugin_verify_timeout": OptionSpec(
                    type="int",
                    required=False,
                    default=90,
                    min_value=10,
                    description="Seconds to wait after a Decky restart for the backend to report loaded.",
                ),
                **_COMMON,
            },
        ),
        "verify": ArgumentSpec(
            short_description="Read-only: is a Decky plugin loaded and healthy (running system)",
            description=[
                "Reads Decky's unit state, the plugin's backend process (Decky titles it",
                "'<name> (<path>/main.py)'), the newest log the backend wrote since it started,",
                "Decky's disabled list and the plugin's config judged by the plugin's own",
                "validator. Reports loaded / failed / cannot tell — never infers loaded from",
                "files on disk. Writes nothing.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "decky_plugin_require_loaded": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description="Fail unless the outcome is `loaded` (and the config parsed).",
                ),
                **_COMMON,
            },
        ),
    }
)
