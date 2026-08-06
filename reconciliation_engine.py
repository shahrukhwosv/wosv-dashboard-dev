"""
Reconciliation engine: matches Lightspeed card sales against ValorPay
charges to find mismatches (wrong amount typed in, or no charge at all).

Matching strategy (four passes):
  PASS 1 - Confident match: amount matches exactly (within 1 cent) AND
           timestamps are within the tight tolerance window. The employee
           charged the right amount right away.

  PASS 2 - Delayed match: amount STILL matches exactly, but only within the
           wider time window. Same conclusion as pass 1 (correct charge) -
           the employee was just slower to type it into the Valor terminal.
           This is still a genuine match, not a mismatch, since the amount
           is exactly right. Marked separately so you can see it took longer
           than usual, without it being flagged as an error.

  PASS 3 - Split payment: a customer paid with two (or three) separate
           cards for one sale. No single Valor charge matches the sale
           amount, but two or three nearby charges together add up to it
           exactly. Counted as a genuine match, flagged as split=True so you
           can still see it wasn't a single simple charge.

  PASS 4 - For anything STILL left after all amount-based passes, look for
           the closest-in-time leftover on the other side (ignoring amount)
           within the wide window. This surfaces genuine "likely typo" cases:
           e.g. a Lightspeed sale for $45.00 with no exact match anywhere
           nearby in time, but a Valor charge for $54.00 one minute later -
           probably a transposed-digit typo.

Anything still unmatched after all four passes is a genuine exception:
either the employee never charged the card at all (Lightspeed sale, no
Valor charge anywhere nearby), or a Valor charge exists with no
corresponding Lightspeed sale (could be a duplicate charge, or a sale rung
up under the wrong day/store).
"""

from datetime import timedelta
from itertools import combinations


def _find_best_exact_amount_match(ls, valor_pool, max_seconds):
    best_match = None
    best_diff = None
    for v in valor_pool:
        if abs(ls["total"] - v["base_amount"]) > 0.01:
            continue
        time_diff = abs((ls["timestamp"] - v["timestamp"]).total_seconds())
        if time_diff > max_seconds:
            continue
        if best_diff is None or time_diff < best_diff:
            best_match = v
            best_diff = time_diff
    return best_match, best_diff


def _find_split_payment_match(ls, valor_pool, max_seconds, max_way_split=3):
    """
    Looks for 2 or 3 nearby Valor charges whose amounts sum exactly to the
    Lightspeed sale total - covers a customer splitting one sale across
    multiple cards. Only considers charges within max_seconds of the sale to
    keep the search small. Returns the matching group (list of charges) and
    the max time gap among them, or (None, None) if nothing fits.
    """
    candidates = [
        v for v in valor_pool
        if abs((ls["timestamp"] - v["timestamp"]).total_seconds()) <= max_seconds
    ]
    # Try 2-way splits first (by far the most common), then 3-way.
    for way in range(2, max_way_split + 1):
        for combo in combinations(candidates, way):
            total = sum(v["base_amount"] for v in combo)
            if abs(total - ls["total"]) <= 0.01:
                max_diff = max(
                    abs((ls["timestamp"] - v["timestamp"]).total_seconds())
                    for v in combo
                )
                return list(combo), max_diff
    return None, None


def reconcile(lightspeed_sales, valor_charges, time_tolerance_minutes=2, wide_window_minutes=30):
    """
    lightspeed_sales: list of dicts with at least {sale_id, employee_name, total, timestamp}
    valor_charges: list of dicts from valor_parser.parse_valor_csv (the 'sales' list)

    Returns a dict with:
        matched: list of match records. Each has "split": False for a normal
            single-charge match ({lightspeed, valor, time_diff_seconds,
            delayed, split}), or "split": True for a multi-card match
            ({lightspeed, valor_charges (list), time_diff_seconds, delayed,
            split}).
        likely_mismatch: list of {lightspeed, closest_valor, amount_diff, time_diff_seconds}
            -- Lightspeed sales with NO exact amount match anywhere nearby
               (single or split), but a plausible nearby Valor charge with a
               different amount
        missing_charge: list of lightspeed sales with NO nearby Valor charge at all
            -- employee likely forgot to charge the card
        unexplained_valor_charge: list of Valor charges with no matching or nearby Lightspeed sale
            -- possible duplicate charge or data entry issue
    """
    ls_pool = list(lightspeed_sales)
    valor_pool = list(valor_charges)

    matched = []
    tight_seconds = timedelta(minutes=time_tolerance_minutes).total_seconds()
    wide_seconds = timedelta(minutes=wide_window_minutes).total_seconds()

    # PASS 1: exact amount + tight time window (fast, confident matches)
    for ls in list(ls_pool):
        best_match, best_diff = _find_best_exact_amount_match(ls, valor_pool, tight_seconds)
        if best_match:
            matched.append({
                "lightspeed": ls,
                "valor": best_match,
                "time_diff_seconds": best_diff,
                "delayed": False,
                "split": False,
            })
            ls_pool.remove(ls)
            valor_pool.remove(best_match)

    # PASS 2: exact amount, but only found within the WIDER time window.
    # Still a real match - the amount is exactly right, it just took the
    # employee longer than usual to enter it. Not an error.
    for ls in list(ls_pool):
        best_match, best_diff = _find_best_exact_amount_match(ls, valor_pool, wide_seconds)
        if best_match:
            matched.append({
                "lightspeed": ls,
                "valor": best_match,
                "time_diff_seconds": best_diff,
                "delayed": True,
                "split": False,
            })
            ls_pool.remove(ls)
            valor_pool.remove(best_match)

    # PASS 3: split payments - two or three nearby charges together add up
    # exactly to the sale. Checked within the tight window since a split
    # tender happens in one checkout moment, not spread over minutes.
    for ls in list(ls_pool):
        combo, max_diff = _find_split_payment_match(ls, valor_pool, tight_seconds)
        if combo:
            matched.append({
                "lightspeed": ls,
                "valor_charges": combo,
                "time_diff_seconds": max_diff,
                "delayed": False,
                "split": True,
            })
            ls_pool.remove(ls)
            for v in combo:
                valor_pool.remove(v)

    # PASS 4: for what's genuinely left, find the closest-in-time leftover
    # regardless of amount - these are real mismatches (no exact-amount or
    # split-payment candidate existed anywhere within the wide window).
    likely_mismatch = []
    for ls in list(ls_pool):
        best_candidate = None
        best_diff = None
        for v in valor_pool:
            time_diff = abs((ls["timestamp"] - v["timestamp"]).total_seconds())
            if time_diff > wide_seconds:
                continue
            if best_diff is None or time_diff < best_diff:
                best_candidate = v
                best_diff = time_diff

        if best_candidate:
            likely_mismatch.append({
                "lightspeed": ls,
                "closest_valor": best_candidate,
                "amount_diff": round(best_candidate["base_amount"] - ls["total"], 2),
                "time_diff_seconds": best_diff,
            })
            ls_pool.remove(ls)
            valor_pool.remove(best_candidate)

    # Whatever's left in each pool is unexplained
    missing_charge = ls_pool
    unexplained_valor_charge = valor_pool

    return {
        "matched": matched,
        "likely_mismatch": likely_mismatch,
        "missing_charge": missing_charge,
        "unexplained_valor_charge": unexplained_valor_charge,
    }
