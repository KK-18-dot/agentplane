# smoke suite

Offline, deterministic cases that exercise the full `agentplane run` path with the built-in mock
provider. They verify the result contract (HANDOFF.md shape, status vocabulary, ledger), not model
quality. Run them after any change to agentplane itself:

    agentplane eval run evals/suites/smoke --role dry

Point the same suite at a real role to check that a provider honours the contract end to end:

    agentplane eval run evals/suites/smoke --role impl_claude
