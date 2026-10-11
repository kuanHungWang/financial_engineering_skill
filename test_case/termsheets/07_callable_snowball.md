# Term Sheet — 3Y USD Callable Snowball Note (SOFR-linked)

| Item | Detail |
|---|---|
| Issuer | Mock Bank plc (holds the call) |
| Holder | Investor (long) |
| Notional | USD 10,000,000 |
| Trade / Valuation Date | 9 October 2026 |
| Effective Date | 14 October 2026 |
| Maturity Date | 14 October 2029 |
| Coupon Periods | Semi-annual, Act/360, Modified Following, NY calendar |
| First Coupon | 5.00% p.a. fixed (first period) |
| Subsequent Coupons | Coupon(k) = max( Coupon(k−1) + 2.50% − 3M Term SOFR fixed 2 business days before the start of period k, 0% ) |
| Issuer Call | Issuer may call in whole at 100% on any coupon date from 14 October 2027 (excl. maturity), 5 business days' notice; that date's coupon is paid |
| Redemption | 100% at maturity |
| Model | Hull-White 1F calibrated to USD ATM swaption vols |
| Note | If 3M Term SOFR is not available in the engine, use the closest SOFR-based rate index and state the substitution. |
