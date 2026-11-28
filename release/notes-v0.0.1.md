See what your inventory, character storage and server storage are worth on the market, sorted by value.

## Install
1. Install **[Npcap](https://npcap.com/#download)**, the same capture driver DPS meters use. Keep the default options, and leave *"Restrict Npcap driver's access to Administrators only"* unticked.
2. Download **`AION2Vault-v0.0.1-win64.zip`** below, unzip it, and run **`AION2Vault.exe`**. Keep the `_internal` folder next to it.
   - If SmartScreen appears, click **More info â†’ Run anyway**.
3. Open it **before** logging in to your character, then pick your server and faction at the top right.

## What's in v0.0.1
- **Live capture:** reads your inventory, character storage and server storage as you log in, and follows changes while you play.
- **Fair price:**
  - Uses the lowest of the current listing, the 7-day median and the 24h average sale price.
  - Prices more than 2.5Ã— the region median are capped at that median.
  - Hover any item to see the breakdown.
- **Extra columns:** 7-day trends, 24h change, number of listings and how fast an item sells, plus a market fee setting.
- **Kinah:** shown on its own card and not counted as an item.
- **Hidden:** Soul Sync recovery items aren't shown at all.
- **Not counted:** unidentified storage is listed as "Other" but left out of the total.
- **System tray:** closing the window keeps the app tracking in the tray. Click the icon to reopen it, or right-click → Quit.
- **Item data included:** item names and icons ship with the app, so it doesn't contact NCSOFT for them.

## Privacy & safety
- **Read-only:** it never sends anything to the game, and never touches game files or memory.
- **Your data stays local:** only your own items, kept on your PC (`%APPDATA%\AION2Vault`). Nothing is uploaded.
- **Prices:** from the public aion2.exchange API, polled gently and cached.
- **Unofficial:** a fan tool, not affiliated with NCSOFT. Use it at your own risk.

**SHA256:** see `AION2Vault-v0.0.1-win64.zip.sha256`.
