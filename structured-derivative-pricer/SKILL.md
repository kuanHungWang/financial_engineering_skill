---
name: structured-derivative-pricer
description: Price exotic and structured derivatives (barrier options, EKIAKO, autocallables, callable / puttable bonds, cancellable swaps, Bermudan options, equity-linked notes, fixed/floating/SOFR-compounded legs, inverse floaters, range accruals on equities, rates, CMS spreads or baskets, TARNs, kick-in coupons) by writing a declarative YAML trade spec that a pre-built QuantLib engine validates and prices by Monte Carlo, or in closed form when QuantLib has one, and write model documentation / validation reports for a priced trade (SR 11-7 style). Use when the user gives a term sheet or product description and asks for a fair value, NPV, MTM, or pricing model, or asks for a model validation report, model documentation, model review or SR 11-7 report, including "price this note", "value this barrier option", "build a pricing model for...", "write the validation report", "結構型商品評價", "公平價值", "模型驗證報告". You write the spec, never pricing code.
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
(kick-in / knock-out conditions on a leg, knock-out termination, target (TARN) and Bermudan exercise events
on the trade). A **path-dependent** leg carries a state between periods (snowball, memory coupon), with its
update and payoff given as checked formulas.

## Setup (once per machine)

```bash
PY=$(<skill_dir>/scripts/setup.sh)      # creates <skill_dir>/.venv with QuantLib, prints its python
```

After that, run every command below as `$PY <skill_dir>/scripts/price.py ...`.

## Project layout

For a new model (the first trades of a product, a prototype, a validation), keep the top of the working directory
to the files the user owns and reads: trades and results. Everything else goes in a sub-folder. A production
project has its own structure; follow it (see "Reusing a validated model" and "Fitting the user's folders").

```
<project>/
├── trade_<name>.yaml        trade specs the user prices (one per trade)
├── result_<name>.json       price.py price --out (result.json when there is one trade)
├── market/                  market files, plus the script that converted the user's data, if you wrote one
├── fixings/                 past fixings
├── report/                  only when a report is asked for (see "Validation report")
│   ├── report.md            the report
│   ├── report_config.yaml
│   ├── cases/               benchmark and limiting-case trade files the config names
│   ├── materials.md
│   └── evidence.json
├── diagnostics/             inspect output, analysis scripts, kept logs
└── pricing_engine/          stand-alone copy, only after a yes
```

- **Read command output directly; do not redirect it to files.** Every script prints its JSON result on stdout and
  progress on stderr, so the tool call already captures both. No `> out.json`, `2> err.txt` or
  `> inspect_manifest.json` in the project (`inspect` writes its `manifest.json` itself). If a long run's output
  must be kept, write it to `diagnostics/logs/`.
- A helper script you write for analysis goes in `diagnostics/`; one that builds market data from the user's files
  goes in `market/`, next to its output, so the data can be rebuilt.
- A test-only trade (straight bond, zero vol, single exercise date) is report material: `report/cases/`, never
  next to the user's trades.
- Follow a layout the user already has, or one they ask for, over this one.

## Workflow

1. **Read the term sheet.** List every economic feature: underlying(s), dates, strike, notional,
   coupons, barriers (level, direction, *observation frequency*), knock-in/out effects,
   rebates, early termination, who pays what. Resolve ambiguities with the user **before** writing
   anything. Two ambiguities matter most: whether a barrier is observed at expiry only or daily, and
   whether a level is a % of the initial fixing or an absolute price. For rate legs also pin down: fixing in
   advance or compounded in arrears, fixing lag / lockout, day counts, and the calendar of daily observations.
   For a callable / puttable / cancellable product pin down:
   - who holds the right (issuer or investor),
   - the exercise dates after notice,
   - the exercise price as cash,
   - whether the coupon of the exercise date is still paid.
2. **Map features to components** with [reference/components.md](reference/components.md), which
   covers the mapping table, the sign conventions, and worked product recipes. For exact field
   names, defaults and allowed values, run `price.py catalog [kind]`. The catalog is generated
   from the code and is always current.
3. **Market data.** Ask the user for curves, spot, dividends and vols (swaption vols to calibrate Hull-White), or look for files in the working
   directory. The format is in [reference/market_data.md](reference/market_data.md). Convert what they
   give (CSV, pasted tables) into that YAML. **Never invent market data silently.** For a demo or test,
   `scripts/make_mock_market.py market/OUT.yaml --as-of YYYY-MM-DD` writes clearly-labelled mock data. Say
   in your answer that the price uses mock data.
