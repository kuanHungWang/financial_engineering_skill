# Market data format

One YAML (or JSON) document. Point to it from the trade spec with `market_data: <path>`, or
inline it under `market_data:`. Exact fields: `price.py catalog curve` and `price.py catalog vol`.

```yaml
as_of: 2026-09-28                 # should equal the trade's valuation_date (warning otherwise)
curves:                           # discount / funding curves, referenced by name from the model
  USD:
    type: bootstrap               # QuantLib piecewise bootstrap with the currency conventions
    currency: USD                 # USD | EUR | JPY | GBP | TWD
    deposit: [{tenor: 3M, rate: 0.0425}, {tenor: 6M, rate: 0.0415}]
    swap:    [{tenor: 1Y, rate: 0.0395}, {tenor: 5Y, rate: 0.0360}]
  USD-ZERO:
    type: zero                    # continuously-compounded zero rates
    tenors: [6M, 1Y, 2Y, 5Y]
    rates:  [0.042, 0.040, 0.038, 0.037]
  USD-FLAT: {type: flat, rate: 0.04}
equities:
  SPX:
    spot: 5000.0
    calendar: UnitedStates.NYSE   # used for Heston calibration dates
    dividend: {type: flat, rate: 0.015}   # any curve type; continuous dividend yield
    vol:
      type: surface               # or {type: constant, value: 0.2} / {type: term, tenors, vols}
      strike_mode: relative       # strikes as fraction of spot; 'absolute' for price levels
      tenors:  [1M, 3M, 6M, 1Y, 2Y]
      strikes: [0.8, 0.9, 1.0, 1.1, 1.2]
      vols:                       # vols[i][j] = implied vol at tenors[i], strikes[j]
        - [0.26, 0.22, 0.19, 0.17, 0.16]
        - ...
```

## Converting user data

- **Tenors** are strings like `1W`, `3M`, `1Y`. Convert "3 months" or "0.25y" to `3M`.
- **Rates and vols are decimals**: 4.25% is `0.0425`. Check that no value above 1 slipped
  through as a percentage.
- **Vol surface layout**: one row per tenor, one column per strike. If the user's table has
  strikes as rows, transpose it. Moneyness in % (80, 90, 100) becomes `strike_mode: relative`
  with 0.8, 0.9, 1.0.
- **Missing dividend**: ask. Using 0 overprices calls and underprices puts. If the user accepts
  0, state it.
- **Time-to-expiry from a date grid**: convert expiry dates to tenors from `as_of`, or ask for
  tenors.
- **Coverage**: the vol surface and the curve should reach the trade maturity. Otherwise
  validation warns that flat extrapolation is used. Pass the warning on to the user.

## Behaviour to know about

- A curve named only by a rate observable (e.g. a forecast curve different from the discount curve) is
  built too. Under `hull_white` it moves with the model factor (deterministic basis to the discount curve).
- Past index fixings are not market data files: they go in the observable's `history` in the trade spec.
- `bootstrap` curves: deposits use the currency's money-market day count (Act/360; Act/365F for
  GBP, TWD) with 2 settlement days; swaps are annual 30/360 fixed against a 6M floating index on
  the currency's calendar. The curve is on Actual/365F. Beyond the last quote it extrapolates
  with its `method` (`logLinearDiscount` = flat forward; `linearZero` continues the last zero-rate
  slope), so quote tenors should reach the longest date the trade reads, including CMS tenors.
- Curves whose day count is not Actual/365F (e.g. a `flat` or `zero` curve on 30/360) are
  re-expressed on Actual/365F for simulation. Discount factors are unchanged on every date,
  and the result carries a note saying so.
- Vol surfaces use bicubic interpolation and flat strike extrapolation, which keeps the Dupire
  local vol well-defined. Where local variance is still negative, the engine floors it at ATM
  vol and warns with the fraction of the grid affected. That usually means butterfly or
  calendar arbitrage in the quotes.
- Mock data: `scripts/make_mock_market.py OUT.yaml --as-of <date> [--ticker SPX --spot 5000]`
  writes synthetic data labelled as mock. Use it only for demos and tests.
