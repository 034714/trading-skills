# v1.8.0 — Stock research freshness and three-skill documentation

Status: prepared notes only; no tag or GitHub Release created. Proposed publication: draft=false, after explicit confirmation of repository and exact commit SHA.

- Stock: retain and publish verified installed-skill updates for contract discovery filters, 1D/4H/1H source timestamps and stale-data flags, US DST boundaries, structural targets and conditional-card expiry.
- Stock: explicitly mark news, macro and earnings as not checked; holidays and early closes remain unverified. Correct example risk units and avoid assuming missing contract multipliers.
- Documentation: clarify the distinct read-only roles of pm-edge, trade and stock; installation, Python requirements, snapshot freshness and manual event checks; add an English overview.
- Prediction markets: document equal-size executable ASK+ASK pairing after fees, non-atomic execution risk, and uncalibrated weather estimates. Remove the zero-fee weather example. Installed pm-edge matches the repository; no new model capability is claimed.
- Trade: correct the outdated statement that funding/OI/account-ratio snapshots are not fetched; IV versus RV remains descriptive, not a trading signal.
- Offline validation: stock 28 tests plus 7 subtests; trade 27 tests plus 17 subtests; pm-edge 46 tests. All passed. No live API or profitability validation performed.

Proposed assets: pm-edge.zip, trade.zip, stock.zip, built from the approved commit only and excluding caches, ledgers, credentials and local tool state. Assets are not published by this change.
