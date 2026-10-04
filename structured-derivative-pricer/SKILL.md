---
name: structured-derivative-pricer
description: Price exotic and structured derivatives (barrier options, EKIAKO, autocallables, equity-linked notes, fixed/floating/SOFR-compounded legs, inverse floaters, range accruals on equities, rates, CMS spreads or baskets, TARNs, kick-in coupons) by writing a declarative YAML trade spec that a pre-built QuantLib engine validates and prices by Monte Carlo, or in closed form when QuantLib has one. Use when the user gives a term sheet or product description and asks for a fair value, NPV, MTM, or pricing model, including "price this note", "value this barrier option", "build a pricing model for...", "結構型商品評價", "公平價值". You write the spec, never pricing code.
---

# Structured Derivative Pricer

A factory-style pricing engine. **You write a YAML spec. You never write pricing code.**
The engine turns the spec into a graph of pre-tested components (market data → model →
fixing paths → cashflow paths + discount factors → price), checks the whole spec before
running, and picks the pricing method.

```
curve ─▶ discount ─▶ underlying ─▶ model ─▶ scenario ─▶ observables ─▶ legs ─▶ masks ─▶ price
                                                   └────────▶ discount_factors ─────────┘
```

Products are assembled from building blocks: **observables** (equity, term/swap/overnight rates, linear
combinations, worst-of), **legs** (fixed, floating, range accrual, option, fixed amount) and **masks**
(kick-in / knock-out conditions on a leg, knock-out termination and target (TARN) events on the trade).

## Setup (once per machine)

```bash
PY=$(<skill_dir>/scripts/setup.sh)      # creates <skill_dir>/.venv with QuantLib, prints its python
```

After that, run every command below as `$PY <skill_dir>/scripts/price.py ...`.

## Workflow

1. **Read the term sheet.** List every economic feature: underlying(s), dates, strike, notional,
   coupons, barriers (level, direction, *observation frequency*), knock-in/out effects,
   rebates, early termination, who pays what. Resolve ambiguities with the user **before** writing
   anything. Two ambiguities matter most: whether a barrier is observed at expiry only or daily, and
   whether a level is a % of the initial fixing or an absolute price. For rate legs also pin down: fixing in
   advance or compounded in arrears, fixing lag / lockout, day counts, and the calendar of daily observations.
2. **Map features to components** with [reference/components.md](reference/components.md), which
   covers the mapping table, the sign conventions, and worked product recipes. For exact field
   names, defaults and allowed values, run `price.py catalog [kind]`. The catalog is generated
   from the code and is always current.
3. **Market data.** Ask the user for curves, spot, dividends and vols, or look for files in the working
   directory. The format is in [reference/market_data.md](reference/market_data.md). Convert what they
   give (CSV, pasted tables) into that YAML. **Never invent market data silently.** For a demo or test,
   `scripts/make_mock_market.py OUT.yaml --as-of YYYY-MM-DD` writes clearly-labelled mock data. Say
   in your answer that the price uses mock data.
4. **Write the spec** in the working directory (e.g. `trade_<name>.yaml`). Start from the closest
   file in `examples/`.
5. **Validate:** `price.py validate trade.yaml`. It returns every error at once, each with its
   input path, message and hint (often "Did you mean ..."). Fix them all, then validate again.
   Exit code 2 means invalid.
6. **Plan:** `price.py plan trade.yaml`. Check `method` and `method_reason`, which say analytic vs MC and
   why. Check that `graph` and `simulation.fixing_dates` look right. A daily barrier over 1Y should
   show about 250 fixing dates. If it shows 1, you observed it at expiry only.
7. **Price:** `price.py price trade.yaml --out result.json`. Use `--set pricing.num_paths=100000`
   to tighten the error without editing the file.
8. **Check, then report.** See "Reading the result" below.

## Hard rules

- Do **not** write Python to price, simulate, or post-process a trade. Do not edit `pricer/`.
  If a feature cannot be expressed with the catalog, **stop and tell the user**: name the missing
  component, for example "a range-accrual coupon leg" or "Bermudan issuer call". Custom components
  are a planned extension point (see "Extending"). They are not a workaround to improvise.
- Do not approximate a feature you cannot express. For example, do not drop a coupon's knock-out
  condition because it is inconvenient. Say that it is unsupported.
- Keep `pricing.seed` fixed across runs you compare. Prices are exactly reproducible per seed.
- `continuous` barrier observation is priced only by closed forms. Most term sheets say
  "daily closing price". Use a `1D` generated schedule for those.
