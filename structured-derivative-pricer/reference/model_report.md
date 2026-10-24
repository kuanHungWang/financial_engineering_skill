# Model documentation and validation report

Two parts, two owners:

- **The material** (every number): `scripts/report.py evidence` prices the trade and its variants with the engine
  and writes `materials.md` (tables, one section per topic) and `evidence.json` (the same, unrounded).
- **The report** (layout and text): you write it, following a **template**: the user's own format when they give
  one, otherwise `templates/sr11-7.md`, an outline structured after the Federal Reserve / OCC *Supervisory Guidance
  on Model Risk Management* (SR 11-7, 2011). The default covers SR 11-7's technical pillars: model development,
  implementation and use (purpose, product, method, data, assumptions, limitations) and model validation
  (conceptual soundness incl. sensitivity and stress, ongoing monitoring incl. process verification and
  benchmarking, outcomes analysis).

**Out of scope: governance, policies and controls** (roles, ownership, model inventory, risk tiering, approval,
independence of validators, board reporting, issue tracking, exceptions). They differ between organisations. Do not
write them and do not invent a rating, approval or sign-off. If a user's template has such sections, fill them only
with what the user supplied, otherwise leave a visible "[To be completed by the organisation]" line.

## The material

```bash
$PY <skill_dir>/scripts/report.py evidence INPUT --out-dir DIR [--run-tests]
```

`INPUT` is the trade YAML (all tests at defaults) or a **report config** (below). It prints a short summary:
`status_counts` and `attention` (every WARN / FAIL). Exit 1 if a test FAILED, 2 for bad input. It takes about 30
pricings: seconds for a fast trade, a few minutes for a large Bermudan; progress goes to stderr. `--run-tests` also
runs the engine's test suite (about a minute) and records the result. Use `--out-dir report` (or
`reports/<trade id>/` for several trades, or where the user wants it), never the skill directory. The config, its
case trade files, the evidence and `report.md` all live in that folder, so the project's top level keeps only the
user's trades and results (SKILL.md, "Project layout"). Read the printed summary directly; do not redirect stdout or
stderr to files.

## Workflow

1. **Price first** with the normal workflow (validate, plan, price). Resolve or understand every warning. A report
   on a trade that does not price correctly is worthless.
2. **Ask the user** (one short message) what they have for:
   - the **purpose and use** of the model (valuation for the books, independent price verification, client
     pricing, risk), and who uses it;
   - **reference prices** for outcomes analysis: counterparty / issuer marks, dealer or vendor quotes, a previous
     model's price, realised settlement amounts. Never invent them; with none, the section says none were supplied;
   - the **data sources** of each market input and of the fixings;
   - their own **report template** or format (a Word / PDF / Markdown document, or a list of sections). Without
     one, use `templates/sr11-7.md`.
3. **Design the tests** in a report config (below), saved as `report/report_config.yaml`: pick benchmarks and
   limiting cases that test *this* product's risk drivers, not generic ones. Keep the trade's own seed.
4. **Run evidence.** Read `attention`. Investigate every WARN / FAIL before writing about it: re-run with more
   paths, `price.py inspect`, check the market data. A FAIL in a case you designed may be a wrong expectation;
   fix the config only if the expectation was wrong, and say so in the report. Never loosen a tolerance to turn a
   FAIL into a PASS without saying why.
5. **Write `DIR/report.md`** from `materials.md` and the template (rules below). A user's template in .docx or
   .pdf is read with the matching skill. Keep its headings, order and fixed wording. If it has no place for a WARN /
   FAIL or a core result (sensitivities, stress, convergence), ask the user where it goes rather than dropping it.
6. **Deliver**: the price with its CI, the test counts, every WARN / FAIL in one line each, the conclusion, and any
   sections left for the organisation. Offer once to convert the report to the user's format (Word, PDF, a
   shareable page) with the tools available.

## Writing the report

- **Numbers are copied exactly from `materials.md`** (or `evidence.json`): same digits, same rounding or coarser,
  never recomputed, never estimated. A number not in the materials is not in the report, except term-sheet facts
  (notional, dates, barrier levels) and inputs the user gave. A derived figure the report needs (e.g. a change in
  %) must already be in the materials; if it is not, rerun `evidence` with a config that produces it.
- **Tables**: reuse the materials' tables where the template asks for results; trim columns if needed, but do not
  change values.
- **Say what the evidence says.** A WARN or FAIL is reported as such, with its cause if found and its impact on the
  price. Do not describe a test as passed that was not run (e.g. the unit tests without `--run-tests`).
- **Mock data**: if the materials say the market data is mock, say so at the top and in the conclusion; the
  numbers are illustrative only.
