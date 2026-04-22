#!/usr/bin/env python3
"""Fetch monsters from 5etools and convert to KFC schema."""
import httpx
import asyncio
import json
from pathlib import Path

BASE_URL = "https://5e.gateslab.win"
OUT_FILE = Path(__file__).parent.parent / "public" / "json" / "se_monsters.json"

CR_MAP = {"1/8": 0.125, "1/4": 0.25, "1/2": 0.5}

def parse_cr(cr_val) -> float:
    if cr_val is None:
        return 0
    if isinstance(cr_val, dict):
        cr_val = cr_val.get("cr", 0)
    if isinstance(cr_val, (int, float)):
        return float(cr_val)
    return CR_MAP.get(str(cr_val), 0)

def get_type(monster: dict) -> str:
    t = monster.get("type", "")
    if isinstance(t, dict):
        return t.get("type", "Unknown").title()
    return str(t).title()

def get_tags(monster: dict) -> str:
    t = monster.get("type", {})
    if isinstance(t, dict):
        tags = t.get("tags", [])
        return ", ".join(str(x) for x in tags) if tags else ""
    return ""

def get_ac(monster: dict) -> int:
    ac = monster.get("ac", [{}])
    if isinstance(ac, list) and ac:
        first = ac[0]
        if isinstance(first, dict):
            return first.get("ac", 10)
        return int(first)
    return int(ac) if ac else 10

def get_hp(monster: dict) -> int:
    hp = monster.get("hp", {})
    if isinstance(hp, dict):
        return hp.get("average", 1)
    return int(hp) if hp else 1

def get_init(monster: dict) -> int:
    dex = monster.get("dex", 10)
    return (dex - 10) // 2

def convert(m: dict) -> dict:
    has_lair = bool(
        m.get("dragonCastingColor") or any(
            t.get("name", "").lower() == "lair actions"
            for t in (m.get("action") or [])
        )
    )
    sources_raw = m.get("source", "")
    page = m.get("page", "")
    sources_str = f"{sources_raw}: {page}" if page else sources_raw

    return {
        "name": m.get("name", "Unknown"),
        "cr": parse_cr(m.get("cr")),
        "size": (m.get("size") or ["M"])[0] if isinstance(m.get("size"), list) else m.get("size", "M"),
        "type": get_type(m),
        "tags": get_tags(m),
        "section": m.get("group", ""),
        "alignment": " ".join(m.get("alignment", [])) if isinstance(m.get("alignment"), list) else str(m.get("alignment", "")),
        "environment": ", ".join(m.get("environment", [])) if m.get("environment") else "",
        "ac": get_ac(m),
        "hp": get_hp(m),
        "init": get_init(m),
        "lair?": "lair" if has_lair else "",
        "legendary": "legendary" if m.get("legendary") else "",
        "unique": "unique" if m.get("isNpc") else "",
        "sources": sources_str,
    }

async def fetch_all_monsters() -> list[dict]:
    async with httpx.AsyncClient() as client:
        index = (await client.get(f"{BASE_URL}/data/bestiary/index.json", timeout=30)).json()
        tasks = [client.get(f"{BASE_URL}/data/bestiary/{fname}", timeout=30) for fname in index.values()]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        monsters = []
        for r in responses:
            if isinstance(r, Exception):
                continue
            data = r.json()
            monsters.extend(data.get("monster", []))
        return monsters

async def main():
    print("Fetching monsters from 5etools...")
    monsters = await fetch_all_monsters()
    print(f"Fetched {len(monsters)} monsters, converting...")
    converted = [convert(m) for m in monsters]
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(converted, f, indent=2)
    print(f"Wrote {len(converted)} monsters to {OUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
