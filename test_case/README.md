# Smoke test cases for `structured-derivative-pricer`

Simple run-through cases: a term sheet + mock market data each. No expected values — the goal is only
to see that the skill installs, writes a spec, validates, plans and prices without falling over.

**All market data here is MOCK (synthetic). Valuation date for every case: 9 October 2026.**

## Layout

```
termsheets/                      natural-language term sheets (what a user would paste)
market_data/                     raw mock data, deliberately NOT in the skill's YAML format,
                                 so the skill's conversion step is exercised too
  usd_curve.csv                    deposits + swaps, rates in %
  equity_spot_dividend.csv         SPX / NDX spot, dividend yield in %
  spx_vol_surface.csv              implied vol in %, rows = tenor, cols = moneyness
  ndx_vol_surface.csv              same layout
  usd_swaption_atm_normal_vol_bp.csv  normal vols in bp, rows = expiry, cols = swap tenor
  spx_historical_closes.csv        SPX closes 2026-07-10 .. 2026-10-08 (for case 06)
```

## Cases

| # | Term sheet | Exercises | Market data needed |
|---|---|---|---|
| 01 | European call on SPX | analytic BSM path | curve, SPX spot/div/surface |
| 02 | Up-and-out call, daily close barrier, rebate | MC, daily `1D` schedule, knock-out | curve, SPX |
| 03 | Worst-of SPX/NDX phoenix memory autocall | multi-asset, memory coupon, autocall, KI at maturity | curve, SPX, NDX, correlation 0.85 |
| 04 | 5NC1 callable fixed rate note | Hull-White calibration, Bermudan issuer call | curve, swaption vols |
| 05 | CMS 10Y−2Y spread range accrual | CMS observables, `linear`, range accrual under HW | curve, swaption vols |
| 06 | Seasoned SPX daily range accrual | past fixings / seasoned pricing | curve, SPX, historical closes |
| 07 | Callable snowball | `path_dependent` state + Bermudan call | curve, swaption vols |

## Suggested prompts (in the folder where the skill is installed)

Copy this `test_case/` folder into the test project, then for example:

- "Price `termsheets/01_spx_european_call.md` using the market data in `market_data/`."
- "Price the up-and-out call in `termsheets/02_spx_up_and_out_call.md`. Market data is in `market_data/`."
- "Here is a term sheet: `termsheets/03_worst_of_phoenix_autocall.md`. What is its fair value? Use `market_data/`."
- "Value the callable bond in `termsheets/04_callable_fixed_rate_bond.md`, calibrating Hull-White to the swaption vols."
- "Price `termsheets/05_cms_spread_range_accrual.md`."
- "Price the seasoned note in `termsheets/06_seasoned_spx_range_accrual.md` as of 2026-10-09; past closes are in `market_data/spx_historical_closes.csv`."
- "Price `termsheets/07_callable_snowball.md`."
- After any successful price: "Show me the intermediate results" (inspect) or "Write an SR 11-7 validation report for it" (report).

Things worth watching while it runs: does it ask about barrier observation / call rights before writing the
spec, convert % and bp to decimals, flag that the data is mock, and report warnings (e.g. vol surface or
curve extrapolation).
