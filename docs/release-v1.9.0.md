# v1.9.0 — pm-liquid：流动性优先的事件合约研究

Status: release notes for v1.9.0. Publication after explicit confirmation of repository and exact commit SHA.

- New skill **pm-liquid** (`skills/pm-liquid`): liquidity-first, read-only event-contract research for Polymarket and Kalshi.
  - Two-sided real order books (YES and NO tokens); Polymarket CLOB quotes override stale Gamma bestBid/bestAsk; Kalshi YES asks derived from real NO bids; NO-side books fetched for the top few by volume.
  - Executable-size gate: 20 shares complete fill, spread ≤5c, price band 10–90c by default. Closed markets (endDate/close_time past) are removed before the Top-N cut.
  - Longshot screen: default no <10c buys and no >90c chases, based on the full-sample Kalshi calibration pattern (<20c underperforms, >80c outperforms).
  - Maker-first execution notes; taker fees read per market (unknown ≠ zero); Kalshi fee shown as `?` until checked per series.
  - Volume accounting warnings: Kalshi $1 notional and combo inflation; Polymarket wash-trading history; 24h volume is annotated as context only, not an executability gate.
  - Cross-platform same-event matching (strict token overlap) plus `--query` for targeted two-platform reads.
  - 90-second order-book cache to avoid CLOB bursts; default scan about 40s cold and about 20s warm.
  - Offline tests: 9 passed.
- pm-edge (light update):
  - Added category routing: liquid sports/macro/politics → pm-liquid; weather, counts, awards and box office remain in pm-edge; a bare “scan” no longer defaults to weather.
  - Removed “未校准/未验证情景” wording from output guidance and README; market quotes are described as the sentiment baseline, model outputs as scenarios without claiming measured probabilities.
  - Added executable-liquidity and low/high-price discipline to the default section.
- Documentation: README now lists four skills, pm-liquid installation/examples/commands, and the pm-liquid asset name. English overview updated.
- Validation: pm-edge 82 tests; pm-liquid 9; trade 27 tests + 17 subtests; stock 28 tests + 7 subtests. All passed offline. Live smoke: pm-liquid default scan returned 264 candidates in 43s (93 executable, 171 observation) at the time of the run; this is interface availability, not a profitability claim.

Assets: pm-edge.zip, pm-liquid.zip, trade.zip, stock.zip, built from the tagged commit only, excluding caches, ledgers, credentials and local tool state.
