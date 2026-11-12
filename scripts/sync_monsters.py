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
import os
from pathlib import Path

BASE_URL = os.environ.get("FIVETOOLS_BASE_URL", "https://5e.gateslab.win")
OUT_MONSTERS = Path(__file__).parent.parent / "public" / "json" / "se_monsters.json"
OUT_SOURCES = Path(__file__).parent.parent / "public" / "json" / "se_sources.json"

CR_MAP = {"1/8": "1/8", "1/4": "1/4", "1/2": "1/2"}

# ── Size mapping: 5etools abbreviations → kobold lowercase full text ──
SIZE_MAP: dict[str, str] = {
    "T": "tiny",
    "S": "small",
    "M": "medium",
    "L": "large",
    "H": "huge",
    "G": "gargantuan",
}

# ── Alignment mapping: 5etools axis codes → full words ──
_ALIGN_LC_MAP: dict[str, str] = {"L": "lawful", "N": "neutral", "C": "chaotic"}
_ALIGN_GE_MAP: dict[str, str] = {"G": "good", "N": "neutral", "E": "evil"}

# ── Pre-computed map for common 2-letter alignment arrays ──
_ALIGN_TWO_LETTER: dict[tuple, str] = {
    ("L", "G"): "lawful good",
    ("N", "G"): "neutral good",
    ("C", "G"): "chaotic good",
    ("L", "N"): "lawful neutral",
    ("C", "N"): "chaotic neutral",
    ("L", "E"): "lawful evil",
    ("N", "E"): "neutral evil",
    ("C", "E"): "chaotic evil",
}


def _decode_alignment_list(align_list: list) -> str:
    """Decode a 5etools alignment array like ['C','E'] → \"chaotic evil\"."""
    # ── Pure single-code edge cases: U, A, N alone ──
    if len(align_list) == 1:
        solo = align_list[0]
        if solo == "U":
            return "unaligned"
        if solo == "A":
            return "any"
        if solo == "N":
            return "neutral"

    lc_codes: set[str] = set()
    ge_codes: set[str] = set()

    for code in align_list:
        if not isinstance(code, str):
            continue
        if code in ("L", "N", "C", "NX"):
            lc_codes.add(code)
        if code in ("G", "N", "E", "NY"):
            ge_codes.add(code)

    # ── No recognized codes at all ──
    if not lc_codes and not ge_codes:
        return " ".join(str(c) for c in align_list)  # fallback

    # Single L/C code without G/E partner
    if len(align_list) == 1:
        solo = align_list[0]
        if solo in _ALIGN_LC_MAP:
            return _ALIGN_LC_MAP[solo]
        if solo in _ALIGN_GE_MAP:
            return _ALIGN_GE_MAP[solo]

    # ── Two-letter combos (fast path) ──
    if len(align_list) == 2:
        key = (align_list[0], align_list[1])
        if key in _ALIGN_TWO_LETTER:
            return _ALIGN_TWO_LETTER[key]

    # ── "N" can appear on both axes — disambiguate ──
    # If N appears alongside an L/C code, it's on the G/E axis (neutral good/evil).
    # If N appears alongside a G/E code, it's on the L/C axis (lawful/chaotic neutral).
    # If N appears alone → "neutral" (handled above).

    # ── Complex patterns with NX/NY ──
    has_nx = "NX" in lc_codes
    has_ny = "NY" in ge_codes
    lc_specific = lc_codes - {"NX", "N"}
    ge_specific = ge_codes - {"NY", "N"}

    # Any alignment on L/C axis, non-good on G/E: ['L','NX','C','NY','E']
    if has_nx and has_ny and len(lc_specific) >= 2 and "E" in ge_specific:
        return "non-good"
    # Any alignment on L/C axis, evil only: ['L','NX','C','E']
    if has_nx and not has_ny and len(lc_specific) >= 2 and ge_specific == {"E"}:
        return "any evil"
    # Any chaotic alone on L/C, non-lawful on G/E: ['C','G','NY','E']
    if not has_nx and has_ny and lc_specific == {"C"} and len(ge_specific) >= 2:
        return "any chaotic"
    # Non-lawful on L/C, any on G/E: ['NX','C','G','NY','E']
    if has_nx and has_ny and "C" in lc_specific and len(ge_specific) >= 2:
        return "non-lawful"

    # NX + NY + N only → neutral (non-lawful, non-good, and neutral = just neutral)
    if has_nx and has_ny and not lc_specific and not ge_specific and "N" in lc_codes:
        return "neutral"

    # ── Fallback: build from the two axes ──
    lc_word = None
    ge_word = None

    # Extract explicit axis codes (L, C for law/chaos; G, E for good/evil)
    for code in align_list:
        if code in ("L", "C"):
            lc_word = _ALIGN_LC_MAP[code]
        elif code in ("G", "E"):
            ge_word = _ALIGN_GE_MAP[code]

    # Handle N: if present and both axes are empty, just return "neutral".
    # Otherwise place N on whichever axis is still missing a word.
    has_n = "N" in lc_codes or "N" in ge_codes
    if has_n:
        if lc_word is None and ge_word is None:
            return "neutral"
        elif lc_word is None:
            lc_word = "neutral"
        elif ge_word is None:
            ge_word = "neutral"
        # If both filled, N is redundant — ignore

    # Handle NX (non-lawful) and NY (non-good) — only if no explicit axis word yet
    if has_nx and lc_word is None:
        lc_word = "non-lawful"
    if has_ny and ge_word is None:
        ge_word = "non-good"

    if lc_word and ge_word:
        return f"{lc_word} {ge_word}"
    if lc_word:
        return lc_word
    if ge_word:
        return ge_word
    return " ".join(str(c) for c in align_list)