4. **Write the spec** in the working directory (e.g. `trade_<name>.yaml`; see "Project layout"). Start from the
   closest file in `examples/`, or, when the project already has a validated trade of this type, from that trade
   (see "Reusing a validated model").
5. **Validate:** `price.py validate trade.yaml`. It returns every error at once, each with its
   input path, message and hint (often "Did you mean ..."). Fix them all, then validate again.
   Exit code 2 means invalid.
6. **Plan:** `price.py plan trade.yaml`. Check `method` and `method_reason`, which say analytic vs MC and
   why. Check that `graph` and `simulation.fixing_dates` look right. A daily barrier over 1Y should
   show about 250 fixing dates. If it shows 1, you observed it at expiry only.
7. **Price:** `price.py price trade.yaml --out result.json`. Use `--set pricing.num_paths=100000`
   to tighten the error without editing the file.
8. **Check, then report.** See "Reading the result" below.
9. **Intermediate results, on request** (UAT, model review): `price.py inspect trade.yaml`. See below.
10. **Model documentation / validation report, on request.** See "Validation report" below.
11. **Offer a stand-alone copy.** After the first successful price in a project, ask the user once, without being asked:
    the trade YAML alone cannot be re-priced without this skill, so offer to copy the engine into their project. See
    "Stand-alone copy" below. Do not export without a yes.

## Validation report

When the user asks for model documentation, a validation or model review report, or "SR 11-7", read
[reference/model_report.md](reference/model_report.md) first. It has the workflow, the report config, the tests and
the writing rules. In short: `scripts/report.py evidence` runs the tests (convergence, seeds, sensitivities, stress,
benchmarks, limiting cases, reference prices) and writes the material, `materials.md`. You then write `report.md`
from the material, following the user's own template when they give one (ask), otherwise the SR 11-7 outline
`templates/sr11-7.md`. Everything the report needs (config, case trades, evidence, the report) goes in `report/`.

- Copy every number exactly from `materials.md`; never compute or estimate one yourself.
- Governance, policies and controls are out of scope (they differ between organisations): write no approval,
  rating, ownership or inventory content. Sections of a user's template that ask for them get only what the user
  supplies.
- Ask for reference prices (counterparty, dealer, vendor marks) and data sources; never invent them.

## Stand-alone copy

`scripts/export_engine.py [DEST] --trade trade.yaml ...` copies the engine into `DEST` (default `./pricing_engine`
in the working directory). It writes a pinned, self-contained folder: engine source, `requirements.lock.txt` with
every package (dependencies included) at its exact version, a `README.md` for people, `price.sh` / `baseline.sh`
wrappers, the docs, examples and tests, and `ENGINE.json`. The version hashes the pricing code, the lock and the
Python version, so a library upgrade is a new version too. The copy is built and checked in a staging folder:
each `--trade` is priced by the copy, compared to the skill's price (`matches_skill` must be true), and recorded
under `DEST/baselines/`. DEST is replaced only if every check passes (`dest_unchanged: true` otherwise). The trade,
market and fixings files stay where they are. `DEST/baseline.sh verify` later re-prices every baseline and reports
`reproduced`, `inputs_changed` or `MISMATCH`, with an `environment` block naming the cause (edited engine files,
library drift). The checks use the skill's own Python; `--check-env` also builds the copy's locked `.venv` the way the
user will (about a minute, needs network) and verifies with it. Offer it when the user will rely on the copy.

- **Ask** in one short question after step 8. Say what it gives them (re-pricing with no AI, pinned versions,
  recorded prices to check against) and roughly how big it is (about 1 MB, plus a `.venv` built on first use). Name
  the folder you will use: the user's structure decides where (e.g. `derivative/pricer/`); without one, suggest
  keeping trades, `market/` and `fixings/` beside it.
- **Already exported** (`DEST/ENGINE.json` exists): do not ask again. Same version: record each newly priced trade
  as a baseline (`$PY <skill_dir>/scripts/export_engine.py DEST --trade new_trade.yaml`), and say so. Different version: tell
  the user both versions, and upgrade only on a yes with `--upgrade`. That re-verifies every baseline with the new
  engine and, if they pass, moves the old engine to `DEST/archive/<version>/`. The archive still prices and verifies
  (`archive/<version>/baseline.sh verify --baselines DEST/baselines`). Report every `MISMATCH`.
- Never edit files inside an export, as the hard rule for `pricer/` says. After exporting, point the user to
  `DEST/README.md` and give the re-price command.

