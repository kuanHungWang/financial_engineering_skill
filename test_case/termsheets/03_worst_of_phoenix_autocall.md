# Term Sheet — 2Y Worst-of Phoenix Autocallable Note on SPX / NDX

**Issuer:** Mock Bank plc  **Currency:** USD  **Denomination / Notional:** USD 1,000,000
**Trade Date / Strike Date / Valuation Date:** 9 October 2026  **Issue Date:** 16 October 2026
**Maturity Date:** 18 October 2028

**Underlyings:** S&P 500 (SPX), Nasdaq-100 (NDX). Initial Levels = closing levels on Strike Date
(SPX 4,950.10, NDX 20,310.80).
**Worst Performance** on a date = min over underlyings of (Closing Level / Initial Level).

**Observation Dates:** quarterly, on the 9th of Jan, Apr, Jul, Oct, from 9 Jan 2027 to 9 Oct 2028
(8 observations, modified following, NYSE). Each payment date is 5 business days after the observation.

**Conditional Coupon (memory):** 2.25% per quarter (9% p.a.). On an observation date, if the Worst
Performance ≥ 70% (Coupon Barrier), the note pays 2.25% × Notional **plus** all coupons previously
missed and not yet paid.

**Autocall:** On every observation date except the last, if the Worst Performance ≥ 100%
(Autocall Trigger), the note redeems early at 100% of Notional (plus that date's coupon). No further
payments.

**Redemption at Maturity** (if not autocalled):
- If the Worst Performance on the final observation date ≥ 60% (Knock-In Barrier, observed at
  final date only): 100% of Notional.
- Otherwise: Notional × Worst Performance.

**Correlation SPX/NDX:** assume 0.85.
