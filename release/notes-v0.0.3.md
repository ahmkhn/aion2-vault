**Fix for prices not loading with `SSL: CERTIFICATE_VERIFY_FAILED`.**

## Fixes
- **Prices load on more PCs.** HTTPS certificates are now checked the same way Windows and Edge check them. Before, some PCs that hadn't yet received a newer certificate root (aion2.exchange uses Let's Encrypt's newer chain) refused to load prices.
  - This also works with antivirus or network tools that scan HTTPS traffic.
  - Security is unchanged: expired, self-signed and mismatched certificates are still rejected.
- **Clearer error.** If a certificate still can't be verified, the app now says to check your PC's date and time and any antivirus HTTPS scanning, instead of showing a raw error.

## Install
1. Install **[Npcap](https://npcap.com/#download)**, the same capture driver DPS meters use. Keep the default options, and leave *"Restrict Npcap driver's access to Administrators only"* unticked.
2. Download **`AION2Vault-v0.0.3-win64.zip`** below, unzip it, and run **`AION2Vault.exe`**. Keep the `_internal` folder next to it.
   - If SmartScreen appears, click **More info → Run anyway**.
3. Open it **before** logging in to your character, then pick your server and faction at the top right.

Updating: close the old version (tray icon → Quit), then unzip this one anywhere. Your settings and cached prices carry over.

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
- **Market scope:** prices come from your server's market. The new world market isn't supported yet.

## Privacy & safety
- **Read-only:** it never sends anything to the game, and never touches game files or memory.
- **Your data stays local:** only your own items, kept on your PC (`%APPDATA%\AION2Vault`). Nothing is uploaded.
- **Prices:** from the public aion2.exchange API, polled gently and cached.
- **Unofficial:** a fan tool, not affiliated with NCSOFT. Use it at your own risk.

**SHA256:** see `AION2Vault-v0.0.3-win64.zip.sha256`.
