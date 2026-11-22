# Appealable Claim Adjudicator

A GenLayer Intelligent Contract for evidence-based claim adjudication with bonds, an appeal window and a second round of consensus.

Contract-only repository: no frontend.

## Purpose

Anyone can submit a factual claim with an https evidence URL and a bond. GenLayer validators judge the claim from the evidence. A challenger can appeal a "supported" verdict by posting a matching bond, which triggers a second round using both sides' evidence.

## Lifecycle

| Step | Function | Result |
|---|---|---|
| 1 | `submit_claim(statement, evidence_url)` payable | status `open`, claimant bond held |
| 2 | `adjudicate(claim_id)` | round 1 verdict; `provisional` (appeal window opens) or `final` refund if inconclusive |
| 3 | `appeal(claim_id, appeal_evidence_url)` payable | status `appealed`, appellant bond held |
| 4 | `adjudicate_appeal(claim_id)` | round 2: bonds go to the winner, or both are refunded |
| 5 | `finalize(claim_id)` | after the window with no appeal: refund if "supported", bond forfeited to treasury if "refuted" |
| 6 | `withdraw()` | pull-payment of credited balance |

## How consensus is used

The leader fetches the evidence and asks an LLM for a JSON verdict. Each validator re-runs the same check and must agree on the **verdict class only** (`supported`, `refuted`, `inconclusive`). Free-text reasoning is never compared, so consensus binds one consequential outcome.

## Outcomes in round 2

- Same verdict as round 1: round 1 is upheld, claimant takes both bonds.
- Different verdict: round 1 is overturned, appellant takes both bonds.
- `inconclusive`: both bonds are refunded.

## State design

- `claims`: full record of each claim, both rounds and the outcome.
- `balances`: withdrawable funds (pull-payments, zeroed before transfer).
- `reputation` / `strikes`: per-address counters of favorable and unfavorable final outcomes.
- `treasury`: bonds forfeited by refuted, unchallenged claims.

## Known limitations

- Only "supported" verdicts can be appealed.
- Treasury funds currently have no withdrawal path.
- Appeal window is 24 hours (`APPEAL_WINDOW_SECONDS`).

## Deploy

Deploy `contracts/appealable_claim_adjudicator.py` in GenLayer Studio (no constructor arguments).
