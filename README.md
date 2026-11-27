# AION 2 Vault

See what your inventory, character storage and server storage are worth on the market, sorted by value.

**[⬇ Download the latest release](https://github.com/ahmkhn/aion2-vault/releases/latest)**

## Install

1. Install **[Npcap](https://npcap.com/#download)**, the same capture driver DPS meters use. Keep the default options, and leave *"Restrict Npcap driver's access to Administrators only"* unticked.
2. Download `AION2Vault-vX.Y-win64.zip` from Releases, unzip it anywhere, and run **`AION2Vault.exe`**.
   - If Windows SmartScreen appears, click **More info → Run anyway**.

## Use

1. Open AION 2 Vault **before** logging in to your character. If you're already in game, go to character select and back.
2. Pick your **server and faction** at the top right.
3. Your items appear automatically. Open each storage in game once if it shows empty.
4. Hover over any item to see how its price was worked out.

**Price modes:**
- **Fair price** (default): the lowest of the current listing, the 7-day median and the 24h average sale price. If that's more than 2.5× the median on other servers in your region, the region median is used instead.
- **Lowest listing:** the cheapest current listing on your server.
- **Avg sold 24h:** the average sale price over the last 24 hours.

Set **Market fee %** to see what you'd actually receive after the market fee.

## Privacy & safety

- **Read-only:** it listens to the game's network traffic, like a DPS meter. It never sends anything to the game, never touches game files or memory, and never reads your login.
- **Only your own data:** it reads just your own items, keeps them on your PC (`%APPDATA%\AION2Vault`) and uploads nothing.
- **Outside services:**
  - Prices come from the public [aion2.exchange](https://aion2.exchange) API, fetched gently and cached. Your item IDs are mixed with random items, so a request doesn't reveal exactly what you own.
  - Item names and icons ship inside the app, so it doesn't contact NCSOFT for them. Only items newer than the release are looked up online.
- **Unofficial:** this is a third-party fan tool, not affiliated with NCSOFT. AION, item names and icons are © NCSOFT. Use it at your own risk.

## Troubleshooting

| Status | Fix |
|---|---|
| Npcap is required | Install Npcap, then press **Check again** |
| Npcap is set to administrators only | Press **Restart as administrator**, or reinstall Npcap with that option unticked |
| Connected mid-session | Relog, or switch character |
| Game not running | Start AION 2. The app looks for `AION2.exe` |

The log is in `%APPDATA%\AION2Vault\vault.log`. Deleting that folder resets the app.