- Optionality on a rate (cap, floor, inverse floater, rate range, rate barrier) needs `model: hull_white`.
  The validator rejects it under the equity models, whose rates are deterministic. Do not remove a floor
  to get past that without telling the user.
- Conventions (calendars, day counts, schedules, index definitions) come from QuantLib. Name the index with
  `quantlib_index` (Sofr, Euribor, EuriborSwapIsdaFixA, ...) when QuantLib has it. Give custom fields only for
  indices QuantLib lacks, and take them from the term sheet.
- Past fixings (e.g. the first in-advance fixing at inception) go in the observable's `history`. Ask the
  user for them; never invent them.

## Reading the result

| field | what to check |
|---|---|
| `method`, `method_reason` | analytic (closed form, exact) or mc, and why no closed form matched |
| `price`, `std_error`, `confidence_interval` | if `std_error` is too large relative to price, re-run with more paths |
| `legs` | PV per leg. The signs follow `position`, from the holder's view |
| `leg_diagnostics`, `events` | knock-in / knock-out / autocall probabilities. Sanity-check them against intuition |
| `expected_cashflows` | expected amount, DF and survival probability per payment date |
| `cross_check` | when MC ran and a closed form also applies, `agrees` must be true |
| `martingale_check` | `ok` must be true. It tests the simulated forward against the curve forward |
| `warnings`, `notes` | report every warning to the user, e.g. vol extrapolation or local-vol flooring |

Report the price with its CI, the method, the key probabilities, any warnings, and the model
assumptions (model type, and whether the data is mock). For model risk on path-dependent
products, it can help to price under both `black_scholes` (local vol from the surface) and
`heston`, and show the gap.

## Coverage

Models:
- `black_scholes`: one or several correlated equity underlyings (constant / term / Dupire local vol).
- `heston`: single asset, given or calibrated parameters.
- `hull_white`: 1F rates, given `mean_reversion` and `volatility`.

Observables:
- equity level or performance,
- term rate, swap rate (CMS), overnight rate,
- `linear` (spreads, baskets, inverse-floater formulas), `min_of` / `max_of`.

Legs:
- `fixed_leg`,
- per-period values on every coupon leg (step-up rates, variable ranges, per-period caps / floors / spreads,
  amortising coupon notional), which also cover hybrid coupons such as fixed then range accrual,
- `floating_leg` (in advance, in arrears, or compounded in arrears with lockout; gearing, spread, cap, floor),
- `range_accrual` (fixed or floating coupon; any number of ranges on any observables),
- `option_payoff` (on an underlying or an observable),
- `fixed_amount`.

Masks:
- `knock_in` (kick-in) and `knock_out` conditions on any leg,
- trade-level `knock_out` (termination / autocall) and `target` (TARN) with rebate / guarantee.

Closed forms: European vanilla (BSM, Heston) and single continuous barrier (BSM).

Not yet supported, so say so rather than approximating:
- callable bonds and cancellable swaps in a trade spec (the Longstaff-Schwartz building block exists in
  `pricer/exercise.py`, but no YAML component uses it yet),
- Hull-White calibration to swaptions,
- equity plus stochastic-rate hybrids,
- FX / quanto / multi-currency,
- snowball / memory coupons,
- digital coupons other than range accrual,
- historical barrier fixings for seasoned trades (past barrier observation dates are ignored with a warning;
  past rate / range fixings are supported through `history`),
- custom payoff code (planned: a bridge for AI-written payoff components).

## Extending (for developers, not during a pricing task)

Each component is a class registered with `@register(kind, type)` and a pydantic `Spec`
(`pricer/registry.py`). A new observable, leg, condition, event, model or analytic pricer needs one class.
Legs and observables read the market only through `models.Scenario` (equity paths, `discount`, `bond`,
`growth`), so they work under every model. Validation,
the catalog and the graph pick it up automatically. Add a regression test in `tests/` against a
closed form or a limiting case. Run the suite with `$PY -m pytest <skill_dir>/tests -q`.

`pricer/exercise.py` holds model-agnostic early-termination blocks that work on (periods x paths) cashflow
tables, for wiring Bermudan / callable / autocall features into components (see
[reference/exercise.md](reference/exercise.md)):
- `longstaff_schwartz`: optimal holder or issuer exercise by least-squares regression,
- `autocall`: termination on the first period an autocall mask holds,
- `summarize`: expected cashflows per period, fair value per path, price with confidence interval.
