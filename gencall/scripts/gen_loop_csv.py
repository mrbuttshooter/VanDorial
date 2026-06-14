"""
GenCall loop number-pair CSV generator (sale-zone "drop zone" driven).

Generates a SIPp ``-inf`` CSV of A/B (``oad``/``dad``) number pairs for a Loop
Campaign. Instead of hand-writing switch regex, you pick a **sale zone** (one
country / breakout per side) and the generator pulls that zone's dial code(s)
from the Sale Codes deck and builds matching numbers. The output is the simple
2-column ``A;B`` form the LoopEngine consumes — the engine appends the per-call
hold column (``[field2]``) itself.

  origin  A-number = oad  (e.g. zone "Nigeria-Lagos"          -> code 2341)
  dest    B-number = dad  (e.g. zone "Guinea-Mobile (Orange)" -> code 22462...)

A zone may carry several codes; by default B/A numbers are spread across all of
the chosen zone's codes (pin one with ``--dad-code`` / ``--oad-code``). Each
number is ``<code><random subscriber digits>`` padded to ``--length`` digits and
is guaranteed to start with a real zone code, so MADA routes it to that zone.

Sale Codes deck resolution (first that exists):
  1. ``--codes PATH``
  2. ``$GENCALL_SALE_CODES``
  3. ``<this dir>/data/sale_codes.csv``         (full deck — delivered to the box)
  4. ``<this dir>/data/sale_codes.sample.csv``  (committed sample — tests/demo)

Examples::

    # browse zones
    python3 -m gencall.scripts.gen_loop_csv --list-zones guinea

    # 100 pairs: Nigeria-Lagos -> Guinea-Mobile (Orange), 11-digit numbers
    python3 -m gencall.scripts.gen_loop_csv \\
        --oad-zone "Nigeria-Lagos" --dad-zone "Guinea-Mobile (Orange)" \\
        --count 100 --length 11 --out /tmp/loop_numbers.csv

    # pin the exact dad code (only 22462) instead of spreading
    python3 -m gencall.scripts.gen_loop_csv \\
        --oad-zone "Nigeria-Lagos" --dad-zone "Guinea-Mobile (Orange)" \\
        --dad-code 22462 --count 50 --out /tmp/loop_numbers.csv

Advanced: raw switch patterns are still accepted via ``--oad``/``--dad`` (the
NetAxis ``^....2341.*\\|...`` form); see ``parse_skeleton``.
"""

from __future__ import annotations

import argparse
import csv as _csv
import logging
import os
import random
import re
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Set, Tuple

Pair = Tuple[str, str]

_log = logging.getLogger("gencall.gen_loop_csv")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DECK_FULL = os.path.join(_HERE, "data", "sale_codes.csv")
_DECK_SAMPLE = os.path.join(_HERE, "data", "sale_codes.sample.csv")


# ── Sale Codes deck ───────────────────────────────────────────────────────────

def resolve_deck_path(explicit: Optional[str] = None) -> str:
    """Return the first Sale Codes deck path that exists (see module docstring)."""
    for p in (explicit, os.environ.get("GENCALL_SALE_CODES"), _DECK_FULL, _DECK_SAMPLE):
        if p and os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "no Sale Codes deck found; pass --codes, set $GENCALL_SALE_CODES, or "
        f"place the deck at {_DECK_FULL}"
    )


def load_zones(deck_path: str) -> "OrderedDict[str, List[str]]":
    """Load ``zone -> [codes]`` from a 2-column (zone,code) CSV.

    A header row ``zone,code`` is skipped. Non-digit codes are ignored. Codes are
    de-duplicated per zone and kept shortest-first (the base breakout first).
    """
    zones: "OrderedDict[str, List[str]]" = OrderedDict()
    with open(deck_path, "r", encoding="utf-8", newline="") as fh:
        reader = _csv.reader(fh)
        for i, row in enumerate(reader):
            if len(row) < 2:
                continue
            zone, code = row[0].strip(), row[1].strip()
            if i == 0 and zone.lower() == "zone" and code.lower() == "code":
                continue
            if not code.isdigit():
                continue
            zones.setdefault(zone, [])
            if code not in zones[zone]:
                zones[zone].append(code)
    for z in zones:
        zones[z].sort(key=lambda c: (len(c), c))
    return zones


