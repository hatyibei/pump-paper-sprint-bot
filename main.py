#!/usr/bin/env python3
"""Entry for make.fun's "Pump.fun Paper Sprint: Any Bot Can Win".

Strategy (paper trading on the house's 6-hour replay):

- Watch every newly created coin, in first-seen order.
- Buy a coin once, and only once it shows real demand: at least
  MIN_TRADES trades and at least MIN_REAL_QUOTE lamports of real quote
  reserves, i.e. other traders have actually put money in.
- Stake a fixed slice of starting capital per coin, capped by the
  per-mint size cap, the open-position cap and free cash.
- Bank half a position once its spot value reaches 1.8x cost, exit in
  full at 3x cost or at 55% of cost (stop-loss), and rotate out of
  positions that sit nearly flat for MAX_FLAT_SLOTS slots. A coin that
  only proves itself long after creation is skipped: late entries buy
  the top.
- The replay flattens whatever is left at the end; this bot never
  reads the clock, the environment or an unseeded random, so its two
  graded runs agree byte for byte.

Run by the grader as ``python3 -P -s /bot/main.py`` with the SDK shim
vendored at vendor/makefun_bot.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

SOL = makefun_bot.LAMPORTS_PER_SOL

MIN_TRADES = 3
MIN_REAL_QUOTE = 2 * SOL
MAX_ENTRY_AGE_SLOTS = 2000
TRIM_NUM, TRIM_DEN = 9, 5  # sell half once value reaches 1.8x cost
TAKE_PROFIT_NUM, TAKE_PROFIT_DEN = 3, 1  # sell everything at 3x cost
STOP_LOSS_NUM, STOP_LOSS_DEN = 55, 100
MAX_FLAT_SLOTS = 1500
BUY_SLIPPAGE_BPS = 2000
SELL_SLIPPAGE_BPS = 2000

#: Mints created and not yet decided on, in first-seen order.
watching = []
watching_set = set()
#: Mints already bought once (never re-entered).
bought = set()
#: Slot each held mint was first seen in our account.
held_since = {}
#: Mints whose position has already been trimmed once (half sold).
trimmed = set()


def spot_value_lamports(state, mint):
    """Rough spot value of our holding: tokens * virtual quote / virtual token."""
    holding = state.positions.get(mint)
    curve = state.curves.get(mint)
    if holding is None or curve is None or curve.virtual_token_reserves <= 0:
        return None
    return holding.token_amount * curve.virtual_quote_reserves // curve.virtual_token_reserves


def on_batch(state, batch):
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    free = state.free_cash_lamports()
    slots = caps["max_open_positions"] - state.committed_position_count()
    orders = []

    for e in batch["events"]:
        if e["event"] == "CreateEvent" and e["mint"] not in watching_set:
            watching.append(e["mint"])
            watching_set.add(e["mint"])

    # Exits first: they free cash and position slots for new entries.
    for mint in list(state.positions):
        holding = state.positions[mint]
        held_since.setdefault(mint, state.slot)
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        value = spot_value_lamports(state, mint)
        if value is None or holding.cost_lamports <= 0:
            continue
        age = state.slot - held_since[mint]
        sell_amount = 0
        if (
            value * TAKE_PROFIT_DEN >= holding.cost_lamports * TAKE_PROFIT_NUM
            or value * STOP_LOSS_DEN <= holding.cost_lamports * STOP_LOSS_NUM
            or (age > MAX_FLAT_SLOTS and value * 10 < holding.cost_lamports * 11)
        ):
            sell_amount = holding.token_amount  # full exit
        elif (
            mint not in trimmed
            and value * TRIM_DEN >= holding.cost_lamports * TRIM_NUM
        ):
            sell_amount = holding.token_amount // 2  # bank half, let the rest run
            trimmed.add(mint)
        if sell_amount > 0:
            if len(orders) >= caps["max_orders_per_batch"]:
                break
            orders.append(
                {
                    "side": "sell",
                    "mint": mint,
                    "token_amount": sell_amount,
                    "max_slippage_bps": SELL_SLIPPAGE_BPS,
                }
            )
            free += value * sell_amount // holding.token_amount

    # Entries: one shot per coin, only after real demand shows up.
    stake = int(state.hello["capital"]["lamports"]) // 200
    decided = []
    for mint in watching:
        if len(orders) >= caps["max_orders_per_batch"]:
            break
        if mint in bought:
            decided.append(mint)
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            decided.append(mint)
            continue
        if (
            curve.created_slot is not None
            and state.slot - curve.created_slot > MAX_ENTRY_AGE_SLOTS
        ):
            decided.append(mint)  # proven too late; late entries buy the top
            continue
        if curve.trades < MIN_TRADES or curve.real_quote_reserves < MIN_REAL_QUOTE:
            continue  # not proven yet; keep watching
        decided.append(mint)
        cap = state.max_buy_lamports(mint)
        amount = min(cap, stake)
        if amount <= 0 or slots <= 0 or free < amount + tx:
            continue
        orders.append(
            {
                "side": "buy",
                "mint": mint,
                "quote_lamports": amount,
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            }
        )
        bought.add(mint)
        free -= amount + tx
        slots -= 1
    for mint in decided:
        watching_set.discard(mint)
    watching[:] = [m for m in watching if m in watching_set]

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