- **Limitations**: the engine-derived list plus product-specific ones: e.g. coupon-on-memory wording, settlement
  lags not modelled, issuer credit not in the discount curve, no funding / XVA, observation-date conventions. Give
  the direction of the bias where known.
- **Methodology** explains *why* the model fits the product's risk drivers (e.g. local vol reprices the smile the KI
  put depends on; one-factor HW is adequate for a callable fixed-rate bond but not for a CMS spread), and the
  alternatives considered (refer to the benchmarks).
- **Sensitivities**: a spot bump is sticky-moneyness for a vol surface with `strike_mode: relative`, sticky-strike
  for `absolute`; say which. Call a sensitivity not significant when the materials say so, rather than
  interpreting noise.
- **Ongoing monitoring**: what to rerun and when (each valuation: `price.py price`; after an engine change:
  `baseline.py verify` of an export; periodically: this report), what triggers review (FAIL, references out of
  tolerance, calibration misses, the market moving to where stresses showed large losses).
- **Conclusion**: fitness for the stated purpose and any conditions (e.g. "use with at least 50,000 paths").
- **Findings**: id, description, impact, recommendation; neutral and specific; "none" when there are none.
- **Check before delivering**: reread the report against `materials.md`; every number must appear there.

## Report config

Every key but `trade` is optional. Paths are relative to the config file, so with the config in `report/` the
base trade is `../trade_<name>.yaml` and case trades are `cases/<name>.yaml`.

```yaml
trade: ../trade_acn.yaml
set: [pricing.num_paths=50000]          # overrides applied to every run (base, variants, their own trade files)

convergence: {paths: [2500, 5000, 10000, 20000, 50000]}   # default: N/16, N/8, N/4, N/2, N
seeds: {count: 8}                       # seed-stability runs (seed+1 ... seed+count); 0 to skip
precision: {max_std_error: 500}         # optional PASS/FAIL on the base std error

bumps:                                  # sensitivity bump sizes (defaults shown)
  spot: 0.01                            # relative, per model underlying (delta, gamma)
  vol: 0.01                             # absolute, every vol point of the underlying (vega)
  dividend: 0.0001                      # absolute dividend yield
  rate: 0.0001                          # absolute, every quote of every curve (parallel PV01)
  swaption_vol: null                    # absolute; null = 1bp normal / 1% lognormal (calibrated Hull-White)
  model_volatility: 0.0001              # Hull-White sigma, when given rather than calibrated
custom_bumps:                           # any other sensitivity: central difference of two overrides
  - {label: "Correlation +5%", up: ["model.correlation=[[1,0.55],[0.55,1]]"],
     down: ["model.correlation=[[1,0.45],[0.45,1]]"], size: 0.05, unit: "absolute correlation"}

stress:                                 # replaces the default grid; dims: spot (relative), vol, rate, dividend,
  - {label: "Spot -40%, vol +15pt", spot: -0.40, vol: 0.15}   # swaption_vol (absolute), swaption_vol_scale,
  - {label: "Rates +300bp", rate: 0.03}                         # set (list of overrides)

benchmarks:                             # other models / methods for the same trade
  - label: Heston
    description: stochastic-vol benchmark for forward-skew exposure
    trade: cases/trade_acn_heston.yaml  # a full trade file, or `set:` overrides of the base trade
    expect: {approx: base, tol_se: 4}   # optional; without it the row is INFO

cases:                                  # limiting / boundary cases with an expected outcome
  - label: Autocall never triggers
    rationale: with the autocall barrier unreachable the note is a 2Y coupon note with a KI put
    set: [trade.termination_events.0.barrier=100]
    expect: {lt: base}
  - label: Matches the vanilla it collapses to
    trade: cases/vanilla_equivalent.yaml
    expect: {approx: benchmark:Heston}  # rhs: a number, base, case:LABEL or benchmark:LABEL (earlier entries)

references:                             # outcomes analysis: prices from outside the model
  - {label: Issuer mark, price: 1001500, source: Issuer, date: 2026-09-28, tolerance_pct: 0.01}
  - {label: Vendor, price: 1004200, source: Vendor X, date: 2026-09-28, tolerance: 2500}
```

`expect` takes one of `approx`, `le`, `ge`, `lt`, `gt`, and a tolerance: `tol` (absolute) or `tol_se` (in combined
standard errors, default 4; at least 1e-9 relative, so two closed forms can match). Inequalities get the same
statistical slack. Labels are unique per kind, and the whole config is checked before anything is priced. Reference prices must be in the trade's
units (currency amount on the trade's notional, holder's sign); convert a quote in % of notional before entering it.

The market file is inlined into each trade (the base and any benchmark / case trade file) before `set` is
applied, so `set` can change market data too:
`market_data.equities.SPX.vol={type: constant, value: 0.0001}` (near-deterministic case),
`market_data.equities.SPX.spot=6000`.

