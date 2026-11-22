"""
Lifecycle tests for AppealableClaimAdjudicator, using GenLayer's
Direct Mode (`genlayer-test` / `gltest`): the contract runs in-memory,
no GenLayer Studio or Docker required.

Setup:
    pip install genlayer-test==0.29.2

Run (from the repository root):
    gltest tests/ -v
"""

import json

import pytest

CONTRACT_PATH = "contracts/appealable_claim_adjudicator.py"
STATEMENT = "Kyiv is the capital city of Ukraine."
EVIDENCE = "https://en.wikipedia.org/wiki/Kyiv"
APPEAL_EVIDENCE = "https://en.wikipedia.org/wiki/Ukraine"
BOND = 10**18  # 1 GEN, well above the 0.001 GEN minimum


@pytest.fixture
def contract(direct_deploy, direct_vm):
    deployed = direct_deploy(CONTRACT_PATH)
    direct_vm.value = 0
    return deployed


def _submit(contract, direct_vm, claimant, bond=BOND):
    """Submit a funded claim as `claimant` and return its id (ids start at 0)."""
    direct_vm.sender = claimant
    direct_vm.value = bond
    try:
        contract.submit_claim(STATEMENT, EVIDENCE)
    finally:
        direct_vm.value = 0
    return int(contract.total_claims()) - 1


def _judge(contract, direct_vm, sender, action, claim_id, verdict):
    """
    Run `adjudicate` or `adjudicate_appeal` with deterministic web and LLM
    mocks. Every validator reads the same evidence and returns the same
    verdict class, so consensus is reached on that single outcome.
    """
    direct_vm.mock_web(
        r".*",
        {"status": 200, "body": "Mocked evidence page about the claim."},
    )
    direct_vm.mock_llm(
        r".*",
        json.dumps({"verdict": verdict, "reasoning": "Mocked reasoning."}),
    )
    direct_vm.sender = sender
    try:
        return getattr(contract, action)(claim_id)
    finally:
        direct_vm.clear_mocks()


def _appeal(contract, direct_vm, appellant, claim_id, bond=BOND):
    direct_vm.sender = appellant
    direct_vm.value = bond
    try:
        contract.appeal(claim_id, APPEAL_EVIDENCE)
    finally:
        direct_vm.value = 0


class TestSubmitClaim:
    def test_submit_locks_bond(self, contract, direct_vm, direct_alice):
        claim_id = _submit(contract, direct_vm, direct_alice)

        claim = contract.get_claim(claim_id)
        assert claim["status"] == "open"
        assert claim["bond"] == BOND
        assert claim["statement"] == STATEMENT
        assert claim["round1_verdict"] == ""

    def test_bond_below_minimum_reverts(self, contract, direct_vm, direct_alice):
        direct_vm.sender = direct_alice
        direct_vm.value = 1
        try:
            with direct_vm.expect_revert("Bond below minimum"):
                contract.submit_claim(STATEMENT, EVIDENCE)
        finally:
            direct_vm.value = 0

    def test_short_statement_reverts(self, contract, direct_vm, direct_alice):
        direct_vm.sender = direct_alice
        direct_vm.value = BOND
        try:
            with direct_vm.expect_revert("Statement too short to adjudicate"):
                contract.submit_claim("too short", EVIDENCE)
        finally:
            direct_vm.value = 0

    def test_non_https_evidence_reverts(self, contract, direct_vm, direct_alice):
        direct_vm.sender = direct_alice
        direct_vm.value = BOND
        try:
            with direct_vm.expect_revert("evidence_url must be an https URL"):
                contract.submit_claim(STATEMENT, "http://example.com/page")
        finally:
            direct_vm.value = 0

    def test_unknown_claim_reverts(self, contract, direct_vm, direct_alice):
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Unknown claim"):
            contract.get_claim(999)


class TestRoundOne:
    def test_supported_verdict_opens_appeal_window(
        self, contract, direct_vm, direct_alice
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "supported")

        claim = contract.get_claim(claim_id)
        assert claim["status"] == "provisional"
        assert claim["round1_verdict"] == "supported"
        assert claim["appeal_deadline"] > 0

    def test_inconclusive_verdict_refunds_bond(
        self, contract, direct_vm, direct_alice
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "inconclusive")

        claim = contract.get_claim(claim_id)
        assert claim["status"] == "final"
        assert claim["outcome"] == "refunded"
        assert contract.get_balance(direct_alice) == BOND

    def test_round_one_cannot_be_repeated(self, contract, direct_vm, direct_alice):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "supported")

        with direct_vm.expect_revert("Claim is not open for round 1"):
            _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "supported")


