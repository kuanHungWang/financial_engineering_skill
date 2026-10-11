# Term Sheet — Seasoned 1Y SPX Daily Range Accrual Note (priced mid-life)

> This note was traded on 10 July 2026. Price it **as of 9 October 2026**. Past SPX closes are
> in `market_data/spx_historical_closes.csv`.

| Item | Detail |
|---|---|
| Holder | Investor (long) |
| Notional | USD 2,000,000 |
| Trade / Strike / Effective Date | 10 July 2026 |
| Initial Level | SPX close on 10 July 2026 (5,000.00) |
| Maturity Date | 12 July 2027 |
| Valuation Date | 9 October 2026 |
| Coupon Periods | Quarterly: 10 Jul 2026 → 12 Oct 2026 → 11 Jan 2027 → 12 Apr 2027 → 12 Jul 2027; paid on each period end date |
| Coupon | 8.00% p.a. × (n / N) × Act/360 day fraction |
| n / N | n = NYSE trading days in the period on which the SPX close is between 90% and 110% of Initial Level (inclusive); N = NYSE trading days in the period |
| Redemption | 100% at maturity (capital protected) |
