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
import os
import random
import re
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

Pair = Tuple[str, str]

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


# ── Pair generation ───────────────────────────────────────────────────────────

def _side_codes(zones, zone_name, code_override, pattern):
    """Resolve the list of codes for one side, plus an optional validation regex.

    Returns ``(codes_or_None, regex_or_None, pattern_or_None)``. Exactly one of
    (zone_name|code_override) or pattern is expected."""
    if pattern:
        return None, re.compile(translate_pattern(pattern)), pattern
    if code_override:
        if not code_override.isdigit():
            raise ValueError(f"--*-code must be digits, got {code_override!r}")
        return [code_override], None, None
    if zone_name:
        z = find_zone(zones, zone_name)
        return list(zones[z]), None, None
    raise ValueError("each side needs a zone (--*-zone), a code (--*-code) or a pattern (--oad/--dad)")


def generate_pairs(zones, *, oad_zone=None, oad_code=None, oad_pattern=None,
                   dad_zone=None, dad_code=None, dad_pattern=None,
                   count=100, length=11, seed=None, unique=True) -> List[Pair]:
    """Generate ``count`` validated (A, B) pairs.

    Each side is driven by a zone (codes spread across the zone), a pinned code,
    or a raw switch pattern. Numbers are re-validated (must start with a chosen
    code, or match the pattern) before being accepted."""
    rng = random.Random(seed)
    a_codes, a_re, a_pat = _side_codes(zones, oad_zone, oad_code, oad_pattern)
    b_codes, b_re, b_pat = _side_codes(zones, dad_zone, dad_code, dad_pattern)

    def make(codes, regex, pat) -> str:
        if pat:
            n = gen_from_pattern(pat, length, rng)
            return n if regex.match(n) else make(codes, regex, pat)
        code = rng.choice(codes)
        return gen_from_code(code, length, rng)

    pairs: List[Pair] = []
    seen: set = set()
    attempts, cap = 0, count * 50 + 100
    while len(pairs) < count and attempts < cap:
        attempts += 1
        a = make(a_codes, a_re, a_pat)
        b = make(b_codes, b_re, b_pat)
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
                       seed=None, origin_code="", dest_code="", out_dir=None):
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
                   help="total digits per number (default 11; min code+4 enforced)")
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
