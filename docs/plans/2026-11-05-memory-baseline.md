# Memory reliability slice 0 baseline

Date: 2026-11-07  
Starting git commit: `562f51f`  
Reproduce: `uv run pytest -q packages/plugins/memory/tests/test_memory_scenarios.py`

This synthetic, scripted suite uses invented people, fixed extraction responses, `FakeEmbedder`, an
in-memory repository and search fake. It is not a measured production quality result. It contains
43 scenarios: 22 regressions and 21 controls. The 22 expected baseline failures below are strict
xfails, so a later fix that makes one pass requires deliberately changing its fixture baseline.

## Separate deterministic measures

| Measure | Count |
| --- | ---: |
| Wrong-person mutations | 2 |
| Wrong-person answers (context fact lines without attribution) | 4 |
| Stale-current answers (expired fact still appears in context) | 1 |
| Erased-fact returns | 2 |
| Erased-text index documents | 3 |

The suite does not generate model answers. Wrong-person answers count fact lines rendered without
the plan's person attribution phrase. The stale-current count records an expired fact made available
in context, not a live model answer.

| Category | Pass | Fail |
| --- | ---: | ---: |
| `similar_wording` | 1 | 3 |
| `ambiguous_subject` | 0 | 5 |
| `explicit_vs_auto` | 1 | 2 |
| `new_conversation` | 3 | 1 |
| `correction` | 2 | 2 |
| `expired_plan` | 2 | 1 |
| `multilingual` | 3 | 1 |
| `injection` | 1 | 2 |
| `bare_tag_reply` | 3 | 2 |
| `erasure` | 5 | 3 |
| **Total** | **21** | **22** |

## Expected baseline failures

| Scenario | Target slice | Baseline reason | Single behavior change that turns it green |
| --- | --- | --- | --- |
| `similar-01` | 1 | Alice and Bob's identical preferences collapse into one row. | Restrict duplicate reuse to the same attributed person. |
| `similar-02` | 1 | A same-person near duplicate changes the first wording. | Discard a same-identity near duplicate without updating its row. |
| `ambiguous-01` | 1 | An unlabeled claim about “someone” is stored. | Drop `about_other` candidates without a verified label. |
| `ambiguous-02` | 1 | An unlabeled claim about a friend is stored. | Drop `about_other` candidates without a verified label. |
| `explicit-auto-01` | 1 | Bob's near duplicate rewrites Alice's explicit wording. | Do not let automatic extraction update the existing explicit row. |
| `new-conversation-01` | 1 | A fact from conversation A reaches B without “about Alice” attribution. | Render each fact with its trusted subject attribution. |
| `correction-01` | 1 | The `replaces` argument is rejected and the old fact remains. | Implement the planned atomic explicit replacement. |
| `expired-01` | 3 | The temporary booth plan remains available after Friday. | Add a resolved validity end bound and exclude it after expiry. |
| `injection-01` | 1 | The extraction request does not mark the message as untrusted; the password candidate is already denied. | Mark the supplied human body as untrusted in the provider request. |
| `bare-tag-01` | 1 | A synthetic bare-tag prompt reaches extraction. | Skip extraction for the trusted `synthetic` turn origin. |
| `reply-01` | 1 | A synthetic reply prompt reaches extraction. | Skip extraction for the trusted `synthetic` turn origin. |
| `ambiguous-03` | 1 | An unlabeled claim about a cousin is stored. | Drop `about_other` candidates without a verified label. |
| `explicit-auto-03` | 1 | A trailing-period exact save changes the original wording. | Treat normalized exact explicit saves as no-change reuse. |
| `correction-03` | 1 | An unlabeled automatic claim about Carol is stored. | Drop the unlinked third-party automatic candidate. |
| `injection-03` | 1 | The extraction request does not mark the injected body as untrusted. | Mark the supplied human body as untrusted in the provider request. |
| `similar-04` | 1 | Bob's null-subject near duplicate rewrites Alice's row. | Discard the near duplicate without changing Alice's text or creator. |
| `ambiguous-04` | 1 | The `P1` mention label is not mapped to Bob. | Resolve verified request-local mention labels to trusted subjects. |
| `ambiguous-05` | 1 | The out-of-range `P9` label does not stop an unlinked save. | Reject invalid labels and drop the unlinked third-party candidate. |
| `multilingual-04` | 1 | Alice and Bob's identical CJK preferences collapse into one row. | Apply the same subject identity check to CJK facts. |
| `erasure-06` | 1 | Bob's `/forgetme` purge misses Alice's verified fact about Bob. | Store Bob as the fact subject so his user purge finds it. |
| `erasure-07` | 1 | An unlinked third-party fact is stored and remains after Bob's purge. | Drop the unlabeled `about_other` candidate before persistence. |
| `erasure-08` | 2a | A failed search deletion leaves erased text in the index. | Retry/heal the durable index deletion after the initial failure. |

`erasure-06` covers the required `/forgetme` third-party failure: Alice states a verified fact about
Bob, then Bob's user purge must remove it. The synthetic runner exercises the memory plugin's
`purge_user` hook used by that privacy flow.

Multilingual and temporal scenarios use scripted extraction output. They test persistence and
retrieval rules, not model extraction quality.

## Not measured

- Recall@5 on real embeddings: only `FakeEmbedder` is used.
- Evidence-supported answer accuracy: no live model or source-link table is available in this slice.
- Abstention precision: no live model answers are generated.
- Source lookup availability: source lookup is not implemented here.
- Production provider tokens, calls and latency: scripted completions do not measure production usage.

Held-out note: no live quality run was made, so no prompt or threshold was tuned on this set.
