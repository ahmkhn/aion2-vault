"""Write web/itemdb.json from whatever tools/.itemdb_cache.json holds so far.

Handy for a test build while build_itemdb.py is still running; it never deletes icons.
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")

with open(os.path.join(HERE, ".itemdb_cache.json"), encoding="utf-8") as f:
    cache = json.load(f)
db = {}
for k, v in cache.items():
    if not v.get("name"):
        continue
    if v.get("icon") and not os.path.exists(os.path.join(WEB, v["icon"])):
        v = {**v, "icon": None}
    db[k] = v
with open(os.path.join(WEB, "itemdb.json"), "w", encoding="utf-8") as f:
    json.dump({"built": int(time.time()), "items": db}, f, separators=(",", ":"))
with_icon = sum(1 for v in db.values() if v.get("icon"))
print(f"itemdb.json: {len(db)} items ({with_icon} with icons)")
