# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

DEFAULT_AUTHOR = "Collection maintainers"
from ansible_devkit.argspec.models import ArgumentSpec, ArgumentSpecs, OptionSpec

argument_specs = ArgumentSpecs(
    argument_specs={
        "main": ArgumentSpec(
            short_description="Reconcile a Steam account's Workshop subscriptions over the Web API",
            description=[
                "Subscribes the account to every declared Workshop item it is not yet",
                "subscribed to (and unsubscribes only items declared state=absent), then reads",
                "the subscriptions back to prove it. Runs on the CONTROLLER: a subscription",
                "belongs to the account, and every Steam client signed in to it downloads the",
                "item. Generate-don't-apply: reports the plan unless steam_workshop_apply.",
            ],
            version_added="1.0",
            author=DEFAULT_AUTHOR,
            options={
                "steam_workshop_apply": OptionSpec(
                    type="bool",
                    required=False,
                    default=False,
                    description="Actually subscribe/unsubscribe. Default false reports the plan only.",
                ),
                "steam_workshop_steamid": OptionSpec(
                    type="str",
                    required=True,
                    pattern=r"^7656119\d{10}$",
                    description=[
                        "The account's 17-digit SteamID64; set it in inventory. (The number Steam",
                        "uses for userdata/ and the DST client's save folder is the 32-bit",
                        "ACCOUNT id; SteamID64 = account id + 76561197960265728.)",
                    ],
                ),
                "steam_workshop_items": OptionSpec(
                    type="list",
                    elements="dict",
                    required=False,
                    default=[],
                    options={
                        "id": OptionSpec(
                            type="str",
                            required=True,
                            description="Workshop item id (digits).",
                        ),
                        "app_id": OptionSpec(
                            type="int",
                            required=True,
                            description="The game's Steam app id (DST: 322330).",
                        ),
                        "name": OptionSpec(
                            type="str",
                            required=False,
                            default="",
                            description="Label for reports.",
                        ),
                        "state": OptionSpec(
                            type="str",
                            required=False,
                            default="present",
                            choices=["present", "absent"],
                            description="absent unsubscribes; omitted items are never touched.",
                        ),
                    },
                    description="The declared subscriptions.",
                ),
                "steam_workshop_api_key_item": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description=[
                        "1Password item holding the account owner's Steam Web API key (minted",
                        "at steamcommunity.com/dev/apikey, once, by the account holder). Required.",
                    ],
                ),
                "steam_workshop_api_key_field": OptionSpec(
                    type="str",
                    required=False,
                    default="credential",
                    description="Field of the item holding the key.",
                ),
                "steam_workshop_secret_vault": OptionSpec(
                    type="str",
                    required=False,
                    default="",
                    description="1Password vault of the key item. Required.",
                ),
                "steam_workshop_api_base": OptionSpec(
                    type="str",
                    required=False,
                    default="https://api.steampowered.com",
                    description="Web API base URL. Only tests point it elsewhere.",
                ),
            },
        ),
    }
)
