# Components: semantics, conventions, recipes

Exact fields, defaults and enums: `price.py catalog [curve|vol|schedule|model|observable|leg|condition|event|analytic]`.
This file explains what the components *mean* and how to combine them.

## Document layout

```yaml
valuation_date: 2026-09-28          # pricing date (market data as_of should match)
market_data: market.yaml            # path relative to this file, or the market document inline
fixings: fixings.yaml               # optional past fixings shared by trades (path or inline); see below
model: {type: ..., ...}             # one model; it names the underlying(s) and discount curve
observables: {name: {type: ...}}    # named quantities read by legs, ranges, barriers (rates, spreads, worst-of)
schedules: {name: {type: ...}, ...} # named date lists used by legs / conditions / events
trade:
  id: ...
  currency: USD
  effective_date: ...               # optional; default valuation_date (accrual start)
  maturity: 2027-09-28              # ISO date or tenor from effective_date ("1Y")
  calendar: WeekendsOnly            # adjusts dates given as tenors (default WeekendsOnly)
  business_day_convention: ModifiedFollowing
  reference_levels: {SPX: 5012.3}   # optional initial fixings; default = the close on effective_date, else spot
  history: {SPX: {2026-07-28: 5012.3, ...}}   # optional inline past closes; win over the fixings file
  legs: [...]
  termination_events: [...]
pricing: {method: auto, num_paths: 20000, seed: 42, antithetic: true, ...}
```