# Countries whose own name contains a dash, so country-splitting must NOT cut
# them at the first '-'. Extend as the deck reveals more.
KNOWN_DASH_COUNTRIES = ("Guinea-Bissau", "Timor-Leste")


def derive_country(zone: str) -> str:
    """Derive a country label from a zone name.

    Strips a trailing ``(operator)``, then takes the text before the first
    ``-`` — EXCEPT for known dash-named countries (e.g. Guinea-Bissau), which
    are kept whole. So ``Nigeria-Lagos`` -> ``Nigeria``,
    ``Guinea-Mobile (Orange)`` -> ``Guinea``, ``Guinea-Bissau-Mobile`` ->
    ``Guinea-Bissau``, ``Bosnia & Herzegovina (BH Telecom)`` ->
    ``Bosnia & Herzegovina``.
    """
    name = zone.split(" (", 1)[0].strip()
    for dc in KNOWN_DASH_COUNTRIES:
        if name == dc or name.startswith(dc + "-"):
            return dc
    return name.split("-", 1)[0].strip()


def build_country_tree(zones: Dict[str, List[str]]) -> "OrderedDict[str, List[str]]":
    """Group zone names by derived country: ``country -> [zone, ...]`` (sorted)."""
    tree: "OrderedDict[str, List[str]]" = OrderedDict()
    for zone in zones:
        tree.setdefault(derive_country(zone), []).append(zone)
    out: "OrderedDict[str, List[str]]" = OrderedDict()
    for country in sorted(tree):
        out[country] = sorted(tree[country])
    return out


def find_zone(zones: Dict[str, List[str]], query: str) -> str:
    """Resolve a zone name from a query: exact (case-insensitive) wins; otherwise
    a UNIQUE case-insensitive substring match. Raises ValueError listing
    candidates when ambiguous or absent."""
    q = query.strip().lower()
    for z in zones:
        if z.lower() == q:
            return z
    hits = [z for z in zones if q in z.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"no zone matches {query!r} (try --list-zones {query})")
    preview = ", ".join(hits[:12]) + (" ..." if len(hits) > 12 else "")
    raise ValueError(
        f"{query!r} is ambiguous ({len(hits)} zones): {preview}. "
        "Use the exact zone name or a longer substring."
    )


# ── Number building ───────────────────────────────────────────────────────────

