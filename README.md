[English](./README.md) | [繁體中文](./README.zh-TW.md)

# Structured Derivative Pricer — a Claude skill

A [Claude skill](https://docs.claude.com/en/docs/claude-code/skills) that prices exotic and structured
derivatives from a term sheet, and writes model documentation / validation reports (SR 11-7 style) for a priced
trade.

**Claude writes a declarative YAML trade spec. Claude never writes pricing code.** A pre-built, pre-tested
QuantLib engine validates the spec, picks the pricing method (closed form when one exists, Monte Carlo
otherwise) and prices it. The price is reproducible per seed and does not depend on code the AI improvised.

```
You: "Price this autocallable note" + term sheet + market data
        │
        ▼
Claude: reads the term sheet, asks about ambiguities, converts your market data,
        writes trade_<name>.yaml
        │
        ▼
Engine: validate ─▶ plan (analytic or MC, and why) ─▶ price ─▶ result.json
```

---

## Installation

### Requirements

- [Claude Code](https://docs.claude.com/en/docs/claude-code) (or another Claude client that supports skills)
- Python 3.11 (or [uv](https://github.com/astral-sh/uv), which fetches the right Python itself)
- Network access on first use, to install QuantLib, pydantic, PyYAML, NumPy and pandas

### Install the skill

Clone the repository and put the `structured-derivative-pricer/` folder where Claude Code looks for skills.

```bash
git clone https://github.com/kuanHungWang/financial_engineering_skill.git
```

For every project (personal skills):

```bash
mkdir -p ~/.claude/skills && cp -R financial_engineering_skill/structured-derivative-pricer ~/.claude/skills/
```

Or for one project only, copy it into that project's `.claude/skills/` folder instead. A symlink
(`ln -s`) works too, if you want `git pull` to update the skill.

### Set up the Python environment (optional)

The skill runs this itself the first time it is used. To do it ahead of time:

```bash
~/.claude/skills/structured-derivative-pricer/scripts/setup.sh
```

It creates `structured-derivative-pricer/.venv` with QuantLib and the other requirements, and prints the path of
its Python. It is idempotent: running it again is a no-op.

### Check the install

```bash
PY=$(~/.claude/skills/structured-derivative-pricer/scripts/setup.sh)
$PY -m pytest ~/.claude/skills/structured-derivative-pricer/tests -q
```

### Try it

The [`test_case/`](./test_case/README.md) folder has seven natural-language term sheets and raw mock market
data (CSV, deliberately not in the skill's format). Copy it into a project and ask Claude, for example:

> Price `termsheets/03_worst_of_phoenix_autocall.md`. Use the market data in `market_data/`.

> Write an SR 11-7 validation report for it.

---

## What it can do

### Pricing

Give Claude a term sheet or a product description and ask for a fair value, NPV, MTM or a pricing model.
It works in English and Chinese (e.g. "結構型商品評價", "公平價值").

Products are assembled from building blocks, so many term sheets need no new code:

| Block | Available |
|---|---|
| **Models** | `black_scholes` (one or several correlated equities; constant, term or Dupire local vol) · `heston` (single asset, given or calibrated to the vol surface) · `hull_white` 1F rates (given, or calibrated to ATM swaptions: coterminal, full matrix or listed points) |
| **Observables** | equity level or performance · term rate · swap rate (CMS) · overnight rate (SOFR) · `linear` combinations (spreads, baskets, inverse-floater formulas) · `min_of` / `max_of` (worst-of, best-of) |
| **Legs** | fixed · floating (in advance, in arrears, compounded in arrears with lockout; gearing, spread, cap, floor) · range accrual (any number of ranges on any observables) · option payoff · fixed amount · `path_dependent` (a state carried between periods: snowball, memory / phoenix coupon, ratchet, cliquet, lookback, cumulative cap) |
| **Masks / events** | kick-in and knock-out conditions on any leg · trade-level knock-out (autocall) · target redemption (TARN) · Bermudan exercise (issuer call, holder put, cancellation, Bermudan option) by Longstaff-Schwartz |
| **Closed forms** | European vanilla (BSM, Heston) · single continuous barrier (BSM) |

Worked examples in [`structured-derivative-pricer/examples/`](./structured-derivative-pricer/examples/):
vanilla call, up-and-out call, EKIAKO put (BSM and Heston), phoenix memory autocall, callable snowball,
Bermudan put, callable fixed-rate bond (given and calibrated Hull-White), cancellable swap, CMS spread range
accrual, CMS inverse floater in arrears, dual-equity range accrual, fixed-then-range-accrual hybrid, inverse
floater TARN, SOFR FRN with lockout, seasoned range accrual note.

### Seasoned trades

A trade can be priced at any date of its life, mid-period included. Past fixings come from a shared fixings
file; future ones from the simulation. A spec is written once and re-priced by changing only the valuation date
and the market data.

### Result checks

Every price comes with what you need to trust it:

- method and why (analytic or MC), price, standard error and confidence interval,
- PV per leg, knock-in / knock-out / autocall / exercise probabilities, expected cashflows per date,
- a cross-check against a closed form when MC ran and one applies,
- a martingale check of the simulated forward against the curve,
- the Bermudan in-sample vs out-of-sample gap,
- Hull-White calibration fit and Heston calibration RMSE,
- warnings such as vol-surface extrapolation.

### Intermediate results (UAT, model review)

`price.py inspect` re-prices the trade exactly (same seed, same price) and writes the output of every graph node
to disk: paths, fixings, cashflows, discount factors, exercise state and regression coefficients, as CSV and
JSON. Useful for reconciling a cashflow by hand or checking an exercise boundary.

### Validation report

On request, `report.py evidence` runs the tests a reviewer expects (convergence, seed stability, sensitivities,
stress, benchmarks, limiting cases, reference prices) and collects the material. Claude then writes the report
from that material, in your template or the SR 11-7 outline in
[`templates/sr11-7.md`](./structured-derivative-pricer/templates/sr11-7.md). Every number is copied from the
evidence, never computed by the AI.

### Stand-alone copy

After the first price, Claude offers to export the engine into your project (`export_engine.py`). The copy
re-prices with no AI and no skill installed: pinned package versions (`requirements.lock.txt`), recorded baseline
prices, and `baseline.sh verify` to detect when a re-price no longer matches and why (edited files, library
drift). Engine upgrades re-verify every baseline and archive the old version.

---

## Limitations

Not supported yet. The skill says so instead of approximating:

- more than one Bermudan exercise right in a trade (e.g. both an issuer call and a holder put),
- American (continuous) exercise (a daily Bermudan schedule is used only if you agree),
- equity plus stochastic-rate hybrids,
- FX, quanto and multi-currency products,
- past monitoring of a `continuous` barrier (closed forms only look forward from today; a daily-close barrier
  priced by MC handles history fine),
- custom payoff code (planned as an extension point).

Other things to know:

- **Market data is yours to provide.** The skill converts what you give it (CSV, pasted tables) but never invents
  data. Mock data exists for demos and is always labelled as such in the answer.
- **Rate optionality needs Hull-White.** Caps, floors, rate ranges and rate barriers are rejected under the equity
  models, whose rates are deterministic.
- **Monte Carlo prices carry error.** Check `std_error`, and raise `num_paths` when it is too large.
- **Governance is out of scope in reports.** Approval, rating, ownership and inventory sections get only what you
  supply.
- **It is a model, not advice.** The output is a model value under the stated assumptions. Validate it against
  your own controls before relying on it.

---

## The engine behind it

### Spec → graph → price

The engine is a factory: it turns the YAML spec into a graph of pre-tested components and runs it.

```
curve ─▶ discount ─▶ underlying ─▶ model ─▶ scenario ─▶ observables ─▶ legs ─▶ masks ─▶ price
                                                   └────────▶ discount_factors ─────────┘
```

1. **Validate** (`price.py validate`): the whole spec is checked before anything runs, by pydantic schemas. Every
   error comes back at once, with its input path and a hint ("Did you mean ...").
2. **Plan** (`price.py plan`): shows the graph, the fixing dates, and the chosen method with its reason.
3. **Price** (`price.py price`): closed form when one matches, otherwise Monte Carlo (antithetic paths, fixed
   seed), with Longstaff-Schwartz regression for Bermudan exercise.

### Built on QuantLib

[QuantLib](https://www.quantlib.org/) supplies the curves, calendars, day counts, schedules, index definitions
(`Sofr`, `Euribor`, `EuriborSwapIsdaFixA`, ...), the Black-Scholes / Heston / Hull-White processes and their
calibration, and the closed-form engines. The pricer adds the composable payoff layer, path simulation,
history-aware observables and exercise logic on top.

### Layout

```
structured-derivative-pricer/
├── SKILL.md            instructions Claude follows
├── pricer/             the engine (registry, spec, models, observables, legs, masks, Bermudan, analytic, ...)
├── scripts/            price.py, report.py, export_engine.py, baseline.py, make_mock_market.py, setup.sh
├── reference/          components, market data format, exercise, model report guide
├── templates/          SR 11-7 report outline
├── examples/           example trade specs + mock market and fixings
└── tests/              regression tests against closed forms and limiting cases
docs/index.html         static component reference (open in a browser)
test_case/              smoke-test term sheets and raw mock market data
```

### Commands

```bash
PY=$(structured-derivative-pricer/scripts/setup.sh)
$PY structured-derivative-pricer/scripts/price.py catalog [kind]       # every component, field, default
$PY structured-derivative-pricer/scripts/price.py validate trade.yaml
$PY structured-derivative-pricer/scripts/price.py plan trade.yaml
$PY structured-derivative-pricer/scripts/price.py price trade.yaml --out result.json --set pricing.num_paths=100000
$PY structured-derivative-pricer/scripts/price.py inspect trade.yaml --out-dir diagnostics/inspect
```

A minimal spec ([`examples/vanilla_call.yaml`](./structured-derivative-pricer/examples/vanilla_call.yaml)):

```yaml
valuation_date: 2026-09-28
market_data: market/mock_market.yaml
model: {type: black_scholes, underlying: SPX, discount_curve: USD}
trade:
  id: CALL-SPX-1Y
  maturity: 1Y
  legs:
    - {id: call, type: option_payoff, underlying: SPX, option_type: call, strike: 1.0, notional: 1000000}
```

### Extending

Each component is a class registered with `@register(kind, type)` and a pydantic `Spec`
(`pricer/registry.py`). A new observable, leg, condition, event, model or analytic pricer is one class;
validation, the catalog and the graph pick it up automatically. Add a regression test in `tests/` against a closed
form or a limiting case. After adding components, refresh the docs page with
`$PY docs/build_catalog.py`. See the "Extending" section of
[`SKILL.md`](./structured-derivative-pricer/SKILL.md).
