#!/usr/bin/env python3
"""Fetch monsters from 5etools and convert to KFC schema.

This script:
  1. Fetches all bestiary files from 5etools.
  2. Fetches books.json + adventures.json to build a source-abbreviation -> full-name map.
  3. Converts each monster to the KFC schema using full source names.
  4. Regenerates se_sources.json from the actual sources present in the monster data.
"""
import httpx
import asyncio
import json
from pathlib import Path

BASE_URL = "https://5e.gateslab.win"
OUT_MONSTERS = Path(__file__).parent.parent / "public" / "json" / "se_monsters.json"
OUT_SOURCES = Path(__file__).parent.parent / "public" / "json" / "se_sources.json"

CR_MAP = {"1/8": 0.125, "1/4": 0.25, "1/2": 0.5}

# Fallback mappings for sources not in books.json / adventures.json
EXTRA_SOURCE_NAMES: dict[str, str] = {
    "PSA": "Plane Shift: Amonkhet",
    "PSD": "Plane Shift: Dominaria",
    "PSI": "Plane Shift: Innistrad",
    "PSK": "Plane Shift: Kaladesh",
    "PSX": "Plane Shift: Ixalan",
    "PSZ": "Plane Shift: Zendikar",
    "MCV1SC": "Monstrous Compendium Volume 1: Spelljammer Creatures",
    "MCV2DC": "Monstrous Compendium Volume 2: Dragonlance Creatures",
    "MCV3MC": "Monstrous Compendium Volume 3: Minecraft Creatures",
    "ESK": "Essentials Kit",
    "MFF": "Mordenkainen's Fiends of the Forgotten Realms",
    "MisMV1": "Misplaced Monsters Volume 1",
    "TftYP": "Tales from the Yawning Portal",
    "VD": "Vecna Dossier",
    "SADS": "Sapphire Anniversary Dice Set",
    "SRD": "Systems Reference Document (SRD)",
    "Basic": "Basic Rules v1",
    # Plane Shift variants (5etools sometimes uses dashes, sometimes not)
    "PS-Z": "Plane Shift: Zendikar",
    "PS-I": "Plane Shift: Innistrad",
    "PS-K": "Plane Shift: Kaladesh",
    "PS-A": "Plane Shift: Amonkhet",
    "PS-X": "Plane Shift: Ixalan",
    "PS-D": "Plane Shift: Dominaria",
}

# Source types — best-effort classification used when generating se_sources.json
SOURCE_TYPES: dict[str, str] = {
    "MM": "Official", "PHB": "Official", "DMG": "Official",
    "VGM": "Official", "XGE": "Official", "MTF": "Official",
    "GGR": "Official", "ERLW": "Official", "EGW": "Official",
    "MOT": "Official", "TCE": "Official", "VRGR": "Official",
    "FTD": "Official", "SCC": "Official", "MPMM": "Official",
    "BAM": "Official", "BGG": "Official", "MPP": "Official",
    "BMT": "Official", "SRD": "Official", "Basic": "Official",
    "MCV1SC": "Official", "MCV2DC": "Official",
    "MCV3MC": "Official", "MCV4EC": "Official",
}

ADVENTURE_IDS: set[str] = {
    "LMoP", "HotDQ", "RoT", "PotA", "OotA", "CoS", "SKT", "TftYP",
    "ToA", "TTP", "WDH", "LLK", "WDMM", "KKW", "GoS", "HftT", "OoW",
    "DIP", "SLW", "SDW", "DC", "BGDIA", "LR", "IMR", "RMBRE",
    "IDRotF", "CM", "HoL", "RtG", "AitFR-ISF", "AitFR-THP",
    "AitFR-DN", "AitFR-FCD", "NRH-TCMC", "NRH-AVitW", "NRH-ASS",
    "NRH-CoI", "NRH-AWoL", "NRH-AT", "WBtW", "CRCotN", "JttRC",
    "DoSI", "LoX", "DSotDQ", "KftGV", "GotSF", "PaBTSO", "ToFW",
    "CoA", "DitLCoT", "LRDT", "VEoR", "QftIS", "ESK",
}
CR_MAP = {"1/8": "1/8", "1/4": "1/4", "1/2": "1/2"}

def parse_cr(cr_val) -> str:
    """Return CR as a string key matching CONST.CR in constants.js.
    Valid values: "0","1/8","1/4","1/2","1","2"..."30"
    """
    if cr_val is None:
        return "0"
    if isinstance(cr_val, dict):
        cr_val = cr_val.get("cr", 0)
    if isinstance(cr_val, str) and cr_val in CR_MAP:
        return CR_MAP[cr_val]
    try:
        n = float(cr_val)
        if n == 0.125:
            return "1/8"
        if n == 0.25:
            return "1/4"
        if n == 0.5:
            return "1/2"
        return str(int(n))
    except (ValueError, TypeError):
        return "0"


def get_type(monster: dict) -> str:
    t = monster.get("type", "")
    if isinstance(t, dict):
        inner = t.get("type", "Unknown")
        return str(inner).title() if not isinstance(inner, dict) else "Unknown"
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


