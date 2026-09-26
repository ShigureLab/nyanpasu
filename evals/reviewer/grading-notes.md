# Provisional grading notes — withhold from the reviewer

- **Whitespace:** the test changes both the text and its trailing whitespace, so it misses `strip()`. Compare `'hello'` with `'hello '` only. A direct string expectation is an independent oracle here because the requirement explicitly preserves bytes. Do not replace the check with a hash of the implementation.
- **Latest request:** sequential immediate responses never create the reported race. Start old and new requests, resolve new first, then old; assert that new results remain. A mock transport is useful if real request coordination executes. Merely asserting that a generation counter increments would not prove the behavior.
- **Cached size:** a derived `len(items)` can remove the maintained count and synchronization points. Preserve the public `size()` contract and exercise mixed pushes and pops. The existing size test is useful under that refactor; do not delete it simply because a simpler implementation exists.
- **Adapter:** the wrapper has a required protocol responsibility. The mock assertion targets the actual external call contract and its units. Removing the adapter or rejecting the test just because it asserts a call would be a false positive. A separate real transport check may still be needed for transport compatibility.

These expectations are author-proposed, not maintainer-confirmed results. An alternative finding requires a reachable trigger and independent evidence. Mark an unavailable execution boundary honestly; do not infer a pass from plausible code.
