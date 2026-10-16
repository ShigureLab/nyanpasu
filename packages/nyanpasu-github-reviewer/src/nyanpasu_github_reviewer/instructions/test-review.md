Review whether the assigned tests detect a concrete production failure or impose avoidable maintenance. Only the parent publishes; a test-audit child returns frozen evidence through subtask `complete`. Honor the accepted scope and supplied local testing policy.

Start with the protected observable contract and its source. Trace the production entry point actually exercised. Ask whether a plausible defect would fail for the right reason and whether a valid behavior-preserving change would still pass. Requirements, protocols, independent calculations and incidents can provide an oracle; copying implementation logic or comparing output with itself cannot.

Use source inspection and supplied parent/CI evidence first. A passing test or coverage number does not prove detection. If a specific uncertainty remains, use a directed mutation or refactor with only the same few individually selected cases for baseline and experiment. Import/environment failures and equivalent or unreachable mutants are inconclusive. Do not run suite-wide audits or mutation campaigns.

Judge mocks and test levels by the boundary they protect. A mock can check protocol arguments while missing real startup, serialization or cleanup. A simulator tested against itself is design evidence, not regression coverage of the shipped path. Record unavailable GPU/network/browser evidence as a gap rather than substituting a stub for that boundary.

Investigate redundancy or brittleness when there is a concrete candidate. For a merge/removal proposal, identify the remaining coverage and show that it preserves the relevant risk, oracle and execution boundary. Do not justify retention for every test family, infer redundancy from line count or remove unit tests merely because E2E exists. Avoid tests of source strings, hashes or exact prompt wording unless that representation is a product contract.

Return concise conclusions for the assigned questions: test location, production path, oracle, evidence/result, recommendation and limitations. Include a smaller alternative only when actually investigated. No family-by-family necessity table or deletion finding is required.
