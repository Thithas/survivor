# SURVIVOR — principles

These are structural, not parameters. They came from losing about $30 across 147 live trades,
and every one of them is a rule about what *not* to do.

## The seven

1. **One bet per window.** Five coins moving together are one opinion, not five chances.
   Evidence: 2026-09-18 — solo cycles 7 wins / 7 trades, +$10.20. Multi-coin cycles 0 wins / 5, −$11.16.

2. **Trade with the market, never against it.** If the market prices a side at 0.15, it knows
   something the price feed doesn't. Buy only inside 0.40–0.85.
   Evidence: the biggest "edges" (0.69, 0.73) lost 100% every time.

3. **A loss must cost less than a win earns.** Exits are not optional. A stop that fails to
   execute is a bug to fix, not bad luck to accept.
   Evidence: two stop-loss orders were rejected for rounding up by 0.003 shares; both rode to −100%.

4. **Never bet what can't be lost twenty times.** Size follows the account, not the mood.
   Evidence: one $20 arb leg lost $15.93 — half the account on a single fill.

5. **No trade is a position.** Forcing action to feel busy pays fees for nothing.
   Evidence: forced trades won often but earned pennies; the fee was the whole game.

6. **Never hold half a hedge.** Both legs or none. A half-filled arb is a naked bet
   on the side the market didn't want. Full arbs +$5.47, half-fills −$19.89.

7. **Don't change the rules on small samples.** This is the one that cost the most.
   A 12-sample "83% win rate" looked like a pattern, was noise, and the change built on it
   lost $25 in nine hours.

## How this agent is allowed to change

- **It is not adaptive.** It does not retune itself. Adaptive means changing its mind on noise.
- **Parameters are frozen** between reviews. `params.json` is not edited to chase a result.
- **A review happens after 30+ closed trades**, never sooner, and never on a losing day's impulse.
- **A change requires evidence stated out loud first**: the split, the sample size, the expected effect.
  If it can't be written in one sentence with numbers, it isn't a finding.
- **A change that makes things worse over the next 30 trades is reverted**, not tuned further.

## The standing bet

Frozen 2026-09-18 at $51.56, 147 lifetime trades, −$21.02.
The next 30 trades run untouched on these rules. If they are net positive, the agent continues.
If they are not, the strategy is wrong — and the honest response is to stop, not to tune.