def _decode_size(size_val) -> str:
    """Decode a 5etools size value (usually a list like ['M']) → \"medium\"."""
    if isinstance(size_val, list) and size_val:
        first = size_val[0]
        if isinstance(first, str) and first in SIZE_MAP:
            return SIZE_MAP[first]
        return str(first)
    if isinstance(size_val, str):
        return SIZE_MAP.get(size_val, size_val)
    return str(size_val) if size_val else "medium"


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
    """Robustly convert 5etools alignment to a kobold-compatible string."""
    if not alignment:
        return ""
    if isinstance(alignment, str):
        return alignment
    if isinstance(alignment, list):
        # Collect all texts first (handles dicts with chance inside lists)
        texts: list[str] = []
        for a in alignment:
            if isinstance(a, str):
                texts.append(a)
            elif isinstance(a, dict):
                inner = a.get("alignment") or a.get("special") or ""
                texts.append(_alignment_str(inner))
            elif isinstance(a, list):
                texts.append(_alignment_str(a))
        # If all entries look like axis codes (single letter or NX/NY), decode as an alignment array
        _AXIS_CODES = {"L", "N", "C", "G", "E", "U", "A", "NX", "NY"}
        if all(t in _AXIS_CODES for t in texts if t):
            return _decode_alignment_list(texts)
        # Otherwise join (e.g. multiple alignments separated by "or"), deduplicating
        return " or ".join(dict.fromkeys(texts))
    if isinstance(alignment, dict):
        return str(alignment.get("special") or alignment.get("alignment") or "")
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
        "size": _decode_size(m.get("size")),
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


def _raise_for_status(response, url: str | None = None) -> None:
    """Raise a clear, actionable error for any non-200 response.

    Surfaces the URL and status code so a rate-limited or unreachable
    5etools backend is immediately identifiable instead of failing later
    with an opaque JSON decode error.
    """
    if response.status_code == 200:
        return
    target = url or str(response.url)
    body = (response.text or "").strip()
    detail = f": {body[:200]}" if body else ""
    raise RuntimeError(
        f"5etools request failed: {target} returned HTTP "
        f"{response.status_code} {response.reason_phrase}{detail}"
    )


async def fetch_all_monsters(client: httpx.AsyncClient) -> list[dict]:
    index_resp = await client.get(f"{BASE_URL}/data/bestiary/index.json", timeout=30)
    _raise_for_status(index_resp)
    index = index_resp.json()
    tasks = [
        client.get(f"{BASE_URL}/data/bestiary/{fname}", timeout=30)
        for fname in index.values()
    ]
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    monsters = []
    for fname, r in zip(index.values(), responses):
        if isinstance(r, Exception):
            raise RuntimeError(
                f"Failed to fetch {BASE_URL}/data/bestiary/{fname}: {r!r}"
            ) from r
        _raise_for_status(r, url=f"{BASE_URL}/data/bestiary/{fname}")
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


def resolve_copy(monster: dict, lookup: dict[tuple, dict], depth: int = 0) -> dict:
    """Resolve 5etools _copy references, inheriting fields from base monsters.

    The _copy system lets a monster declare ``_copy: {name, source}`` to
    inherit all fields from a base monster.  Explicit fields on the copying
    monster **override** inherited fields.  ``_mod`` and ``_templates`` are
    skipped because they only affect text blocks, not the key stat fields.
    """
    MAX_DEPTH = 5

    copy_info = monster.get("_copy")
    if not copy_info:
        return monster

    if depth > MAX_DEPTH:
        result = dict(monster)
        result.pop("_copy", None)
        return result

    base_name = copy_info.get("name", "")
    base_source = copy_info.get("source", "")
    lookup_key = (base_name.lower(), base_source)
    base = lookup.get(lookup_key)

    if base is None:
        result = dict(monster)
        result.pop("_copy", None)
        return result

    # Recursively resolve the base (handles chained _copy references)
    resolved_base = resolve_copy(base, lookup, depth + 1)

    # Start with all fields from the resolved base
    result = dict(resolved_base)

    # Overlay explicit fields from the copying monster (these take precedence)
    _SKIP_KEYS = {"_copy", "_templates", "_mod"}
    for key, value in monster.items():
        if key not in _SKIP_KEYS:
            result[key] = value

    # Clean up
    result.pop("_copy", None)

    return result


async def main():
    async with httpx.AsyncClient(verify=False) as client:
        print("Fetching source name map from 5etools...")
        source_name_map = await fetch_source_name_map(client)
        print(f"  Loaded {len(source_name_map)} source name mappings")

        print("Fetching monsters from 5etools...")
        monsters = await fetch_all_monsters(client)
        print(f"Fetched {len(monsters)} monsters")

        # Build lookup dict for _copy resolution: (name.lower(), source) -> raw monster
        lookup: dict[tuple, dict] = {}
        for m in monsters:
            key = (m.get("name", "").lower(), m.get("source", ""))
            lookup[key] = m

        copy_count = sum(1 for m in monsters if m.get("_copy"))
        print(f"Resolving _copy references ({copy_count} monsters)...")
        resolved = [resolve_copy(m, lookup) for m in monsters]
        print(f"  Resolved successfully")

    converted = [convert(m, source_name_map) for m in resolved]

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
