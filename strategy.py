import math


def decide(start_price, current_price):
    if start_price is None or current_price is None:
        return None
    # Equality is not evidence for either outcome.  The old >= comparison
    # turned an unchanged opening/current price into an UP vote, which is
    # especially dangerous when phase 2 is allowed to sample at the boundary.
    # Keep this guard deliberately narrow: non-numeric/non-finite inputs retain
    # the comparison behavior that callers had before this fix.
    if start_price == current_price:
        try:
            if math.isfinite(start_price) and math.isfinite(current_price):
                return None
        except TypeError:
            pass
    return "UP" if current_price >= start_price else "DOWN"


def final_decision(price_side, book_side, chainlink_side):
    if price_side and book_side and price_side == book_side:
        return price_side
    if chainlink_side and chainlink_side in (price_side, book_side):
        return chainlink_side
    return price_side or book_side or chainlink_side