class TestAppealRules:
    def test_cannot_appeal_before_round_one(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)

        with direct_vm.expect_revert("Claim is not appealable"):
            _appeal(contract, direct_vm, direct_bob, claim_id)

    def test_claimant_cannot_appeal_own_claim(
        self, contract, direct_vm, direct_alice
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "supported")

        with direct_vm.expect_revert("Claimant cannot appeal their own claim"):
            _appeal(contract, direct_vm, direct_alice, claim_id)

    def test_appeal_bond_must_match(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "supported")

        with direct_vm.expect_revert("Appeal bond must match the claim bond"):
            _appeal(contract, direct_vm, direct_bob, claim_id, bond=BOND // 2)

    def test_refuted_verdict_cannot_be_appealed(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "refuted")

        with direct_vm.expect_revert("Only a 'supported' verdict can be appealed"):
            _appeal(contract, direct_vm, direct_bob, claim_id)

    def test_appeal_records_appellant_and_bond(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "supported")
        _appeal(contract, direct_vm, direct_bob, claim_id)

        claim = contract.get_claim(claim_id)
        assert claim["status"] == "appealed"
        assert claim["appeal_bond"] == BOND
        assert claim["appeal_evidence_url"] == APPEAL_EVIDENCE

    def test_round_two_requires_an_appeal(
        self, contract, direct_vm, direct_alice
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "supported")

        with direct_vm.expect_revert("Claim has no pending appeal"):
            _judge(
                contract, direct_vm, direct_alice, "adjudicate_appeal", claim_id, "supported"
            )


class TestRoundTwoOutcomes:
    def _appealed_claim(self, contract, direct_vm, claimant, appellant):
        claim_id = _submit(contract, direct_vm, claimant)
        _judge(contract, direct_vm, claimant, "adjudicate", claim_id, "supported")
        _appeal(contract, direct_vm, appellant, claim_id)
        return claim_id

    def test_same_verdict_upholds_round_one(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        claim_id = self._appealed_claim(contract, direct_vm, direct_alice, direct_bob)
        _judge(contract, direct_vm, direct_bob, "adjudicate_appeal", claim_id, "supported")

        claim = contract.get_claim(claim_id)
        assert claim["status"] == "final"
        assert claim["outcome"] == "upheld"
        assert contract.get_balance(direct_alice) == 2 * BOND
        assert contract.get_balance(direct_bob) == 0
        assert contract.get_standing(direct_alice)["wins"] == 1
        assert contract.get_standing(direct_bob)["strikes"] == 1

    def test_different_verdict_overturns_round_one(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        claim_id = self._appealed_claim(contract, direct_vm, direct_alice, direct_bob)
        _judge(contract, direct_vm, direct_bob, "adjudicate_appeal", claim_id, "refuted")

        claim = contract.get_claim(claim_id)
        assert claim["status"] == "final"
        assert claim["outcome"] == "overturned"
        assert contract.get_balance(direct_bob) == 2 * BOND
        assert contract.get_balance(direct_alice) == 0
        assert contract.get_standing(direct_bob)["wins"] == 1
        assert contract.get_standing(direct_alice)["strikes"] == 1

    def test_inconclusive_round_two_refunds_both(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        claim_id = self._appealed_claim(contract, direct_vm, direct_alice, direct_bob)
        _judge(
            contract, direct_vm, direct_bob, "adjudicate_appeal", claim_id, "inconclusive"
        )

        claim = contract.get_claim(claim_id)
        assert claim["outcome"] == "refunded"
        assert contract.get_balance(direct_alice) == BOND
        assert contract.get_balance(direct_bob) == BOND

    def test_final_claim_cannot_be_judged_again(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        claim_id = self._appealed_claim(contract, direct_vm, direct_alice, direct_bob)
        _judge(contract, direct_vm, direct_bob, "adjudicate_appeal", claim_id, "supported")

        with direct_vm.expect_revert("Claim has no pending appeal"):
            _judge(
                contract, direct_vm, direct_bob, "adjudicate_appeal", claim_id, "refuted"
            )


class TestFinalizeAndWithdraw:
    def test_finalize_before_window_closes_reverts(
        self, contract, direct_vm, direct_alice
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "supported")

        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Appeal window is still open"):
            contract.finalize(claim_id)

    def test_finalize_requires_a_provisional_claim(
        self, contract, direct_vm, direct_alice
    ):
        claim_id = _submit(contract, direct_vm, direct_alice)

        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Claim is not awaiting finalization"):
            contract.finalize(claim_id)

    def test_withdraw_with_nothing_credited_reverts(
        self, contract, direct_vm, direct_bob
    ):
        direct_vm.sender = direct_bob
        with direct_vm.expect_revert("Nothing to withdraw"):
            contract.withdraw()

    def test_withdraw_clears_balance(self, contract, direct_vm, direct_alice):
        claim_id = _submit(contract, direct_vm, direct_alice)
        _judge(contract, direct_vm, direct_alice, "adjudicate", claim_id, "inconclusive")
        assert contract.get_balance(direct_alice) == BOND

        direct_vm.sender = direct_alice
        contract.withdraw()

        assert contract.get_balance(direct_alice) == 0