Anywhere a date is expected you may write an ISO date, `valuation_date`, `effective_date`,
`maturity`, or a tenor such as `6M` (relative to effective_date, or to a schedule's start). Tenor dates are
business-day adjusted with the trade's calendar. Generated schedules roll from the unadjusted date, as
QuantLib's `Schedule` does, so `maturity: 5Y` on a Sunday creates no stub.

## Market conventions come from QuantLib

The engine never computes a date or a day count itself. Each convention comes from a QuantLib object:

| convention | QuantLib object |
|---|---|
| calendars, business days, `advance`, adjustments | `ql.Calendar` (`businessDayList` for daily observation) |
| schedule generation (rule, EOM, stubs) | `ql.Schedule` |
| coupon accrual fractions, including ActualActual.Bond stubs | `ql.FixedRateLeg` on that schedule |
| term rate value date, maturity, EOM, day count | `ql.IborIndex` (`valueDate`, `maturityDate`, `dayCounter`) |
| swap-rate underlying swap | `ql.SwapIndex` conventions + backward `ql.Schedule` |
| overnight compounding days and fractions | `ql.OvernightIndexedCoupon` (`valueDates`, `dt`) |
| default fixing lag and calendar of a floating leg | the index's `fixingDays` / `fixingCalendar` |

- **Prefer a QuantLib index by name** over hand-typed conventions: `quantlib_index`.
  - `term_rate`: Euribor, USDLibor, GBPLibor, JPYLibor, Tibor, Bibor, Jibar, Shibor, Bbsw, Pribor, Robor,
    Wibor, Zibor, THBFIX.
  - `overnight_rate`: Sofr, Estr, Sonia, Saron, Tonar, Corra, Aonia, Eonia, FedFunds.
  - `swap_rate`: EuriborSwapIsdaFixA/B, EuriborSwapIfrFix, UsdLiborSwapIsdaFixAm/Pm,
    JpyLiborSwapIsdaFixAm, GbpLiborSwapIsdaFix, ChfLiborSwapIsdaFix.
- When an index is not in QuantLib (e.g. TAIFX), give the custom fields. They build a `ql.IborIndex` /
  `ql.SwapIndex` / `ql.OvernightIndex`, so QuantLib still does the date logic.
- Tests check every rate observable against QuantLib's own `index.fixing()` forecast.

## The building blocks

```
observables ──▶ legs (cashflow per payment date and path) ──▶ masks ──▶ discount ──▶ price
   equity / term_rate / swap_rate / overnight_rate              leg conditions: knock_in (kick-in), knock_out
   linear (spread, basket, K - m·L) / min_of / max_of           trade events:  knock_out (termination), target (TARN)
```

Every product is a combination of these blocks. You never write a payoff formula.

### Observables

| type | value on fixing date t | notes |
|---|---|---|
| `equity` | `level` S_t, or `performance` S_t / S_ref | every model underlying is also usable **by name** as its level |
| `term_rate` | simple forward (P(t,s)/P(t,e) − 1)/τ, s = t + spot_lag, e = s + tenor | IBOR-style index (3M TAIFX, EURIBOR 6M, term SOFR) |
| `swap_rate` | par swap rate (P(t,T0) − P(t,Tn)) / Σ τ_i P(t,Ti) | CMS; single curve |
| `overnight_rate` | 1-business-day simple rate | SOFR / ESTR / TONA; the index of compounded legs |
| `linear` | constant + Σ weight·observable | spread `[1, -1]`, basket, inverse floater `K − m·L` |
| `min_of` / `max_of` | worst-of / best-of | use `performance` observables |

- Rate observables name a market `curve`. Under `hull_white` they are stochastic, and other curves keep a
  deterministic basis to the discount curve. Under the equity models they are deterministic forwards.
- **Nonlinear features on a deterministic observable are rejected.** A cap, floor, range, barrier or option on a
  rate under `black_scholes` would get only intrinsic value. Linear use is fine, e.g. a plain FRN, or a
  SOFR-paying equity range accrual.
- **Past fixings.** A fixing date before the valuation date (for example the first in-advance fixing at
  inception, or a seasoned trade) is read from history, never simulated. Validation lists the missing dates.
  - The **fixings file** (`fixings:` at the top of the document) holds `equities: {SPX: {YYYY-MM-DD: level}}`
    and `rates: {NAME: {YYYY-MM-DD: value}}`. It is meant to be shared by trades and appended each day; a run
    ignores its dates on or after the valuation date. A rate observable reads `rates.<fixings_key>`, by
    default its own name; set `fixings_key` to use a market-wide name (e.g. `USD-SOFR`).
  - Inline history wins date by date: a rate observable's `history: {YYYY-MM-DD: value}`, and
    `trade.history: {SPX: {YYYY-MM-DD: level}}` for an underlying.
  - Equity history is closing **levels**, shared by every observable, barrier and range on that underlying,
    also for a `performance` observable (which divides them by the reference level). An equity observable's own
    `history` overrides it date by date.
  - Composites (`linear`, `min_of`, `max_of`) read the history of their leaves.

### Legs

| type | cashflow |
|---|---|
| `fixed_leg` | notional · rate · τ per period |
| `floating_leg` | notional · clip(gearing·X + spread, floor, cap) · τ |
| `range_accrual` | notional · rate · τ · (weighted days on which **every** `range` condition holds / weighted days) |
| `option_payoff` | notional · max(φ·(X_T − K), 0) on an underlying (relative/absolute strike) or an observable (absolute) |
| `fixed_amount` | a known amount on one date |

**Periods** (fixed, floating, range accrual) follow these rules:
- The schedule lists accrual **end** dates. The first period starts at `accrual_start` (default effective_date).
- Payment = end + `payment_lag` business days.
- Periods paid on or before the valuation date are dropped.

**Per-period values.** These fields each take a single number, a per-period list, or date steps:
- `notional`,
- the fixed `rate`,
- floating `gearing`, `spread`, `cap`, `floor`,
- each range's `lower` / `upper`.

```yaml
rate: 0.05                                   # the same in every period
upper: [0.045, 0.045, 0.05, 0.05]            # one per schedule period, paid periods included
upper: {effective_date: 0.045, 4Y: 0.05}     # steps keyed by period start (a period takes the last key before its midpoint)
cap: {effective_date: null, 2Y: 0.06}        # null = no cap / floor / bound in those periods
```

- A step-up coupon, a variable range per period, a cap that starts in year 3, and amortising coupon
  notionals are all data, not new components. Principal amortisation itself is `fixed_amount` legs.
- **Hybrid coupons**, e.g. fixed in year 1 then range accrual, can be written two ways:
  - one `range_accrual` leg whose bounds are `null` in year 1: a period with no bound accrues every day, so it
    pays the fixed rate;
  - two legs on split schedules: `{type: generated, end: 1Y}` for the first leg, and `{start: 1Y}` plus
    `accrual_start: 1Y` for the second.
- A `target` event may list both legs of a hybrid.

**Floating fixing** (`fixing`). `fixing_lag` and `fixing_calendar` default to the index's own, as in QuantLib's
`IborLeg`. For an index built from several observables (`linear`), give them explicitly.
- `in_advance`: X is fixed `fixing_lag` business days before the accrual start (IBOR style).
- `in_arrears`: X is fixed `fixing_lag` business days before the accrual end.
- `compounded_in_arrears`:
  - Computes (Π(1 + r_i d_i) − 1)/τ over the overnight index's business days in [start, end).
  - `lockout_days: n` means the last n daily rates equal the rate of the business day before them (rate cut-off).
  - Checked against QuantLib's `OvernightIndexedCoupon`. Spread is added after compounding.

**Range accrual**:
- The coupon is a fixed `rate` or a floating `index` (same fields as floating_leg).
- Observation days are the business days of `observation_calendar` in [start, end).
- `day_basis: calendar` makes every calendar day count, carrying the previous business day's value.
- `range_cutoff_days: n` freezes the last n observations at the one before them.
- Each `range` entry is `{observable, lower, upper, lower_inclusive, upper_inclusive}`, with one or both bounds.
- Several entries mean all must hold. That covers "all underlyings in range".

### Masks

| mask | where | effect |
|---|---|---|
| `knock_in` condition | on any leg | **kick-in**: a cashflow is paid only if the barrier was hit on or before its cut-off |
| `knock_out` condition | on any leg | the cashflow is paid only if the barrier was **not** hit on or before its cut-off |
| `knock_out` event | trade | **termination** on the first hit: later cashflows cancelled, optional rebate |
| `target` event | trade | **TARN**: terminates on the payment date where accumulated coupons of `legs` reach `target` |
| `bermudan_call` event | trade | **early exercise** (callable, puttable, cancellable, Bermudan option): one party may end the trade on each `observation` date, decided by Longstaff-Schwartz |

- The **cut-off** of a cashflow is:
  - an option's expiry,
  - a coupon period's accrual **end**,
  - a fixed amount's payment date.
- Barriers read an `underlying` (`barrier_mode` relative, the default, or absolute) or any `observable` (absolute,
  in its units; e.g. `min_of` of performances for a worst-of barrier).
- Termination: a cashflow **on** the event date is paid only if `settle_on_event_date: true` (autocall style).
  A `target` event always settles its date.
- `target` has these fields:
  - `crossing_coupon`: `capped` (pays exactly up to the target), `full`, or `none`.
  - `rebate`: the early-redemption amount, paid on the termination date if it is **before** maturity. At maturity
    the trade's own principal leg pays.
  - `guaranteed: true`: if the target is never reached, the shortfall is paid at maturity.
- `target` is a cash amount. Write `0.12 * notional` as a number (e.g. `120000`).

### Bermudan exercise (`bermudan_call`)

Use it for an optional early termination. The exercise is a choice someone makes, not a barrier.

| field | meaning |
|---|---|
| `observation` | schedule of exercise dates. Use the dates after notice, and leave out the date the principal leg pays (usually maturity) |
| `exerciser` | `issuer`: the party paying the holder may call or cancel. It exercises when that is **cheaper** than continuing (callable bond, cancellable note, swap cancellable by the counterparty). `holder`: the holder may put. It exercises when that is **worth more** than continuing (puttable bond, Bermudan option) |
| `exercise_price` | **cash** the holder receives on exercise: `1000000` for a call at par on a 1,000,000 note, `0` for a free cancellation. Takes a number, one value per schedule date, or `{date: value}` steps (last key on or before the date). `null` means no exercise on that date |
| `exercise_option` | adds an option's intrinsic value on exercise, `{underlying \| observable, option_type, strike, strike_mode, notional}` with the same units as `option_payoff`. A Bermudan option is `exercise_price: 0` + `exercise_option` |
| `regressors` | observables or underlyings that drive the decision. Default: every model underlying, plus the short rate under `hull_white`. Name a swap rate for a cancellable swap, or the worst-of performance for a multi-asset note |
| `degree` | polynomial degree of the regression (default 2) |
| `settle_on_event_date` | the cashflows paid on the exercise date are still paid (default `true`: the coupon is paid, then the bond is called) |

- On exercise, every later cashflow is cancelled, including those of other legs and other events' rebates.
- Another termination event (autocall, TARN) that ends the trade first blocks exercise on that path. On the same
  date, the other event wins.
- **Model.** A call on a rate product needs `hull_white`, because the decision depends on rates. On an equity
  model, rates are deterministic and only the equity drives the decision.
- **One** `bermudan_call` per trade.
- **The exercise rule.** It is fitted on `pricing.exercise_fit_paths` independent paths (default `num_paths`),
  then applied to the pricing paths. The rule is slightly sub-optimal, so the price leans **against** the
  exerciser:
  - a holder-exercised option is a lower bound,
  - a bond the issuer can call is valued slightly high for the holder.
  `events.<id>.policy.in_sample_price` refits on the pricing paths. It errs in the other direction. A gap of more
  than a few standard errors between the two means the regressors miss what drives the decision.
- **Path counts.** Pricing costs about twice the simulation time. `exercise_fit_paths: 0` fits in sample, which
  is faster but biased toward the exerciser.

### Seasoned trades (priced after inception)

Set `effective_date` to the trade date and keep the original schedules. Nothing else is specific to a
seasoned trade, because past fixings are handled in one place for every payoff. The same spec prices a new
deal on its trade date and the live position at any later date: change `valuation_date` and the market data,
and keep appending the fixings file.

- **One read path.** Every component reads the market on a date through its observables (a model underlying is
  the observable of its own name). A date before the valuation date returns the known fixing from history; a
  later one returns the simulated value. A fixing **on** the valuation date is simulated from today's spot /
  curve. Composites (`linear`, `min_of`, worst-of performances) combine their leaves' history the same way.
- **One check.** Every component declares the dates it reads. Validation collects the past ones for the whole
  trade and lists, per input path, each date without a fixing. `plan` shows `past_fixings_from_history`.
- So any coupon, range, barrier, index formula or option payoff mixes known and simulated fixings without its
  own code. That includes a range accrual or a compounded rate in the middle of a period, a barrier already
  touched, and an option whose expiry has passed but which is not yet paid.
- Closed forms start from today's spot, so a trade that reads a past fixing is priced by Monte Carlo.

What is not a fixing, and so stays specific:

| feature | before the valuation date |
|---|---|
| cashflows | paid on or before the valuation date: dropped. The current coupon period keeps its full accrual |
| `knock_out` event (autocall, KO) | a hit on a past date (read from history) is an error: the trade has terminated |
| `target` (TARN) | coupons already paid are recomputed by their legs from past fixings (so history must reach back to the first one); a reached target is an error (terminated). `accumulated: <cash>` overrides the computation |
| `bermudan_call` | only exercise dates after the valuation date (a past date was a decision, not a fixing) |
| relative strikes / barriers / performance | `reference_levels`, else the close on `effective_date` from history |
| `observation: continuous` | only monitors forward from today (warning): confirm with the user the barrier was not hit |

## Conventions (read these before writing a spec)

- **Perspective.** The trade is priced from the holder's view. `position: long` means the holder
  receives the cashflow, and `short` means it pays. An investor who sells a put inside a note has
  `position: short` on that leg. A swap is one long and one short leg.
- **Relative vs absolute levels.** On an `underlying`, `strike_mode` / `barrier_mode: relative` (the default)
  means a fraction of the reference level: `1.0` is 100% of the initial fixing. On an `observable`, levels are
  absolute in the observable's units. For % of initial, use an `equity` observable with `mode: performance`.
- **Option payoff units.** With a relative strike, payoff = `notional × max(φ·(S_T/S_ref − k), 0)`,
  a percentage-of-notional payoff. With an absolute strike, payoff = `notional × max(φ·(X_T − K), 0)`,
  where `notional` is the number of units.
- **Barrier touch.** `up` means breached when value ≥ B. `down` means breached when value ≤ B.
- **European vs American is the observation schedule.**
  - European / at-expiry barrier: a `dates` schedule with `[maturity]` (one date).
  - American on daily closes: a `generated` schedule with `frequency: 1D` and the right calendar.
  - Discrete (e.g. quarterly autocall dates): a `generated` schedule with `frequency: 3M`.
  - `observation: continuous` is for closed forms only (single_barrier).
- **Rebates are cash amounts**, not percentages. An autocall's par redemption is a `rebate` equal to the
  notional, paid at hit. **Do not observe the autocall on the date the principal leg pays** (usually
  maturity), or par is paid twice. Validation warns about this.
- Schedules exclude their start date unless `include_start: true`.
- Rates and levels are decimals: 4.25% is `0.0425`.

## Term sheet → component mapping

| Term-sheet language | Component |
|---|---|
| "Put/call on X, strike K% of initial" | leg `option_payoff`, `strike: K/100` |
| "worst-of put on A and B" | `min_of` of `performance` observables; `option_payoff` with `observable`, `strike: 1.0` |
| "fixed coupon c% p.a. paid quarterly, 30/360" | leg `fixed_leg`, `rate: c/100`, quarterly schedule |
| "3M TAIFX/EURIBOR + s, fixed 2 days before the period" | `term_rate` observable; `floating_leg` `in_advance`, `fixing_lag: 2`, `spread: s` |
| "SOFR compounded in arrears + s, 2-day lockout" | `overnight_rate`; `floating_leg` `compounded_in_arrears`, `lockout_days: 2` |
| "coupon = max(K − m × LIBOR, 0)" (inverse floater) | `floating_leg` `gearing: -m`, `spread: K`, `floor: 0` (needs `hull_white`) |
| "coupon = 5% − 0.8 × CMS5Y, cap 4%, floor 2%, fixed in arrears" | `swap_rate` CMS5Y; `floating_leg` `gearing: -0.8`, `spread: 0.05`, `cap: 0.04`, `floor: 0.02`, `fixing: in_arrears` (or a `linear` index with gearing 1) |
| "coupon = a + b × X + c × Y, collared" | `linear` observable `{constant: a, terms: [X: b, Y: c]}` as the `index` + `cap` / `floor` |
| "year 1 fixed c%, then range accrual" | `range_accrual` with bounds `{effective_date: null, 1Y: ...}`, or `fixed_leg` + `range_accrual` on split schedules |
| "coupon steps up 3% → 4% → 5%" / "range widens each year" | per-period values: a list or `{date: value}` steps |
| "notional amortises 10% a year" (coupons) | `notional` as a list or steps; principal repayments as `fixed_amount` legs |
| "capped / floored floater" | `floating_leg` with `cap` / `floor` (needs `hull_white` for a rate index) |
| "CMS10Y − CMS2Y" | two `swap_rate` observables + `linear` with weights `[1, -1]` |
| "accrues daily while index in [L, U]" | leg `range_accrual` with one `range` entry |
| "accrues only when A and B are both in range" | `range_accrual` with two `range` entries |
| "accrues while spread / basket in range" | `range_accrual` on a `linear` observable |
| "knock-in if closing price ≤ B% on the final valuation date" | condition `knock_in`, `down`, schedule `[maturity]` |
| "coupons start after the index first closes ≥ B" (kick-in) | condition `knock_in` on the coupon leg |
| "coupons stop once the index closes ≤ B" | condition `knock_out` on the coupon leg |
| "the product terminates early if ≥ B% on any observation date" | event `knock_out` on that schedule |
| "autocall: early redemption at par + coupon on observation date" | event `knock_out` + `rebate: notional`, `settle_on_event_date: true` |
| "early redemption once total coupons reach T% (TARN)" | event `target`, `target: T% × notional`, `rebate: notional` |
| "callable at par by the issuer on any coupon date after year 2" | event `bermudan_call`, `exerciser: issuer`, `exercise_price: notional`, schedule of call dates (needs `hull_white` for a rate product) |
| "callable at 102 in year 3, 101 in year 4, par thereafter" | `bermudan_call` with `exercise_price` steps `{3Y: 1020000, 4Y: 1010000, 5Y: 1000000}` (cash) |
| "puttable at par by the investor annually" | event `bermudan_call`, `exerciser: holder`, `exercise_price: notional` |
| "swap cancellable by the counterparty / by us, no fee" | both legs + `bermudan_call` with `exercise_price: 0`, `exerciser: issuer` (they cancel) or `holder` (we cancel); `regressors: [<swap_rate>]` |
| "Bermudan put, exercisable monthly" | `option_payoff` put at expiry + `bermudan_call` `exerciser: holder`, `exercise_price: 0`, `exercise_option` = the same put, on the dates before expiry |
| "principal repaid at maturity" | leg `fixed_amount`, `amount: notional` |
| "fixing 2 business days before payment" | schedule `offset` with `base: <payment schedule>`, `offset: -2D` |

## Recipes (all in `examples/`)

| file | product | blocks |
|---|---|---|
| `vanilla_call.yaml` | European call | `option_payoff`, closed form |
| `ekiako_put.yaml` | European knock-in, American knock-out put | `knock_in` condition + daily `knock_out` event |
| `up_and_out_call_continuous.yaml` | continuous barrier | closed form |
| `autocallable_note.yaml` | autocallable note | fixed_leg + principal + short KI put + knock_out autocall |
| `sofr_frn_lockout.yaml` | SOFR FRN | compounded_in_arrears + lockout, exact under deterministic rates |
| `inverse_floater_tarn.yaml` | inverse floater TARN | floating_leg (gearing −2, floor 0) + `target`, Hull-White |
| `cms_spread_range_accrual.yaml` | CMS spread range accrual | swap_rate × 2 + linear + daily range (+ rate range), Hull-White |
| `dual_equity_range_accrual.yaml` | dual-index range accrual with kick-in | 2-asset local vol, two ranges, `knock_in` on the coupon leg |
| `hybrid_fixed_then_range_accrual.yaml` | 10Y: fixed year 1, then rate range accrual with a stepping range | one `range_accrual`, per-period bounds, Hull-White |
| `cms_inverse_floater_in_arrears.yaml` | 10Y: fixed year 1, then 5% − 0.8×CMS5Y collared 2–4%, in arrears | `fixed_leg` + `floating_leg` on split schedules, Hull-White |
| `callable_fixed_rate_bond.yaml` | 10NC2 callable fixed-rate bond, semi-annual calls at par | `fixed_leg` + principal + `bermudan_call` (issuer), Hull-White |
| `callable_fixed_rate_bond_calibrated.yaml` | same 10NC2 bond | Hull-White volatility calibrated to coterminal swaptions |
| `cancellable_swap.yaml` | 5Y receive-fixed swap, cancellable annually by the counterparty | `fixed_leg` + short `floating_leg` + `bermudan_call` (issuer, price 0, swap-rate regressor), Hull-White |
| `bermudan_put.yaml` | 1Y Bermudan put on SPX, monthly exercise | `option_payoff` + `bermudan_call` (holder, `exercise_option`) |
| `seasoned_range_accrual_note.yaml` | 6M SPX range accrual traded 2 months ago, valued mid-period | shared fixings file (`fixings/mock_fixings.yaml`) for the range days and a daily knock-in put; prices at any date with `--set valuation_date=...` |

## Models

| type | underlyings | rates | closed forms |
|---|---|---|---|
| `black_scholes` | one (`underlying`) or several (`underlyings` + explicit `correlation`) | deterministic | European, single continuous barrier |
| `heston` | one; `params` or calibrated to the surface | deterministic | European |
| `hull_white` | none (rates only) | stochastic 1F, path-wise discounting; give `mean_reversion`, `volatility`, or `calibration` to swaptions | none |

- A vol `surface` gives Dupire local vol (matches all vanilla prices); `constant`/`term` give deterministic vol.
- Hull-White is one-factor, so all rates are perfectly correlated in their moves. That understates the
  volatility of a CMS spread. Say so when reporting a spread product.
- Not yet: equity + stochastic rates hybrids, multi-currency / quanto.

### Hull-White calibration to swaptions

Instead of `volatility` (and optionally `mean_reversion`), give a `calibration` block naming a
`market_data.swaption_vols` matrix. QuantLib's `HullWhite` model is fitted to ATM swaptions on the model's
discount curve with the Jamshidian engine (Levenberg-Marquardt), and the simulation uses the fitted values.

```yaml
model:
  type: hull_white
  discount_curve: USD
  mean_reversion: 0.03            # held fixed; with calibrate_mean_reversion: true it is the starting guess
  calibration:
    swaption_vol: USD             # market_data.swaption_vols.USD
    instruments: coterminal       # coterminal | all | [{expiry: 2Y, tenor: 8Y}, ...]
    calibrate_mean_reversion: false
    error: relative_price         # relative_price | price | implied_vol
```

- `coterminal` (default): annual expiries into the swap that ends at the trade maturity (1Yx9Y ... 9Yx1Y
  for a 10Y trade). Use it for callable bonds and cancellable swaps.
- `all`: every quoted point of the matrix. A constant-parameter 1F model rarely fits a whole matrix.
- Fix `mean_reversion` unless the user asks to fit it. Mean reversion is poorly identified by ATM swaptions
  alone, and it drives the price of a Bermudan.
- `model_diagnostics.calibration` lists the market and model vol of every instrument and the RMS vol
  error. The result warns when an instrument misses by more than 5% of its vol. Report that warning.
- The swaptions' floating index is projected off the discount curve (single curve), the same assumption the
  simulation makes.

## Pricing settings

`method: auto` uses a closed form only when one matches the trade **exactly**. Otherwise it uses
Monte Carlo, and `plan` shows each closed form's reason for not matching. `cross_check: true`
(the default) prices both when MC was chosen or forced and a closed form also applies.
`steps_per_year` (default 52) refines the equity simulation grid between sparse fixing dates (Hull-White
is simulated exactly). `max_memory_mb` guards path storage. Validation suggests a path count that fits.
