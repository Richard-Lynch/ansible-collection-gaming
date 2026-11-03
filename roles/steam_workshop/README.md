# steam_workshop

Reconciles a Steam account's **Workshop subscriptions** against a declared list,
through Steam's Web API (`IPublishedFileService`). This replaces clicking
"Subscribe" on each Workshop page.

A subscription belongs to the **account**, not a device. Once the account is
subscribed, every Steam client signed in to it downloads the item: a mod
subscribed on the website shows up on a Steam Deck signed in to that account
within minutes, before the game is even launched. So this role runs on the
**controller**, once per account.

## Usage

Run it on `localhost`, once per account:

```yaml
- name: Subscribe the account to its Workshop items
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Reconcile the Workshop subscriptions
      ansible.builtin.include_role:
        name: richard_lynch.gaming.steam_workshop
      vars:
        steam_workshop_steamid: "76561197960265729"
        steam_workshop_api_key_item: steam/webapi-key
        steam_workshop_secret_vault: Example-Vault
        steam_workshop_items:
          - {id: "376333686", app_id: 322330, name: Combined Status}
```

The run reports the plan; `steam_workshop_apply=true` subscribes and reads back.

Inputs are in [`meta/argument_specs.py`](meta/argument_specs.py):

- `steam_workshop_steamid` (required): the 17-digit SteamID64. It equals the
  32-bit account id plus `76561197960265728`.
- `steam_workshop_items`: a list of `{id, app_id, name, state}`.
- `steam_workshop_api_key_item` and `steam_workshop_secret_vault` (both
  required): the 1Password item holding the key, and its vault. The field
  defaults to `credential`.

## How it decides, and what it never does

- `GetUserFiles type=mysubscriptions` reads what is subscribed. `Subscribe` is
  called for each declared item that's missing, and `Unsubscribe` **only** for
  items declared `state: absent`.
- **Undeclared subscriptions are never touched.** The account has subscriptions
  for other games and other reasons.
- **Each change is proven by a read-back.** A 200 from `Subscribe` means the
  request was accepted, not that the subscription exists. If the read-back
  doesn't show the change, the run fails. The test suite's fake API accepts a
  subscribe and drops it, to prove that gate fires.
- **The key never reaches argv**: it's passed via the environment, with `no_log`
  on that task.

## Prerequisite (the account holder, once)

A Steam Web API key for the account, from
<https://steamcommunity.com/dev/apikey>, stored in 1Password as the item named by
`steam_workshop_api_key_item`. A regular user's own key is accepted by
`IPublishedFileService/Subscribe` (NexusMods uses the same key type for this).
Every change is proven by the read-back, so a first run against the real API is
its own acceptance test.

## Tests

`files/test_steam_workshop.py` is stdlib-only and runs against a local fake API:
the plan, an idempotent apply, the accepted-but-dropped failure, and refusal
without a key. The molecule scenario runs it too.