# Total E.164 length (country code + national significant number) for common
# destinations. Used as the DEFAULT per-side number length so generated numbers
# are valid E.164 — a flat --length produced wrong-length numbers (e.g. an 11-digit
# Guinea number when valid Guinea is 12), which carriers reject with cause=3
# "no route to destination". Longest-prefix match on the dialed code's leading
# digits. Within-country variation (mobile vs fixed) exists; override a specific
# side with --oad-length / --dad-length when a zone needs an exact length.
E164_TOTAL_LEN = {
    "1": 11, "7": 11, "20": 12, "27": 11, "30": 12, "31": 11, "32": 11,
    "33": 11, "34": 11, "36": 11, "39": 12, "40": 11, "41": 11, "43": 12,
    "44": 12, "45": 10, "46": 11, "47": 10, "48": 11, "49": 12, "51": 11,
    "52": 12, "53": 10, "54": 12, "55": 12, "56": 11, "57": 12, "58": 12,
    "60": 11, "61": 11, "62": 12, "63": 12, "64": 11, "65": 10, "66": 11,
    "81": 12, "82": 12, "84": 11, "86": 13, "90": 12, "91": 12, "92": 12,
    "93": 11, "94": 11, "95": 11, "98": 12,
    "211": 12, "212": 12, "213": 12, "216": 11, "218": 12, "220": 10,
    "221": 12, "222": 11, "223": 11, "224": 12, "225": 13, "226": 11,
    "227": 11, "228": 11, "229": 11, "230": 10, "231": 12, "232": 11,
    "233": 12, "234": 13, "235": 11, "236": 11, "237": 12, "238": 10,
    "239": 10, "240": 12, "241": 11, "242": 12, "243": 12, "244": 12,
    "245": 10, "248": 10, "249": 12, "250": 12, "251": 12, "252": 12,
    "253": 11, "254": 12, "255": 12, "256": 12, "257": 11, "258": 12,
    "260": 12, "261": 12, "262": 12, "263": 12, "264": 11, "265": 12,
    "266": 11, "267": 11, "268": 11, "269": 10, "291": 10, "297": 10,
    "350": 11, "351": 12, "352": 11, "353": 12, "354": 10, "355": 12,
    "356": 11, "357": 11, "358": 12, "359": 12, "370": 11, "371": 11,
    "372": 11, "373": 11, "374": 11, "375": 12, "376": 9, "377": 11,
    "380": 12, "381": 12, "382": 11, "385": 12, "386": 11, "387": 11,
    "389": 11, "420": 12, "421": 12, "423": 12, "501": 11, "502": 11,
    "503": 11, "504": 11, "505": 11, "506": 11, "507": 11, "509": 11,
    "591": 11, "592": 10, "593": 12, "595": 12, "598": 11, "880": 13,
    "960": 10, "961": 11, "962": 12, "963": 12, "964": 13, "965": 11,
    "966": 12, "967": 12, "968": 11, "971": 12, "972": 12, "973": 11,
    "974": 11, "975": 11, "976": 11, "977": 13, "992": 12, "993": 11,
    "994": 12, "995": 12, "996": 12, "998": 12,
}


def e164_total_length(dialed_code: str, fallback: int) -> int:
    """Best-effort total E.164 length (CC + NSN) for a dialed code, by longest
    country-code prefix match; ``fallback`` when the country is unknown."""
    for n in (3, 2, 1):
        if dialed_code[:n] in E164_TOTAL_LEN:
            return E164_TOTAL_LEN[dialed_code[:n]]
    return fallback


def gen_from_code(code: str, total_len: int, rng: random.Random,
                  min_sub: int = 4) -> str:
    """``code`` + random subscriber digits, padded to ``total_len`` digits.

    At least ``min_sub`` subscriber digits are always appended (so a long code
    still yields a dialable number even if total_len is short)."""
    target = max(total_len, len(code) + min_sub)
    sub = "".join(str(rng.randint(0, 9)) for _ in range(target - len(code)))
    return code + sub


# ── Pattern path (advanced / legacy switch regex) ─────────────────────────────

def translate_pattern(pattern: str) -> str:
    """Switch regex dialect -> Python ``re``: ``\\|`` alternation becomes ``|``."""
    return pattern.replace(r"\|", "|")


def parse_skeleton(pattern: str) -> Tuple[int, str]:
    """Extract ``(lead_count, token)`` from a pattern's first alternative.

    ``lead_count`` = single ``.`` wildcards before the literal token; ``token`` =
    the longest digit run. Raises ValueError if the pattern has no digit token."""
    alt = pattern.split(r"\|", 1)[0]
    body = alt[1:] if alt.startswith("^") else alt
    runs = re.findall(r"\d+", body)
    if not runs:
        raise ValueError(f"pattern {pattern!r} has no literal digit token")
    token = max(runs, key=len)
    prefix = body.split(token, 1)[0]
    lead, i = 0, 0
    while i < len(prefix):
        ch = prefix[i]
        nxt = prefix[i + 1] if i + 1 < len(prefix) else ""
        if ch == ".":
            if nxt in ("*", "+"):
                i += 2
                continue
            lead += 1
        i += 1
    return lead, token


def gen_from_pattern(pattern: str, total_len: int, rng: random.Random) -> str:
    """Build a number matching a switch pattern: lead random digits + token +
    trailing pad. Validated by the caller against the translated regex."""
    lead, token = parse_skeleton(pattern)
    target = max(total_len, lead + len(token))
    head = ""
    if lead:
        head = str(rng.randint(1, 9)) + "".join(
            str(rng.randint(0, 9)) for _ in range(lead - 1))
    tail = "".join(str(rng.randint(0, 9)) for _ in range(target - lead - len(token)))
    return head + token + tail


