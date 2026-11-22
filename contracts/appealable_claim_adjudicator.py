# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
from dataclasses import dataclass
import datetime
import json
import typing

VERDICTS = ("supported", "refuted", "inconclusive")
APPEAL_WINDOW_SECONDS = 24 * 60 * 60
MIN_BOND = 1_000_000_000_000_000  # 0.001 GEN
MIN_STATEMENT_LEN = 20
# After the appeal window plus this delay, a stuck appeal can be rescued.
RESCUE_DELAY_SECONDS = 3 * 24 * 60 * 60

# Used to send GEN out of the contract. Only emit_transfer() is called.
@gl.evm.contract_interface
class _EOA:
    class View:
        pass

    class Write:
        pass


@allow_storage
@dataclass
class Claim:
    claimant: Address
    statement: str
    evidence_url: str
    bond: u256
    status: str  # open | provisional | appealed | final
    round1_verdict: str
    appellant: Address
    appeal_evidence_url: str
    appeal_bond: u256
    round2_verdict: str
    appeal_deadline: u64
    outcome: str  # "" | upheld | overturned | refunded | forfeited | unchallenged | cancelled


def _now() -> int:
    return int(datetime.datetime.now().timestamp())


class AppealableClaimAdjudicator(gl.Contract):
    """
    Evidence-based claim adjudication with an appeal lifecycle.

    Lifecycle:
      submit_claim  -> status "open"       (claimant posts a bond)
      adjudicate    -> status "provisional" (round 1 verdict, appeal window opens)
      appeal        -> status "appealed"   (challenger posts a matching bond)
      adjudicate_appeal -> status "final"  (round 2 settles who keeps the bonds)
      finalize      -> status "final"      (window expired without appeal)

    Consensus binds ONLY the verdict class (supported / refuted /
    inconclusive). Free-text reasoning is never compared, so validators
    converge on a single consequential outcome. Money moves only through
    pull-payments (withdraw), never during adjudication.
    """

    claims: TreeMap[u32, Claim]
    next_id: u32
    balances: TreeMap[Address, u256]  # withdrawable winnings and refunds
    reputation: TreeMap[Address, u32]  # claims upheld in the claimant's favor
    strikes: TreeMap[Address, u32]  # claims forfeited or overturned
    treasury: u256

    def __init__(self):
        self.next_id = u32(0)
        self.treasury = u256(0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _credit(self, who: Address, amount: u256) -> None:
        if amount == u256(0):
            return
        current = self.balances[who] if who in self.balances else u256(0)
        self.balances[who] = u256(current + amount)

    def _bump(self, book: TreeMap[Address, u32], who: Address) -> None:
        current = book[who] if who in book else u32(0)
        book[who] = u32(current + 1)

    def _judge(self, statement: str, urls: list) -> str:
        """Runs one adjudication round and returns a verdict class."""
        prompt = f"""You are adjudicating a factual claim using ONLY the evidence provided.

CLAIM: {statement}

Respond ONLY in this JSON format:
{{
  "verdict": str,   // "supported" if the evidence clearly shows the claim is true,
                    // "refuted" if the evidence clearly shows it is false,
                    // "inconclusive" if the evidence is missing, unrelated or ambiguous
  "reasoning": str  // one or two sentences citing the evidence
}}
Never guess: if the evidence does not clearly address the claim, answer "inconclusive".
This result must be perfectly parsable by a JSON parser without errors.
"""

        def leader_fn():
            blocks = []
            readable = False
            for url in urls:
                # An unreachable, non-text or empty page must never make the
                # round fail (that would lock the bonds): it counts as no evidence.
                try:
                    response = gl.nondet.web.get(url)
                    status = getattr(response, "status", 200)
                    text = ""
                    if status < 400:
                        text = response.body.decode("utf-8", errors="replace")[:6000]
                        # Mostly undecodable bytes means a binary file, not evidence.
                        if text.count("\ufffd") * 10 > len(text):
                            text = ""
                except Exception:
                    text = ""
                if text.strip():
                    readable = True
                blocks.append(f"SOURCE {url}:\n{text}")
            if not readable:
                return {
                    "verdict": "inconclusive",
                    "reasoning": "Evidence was unreachable or empty.",
                }
            result = gl.nondet.exec_prompt(
                prompt + "\n\nEVIDENCE:\n" + "\n\n".join(blocks),
                response_format="json",
            )
            return result

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            leader_out = leaders_res.calldata
            if leader_out.get("verdict") not in VERDICTS:
                return False
            if not leader_out.get("reasoning"):
                return False
            mine = leader_fn()
            return mine.get("verdict") == leader_out["verdict"]

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        return result["verdict"]

    # ------------------------------------------------------------------
    # Claimant
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def submit_claim(self, statement: str, evidence_url: str) -> u32:
        bond = gl.message.value
        if bond < u256(MIN_BOND):
            raise gl.vm.UserError("Bond below minimum")
        if len(statement) < MIN_STATEMENT_LEN:
            raise gl.vm.UserError("Statement too short to adjudicate")
        if not evidence_url.startswith("https://"):
            raise gl.vm.UserError("evidence_url must be an https URL")

        claim_id = self.next_id
        self.claims[claim_id] = Claim(
            claimant=gl.message.sender_address,
            statement=statement,
            evidence_url=evidence_url,
            bond=bond,
            status="open",
            round1_verdict="",
            appellant=gl.message.sender_address,
            appeal_evidence_url="",
            appeal_bond=u256(0),
            round2_verdict="",
            appeal_deadline=u64(0),
            outcome="",
        )
        self.next_id = u32(self.next_id + 1)
        return claim_id

    @gl.public.write
    def cancel_claim(self, claim_id: u32) -> None:
        """Claimant withdraws a claim that has not been adjudicated yet."""
        if claim_id not in self.claims:
            raise gl.vm.UserError("Unknown claim")
        claim = self.claims[claim_id]
        if gl.message.sender_address != claim.claimant:
            raise gl.vm.UserError("Only the claimant can cancel")
        if claim.status != "open":
            raise gl.vm.UserError("Only an open claim can be cancelled")

        claim.status = "final"
        claim.outcome = "cancelled"
        self._credit(claim.claimant, claim.bond)
        self.claims[claim_id] = claim

    # ------------------------------------------------------------------
    # Round 1
    # ------------------------------------------------------------------

    @gl.public.write
    def adjudicate(self, claim_id: u32) -> typing.Any:
        if claim_id not in self.claims:
            raise gl.vm.UserError("Unknown claim")
        claim = self.claims[claim_id]
        if claim.status != "open":
            raise gl.vm.UserError("Claim is not open for round 1")

        verdict = self._judge(claim.statement, [claim.evidence_url])
        claim.round1_verdict = verdict

        if verdict == "inconclusive":
            # Nothing was proven either way: refund the bond.
            claim.status = "final"
            claim.outcome = "refunded"
            self._credit(claim.claimant, claim.bond)
        else:
            claim.status = "provisional"
            claim.appeal_deadline = u64(_now() + APPEAL_WINDOW_SECONDS)

        self.claims[claim_id] = claim
        return {"claim_id": claim_id, "status": claim.status, "verdict": verdict}

    # ------------------------------------------------------------------
    # Appeal
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def appeal(self, claim_id: u32, appeal_evidence_url: str) -> None:
        if claim_id not in self.claims:
            raise gl.vm.UserError("Unknown claim")
        claim = self.claims[claim_id]
        if claim.status != "provisional":
            raise gl.vm.UserError("Claim is not appealable")
        if claim.round1_verdict != "supported":
            raise gl.vm.UserError("Only a 'supported' verdict can be appealed")
        if _now() > int(claim.appeal_deadline):
            raise gl.vm.UserError("Appeal window has closed")
        if gl.message.sender_address == claim.claimant:
            raise gl.vm.UserError("Claimant cannot appeal their own claim")
        if gl.message.value < claim.bond:
            raise gl.vm.UserError("Appeal bond must match the claim bond")
        if not appeal_evidence_url.startswith("https://"):
            raise gl.vm.UserError("appeal_evidence_url must be an https URL")

        claim.status = "appealed"
        claim.appellant = gl.message.sender_address
        claim.appeal_evidence_url = appeal_evidence_url
        claim.appeal_bond = gl.message.value
        self.claims[claim_id] = claim

    # ------------------------------------------------------------------
    # Round 2
    # ------------------------------------------------------------------

    @gl.public.write
    def adjudicate_appeal(self, claim_id: u32) -> typing.Any:
        if claim_id not in self.claims:
            raise gl.vm.UserError("Unknown claim")
        claim = self.claims[claim_id]
        if claim.status != "appealed":
            raise gl.vm.UserError("Claim has no pending appeal")

        verdict = self._judge(
            claim.statement, [claim.evidence_url, claim.appeal_evidence_url]
        )
        claim.round2_verdict = verdict
        total = u256(claim.bond + claim.appeal_bond)

        if verdict == "inconclusive":
            # Both sides refunded, no winner.
            self._credit(claim.claimant, claim.bond)
            self._credit(claim.appellant, claim.appeal_bond)
            claim.outcome = "refunded"
        elif verdict == claim.round1_verdict:
            # Round 1 upheld: claimant keeps both bonds.
            self._credit(claim.claimant, total)
            self._bump(self.reputation, claim.claimant)
            self._bump(self.strikes, claim.appellant)
            claim.outcome = "upheld"
        else:
            # Round 1 overturned: appellant takes both bonds.
            self._credit(claim.appellant, total)
            self._bump(self.reputation, claim.appellant)
            self._bump(self.strikes, claim.claimant)
            claim.outcome = "overturned"

        claim.status = "final"
        self.claims[claim_id] = claim
        return {"claim_id": claim_id, "outcome": claim.outcome, "verdict": verdict}

    # ------------------------------------------------------------------
    # Finalization and payouts
    # ------------------------------------------------------------------

    @gl.public.write
    def rescue_appeal(self, claim_id: u32) -> str:
        """
        Escape hatch for an appeal that was never adjudicated: once the appeal
        window plus RESCUE_DELAY_SECONDS has passed, both bonds are refunded.
        """
        if claim_id not in self.claims:
            raise gl.vm.UserError("Unknown claim")
        claim = self.claims[claim_id]
        if claim.status != "appealed":
            raise gl.vm.UserError("Claim has no pending appeal")
        if _now() <= int(claim.appeal_deadline) + RESCUE_DELAY_SECONDS:
            raise gl.vm.UserError("Rescue delay has not passed")

        self._credit(claim.claimant, claim.bond)
        self._credit(claim.appellant, claim.appeal_bond)
        claim.outcome = "refunded"
        claim.status = "final"
        self.claims[claim_id] = claim
        return claim.outcome

    @gl.public.write
    def finalize(self, claim_id: u32) -> str:
        """Settles a provisional claim once the appeal window has passed."""
        if claim_id not in self.claims:
            raise gl.vm.UserError("Unknown claim")
        claim = self.claims[claim_id]
        if claim.status != "provisional":
            raise gl.vm.UserError("Claim is not awaiting finalization")
        if _now() <= int(claim.appeal_deadline):
            raise gl.vm.UserError("Appeal window is still open")

        if claim.round1_verdict == "supported":
            self._credit(claim.claimant, claim.bond)
            self._bump(self.reputation, claim.claimant)
            claim.outcome = "unchallenged"
        else:  # "refuted": a false claim forfeits its bond to the treasury
            self.treasury = u256(self.treasury + claim.bond)
            self._bump(self.strikes, claim.claimant)
            claim.outcome = "forfeited"

        claim.status = "final"
        self.claims[claim_id] = claim
        return claim.outcome

    @gl.public.write
    def withdraw(self) -> u256:
        sender = gl.message.sender_address
        amount = self.balances[sender] if sender in self.balances else u256(0)
        if amount == u256(0):
            raise gl.vm.UserError("Nothing to withdraw")
        self.balances[sender] = u256(0)  # zero before transfer
        _EOA(sender).emit_transfer(value=amount)
        return amount

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @gl.public.view
    def get_claim(self, claim_id: u32) -> typing.Any:
        if claim_id not in self.claims:
            raise gl.vm.UserError("Unknown claim")
        c = self.claims[claim_id]
        return {
            "claimant": c.claimant,
            "statement": c.statement,
            "evidence_url": c.evidence_url,
            "bond": c.bond,
            "status": c.status,
            "round1_verdict": c.round1_verdict,
            "appellant": c.appellant,
            "appeal_evidence_url": c.appeal_evidence_url,
            "appeal_bond": c.appeal_bond,
            "round2_verdict": c.round2_verdict,
            "appeal_deadline": c.appeal_deadline,
            "outcome": c.outcome,
        }

    @gl.public.view
    def get_balance(self, who: Address) -> u256:
        return self.balances[who] if who in self.balances else u256(0)

    @gl.public.view
    def get_standing(self, who: Address) -> typing.Any:
        return {
            "wins": self.reputation[who] if who in self.reputation else u32(0),
            "strikes": self.strikes[who] if who in self.strikes else u32(0),
        }

    @gl.public.view
    def get_treasury(self) -> u256:
        return self.treasury

    @gl.public.view
    def total_claims(self) -> u32:
        return self.next_id