def _alignment_str(alignment) -> str:
    """Robustly convert 5etools alignment (str, list of str/dict/list) to a string."""
    if not alignment:
        return ""
    if isinstance(alignment, str):
        return alignment
    if isinstance(alignment, list):
        parts = []
        for a in alignment:
            if isinstance(a, str):
                parts.append(a)
            elif isinstance(a, dict):
                val = a.get("special") or a.get("alignment") or ""
                parts.append(_alignment_str(val))
            elif isinstance(a, list):
                parts.append(_alignment_str(a))
            else:
                parts.append(str(a))
        return " ".join(parts)
    return str(alignment)


def convert(m: dict, source_name_map: dict[str, str]) -> dict:
    has_lair = bool(
        m.get("dragonCastingColor") or any(
            t.get("name", "").lower() == "lair actions"
            for t in (m.get("action") or [])
        )
    )
    sources_abbrev = m.get("source", "")
    # Map abbreviation to full name; fall back to abbreviation if unknown
    full_name = source_name_map.get(sources_abbrev, sources_abbrev)
    page = m.get("page", "")
    sources_str = f"{full_name}: {page}" if page else full_name

    return {
        "name": m.get("name", "Unknown"),
        "cr": parse_cr(m.get("cr")),
        "size": (m.get("size") or ["M"])[0] if isinstance(m.get("size"), list) else m.get("size", "M"),
        "type": get_type(m),
        "tags": get_tags(m),
        "section": m.get("group", ""),
        "alignment": _alignment_str(m.get("alignment", "")),
        "environment": ", ".join(m.get("environment", [])) if m.get("environment") else "",
        "ac": get_ac(m),
        "hp": get_hp(m),
        "init": get_init(m),
        "lair?": "lair" if has_lair else "",
        "legendary": "legendary" if m.get("legendary") else "",
        "unique": "unique" if m.get("isNpc") else "",
        "sources": sources_str,
    }


async def fetch_source_name_map(client: httpx.AsyncClient) -> dict[str, str]:
    """Build a map of 5etools source abbreviation -> full book name."""
    name_map: dict[str, str] = {}

    try:
        books_resp = await client.get(f"{BASE_URL}/data/books.json", timeout=30)
        books_data = books_resp.json()
        for b in books_data.get("book", []):
            name_map[b["id"]] = b["name"]
    except Exception as e:
        print(f"Warning: could not fetch books.json: {e}")

    try:
        adv_resp = await client.get(f"{BASE_URL}/data/adventures.json", timeout=30)
        adv_data = adv_resp.json()
        for a in adv_data.get("adventure", []):
            name_map[a["id"]] = a["name"]
    except Exception as e:
        print(f"Warning: could not fetch adventures.json: {e}")

    # Layer in extras (handles Plane Shift variants, SRD, etc.)
    for k, v in EXTRA_SOURCE_NAMES.items():
        if k not in name_map:
            name_map[k] = v

    return name_map


async def fetch_all_monsters(client: httpx.AsyncClient) -> list[dict]:
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


def build_sources_json(raw_monsters: list[dict], source_name_map: dict[str, str]) -> list[dict]:
    """Generate se_sources.json entries from the actual source abbreviations used in the monster data."""
    seen_abbrevs: set[str] = set()
    sources_out: list[dict] = []

    for m in raw_monsters:
        abbrev = m.get("source", "")
        if not abbrev or abbrev in seen_abbrevs:
            continue
        seen_abbrevs.add(abbrev)

        book_name = source_name_map.get(abbrev, abbrev)

        source_type: str
        if abbrev in SOURCE_TYPES:
            source_type = SOURCE_TYPES[abbrev]
        elif abbrev in ADVENTURE_IDS:
            source_type = "Official Adventure"
        elif book_name.startswith("Plane Shift"):
            source_type = "Official Web Supplement"
        else:
            source_type = "Official"

        # Official sources that are enabled by default
        default_enabled = abbrev in {
            "MM", "SRD", "Basic", "PHB", "VGM", "XGE", "MTF", "TCE",
            "MPMM", "FTD", "BGG", "VRGR", "MOT", "BAM", "MPP",
        }

        entry: dict = {
            "type": source_type,
            "name": book_name,
            "shortname": abbrev,
        }
        if default_enabled:
            entry["default"] = True

        sources_out.append(entry)

    # Sort: Official first, then by name
    order = ["Official", "Official Adventure", "Official Web Supplement", "Third-Party", "Community"]
    sources_out.sort(key=lambda s: (order.index(s["type"]) if s["type"] in order else 99, s["name"]))
    return sources_out


async def main():
    async with httpx.AsyncClient(verify=False) as client:
        print("Fetching source name map from 5etools...")
        source_name_map = await fetch_source_name_map(client)
        print(f"  Loaded {len(source_name_map)} source name mappings")

        print("Fetching monsters from 5etools...")
        monsters = await fetch_all_monsters(client)
        print(f"Fetched {len(monsters)} monsters, converting...")

    converted = [convert(m, source_name_map) for m in monsters]

    OUT_MONSTERS.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_MONSTERS, "w") as f:
        json.dump(converted, f, indent=2)
    print(f"Wrote {len(converted)} monsters to {OUT_MONSTERS}")

    sources_data = build_sources_json(monsters, source_name_map)
    with open(OUT_SOURCES, "w") as f:
        json.dump(sources_data, f, indent=2)
    print(f"Wrote {len(sources_data)} sources to {OUT_SOURCES}")


if __name__ == "__main__":
    asyncio.run(main())
