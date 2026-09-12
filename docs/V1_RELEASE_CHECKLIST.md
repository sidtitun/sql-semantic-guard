# v1.0 release checklist

This is the release gate, not a promise to ship every roadmap idea. v1.0 means
the public API and violation codes are safe to freeze, the core security model
has no known fail-open gaps, and published quality claims are reproducible.

## Required before the first release candidate

- [x] Read-only output audit, strict CI, parser compatibility lanes, fuzzing,
  coverage and latency gates.
- [x] FK-aware joins, aggregation correctness, enum/domain validation, shadow
  mode, and query-complexity budgets.
- [ ] Close semantic fail-open gaps: nested struct/array member validation,
  derived-star resolution, and high-use function signature checks.
- [ ] Add type-preserving column masking and expression-based RLS.
- [ ] Add named role policy sets with monotonic composition: an overlay may
  tighten a base policy but cannot resurrect denied access.
- [ ] Add PII-safe audit records with a pluggable sink.
- [ ] Publish a frozen evaluation corpus and gate semantic recall (>=95%),
  false-block rate (<=2%), and rewrite idempotence (>=95%).
- [ ] Publish policy-as-config plus a CLI so release behavior is reproducible
  without application code.
- [ ] Support named, pyformat, qmark, and numeric driver parameter styles with
  an executable parameters object in every parameterized result.
- [ ] Document every violation code and freeze the v1 public API surface.

## Required before general availability

- [ ] Add dbt manifest import for columns, relationships, accepted values, and
  tags; validate catalog freshness at startup.
- [ ] Replace fixed partition selectivity with catalog-backed partition stats
  for supported warehouses.
- [ ] Promote at least one additional warehouse dialect to the tested support
  matrix; BigQuery is the recommended first target.
- [ ] Ship the MCP validate/policy/catalog tools as the stable agent-facing
  integration surface.
- [ ] Complete an independent security review and threat-model pass.
- [ ] Accumulate 14 consecutive green scheduled fuzz runs.
- [ ] Configure protected-branch required checks and PyPI trusted publishing;
  publish signed artifacts with provenance and an SBOM.
- [ ] Validate two reference deployments and one shadow-to-enforcement pilot.

## Deliberately post-v1 unless demand changes

- Framework adapters, FastAPI service, batch API, intent/SQL consistency
  models, and automatic LLM repair convenience APIs.
- The enforcement kernel remains deterministic and LLM-free. Any learned
  intent checker or repair model must be an optional layer and cannot turn a
  deterministic rejection into an allow decision.