## Reusing a validated model

When the user prices a new trade of a type their project already priced and validated (a trade file with a report,
or a stand-alone copy with baselines), that earlier trade file **is** the model. Reuse it; do not derive a new spec.

1. **Start from the validated trade file**, not from `examples/`. If there are several, ask which one.
2. **Change only term-sheet fields**: id, dates, notional, strike, coupon, barrier levels, call schedule, notice.
   Keep everything that is the model: `model` and its calibration settings, `pricing` (method, `num_paths`, `seed`,
   regressors, `exercise_fit_paths`), observable and index definitions, conventions. A term sheet that needs a model
   field changed (a new feature, another underlying or currency) is no longer the validated model: say so, and say
   what the change touches.
3. **Show the diff** against the validated file before pricing, one line per changed field.
4. Validate, plan (`method` and the `graph` should match the validated trade's), price. Write files where the
   user's structure puts them; create no `report/` or `diagnostics/` unless asked.
5. **With a stand-alone copy**, price with it (`DEST/price.sh price ...`), so the price comes from the engine version
   the model was validated on, and record the trade as a baseline ("Already exported" above). If the skill's
   engine version differs from the copy's, tell the user both.

## Fitting the user's folders

The stand-alone copy knows nothing about folder structures, batches or output naming, on purpose: it prices one
trade per call, `DEST/price.sh price TRADE [--set ...] [--out FILE]`, the same core as the skill's `price.py`. Fitting
it into the user's structure is your job: write a small **adaptor** (`.sh` or `.py`) where the user wants it.
For example:

```
derivative/
├── pricer/                    stand-alone copy (export_engine.py derivative/pricer)
├── market_data/market_2026-10-24.yaml
├── fixed_rate_callable/trade_*.yaml
├── range_accrual/trade_*.yaml
├── batch.sh                   adaptor: every trade_*.yaml -> output/<date>/<product>/<trade>.json
└── output/
```

or one `batch.sh` per product folder, each writing to its own `output/`. Before writing one, ask for the
structure, the run (which trades, which market file, how the valuation date is chosen) and the output (one JSON per
trade, a summary CSV, a file-name pattern), unless the user has already said.

An adaptor only moves inputs and outputs:
- It calls `DEST/price.sh` (or `DEST/baseline.sh`) once per trade. It passes the run's market file and date with
  `--set market_data=<absolute path> --set valuation_date=YYYY-MM-DD`; a relative `market_data` is resolved from
  the trade file, not the current directory.
- It checks every exit code (0 priced, 1 failed, 2 invalid input). It names each failed trade at the end, and its
  own exit code is non-zero if any trade failed. It never skips a failure silently.
- It may collect result fields (price, std_error, confidence_interval, warnings) into a summary, copied as they
  are.
- It never computes, adjusts, rescales or converts a price, never edits market data, and never edits the copy.
  Anything beyond moving and collecting is engine work: stop and tell the user (Hard rules).

Keep it outside `DEST`, so an upgrade replaces the engine and leaves the adaptor alone. Run it once on the user's
trades and check one price against a direct `price.sh` call (or a baseline). Then give the user the command.

## Hard rules

- Do **not** write Python that builds, prices or simulates a trade, or that changes what the engine produced: no
  hand-rolled payoff, model, path generator or discounting, no adjusted price. Do not edit `pricer/`.
  If a feature cannot be expressed with the catalog, **stop and tell the user**: name the missing
  component, for example "a range-accrual coupon leg" or "Bermudan issuer call". Custom components
  are a planned extension point (see "Extending"). They are not a workaround to improvise.
- A shell or Python **adaptor** that runs the engine over the user's folders is allowed, within the limits in
  "Fitting the user's folders": it moves inputs and outputs, never values.
- Python **is** allowed for an analysis sub-task the user asks for, read-only on the files `price.py inspect` wrote
  (see "Inspecting intermediate results"): whether the paths look reasonable, how dispersion grows with vol, the
  Bermudan exercise boundary, checking that a path's cashflows times its discount factors give its `pv`, plots.
  It must not produce new values from new inputs: no re-discounting with another curve, no re-simulation from
  the paths, no changed payoff or exercise rule. Present its results as diagnostics, never as a price or a
  sensitivity; a price or a sensitivity comes from `price.py` runs. Say which files it used.
- Use `path_dependent` only for a coupon that depends on earlier periods (a state). A coupon that a dedicated leg
  covers (fixed, floating with cap / floor, range accrual) or a mask covers (kick-in, knock-out, autocall, TARN) uses
  that component: it carries the right diagnostics, closed forms and validation. Write a formula exactly as the
  term sheet defines the coupon; never tune it to approximate a feature it cannot express.
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
- Past fixings go in a **fixings file** (`fixings: fixings.yaml`; `equities` closes by underlying, `rates` by
  name), shared by trades and appended over time. Inline `trade.history` / observable `history` also work
  and win date by date. This covers the first in-advance fixing at inception and every fixing of a **seasoned
  trade** (priced after its trade date, e.g. mid-period with part of a range accrual or a compounded rate
  already fixed). Ask the user for them; never invent them. Write a trade spec once, with an explicit
  `effective_date`: it then prices at any date of its life by changing only `valuation_date` and the market
  data. A TARN recomputes its paid coupons from their past fixings; if those are not available, ask the user
  for the amount already paid and give it as `accumulated`.

## Reading the result

| field | what to check |
|---|---|
| `method`, `method_reason` | analytic (closed form, exact) or mc, and why no closed form matched |
| `price`, `std_error`, `confidence_interval` | if `std_error` is too large relative to price, re-run with more paths |
| `legs` | PV per leg. The signs follow `position`, from the holder's view |
| `leg_diagnostics`, `events` | knock-in / knock-out / autocall / exercise probabilities. Sanity-check them against intuition |
| `events.<id>.policy` | Bermudan exercise: `in_sample_price` should be within a few `std_error` of `price`. A larger gap means the regressors miss the exercise driver |
| `expected_cashflows` | expected amount, DF and survival probability per payment date |
| `cross_check` | when MC ran and a closed form also applies, `agrees` must be true |
| `martingale_check` | `ok` must be true. It tests the simulated forward against the curve forward |
| `warnings`, `notes` | report every warning to the user, e.g. vol extrapolation or local-vol flooring |

Report the price with its CI, the method, the key probabilities, any warnings, and the model
assumptions (model type, and whether the data is mock). For a calibrated `hull_white`, report the fitted
`mean_reversion` / `volatility` and the worst swaption miss from `model_diagnostics.calibration`. For model risk on path-dependent
products, it can help to price under both `black_scholes` (local vol from the surface) and
`heston`, and show the gap.

## Inspecting intermediate results

`price.py inspect trade.yaml [--node KEY ...] [--out-dir DIR] [--max-paths N]` prices the trade exactly as `price`
does (same seed, same paths, same price) and writes the result of **every graph node** to `DIR`. Always pass
`--out-dir`: `diagnostics/inspect_<trade id>/` in the project when the user will look at the files (the default,
relative to the current directory), your scratch directory when only you need them, or a folder the user named.
`DIR` must be new, empty, or an earlier inspect output (whose listed files are replaced). Nothing is recomputed: each node's own output object is serialized by data type, so
any node, including one added later, can be inspected. Use it when the user wants to check components by hand
(UAT), analyse paths, or reconcile a cashflow.

- **Disk vs stdout.** All data goes to disk; stdout gets only `manifest.json`: each file, its node, its shape,
  and a few summary numbers, plus `not_exported` (QuantLib objects and functions that have no data form). Only the
  `*_stats.csv` files (per-date mean, std, percentiles) and the small JSON files are safe to read into the
  conversation; reach anything else through a short Python analysis that prints a small result.
- **`--node`** takes a key from `plan`'s `graph` (`leg:coupons`, `curve:USD`, `event:autocall`) or a kind (`leg`,
  `curve`, `observable`, ...). Repeat it; the default is every node. `observable:<underlying>` also works.
