# Term Sheet — 3Y USD CMS Steepener Range Accrual Note

| Item | Detail |
|---|---|
| Issuer | Mock Bank plc |
| Holder | Investor (long) |
| Notional | USD 5,000,000 |
| Trade / Valuation Date | 9 October 2026 |
| Effective Date | 14 October 2026 |
| Maturity Date | 14 October 2029 |
| Reference Spread | USD 10Y CMS rate − USD 2Y CMS rate (ISDA Fix, as quoted by QuantLib's USD swap index) |
| Coupon | 6.00% p.a. × n / N, paid quarterly, Act/360, Modified Following, NY calendar |
| n / N | n = number of NYSE business days in the period on which the Reference Spread ≥ 0.00%; N = total business days in the period |
| Redemption | 100% at maturity |
| Model | Hull-White 1F, calibrated to the USD ATM swaption vols |
