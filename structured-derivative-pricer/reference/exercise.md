# Early termination blocks: Longstaff-Schwartz, autocall, summarizer

`pricer/exercise.py` works on simulated cashflow tables and does not depend on the model. It is a developer
building block. Trade specs cannot use it yet.

## Table layout

Every table is **periods × paths**: rows are observation / payment dates, columns are paths. This is the layout of
`LegResult.amounts`. A table is a `(T, N)` ndarray, or a DataFrame whose index holds the dates. If the input has a
date index, the output keeps it. A quantity that is the same on every path can be given as `(T,)` or `(T, 1)`, and
it is broadcast.

| input | meaning |
|---|---|
| `cashflows` | regular cashflow paid on period t while the trade is alive (coupons, the maturity redemption) |
| `forward_df` | forward-start discount factor D(t−1, t). The first row is D(0, t₀). Path-wise under stochastic rates. `forward_discount_factors(cum)` converts cumulative D(0, t) to this form (e.g. from `Scenario.discount`) |
| `exercise_payoff` | LSM: amount received on exercise at t. NaN marks a date or path where exercise is not allowed |
| `observations` | LSM regression state: `(T, N)`, `(T, N, K)`, or a list / dict of `(T, N)` tables |
| `autocall_mask` | autocall: the condition holds on t (only the first True per path counts) |
| `autocall_cashflows` | autocall: amount paid on the autocall date (e.g. par + call coupon) |

When a path terminates on t, it receives the termination amount on t, and every later cashflow is cancelled.
`settle_on_event_date=True` (the default) also pays the regular cashflow of t.

## Longstaff-Schwartz

```python
from pricer.exercise import longstaff_schwartz, summarize
res = longstaff_schwartz(coupons, call_price, fdf, short_rate, exerciser="issuer")   # callable bond
res = longstaff_schwartz(0 * S, np.maximum(K - S, 0), fdf, S, itm_only=True)        # Bermudan put
```

How it works:
- **Backward induction.** On each exercisable date, the realised continuation value (in t-money) is regressed on a
  polynomial basis of the standardised state. The basis is degree `degree`, with pairwise cross terms when there
  are several factors.
- **Who exercises.** With `exerciser="holder"`, the holder exercises when the payoff is above the fitted
  continuation. With `"issuer"`, the issuer exercises when the payoff is below it.
- **Optional settings.** `itm_only` restricts the regression and exercise to paths where the payoff is positive.
  `exercisable` (T,) switches exercise dates off.
- **Out-of-sample pricing.** `res.diagnostics["policy"]` holds the fitted regressions. Pass it as `policy=` on an
  independent set of paths to get a low-biased out-of-sample price. The in-sample price is slightly high-biased.

Outputs (a `TerminationResult`):

| field | meaning |
|---|---|
| `event` | `(T, N)` exercise mask, at most one True per path |
| `survival` | `(T, N)` alive on t (no exercise before t) |
| `cashflows` | actual cashflows, undiscounted |
| `discount` | cumulative D(0, t) |
| `discounted` | discounted actual cashflows |
| `event_probability` / `survival_probability` | per period |
| `diagnostics["exercise_probability"]` | per period |

## Autocall

```python
ac = autocall(coupons, S >= barrier, par + call_coupon, fdf)
```

`autocall` returns the same `TerminationResult`. `event` is the triggered autocall mask, and
`diagnostics["autocall_probability"]` gives the per-period probability.

## Summarizer

```python
s = summarize(res, funding_leg_discounted, antithetic=True, confidence=0.95)
s.price, s.std_error, s.confidence_interval
s.expected_cashflows   # per period: horizontal mean over paths (pv; for results also expected_amount,
                       #   discount_factor, survival_probability, event_probability)
s.fair_values          # per path: vertical sum over periods = fair value distribution
s.to_dict()            # JSON-ready, with fair value quantiles
```

Arguments:
- **Inputs.** Pass any mix of `TerminationResult`s and raw tables of *discounted* cashflows. They must share a path
  count, and they are added together.
- **`antithetic`.** Use it when paths come in (2k, 2k+1) antithetic pairs. The standard error is then computed from
  the pair averages, as the engine does.

## Tests

`tests/test_exercise.py` covers:
- **Bermudan put.** The LSM price is checked against QuantLib's finite-difference Bermudan engine, both in sample
  and with an out-of-sample policy.
- **European limit.** With a single exercise date, the price matches the Black-Scholes European price.
- **Deterministic callable bond.** The price is exact.
- **Vasicek callable bond.** Bounds are checked.
- **Autocall and summarizer.** These are checked against hand-computed tables.