- **`--max-paths N`** writes only the first N paths (rounded up to even under antithetic sampling, so pairs stay
  whole), e.g. for a spreadsheet. Statistics and the price use every path.

How a node's output becomes files (`<node>` is the key with `:` and any character unsafe in a file name
replaced by `_`):

| in the node's output | file |
|---|---|
| dates x paths table or array (paths, cashflows, amounts, exercise state) | `<node>.<field>.csv`: one row per path (antithetic pairs adjacent), one column per date, plus `<node>.<field>_stats.csv`. A deterministic quantity has one row, `path = all` |
| one value per path (hit dates, scales) | columns of `<node>.per_path.csv` |
| list of dates (a schedule) | `<node>.value.csv` |
| scalars, settings, diagnostics, specs (model parameters, calibration, Bermudan regression coefficients) | `<node>.json` |
| a yield curve (curve, discount, dividend) | `<node>.csv`: DF and zero rate on every date the trade uses; a curve seen again in another node is written once |

Two outputs do not belong to a single node, and are written from where the run keeps them:
- `observable_<o>.values.csv` holds the values the legs and events computed on every date they read the
  observable (past dates from history).
- the `price` node writes `price.cashflows.csv`, the cashflow paid on each path after termination and exercise,
  and `price.per_path.csv`, the PV of each path and the termination or exercise that ended it. The price is the
  mean of `pv`.

