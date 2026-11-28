**Fix for v0.0.1 failing to start.** v0.0.1 could show *"Failed to resolve Python.Runtime.Loader.Initialize"* when unzipped from a browser download.

## Fixes
- **Starts when unzipped from a download.** Windows marks downloaded files as "from the internet", which stopped the app window from loading. The app now clears that mark from its own bundled files on start, the same as right-click → Properties → Unblock.
- **Never crashes if the window can't open.** If the window can't start for any other reason, the app opens in your browser and keeps a tray icon to reopen it or quit.

## Install
1. Install **[Npcap](https://npcap.com/#download)**, the same capture driver DPS meters use. Keep the default options, and leave *"Restrict Npcap driver's access to Administrators only"* unticked.
2. Download **`AION2Vault-v0.0.2-win64.zip`** below, unzip it, and run **`AION2Vault.exe`**. Keep the `_internal` folder next to it.
   - If SmartScreen appears, click **More info → Run anyway**.
3. Open it **before** logging in to your character, then pick your server and faction at the top right.

## Features
- **Live capture:** reads your inventory, character storage and server storage as you log in, and follows changes while you play.
- **Fair price:**
  - Uses the lowest of the current listing, the 7-day median and the 24h average sale price.
  - Prices more than 2.5× the region median are capped at that median.
  - Hover any item to see the breakdown.
- **Extra columns:** 7-day trends, 24h change, number of listings and how fast an item sells, plus a market fee setting.
- **Kinah:** shown on its own card.
- **Hidden or not counted:** Soul Sync recovery items are hidden. Unidentified storage shows as "Other" and isn't counted.
- **System tray:** closing the window keeps the app tracking in the tray. Click the icon to reopen it, or right-click → Quit.
- **Item data included:** item names and icons ship with the app, so it doesn't contact NCSOFT.

## Privacy & safety
- **Read-only:** it never sends anything to the game, and never touches game files or memory.
- **Your data stays local:** only your own items, kept on your PC (`%APPDATA%\AION2Vault`). Nothing is uploaded.
- **Prices:** from the public aion2.exchange API, polled gently and cached.
- **Unofficial:** a fan tool, not affiliated with NCSOFT. Use it at your own risk.

**SHA256:** see `AION2Vault-v0.0.2-win64.zip.sha256`.
