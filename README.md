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
| – | `cancel_claim(claim_id)` | claimant only, while `open`: bond credited back, outcome `cancelled` |
| – | `rescue_appeal(claim_id)` | anyone, 3 days after the appeal window if round 2 never ran: both bonds refunded |

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

## Failure handling

Unreachable pages, HTTP errors (status 400 and above), empty pages and binary files (mostly undecodable bytes) count as no evidence. If none of the pages is readable the LLM is not called and the verdict is `inconclusive`, so a bad evidence URL can never block a round or lock bonds. `cancel_claim` and `rescue_appeal` are escape hatches for claims that would otherwise stay stuck.

## Known limitations

- Only "supported" verdicts can be appealed.
- Treasury funds currently have no withdrawal path.
- Appeal window is 24 hours (`APPEAL_WINDOW_SECONDS`).
- Evidence from both sides is passed to the LLM in one block; page content is untrusted and prompt injection is not fully mitigated.
- Reputation counters can be farmed with cheap claims at the minimum bond.

## Testing

Tests run in GenLayer Direct Mode (in-memory, no Studio or Docker). Dependencies are pinned in `pyproject.toml`.

```
pip install genlayer-test==0.29.2
gltest tests/ -v
```

32 tests. They cover claim validation, round 1, appeal rules, all round 2 outcomes (upheld, overturned, refunded), reputation counters, finalize guards, withdrawals, unreadable evidence, cancellation and appeal rescue. Web and LLM calls are mocked so every validator returns the same verdict class.

## Verified on GenLayer Studio

The full lifecycle (before `cancel_claim`, `rescue_appeal` and the evidence handling above were added) was executed on Studio with two accounts: `submit_claim` -> `adjudicate` (supported) -> `appeal` from a second account -> `adjudicate_appeal` (supported). Final state: `status: final`, `outcome: upheld`, both bonds credited to the claimant.

## Deploy

Deploy `contracts/appealable_claim_adjudicator.py` in GenLayer Studio (no constructor arguments).

