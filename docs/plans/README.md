# Implementation plans

One detailed, self-contained plan per roadmap phase. Each expands the
[ROADMAP](../ROADMAP.md) line items into concrete designs against the v0.1.0
codebase: APIs and dataclass changes, files touched, new violation codes and
policy fields, test plans, sequencing, risks, and a definition of done.

| Plan | Roadmap phase | Theme | Target release |
|---|---|---|---|
| [phase-0-hardening.md](phase-0-hardening.md) | Phase 0 | Tested, published, measured core | v0.2 |
| [phase-1-semantics.md](phase-1-semantics.md) | Phase 1 | Deeper semantic validation | v0.3 |
| [phase-2-security.md](phase-2-security.md) | Phase 2 | Enterprise security depth | v0.3–v0.4 |
| [phase-3-cost-and-engines.md](phase-3-cost-and-engines.md) | Phase 3 | Cost intelligence & dialect coverage | v0.4–v0.5 |
| [phase-4-ecosystem.md](phase-4-ecosystem.md) | Phase 4 | Integrations, CLI, config, docs | v0.4–v0.5 |
| [phase-5-exploratory.md](phase-5-exploratory.md) | Phase 5 | Experiments behind decision gates | unscheduled |

## Conventions used in every plan

- **Item numbers** (0.1, 1.4, …) match the roadmap tables exactly.
- **New `Code` values** are listed per item; once released they are frozen
  (cross-cutting rule 2). New `Policy` fields default to *off/None* so every
  release is behavior-compatible for existing users.
- **Fail closed** applies to all new checks: they may add violations, never
  suppress them; internal errors surface as `internal_error`, never a pass.
- **Definition of done** per item = tests listed in its test plan are green,
  docs page for any new code exists, benchmark gate passes.
- Effort figures are focused engineering days, excluding review latency.
