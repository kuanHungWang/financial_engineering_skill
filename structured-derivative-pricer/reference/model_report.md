# Model documentation and validation report

`scripts/report.py` writes a report on one trade's pricing model. The layout comes from a **template**: the user's
own format when they give one (see "Templates"), otherwise the default `templates/sr11-7.md`, structured after the
Federal Reserve / OCC *Supervisory Guidance on Model Risk Management* (SR 11-7, 2011). The evidence is the same
either way. The default covers SR 11-7's two technical pillars:

- **model development, implementation and use**: purpose, product, theory and method, data, assumptions,
  limitations, implementation checks;
- **model validation**: conceptual soundness (incl. sensitivity and stress), ongoing monitoring (process
  verification, benchmarking), outcomes analysis.

**Out of scope: governance, policies and controls** (roles, ownership, model inventory, risk tiering, approval,
independence of validators, board reporting, issue tracking, exceptions). They differ between organisations. Do not
write them and do not invent a rating or an approval. The default template has no such sections. When a user's
template has them, they become `{{manual:...}}` slots: filled only with what the user supplies, otherwise left as a
visible placeholder for the organisation.

## Three steps

```bash
$PY <skill_dir>/scripts/report.py evidence INPUT --out-dir DIR [--run-tests]   # numbers: DIR/evidence.json
$PY <skill_dir>/scripts/report.py narrative DIR [--template T.md]            # skeleton: DIR/narrative.yaml
$PY <skill_dir>/scripts/report.py render DIR                                  # DIR/report.md
```

- **evidence** prices the trade and its variants with the engine and writes every result to `evidence.json`.
  `INPUT` is the trade YAML (all tests at defaults) or a **report config** (below). It prints only a summary:
  `status_counts` and `attention` (every WARN / FAIL). Exit 1 if a test FAILED, 2 for bad input. It takes about
  30 pricings: seconds for a fast trade, longer for a large Bermudan; it reports progress on stderr.
  `--run-tests` also runs the engine's test suite (about a minute) and records the result.
- **narrative** writes `narrative.yaml` for the template's slots: one block per section a person must write, with
  guidance as comments, and marks which evidence sections need commentary because a test there is WARN or FAIL.
  `--template` saves the user's template as `DIR/template.md`, which `render` then uses. It never overwrites
  (`--force`). It also lists `evidence_not_in_template`.
- **render** merges template, evidence and narrative into `report.md` and prints `missing_narrative`,
  `unresolved_tokens`, `manual_placeholders` and `evidence_not_in_template`. It exits 1 until the first two are
  empty; the report still renders, with visible `[TO BE COMPLETED ...]` markers.

Put `DIR` where the user wants the report (e.g. `reports/<trade id>/`), not in the skill directory.

## Workflow

1. **Price first** with the normal workflow (validate, plan, price). Resolve or understand every warning. A report
   on a trade that does not price correctly is worthless.
2. **Ask the user** (one short message) what they have for:
   - the **purpose and use** of the model (valuation for the books, independent price verification, client
     pricing, risk), and who uses it;
   - **reference prices** for outcomes analysis: counterparty / issuer marks, dealer or vendor quotes, a previous
     model's price, realised settlement amounts. Never invent them; with none, the section says none were supplied;
   - the **data sources** of each market input and of the fixings;
   - their own **report template** or format, if they have one (a Word / PDF / Markdown document, or a list of
     sections). Without one, use the SR 11-7 default. See "Templates".
3. **Design the tests** in a report config (below): pick benchmarks and limiting cases that test *this* product's
   risk drivers, not generic ones. Keep the trade's own seed.
4. **Run evidence.** Read `attention`. Investigate every WARN / FAIL before writing about it: re-run with more
   paths, `price.py inspect`, check the market data. A FAIL in a case you designed may be a wrong expectation;
   fix the config only if the expectation was wrong, and say so in the commentary. Never loosen a tolerance to
   turn a FAIL into a PASS without saying why.
5. **Write the narrative** (rules below), then **render**, and repeat until `ok: true`. Tell the user which
   `manual_placeholders` remain for their organisation to fill, and any `evidence_not_in_template`.
6. **Deliver** `report.md`: the price with its CI, the test counts, every WARN / FAIL in one line each, and what
   the narrative concludes. Offer once to turn it into another format (Word, PDF, a shareable page) with the
   tools available; the Markdown stays the source.

## Templates

A template is Markdown: the user's headings, order and fixed wording, with tags where content goes. List the tags
and blocks with `report.py blocks`.

