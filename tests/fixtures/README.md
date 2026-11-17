# Prompt regression examples

`prompt_crypto.json` contains 33 Russian, Ukrainian and English examples for the globally configured crypto trading policy: 14 prohibited offers/requests (including three repetitions of the reported short USDT purchase ad) and 19 protected discussions, warnings and questions.

These are synthetic expected outcomes based on the owner-approved moderation policy, not independently reviewed fraud labels or a representative quality benchmark. SCAM means a policy violation, not proof of criminal fraud. A release probe should check the expected label and, for SCAM, the configured automatic-action threshold (80% in the recorded probe). Protected cases must be OK, not merely below the threshold.

This fixture does not make network calls during pytest. Live probes require separate explicit authorization and a spending limit; credentials and raw provider reports remain outside Git. The 17 November 2026 v2.7 probe also reruns the previous 73 context and advertising cases.

`prompt_bulk_advertising.json` adds 34 cases for v2.8: 13 prohibited bulk-advertising service offers (three repeats of the reported visible ad with its contact replaced by a placeholder) and 21 protected service ads, newsletters, questions and warnings. These also use policy-based expected labels, not fraud findings. The live probe includes the prior 106 examples as regression coverage.
