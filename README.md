[English](./README.md) | [繁體中文](./README.zh-TW.md)

# Structured Derivative Pricer — a Claude skill

A derivatives [Claude skill](https://docs.claude.com/en/docs/claude-code/skills) designed for professional
institutional investors. It builds pricing models for vanilla and exotic derivatives and structured products. Once
the skill is installed, you only describe the product terms. The AI picks the matching model components (model,
curve, calibration, payoff, early termination and so on) and prices the trade by Monte Carlo simulation when there
is no closed form.

The skill does not build a pricing model by generating code. It uses a factory design pattern: most of the common
components (curve, model, calibration, schedule, market convention, Monte Carlo, Longstaff-Schwartz and more) are
built in advance, and a full pricing workflow is assembled from them. Every pricing model is expressed in the end
as one YAML file. This avoids the "generate code → run → debug" loop entirely, so results come out consistently.

---

## Installation

### Requirements

- [Claude Code](https://docs.claude.com/en/docs/claude-code) (or another Claude client that supports skills)
- Python 3 (3.11 recommended), or [uv](https://github.com/astral-sh/uv), which fetches Python 3.11 itself
- Network access on first use, to install QuantLib, pydantic, PyYAML, NumPy and pandas

### Option 1: plugin marketplace (recommended)

In Claude Code, add this repository as a marketplace and install the plugin:

```text
/plugin marketplace add kuanHungWang/financial_engineering_skill
/plugin install financial-engineering@financial-engineering-skill
```

Or the same from your shell:

```bash
claude plugin marketplace add kuanHungWang/financial_engineering_skill
```

```bash
claude plugin install financial-engineering@financial-engineering-skill
```

Restart Claude Code (or run `/reload-plugins`). The skill appears as
`financial-engineering:structured-derivative-pricer`, and Claude uses it on its own when you ask to price a product.
To get a newer version later:

```bash
claude plugin marketplace update financial-engineering-skill
```

```bash
claude plugin update financial-engineering@financial-engineering-skill
```

### Option 2: copy the skill folder

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

### Option 3: other agents

The skill follows the [Agent Skills](https://agentskills.io) standard, so installers such as
[skills.sh](https://skills.sh) can add it to other coding agents too:

```bash
npx skills add kuanHungWang/financial_engineering_skill
```

### Set up the Python environment (optional)

The skill runs `scripts/setup.sh` itself the first time it is used. It creates a `.venv` inside the skill folder
with QuantLib and the other requirements, and prints the path of its Python. It is idempotent: running it again is
a no-op. To do it ahead of time with a copied folder (option 2), and check the install:

```bash
PY=$(~/.claude/skills/structured-derivative-pricer/scripts/setup.sh)
$PY -m pytest ~/.claude/skills/structured-derivative-pricer/tests -q
```

---

## What it can do

### 1. Build and validate pricing models

Give Claude a term sheet or a product description and ask for a fair value, NPV, MTM or a pricing model. It works
in English and Chinese (e.g. "結構型商品評價", "公平價值"). Claude reads the terms, asks about anything ambiguous
(barrier observation frequency, who holds a call right, fixing conventions), converts your market data, and writes
the trade spec. The engine checks the whole spec before running, returning every error at once, then shows the
plan (closed form or Monte Carlo, and why) before pricing.

#### 1.1 Supported asset classes, models, payoffs and early termination

| | Supported |
|---|---|
| **Asset classes** | equity (single or several correlated underlyings) · interest rates (term rates, swap rates / CMS, overnight rates such as SOFR) |
| **Models** | `black_scholes` (constant, term or Dupire local vol) · `heston` (single asset, given or calibrated to the vol surface) · `hull_white` 1F rates (given, or calibrated to ATM swaptions: coterminal, full matrix or listed points) |
| **Observables** | equity level or performance · term rate · CMS · overnight rate · `linear` combinations (spreads, baskets, inverse-floater formulas) · `min_of` / `max_of` (worst-of, best-of) |
| **Payoffs (legs)** | fixed · floating (in advance, in arrears, compounded in arrears with lockout; gearing, spread, cap, floor) · range accrual (any number of ranges on any observables) · option payoff · fixed amount · `path_dependent` (a state carried between periods: snowball, memory / phoenix coupon, ratchet, cliquet, lookback, cumulative cap) · kick-in and knock-out conditions on any leg |
| **Early termination** | knock-out (autocall) · target redemption (TARN) · Bermudan exercise (issuer call, holder put, cancellation, Bermudan option) by Longstaff-Schwartz |
| **Closed forms** | European vanilla (BSM, Heston) · single continuous barrier (BSM) |

Worked examples in [`structured-derivative-pricer/examples/`](./structured-derivative-pricer/examples/):
vanilla call, up-and-out call, EKIAKO put (BSM and Heston), phoenix memory autocall, callable snowball,
Bermudan put, callable fixed-rate bond (given and calibrated Hull-White), cancellable swap, CMS spread range
accrual, CMS inverse floater in arrears, dual-equity range accrual, fixed-then-range-accrual hybrid, inverse
floater TARN, SOFR FRN with lockout, seasoned range accrual note.

#### 1.2 What a model outputs

Every price comes with what you need to trust it:

- method and why (analytic or MC), price, standard error and confidence interval,
- PV per leg, knock-in / knock-out / autocall / exercise probabilities, expected cashflows per date,
- a cross-check against a closed form when MC ran and one applies,
- a martingale check of the simulated forward against the curve,
- the Bermudan in-sample vs out-of-sample gap,
- Hull-White calibration fit and Heston calibration RMSE,
- warnings such as vol-surface extrapolation.

On request (UAT, model review), `price.py inspect` re-prices the trade exactly (same seed, same price) and writes
the output of every step to disk: paths, fixings, cashflows, discount factors, exercise state and regression
coefficients, as CSV and JSON.

### 2. Write validation reports and explain price changes

`report.py evidence` runs the tests a reviewer expects (convergence, seed stability, sensitivities, stress,
benchmarks, limiting cases, reference prices) and collects the material. Claude then writes the report from that
material, in your template or the SR 11-7 outline in
[`templates/sr11-7.md`](./structured-derivative-pricer/templates/sr11-7.md). Every number is copied from the
evidence, never computed by the AI.

To explain why a price moved, the intermediate results of two runs with the same seed (for example two market
dates) can be compared step by step, and the sensitivities and stress tests show which inputs drive the price.

### 3. Price inventory trades and reuse validated models

A trade can be priced at any date of its life, mid-period included. Past fixings come from a shared fixings
file; future ones from the simulation. A spec is written once and re-priced by changing only the valuation date
and the market data.

When a new trade has the same structure as one already validated, Claude starts from the validated trade file and
changes only its term-sheet fields (dates, notional, strike, coupon, barriers, call schedule), keeping the model,
calibration and pricing settings. It shows you the diff before pricing.

### 4. Export the model to run without AI

After the first price, Claude offers to export the engine into your project (`export_engine.py`). The copy
re-prices with no AI and no skill installed: pinned package versions (`requirements.lock.txt`), recorded baseline
prices, and `baseline.sh verify` to detect when a re-price no longer matches and why (changed inputs, edited
files, library drift). Engine upgrades re-verify every baseline and archive the old version.

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

## Pricing engine: QuantLib

The skill uses [QuantLib](https://www.quantlib.org/) as its pricing engine. On top of the curves, calendars, day
counts, market conventions, rate indices (`Sofr`, `Euribor`, `EuriborSwapIsdaFixA`, ...), stochastic processes and
model calibration, and closed-form engines that QuantLib provides, it builds a pricing wrapper layer. Through the
factory design pattern and a building-block workflow, a single command defines the components and chains them
together.

---

## Disclaimer

This project is for research and educational purposes only. It is not investment, financial, legal or tax
advice, and nothing it produces is a recommendation to buy or sell any security or derivative. Prices, Greeks
and validation reports it generates depend on the model, market data and assumptions supplied, and may be wrong.
Validate them independently before relying on them for trading, risk management, accounting or regulatory
purposes. The software is provided "AS IS", without warranty of any kind, as set out in the
[Apache License, Version 2.0](./LICENSE).

---

## License

Licensed under the [Apache License, Version 2.0](./LICENSE). See [NOTICE](./NOTICE) for attribution. QuantLib
is a separate dependency under its own modified BSD license.
