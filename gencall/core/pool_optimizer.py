"""
Adaptive number-pool optimizer (design: cut 404 "no route" on loops).

A loop dials ``<prefix><random subscriber>`` numbers. On a real route, only some
prefixes terminate — the rest come back ``404 No route to destination`` (Q.850
cause 3), capping ASR. Instead of an operator hand-pinning the routable prefix
(e.g. Guinea ``224626``), this learns it from the loop's own ``call_records``:

  1. ``prefix_asr``       — group answered/total by the B-number's leading digits.
  2. ``classify_prefixes`` — keep prefixes that route, drop the 404-heavy ones
                             (only once each has enough attempts to judge).
  3. ``rebuild_pool_csv`` — regenerate the A/B pool drawing B ONLY from the kept
                             prefixes, so the next dial cycle avoids the dead ranges.

The LoopEngine's optimizer monitor runs these on an interval for each running
campaign and restarts its UAC with the rebuilt pool ([loops] adaptive_pool).

Pure functions here (no engine/DB-write side effects beyond reads + file write)
so the policy is unit-testable without a live loop.
"""

from __future__ import annotations

import logging
import os
import random
import tempfile
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("gencall.pool_optimizer")

# A success is a 2xx final code (the loop only marks duration on a real answer);
# everything else (404/486/487/503/0…) is a non-connect for ASR purposes.
def _is_answered(final_code, duration_ms) -> bool:
    try:
        fc = int(final_code) if final_code is not None else 0
    except (TypeError, ValueError):
        fc = 0
    if 200 <= fc < 300:
        return True
    # Belt-and-suspenders: a positive billed duration is also an answer.
    try:
        return duration_ms is not None and int(duration_ms) > 0
    except (TypeError, ValueError):
        return False


def prefix_asr(db, campaign_id: str, prefix_len: int = 6) -> Dict[str, List[int]]:
    """Return ``{prefix: [answered, total]}`` for a campaign's B-numbers.

    ``prefix`` is the first ``prefix_len`` digits of ``b_number`` (country code +
    operator + a digit or two — e.g. ``224626``). Reads ``call_records`` directly;
    returns ``{}`` when there is no DB or no rows.
    """
    stats: Dict[str, List[int]] = {}
    if db is None:
        return stats
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT b_number, final_code, duration_ms FROM call_records "
                    "WHERE campaign_id = :cid AND b_number IS NOT NULL"
                ),
                {"cid": campaign_id},
            ).fetchall()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("prefix_asr read failed for %s: %s", campaign_id, e)
        return stats

    for b_number, final_code, duration_ms in rows:
        b = str(b_number or "")
        if len(b) < prefix_len:
            continue
        pfx = b[:prefix_len]
        cell = stats.setdefault(pfx, [0, 0])
        cell[1] += 1
        if _is_answered(final_code, duration_ms):
            cell[0] += 1
    return stats


def classify_prefixes(
    stats: Dict[str, List[int]],
    min_attempts: int = 30,
    min_asr: float = 0.5,
) -> Tuple[List[str], List[str], List[str]]:
    """Split prefixes into (keep, drop, undecided).

    A prefix is only judged once it has ``>= min_attempts`` calls:
      * keep   — ASR >= ``min_asr`` (it routes).
      * drop   — ASR <  ``min_asr`` (mostly 404 / no route).
    Under ``min_attempts`` it is *undecided* — never dropped on thin evidence, so
    a new prefix gets a fair trial before being pruned.
    """
    keep: List[str] = []
    drop: List[str] = []
    undecided: List[str] = []
    for pfx, (answered, total) in stats.items():
        if total < min_attempts:
            undecided.append(pfx)
            continue
        asr = answered / total if total else 0.0
        (keep if asr >= min_asr else drop).append(pfx)
    return keep, drop, undecided


