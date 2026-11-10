"""Regression tests for the unverified-success-claim guard (bug #2 in docs/BUGS.md)."""

import pytest

from errand_agent.agent.runtime import claims_action, claims_state


@pytest.mark.parametrize("text", [
    "Done! I've booked AquaCare Plumbers for you.",
    "Your AC servicing has been cancelled.",
    "I have successfully rescheduled it to Saturday.",
    "Nothing changed earlier. I've booked it now.",  # negation only in another sentence
    "You're all set for Thursday.",
])
def test_affirmative_claims_detected(text):
    assert claims_action(text)


@pytest.mark.parametrize("text", [
    "Nothing has been booked yet.",
    "I haven't cancelled anything.",
    "It has not been booked; want me to try another provider?",
    "I can't confirm that anything was booked or changed. Nothing has been booked or changed.",
    "I'm having trouble reaching the provider directory. Nothing has been booked.",
])
def test_negated_statements_are_not_claims(text):
    assert not claims_action(text)


def test_state_claims():
    assert claims_state("Your AC service is booked for Thursday 3 PM.")
    assert not claims_state("Your AC service is not booked yet.")