| tag | becomes |
|---|---|
| `{{block:NAME}}` | an evidence table or list (`key_results`, `test_summary`, `components`, `engine_method`, `calibration`, `model_diagnostics`, `market_data`, `input_files`, `limitations`, `run_warnings`, `implementation`, `convergence`, `sensitivity`, `stress`, `benchmarking`, `outcomes`, `cases`, `findings`, `trade_spec`, `cashflows`, `environment`, `header`, `mock_warning`) |
| `{{narrative:KEY "guidance"}}` | text written under `KEY` in `narrative.yaml`; required. Guidance defaults to the heading above |
| `{{commentary:SECTION "guidance"}}` | comment on an evidence section (`implementation`, `convergence`, `sensitivity`, `stress`, `benchmarking`, `outcomes`, `cases`, `methodology`); required when a test there is WARN / FAIL |
| `{{manual:KEY "guidance"}}` | organisation-supplied content (approvals, ownership, tiering, policy references); from `narrative.yaml` `manual:` if the user gave it, else a visible placeholder |
| `{{path\|format}}` | one value from `evidence.json`, e.g. `{{key.price\|,.2f}}` |

**Turning a user's format into a template.** Read their document (a .docx or .pdf with the matching skill,
Markdown or plain text directly), then write `report_template.md` beside the report folder:

1. Keep their section titles, numbering, order and boilerplate wording verbatim.
2. For each section decide what fills it: an evidence block when the section reports results (a "Sensitivity" or
   "Testing" section gets `{{block:sensitivity}}`, a "Model inputs" section `{{block:market_data}}`), a narrative
   slot when it asks for explanation, `{{manual:...}}` when only the organisation can write it (governance,
   approvals, ratings, sign-offs, distribution lists). Put the section's own instructions into the tag's guidance.
3. Add `{{commentary:...}}` next to each evidence block, so a WARN / FAIL has a place to be discussed; `render`
   insists on commentary for every WARN / FAIL section even without a slot.
4. Tests their template does not ask for still run. `render` lists core evidence the template leaves out
   (`evidence_not_in_template`). Ask the user whether to add it, e.g. as an appendix; do not drop a WARN / FAIL
   silently. A mock-data warning is added at the top whenever the data is mock, whatever the template says.
5. Show the user the mapping (their section -> block / narrative / manual) in a short table before writing the
   narrative, and adjust if they disagree.

Then `report.py narrative DIR --template report_template.md`. If the user wants the result in their original file
format, convert the rendered Markdown with the matching skill (e.g. docx) and their document's styles; the Markdown
remains the source.

## Report config

Every key but `trade` is optional. Paths are relative to the config file.

```yaml
trade: trade_acn.yaml
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
    trade: trade_acn_heston.yaml        # a full trade file, or `set:` overrides of the base trade
    expect: {approx: base, tol_se: 4}   # optional; without it the row is INFO

cases:                                  # limiting / boundary cases with an expected outcome
  - label: Autocall never triggers
    rationale: with the autocall barrier unreachable the note is a 2Y coupon note with a KI put
    set: [trade.termination_events.0.barrier=100]
    expect: {lt: base}
  - label: Matches the vanilla it collapses to
    trade: case_vanilla_equivalent.yaml
    expect: {approx: benchmark:Heston}  # rhs: a number, base, case:LABEL or benchmark:LABEL (earlier entries)

references:                             # outcomes analysis: prices from outside the model
  - {label: Issuer mark, price: 1001500, source: Issuer, date: 2026-09-28, tolerance_pct: 0.01}
  - {label: Vendor, price: 1004200, source: Vendor X, date: 2026-09-28, tolerance: 2500}
```

`expect` takes one of `approx`, `le`, `ge`, `lt`, `gt`, and a tolerance: `tol` (absolute) or `tol_se` (in combined
standard errors, default 4). Inequalities get the same statistical slack. Reference prices must be in the trade's
units (currency amount on the trade's notional, holder's sign); convert a quote in % of notional before entering it.

The market is inlined into the trade before the variants run, so `set` can change market data too:
`market_data.equities.SPX.vol={type: constant, value: 0.0001}` (near-deterministic case),
`market_data.equities.SPX.spot=6000`.

## Tests and their SR 11-7 role

Section numbers below are the default template's.

| Report section | SR 11-7 element | Evidence (`tests.` key) | Status rule |
|---|---|---|---|
| 4 Methodology | conceptual soundness | method, model, graph, `implementation.calibration` | WARN if a calibration instrument misses its vol by > 5% |
| 6 Assumptions and limitations | limitations | `known_limitations` (from model and method) + narrative | - |
| 7 Implementation | process verification | `implementation`: validation, reproducibility (same seed twice), martingale check, zero bump, exercise policy in/out of sample; `unit_tests` | FAIL if not reproducible, martingale \|z\| >= 4, a zero bump or pinning the reference levels moves the price, a test fails; WARN on validation warnings or an exercise-policy gap > 4 SE |
| 8 Convergence | conceptual soundness (numerical) | `convergence`, `seed_stability`, `precision` | WARN if a level is > 4 SE from the largest, the std-error slope is outside -0.65..-0.35, or seed dispersion / SE is outside its 99% band |
| 9 Sensitivity | sensitivity analysis | `sensitivities`: P+, P-, first / second order with their SE (common random numbers), delta / gamma per unit | INFO; FAIL if a bump cannot be priced. `significant: no` = within 2 SE of zero |
| 10 Stress | stress testing | `stress` | INFO; FAIL if a scenario cannot be priced. A default scenario this market cannot take (e.g. vol -5pt on a surface with a vol below 5%) is skipped |
| 11 Benchmarking | benchmarking | engine closed-form cross-check, `benchmarks` | per `expect`; cross-check FAIL beyond 4 SE |
| 12 Outcomes | outcomes analysis | `references` | per tolerance, else INFO |
| 13 Limiting cases | conceptual soundness (testing) | `cases` | per `expect` |
| 14 Ongoing monitoring | ongoing monitoring | narrative + reproduction note | - |