# ── Routability guard ─────────────────────────────────────────────────────────
# Some sale zones carry many dial codes of which only a FEW actually route on the
# MADA chain — the rest are operator "breakouts" the switch rejects with
# CAU_NO_RT_DST (a 404 "no route to destination"). Spreading a loop across the
# WHOLE zone then dials mostly-dead numbers: observed live, zone "Guinea-Mobile
# (Orange)" lists 28 codes but only ``22462`` routes — the 27 ``2247x`` breakouts
# were 100% no-route on the wire and silently inflated the no-route rate.
#
# This map pins, per zone, the codes KNOWN to route. Generation is restricted to
# them when a zone is spread, and pinning a non-routable code fails loudly. Keyed
# by the EXACT deck zone name. We only constrain zones we have ground truth for,
# so every other zone is left untouched (no risk of wrongly narrowing a zone
# whose extra codes genuinely route). Shipped in CODE so the guard survives a
# deck re-import on an air-gapped box (the deck may still list the dead codes).
# Extend at runtime with ``$GENCALL_ROUTABLE_CODES`` ("Zone Name=22462|22463;
# Other Zone=555").
ROUTABLE_ALLOWLIST: Dict[str, Set[str]] = {
    "Guinea-Mobile (Orange)": {"22462"},
}


def _env_routable_overrides() -> Dict[str, Set[str]]:
    """Parse ``$GENCALL_ROUTABLE_CODES`` into ``{zone: {code, ...}}`` (empty when
    unset/malformed). Format: ``Zone Name=code|code; Other Zone=code,code``."""
    raw = os.environ.get("GENCALL_ROUTABLE_CODES", "") or ""
    out: Dict[str, Set[str]] = {}
    for chunk in raw.split(";"):
        if "=" not in chunk:
            continue
        zone, codes = chunk.split("=", 1)
        zone = zone.strip()
        cs = {c.strip() for c in codes.replace(",", "|").split("|")
              if c.strip().isdigit()}
        if zone and cs:
            out[zone] = cs
    return out


def allowlist_for(zone_name: str) -> Optional[Set[str]]:
    """Routable-code allowlist for a resolved zone name, or ``None`` when the zone
    has no known routability constraint (so all of its codes are usable)."""
    merged = dict(ROUTABLE_ALLOWLIST)
    merged.update(_env_routable_overrides())
    return merged.get(zone_name)


def routable_codes(zone_name: str, codes: List[str]) -> List[str]:
    """Filter ``codes`` to those known-routable for ``zone_name``.

    A zone with no allowlist entry is returned unchanged. If the allowlist would
    remove EVERY code (deck drift / typo in an override), fall back to the full
    list so generation never yields an empty pool — that case is a misconfig, not
    a reason to crash a campaign."""
    allow = allowlist_for(zone_name)
    if not allow:
        return codes
    kept = [c for c in codes if c in allow]
    return kept or codes


# ── Pair generation ───────────────────────────────────────────────────────────

def _side_codes(zones, zone_name, code_override, pattern):
    """Resolve the list of codes for one side, plus an optional validation regex.

    Returns ``(codes_or_None, regex_or_None, pattern_or_None)``. Exactly one of
    (zone_name|code_override) or pattern is expected. When a zone is given, the
    code list is restricted to the zone's routable codes (see ROUTABLE_ALLOWLIST);
    pinning a code that the zone is known not to route fails loudly."""
    if pattern:
        return None, re.compile(translate_pattern(pattern)), pattern
    if code_override:
        if not code_override.isdigit():
            raise ValueError(f"--*-code must be digits, got {code_override!r}")
        if zone_name:
            z = find_zone(zones, zone_name)
            allow = allowlist_for(z)
            if allow and code_override not in allow:
                raise ValueError(
                    f"code {code_override!r} is not routable for zone {z!r} — "
                    f"the switch returns no-route for it. Routable code(s): "
                    f"{', '.join(sorted(allow))}."
                )
        return [code_override], None, None
    if zone_name:
        z = find_zone(zones, zone_name)
        all_codes = list(zones[z])
        codes = routable_codes(z, all_codes)
        if len(codes) < len(all_codes):
            dropped = ", ".join(c for c in all_codes if c not in codes)
            _log.info("zone %r: spreading across %d routable code(s) of %d "
                      "(excluded unroutable: %s)",
                      z, len(codes), len(all_codes), dropped)
        return codes, None, None
    raise ValueError("each side needs a zone (--*-zone), a code (--*-code) or a pattern (--oad/--dad)")