def rebuild_pool_csv(
    *,
    origin_zone: str,
    origin_code: str,
    keep_prefixes: List[str],
    dad_length: int = 12,
    oad_length: Optional[int] = None,
    count: int = 500000,
    deck_path: Optional[str] = None,
    out_dir: Optional[str] = None,
    seed: Optional[int] = None,
) -> Tuple[str, int]:
    """Write a new A/B pool whose B-numbers come ONLY from ``keep_prefixes``.

    A-numbers are generated from the node's origin zone/code exactly as before;
    B-numbers are spread uniformly across the kept (routable) prefixes. Returns
    ``(csv_path, rows_written)``. Raises ``ValueError`` if there is nothing to
    keep or the origin can't be resolved.
    """
    from gencall.scripts.gen_loop_csv import (
        resolve_deck_path, load_zones, gen_from_code, write_csv,
    )

    if not keep_prefixes:
        raise ValueError("rebuild_pool_csv: keep_prefixes is empty")

    zones = load_zones(resolve_deck_path(deck_path))

    # Origin codes: an explicit pin wins, else spread across the origin zone's codes.
    if origin_code:
        oad_codes = [origin_code]
    else:
        oad_codes = zones.get(origin_zone) or []
        if not oad_codes:
            raise ValueError(f"unknown origin zone {origin_zone!r} (and no origin_code)")
    oad_len = oad_length or (len(oad_codes[0]) + 7)

    rng = random.Random(seed)
    pairs = []
    for i in range(count):
        a = gen_from_code(rng.choice(oad_codes), oad_len, rng)
        b = gen_from_code(keep_prefixes[i % len(keep_prefixes)], dad_length, rng)
        pairs.append((a, b))

    out_dir = out_dir or os.path.join(tempfile.gettempdir(), "gencall_numbers")
    os.makedirs(out_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="adapt_", suffix=".csv", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        write_csv(pairs, fh)
    return path, len(pairs)


def optimize(
    db,
    campaign: dict,
    node: Optional[dict],
    *,
    prefix_len: int = 6,
    min_attempts: int = 30,
    min_asr: float = 0.5,
    last_keep: Optional[List[str]] = None,
) -> Optional[dict]:
    """Analyze one campaign and, if there are dead prefixes to prune, rebuild its
    pool from the routable ones. Returns a report dict (or ``None`` = no change).

    Does NOT restart the loop — the caller (LoopEngine monitor) owns swapping the
    CSV onto the UAC. Returns ``None`` when there's nothing to prune (no dead
    prefixes yet, or pruning would leave no routable prefix), so the loop is left
    alone until there's real evidence. ``last_keep`` is the keep-set applied on
    the previous rebuild; when the new keep-set is identical we return ``None``
    so the loop is not restarted every interval for no change (anti-thrash).
    """
    stats = prefix_asr(db, campaign["id"], prefix_len)
    if not stats:
        return None
    keep, drop, undecided = classify_prefixes(stats, min_attempts, min_asr)
    if not drop:
        return None  # nothing proven dead yet — leave the pool as-is
    # Keep the routable prefixes AND the still-undecided ones (give them a trial);
    # only the proven-dead ``drop`` set is excluded.
    keep_set = sorted(set(keep) | set(undecided))
    if not keep_set:
        # Everything with enough data is dead — don't rebuild to an empty pool.
        logger.warning("adaptive pool %s: all prefixes dead, leaving pool unchanged",
                       campaign["id"])
        return None
    if last_keep is not None and keep_set == sorted(last_keep):
        return None  # keep-set unchanged since last rebuild — don't thrash

    node = node or {}
    dad_length = len(next(iter(stats)))  # observed B-number prefix len is a floor
    # Use the node's configured pool length for the full number length.
    dad_length = int(node.get("pool_length") or 12)
    path, n = rebuild_pool_csv(
        origin_zone=node.get("origin_zone", ""),
        origin_code=node.get("origin_code", ""),
        keep_prefixes=keep_set,
        dad_length=dad_length,
        count=int(node.get("pool_count") or campaign.get("pool_count") or 100000),
    )
    return {
        "campaign_id": campaign["id"],
        "csv_path": path,
        "rows": n,
        "kept": keep_set,
        "dropped": drop,
        "stats": {p: {"answered": a, "total": t, "asr": round(a / t, 3) if t else 0}
                  for p, (a, t) in stats.items()},
    }