`result.json`, `input.json` (the input as priced, after `--set`) and `manifest.json` are always written. Examples:
the Bermudan exercise boundary is in `event_<id>.state.*.csv` and `event_<id>.amount.csv` against the
`exercise_date` in `price.per_path.csv`, with the regression in `exercise_policy.json`. A per-leg breakdown that
reconciles to `pv` is `price.cashflows.csv` times the engine's own `discount_factors.values.csv`. For a question
such as "does dispersion grow with vol", run `inspect` twice into two directories with the same seed, changing only
the market file, and compare the `*_stats.csv` files.

## Coverage

Models:
- `black_scholes`: one or several correlated equity underlyings (constant / term / Dupire local vol).
- `heston`: single asset, given or calibrated parameters (fitted in implied vol to every surface quote; `model_diagnostics.calibration_rmse` is in Black vol, and a fit worse than 2 vol points RMS, or one where the optimizer did not converge, warns).
- `hull_white`: 1F rates, given `mean_reversion` and `volatility`, or calibrated to ATM swaptions
  (`calibration`: coterminal, whole matrix, or listed expiry x tenor points).

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
- `fixed_amount`,
- `path_dependent`: a state carried between periods, S_k = evolve(S_{k-1}, x_k), coupon = payoff(x_k, S_k), as
  formulas (+ − × ÷ **, min, max, clip, 0/1 indicators, where): snowball, snowrange, memory / phoenix coupons,
  ratchet floaters, cliquets, lookbacks, cumulative caps. Its state can be a `bermudan_call` regressor
  (callable snowball).

Masks:
- `knock_in` (kick-in) and `knock_out` conditions on any leg,
- trade-level `knock_out` (termination / autocall) and `target` (TARN) with rebate / guarantee,
- trade-level `bermudan_call`: issuer call / holder put / cancellation / Bermudan option on a schedule, priced
  by Longstaff-Schwartz (callable and puttable bonds, cancellable swaps, Bermudan options).

Seasoned trades: priced after inception, between payment dates. Every payoff reads dates before the valuation
date from history and later dates from the simulation (see "Seasoned trades" in components.md).

Closed forms: European vanilla (BSM, Heston) and single continuous barrier (BSM).

Not yet supported, so say so rather than approximating:
- more than one Bermudan exercise right per trade (e.g. both issuer call and holder put),
- American (continuous) exercise; approximate it by a daily `bermudan_call` schedule only if the user agrees,
- equity plus stochastic-rate hybrids,
- FX / quanto / multi-currency,
- past monitoring of a `continuous` barrier (closed forms only look forward from today),
- custom payoff code (planned: a bridge for AI-written payoff components).

## Extending (for developers, not during a pricing task)

Each component is a class registered with `@register(kind, type)` and a pydantic `Spec`
(`pricer/registry.py`). A new observable, leg, condition, event, model or analytic pricer needs one class.
Legs and observables read the market only through `models.Scenario` (equity paths, `discount`, `bond`,
`growth`), so they work under every model. Legs, conditions and events go one step further: they read only
through observables (`p.obs(name, dates)`, an underlying by its name) and declare their dates only through
`obs.requirements`. That is what makes past fixings work for any payoff: history for past dates, simulation for
later ones, one validation of the missing dates. A test enforces it. Validation,
the catalog and the graph pick it up automatically. Add a regression test in `tests/` against a
closed form or a limiting case. Run the suite with `$PY -m pytest <skill_dir>/tests -q`.

`pricer/exercise.py` holds model-agnostic early-termination blocks that work on (periods x paths) cashflow
tables. The `bermudan_call` event (`pricer/bermudan.py`) runs `longstaff_schwartz` inside the engine's
aggregation. See [reference/exercise.md](reference/exercise.md):
- `longstaff_schwartz`: optimal holder or issuer exercise by least-squares regression,
- `autocall`: termination on the first period an autocall mask holds,
- `summarize`: expected cashflows per period, fair value per path, price with confidence interval.