def generate_pairs(zones, *, oad_zone=None, oad_code=None, oad_pattern=None,
                   dad_zone=None, dad_code=None, dad_pattern=None,
                   count=100, length=11, seed=None, unique=True,
                   oad_length=None, dad_length=None) -> List[Pair]:
    """Generate ``count`` validated (A, B) pairs.

    Each side is driven by a zone (codes spread across the zone), a pinned code,
    or a raw switch pattern. Each number is padded to a VALID E.164 length for
    its country (so carriers don't reject a wrong-length number) — derived from
    the dialed code's country code via ``e164_total_length``. Pass ``oad_length``
    / ``dad_length`` to force an exact length for a side; ``length`` is the final
    fallback for unknown countries. Numbers are re-validated (must start with a
    chosen code, or match the pattern) before being accepted."""
    rng = random.Random(seed)
    a_codes, a_re, a_pat = _side_codes(zones, oad_zone, oad_code, oad_pattern)
    b_codes, b_re, b_pat = _side_codes(zones, dad_zone, dad_code, dad_pattern)

    def make(codes, regex, pat, explicit_len) -> str:
        if pat:
            n = gen_from_pattern(pat, explicit_len or length, rng)
            return n if regex.match(n) else make(codes, regex, pat, explicit_len)
        code = rng.choice(codes)
        target = explicit_len or e164_total_length(code, length)
        return gen_from_code(code, target, rng)

    pairs: List[Pair] = []
    seen: set = set()
    attempts, cap = 0, count * 50 + 100
    while len(pairs) < count and attempts < cap:
        attempts += 1
        a = make(a_codes, a_re, a_pat, oad_length)
        b = make(b_codes, b_re, b_pat, dad_length)
        pair = (a, b)
        if unique and pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    if len(pairs) < count:
        raise RuntimeError(
            f"only generated {len(pairs)}/{count} unique pairs — raise --length "
            "or --allow-dupes (the chosen codes leave too few subscriber digits)")
    return pairs