## Tests and their SR 11-7 role

Section numbers below are the default template's; `materials.md` has one section per row.

| Report section | SR 11-7 element | Evidence (`tests.` key) | Status rule |
|---|---|---|---|
| 4 Methodology | conceptual soundness | method, model, graph, `implementation.calibration` | WARN if a Hull-White instrument misses its vol by > 5% (relative), or a Heston fit's RMS vol error exceeds the engine's limit |
| 6 Assumptions and limitations | limitations | `known_limitations` (from model and method) + product-specific ones you add | - |
| 7 Implementation | process verification | `implementation`: validation, reproducibility (same seed twice), martingale check, zero bump, exercise policy in/out of sample; `unit_tests` | FAIL if not reproducible, martingale \|z\| >= 4, a zero bump or pinning the reference levels moves the price, a test fails; WARN on validation warnings or an exercise-policy gap > 4 SE |
| 8 Convergence | conceptual soundness (numerical) | `convergence`, `seed_stability`, `precision` | WARN if fewer than 3 levels price, a level is > 4 SE from the largest, the std-error slope is outside -0.65..-0.35, or seed dispersion / SE is outside its 99% band |
| 9 Sensitivity | sensitivity analysis | `sensitivities`: P+, P-, first / second order with their SE (common random numbers), delta / gamma per unit | INFO; FAIL if a bump cannot be priced. `significant: no` = within 2 SE of zero |
| 10 Stress | stress testing | `stress` | INFO; FAIL if a scenario cannot be priced. A default scenario this market cannot take (e.g. vol -5pt on a surface with a vol below 5%) is skipped |
| 11 Benchmarking | benchmarking | engine closed-form cross-check, `benchmarks` | per `expect`; cross-check FAIL beyond 4 SE |
| 12 Outcomes | outcomes analysis | `references` | per tolerance, else INFO |
| 13 Limiting cases | conceptual soundness (testing) | `cases` | per `expect` |
| 14 Ongoing monitoring | ongoing monitoring | your text + the reproduction note | - |

Spot bumps and stresses keep the trade's **initial fixings fixed**: `evidence` pins `trade.reference_levels` to the
values the base run used, so relative strikes and barriers stay where they are (otherwise a new trade's levels
would move with spot and delta would be zero). The zero-bump check confirms the pinned trade prices as the original.

Sensitivities are bump-and-revalue on the same seed and paths. Under Monte Carlo, discontinuous payoffs (digital
coupons, discrete barriers, autocalls) make spot and vol differences noisy: read the `SE` column, and if a key
sensitivity is not significant, rerun with more paths (config `set`) or a larger bump rather than reporting noise.
Rate bumps shift curve quotes in parallel (a bootstrap curve is re-bootstrapped), and with a calibrated Hull-White
the model is re-calibrated under every bump, as it would be in production.

## Choosing benchmarks and cases

Pick what tests the product's main risk drivers and the engine's riskiest choices:

| Product | Benchmarks | Limiting / boundary cases |
|---|---|---|
| Vanilla / single barrier | MC (`pricing.method=mc`) vs closed form | barrier far away = vanilla; deep ITM = discounted forward minus strike |
| Autocallable, phoenix, snowball | `heston` vs local vol (forward skew) | autocall barrier unreachable (`lt`/`gt` base by who holds it); KI barrier at 0 = no KI (holder's value up); coupon 0 |
| Worst-of / basket | correlation +/- (custom_bumps) | correlation 1 vs a single-asset trade; one asset only |
| Range accrual | constant vs smile vol; HW sigma +/- | range covering everything = fixed coupon note (trade file); empty range = zero coupon |
| Callable / puttable bond, cancellable swap | calibrated vs given HW parameters; coterminal vs all swaptions | no call dates = straight bond (trade file, `ge` for an issuer call); call price very high = straight bond |
| Bermudan option | more regressors / degree; more `exercise_fit_paths` | single exercise date = European (closed form trade) ; Bermudan >= European |
| Floating / SOFR legs | - | zero spread, no cap/floor = par-like floater; cap very high = uncapped |
| TARN | - | target unreachable = full coupon strip; target tiny = first coupon only |

Build an equivalence case as a separate trade file in `report/cases/` (start from the base trade, change one
feature; its `market_data` and `fixings` paths are relative to the file, so they become `../../market/...`) and compare with `approx`. For a relation (callable <= straight), use `le` / `ge`. State the expected
direction from the **holder's** view, as the engine signs prices.

`evidence.json` has the same results unrounded: `key` (headline numbers), `summary` / `status_counts` (every
test), `base` (the full pricing result), `plan`, `input` (the trade as priced), `market`, `known_limitations`,
`tests.*`, `files` (inputs with SHA-256) and `environment`.
