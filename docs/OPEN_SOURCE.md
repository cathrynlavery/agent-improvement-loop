# Open-source and private-fork model

This project is intended to be useful as a public skill/tool without publishing anyone's private transcripts, client data, family details, or operating receipts.

## What belongs in the public repository

Keep the public repo limited to:

- generic transcript parsers
- generic redaction rules
- generic detector logic
- public-safe examples using synthetic data
- documentation that teaches the workflow without naming private people, customers, accounts, or internal projects

The public repo may explain that the tool mines local personal/work transcripts. That is the point. It must also be clear that transcript mining is local and that content proposals are source material for review, not publishable copy.

## What belongs in a private fork or private overlay

Use a private fork or ignored local files for:

- real run outputs under `.agent-improvement/`
- copied logs from other machines
- private detector catalogs for your own tools, clients, accounts, and internal workflows
- receipts, dogfood output, screenshots, or examples that contain private context
- any proposed content brief that names real customers, family members, private accounts, health/school/daycare details, auth material, or exact business metrics

Recommended local layout:

```text
agent-improvement-loop/
  private/                         # gitignored
    content-workflows.local.json    # private detector ideas / patterns
    dogfood-runs/                   # local real-session outputs
  .agent-improvement/               # gitignored queue/output root if run in-tree
```

## Content route privacy rule

`content_idea` is allowed to mine personal/private sessions locally. That is useful: the best content often comes from real work.

But content output must be treated as **source material**, not publishable text:

1. Keep evidence references; do not dump full transcripts.
2. For high-risk ideas, suppress command and excerpt text.
3. Use synthetic examples in public drafts.
4. Remove names, emails, phone numbers, customer/client details, family details, health details, auth material, exact private metrics, and raw messages.
5. Keep `recommendation=needs_context` for high-risk proposals until the owner approves the abstraction.

The current code enforces this by replacing high-risk content evidence with:

```text
<private workflow evidence redacted>
```

That suppression applies even when `--full` is used.

## Release checklist before sharing publicly

Before making a public repo/tag/release:

- [ ] Run `git grep -nE 'personal name|company name|customer name|client name|phone|email|token|secret|private'` and inspect every hit.
- [ ] Confirm `private/`, `.agent-improvement/`, `*.local.json`, and local log/output folders are ignored.
- [ ] Run tests.
- [ ] Run a synthetic dry-run, not a real-private dry-run, for screenshots/docs.
- [ ] Review README examples for generic machine names and generic accounts.
- [ ] Do not include receipts or real dogfood output in the public branch.

## Private fork workflow

1. Keep `main` public-safe.
2. Keep private detectors/config in a private fork, private branch, or ignored `private/` files.
3. When a private detector becomes generally useful, rewrite it as a generic detector with synthetic tests before upstreaming.
4. Never cherry-pick real proposal JSON or review packets from the private fork into the public repo.