def generate_pool_file(origin_zone, dest_zone, count=500000, length=11,
                       seed=None, origin_code="", dest_code="", out_dir=None,
                       oad_length=None, dad_length=None):
    """Generate an A/B number pool for a node and write it to a file.

    Resolves the deck, builds ``count`` validated pairs for the origin/drop sale
    zones, writes a bare ``A;B`` pool (no header/trailing) to ``out_dir`` (default
    ``$TMP/gencall_numbers``), and returns ``(csv_path, count, preview_rows)``.
    Raises ``ValueError`` on an unknown zone / impossible request.
    """
    import os
    import tempfile

    zones = load_zones(resolve_deck_path())
    pairs = generate_pairs(
        zones,
        oad_zone=origin_zone, oad_code=origin_code or None,
        dad_zone=dest_zone, dad_code=dest_code or None,
        count=count, length=length, seed=seed,
        oad_length=oad_length, dad_length=dad_length,
    )
    out_dir = out_dir or os.path.join(tempfile.gettempdir(), "gencall_numbers")
    os.makedirs(out_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="numbers_", suffix=".csv", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        write_csv(pairs, fh)
    preview = [f"{a};{b}" for a, b in pairs[:10]]
    return path, len(pairs), preview


def write_csv(pairs: List[Pair], out, order: Optional[str] = None) -> None:
    """Write A/B pairs as ``A;B`` rows, one pair per line (no trailing ``;``).

    ``order`` (``"sequential"`` / ``"random"``) prepends that SIPp ``-inf`` header
    line for standalone use; ``None`` (default) writes a bare headerless pool —
    the LoopEngine re-renders its own ``-inf`` (header + per-call hold column)
    from this pool, so a header here is not required.
    """
    if order:
        out.write(order.strip().upper() + "\n")
    for a, b in pairs:
        out.write(f"{a};{b}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gen_loop_csv",
        description="Generate a SIPp -inf A/B number CSV for a GenCall loop campaign, "
                    "by picking sale zones from the Sale Codes deck.")
    p.add_argument("--codes", help="Sale Codes deck CSV (zone,code). Default: bundled deck/sample.")
    p.add_argument("--list-zones", nargs="?", const="", metavar="SUBSTR",
                   help="list zones (optionally filtered by substring) and exit")
    p.add_argument("--list-countries", nargs="?", const="", metavar="SUBSTR",
                   help="list countries (optionally filtered) with their zone counts and exit")
    # origin (A / oad)
    p.add_argument("--oad-zone", help="origin sale zone (A-number)")
    p.add_argument("--oad-code", help="pin the origin code instead of spreading the zone")
    p.add_argument("--oad", help="advanced: raw switch pattern for A (e.g. '^....2341.*')")
    # destination (B / dad)
    p.add_argument("--dad-zone", help="destination/drop sale zone (B-number)")
    p.add_argument("--dad-code", help="pin the destination code instead of spreading the zone")
    p.add_argument("--dad", help="advanced: raw switch pattern for B (e.g. '^..22462.*')")
    # output
    p.add_argument("--count", type=int, default=500000,
                   help="rows to generate (default 500000 — a large random draw pool)")
    p.add_argument("--length", type=int, default=11,
                   help="fallback total digits when the country's E.164 length is unknown")
    p.add_argument("--oad-length", type=int, default=None,
                   help="force exact A-number length (overrides the E.164 default)")
    p.add_argument("--dad-length", type=int, default=None,
                   help="force exact B-number length (overrides the E.164 default)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible output")
    p.add_argument("--out", default="-", help="output path, or '-' for stdout (default)")
    p.add_argument("--order", choices=["sequential", "random"], default=None,
                   help="prepend a SIPp -inf order header (default: none — bare A;B pool)")
    p.add_argument("--allow-dupes", action="store_true", help="allow duplicate A/B pairs")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        deck = resolve_deck_path(args.codes)
        zones = load_zones(deck)
    except (FileNotFoundError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.list_countries is not None:
        q = args.list_countries.lower()
        tree = build_country_tree(zones)
        hits = [(c, zs) for c, zs in tree.items() if q in c.lower()]
        if not hits:
            print(f"no countries match {args.list_countries!r}", file=sys.stderr)
            return 1
        for country, zs in hits:
            print(f"{country}  [{len(zs)} zone(s)]")
        print(f"\n{len(hits)} country(ies); deck: {deck}", file=sys.stderr)
        return 0

    if args.list_zones is not None:
        q = args.list_zones.lower()
        hits = [(z, c) for z, c in zones.items() if q in z.lower()]
        if not hits:
            print(f"no zones match {args.list_zones!r}", file=sys.stderr)
            return 1
        for z, codes in hits:
            shown = ", ".join(codes[:8]) + (" ..." if len(codes) > 8 else "")
            print(f"{z}  [{len(codes)}]  {shown}")
        print(f"\n{len(hits)} zone(s); deck: {deck}", file=sys.stderr)
        return 0

    try:
        pairs = generate_pairs(
            zones,
            oad_zone=args.oad_zone, oad_code=args.oad_code, oad_pattern=args.oad,
            dad_zone=args.dad_zone, dad_code=args.dad_code, dad_pattern=args.dad,
            count=args.count, length=args.length, seed=args.seed,
            unique=not args.allow_dupes,
            oad_length=args.oad_length, dad_length=args.dad_length,
        )
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.out == "-":
        write_csv(pairs, sys.stdout, order=args.order)
    else:
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            write_csv(pairs, fh, order=args.order)
        print(f"wrote {len(pairs)} A/B pairs -> {args.out}  (deck: {deck})",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