Spot bumps and stresses keep the trade's **initial fixings fixed**: `evidence` pins `trade.reference_levels` to the
values the base run used, so relative strikes and barriers stay where they are (otherwise a new trade's levels
would move with spot and delta would be zero). The zero-bump check confirms the pinned trade prices as the original.
A vol surface with `strike_mode: relative` is sticky-moneyness under a spot bump, `absolute` sticky-strike; say
which in the sensitivity commentary.

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

Build an equivalence case as a separate trade file next to the config (start from the base trade, change one
feature) and compare with `approx`. For a relation (callable <= straight), use `le` / `ge`. State the expected
direction from the **holder's** view, as the engine signs prices.

## Writing the narrative

`narrative.yaml` keys: `executive_summary`, `purpose_and_use`, `product_description`, `methodology`, `data`,
`assumptions_limitations`, `ongoing_monitoring`, `conclusion` (all required), `findings` (a list; write `[]` for none: left empty, the report is incomplete),
`commentary.<section>` (`implementation`, `convergence`, `sensitivity`, `stress`, `benchmarking`, `outcomes`,
`cases`, `methodology`), required for a section with a WARN or FAIL test and recommended where a reader needs
interpretation. A user template has its own keys (whatever its `{{narrative:...}}` tags name) and `manual:` entries.
Markdown is allowed inside a block.

- **Numbers come from evidence, through tokens**: `{{key.price|,.2f}}`, `{{key.ci_low|,.0f}}`,
  `{{tests.seed_stability.ratio|.2f}}`, `{{tests.sensitivities.rows.1.first_order|,.0f}}`,
  `{{base.events.autocall.first_termination_probability|.1%}}`, `{{tests.stress.rows.0.change_pct|.1%}}`. The
  path follows `evidence.json`; the format is a Python format spec. Typing a computed number by hand is not
  allowed; term-sheet facts (notional, dates, barrier levels) may be written directly.
- **Say what the evidence says.** A WARN or FAIL is reported as such, with its cause if found and its impact on
  the price. Do not describe a test as passed that was not run (e.g. the unit tests without `--run-tests`).
- **Mock data**: if `key.mock_market_data` is true, the executive summary and conclusion say the numbers are
  illustrative only.
- **Limitations**: add the product-specific ones to the engine-derived list (which the report already shows; do not
  repeat it): e.g. coupon-on-memory wording, settlement lags not modelled, issuer credit not in the discount curve,
  no funding / XVA, observation-date conventions. Give the direction of the bias where known.
- **Methodology** explains *why* the model fits the product's risk drivers (e.g. local vol reprices the smile the
  KI put depends on; one-factor HW is adequate for a callable fixed-rate bond but not for a CMS spread), and the
  alternatives considered (refer to the benchmarks).
- **Ongoing monitoring**: what to rerun and when (each valuation: `price.py price`; after an engine change:
  `baseline.py verify` of an export; periodically: this report), what triggers review (FAIL, references out of
  tolerance, calibration misses, the market moving to where stresses showed large losses).
- **Conclusion**: fitness for the stated purpose and any conditions (e.g. "use with at least 50,000 paths"). No
  approval, rating or sign-off: that is governance.
- **Findings**: `{id, title, description, impact, recommendation}`, neutral and specific.

## Reading `evidence.json`

| key | content |
|---|---|
| `key` | headline numbers: price, std_error, ci_low / ci_high, method, model, num_paths, seed, mock_market_data |
| `summary`, `status_counts` | every test with section, status, one-line detail |
| `base` | the full pricing result (`price.py price` output) |
| `plan` | `price.py plan` output: method, graph, simulation dates |
| `input` | the trade as priced (overrides applied, reference levels pinned, market file named) |
| `market` | summary of curves, equities, swaption vols |
| `known_limitations` | engine-derived limitations for this model and method |
| `tests.*` | the evidence of each section, as in the table above |
| `files` | every input file with its SHA-256 |
| `environment` | Python, library versions, engine code hash, git commit / export version |
