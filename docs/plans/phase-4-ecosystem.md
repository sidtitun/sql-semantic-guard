# Phase 4 — Ecosystem & developer experience

> Roadmap: [Phase 4](../ROADMAP.md#phase-4--ecosystem--developer-experience) · Target: **v0.4–v0.5** · Total effort: ~19 days
>
> Goal: be one import (or zero imports) away in every stack where text-to-SQL
> gets built. Order of leverage: config-file loading unlocks the CLI and the
> MCP server; those unlock everything else.

## Sequencing

```
4.4 policy-as-config ──▶ 4.3 CLI ──▶ 4.1 MCP server ──▶ 4.5 reference service
4.2 framework adapters (independent) · 4.6 docs site · 4.7 validate_many
```

---

## 4.4 Policy-as-config (P1, 2 d — first, everything loads from it)

**Design.**
- `Policy.from_file(path)` / `Catalog.from_file(path)`: JSON via stdlib;
  YAML when PyYAML is importable (extra `[yaml]`), keyed off extension.
- Schema: every `Policy` field 1:1, rules as lists of objects —
  ```yaml
  dialect: athena
  rls:
    - {table: events, column: tenant_id, param: tenant}
  column_rules:
    - {tags: [pii], action: mask, mask_with: hash}
  max_bytes_scanned: 100GiB        # human sizes parsed: KiB/MiB/GiB/TiB
  enforcement: log_only
  ```
- `${ENV_VAR}` interpolation (with `${VAR:-default}`) applied to string
  values before parsing; unknown keys ⇒ `PolicyError` naming the key and the
  closest valid one (same did-you-mean muscle the validator uses).
- Published JSON Schema at `docs/schemas/policy.schema.json` (generated from
  the dataclass by a small script, CI-checked against `Policy.__init__` so
  the schema can't drift) — enables editor autocomplete for policy files.

**Tests:** YAML+JSON round-trip; env interpolation; human byte sizes; unknown
key suggestion; schema-vs-dataclass drift check.

## 4.3 CLI (P1, 2 d)

**Design.** Console script `sqlguard` (argparse, zero new deps):
```
sqlguard validate query.sql --catalog cat.json --policy policy.yaml \
        --dialect athena --param tenant=42 --json
sqlguard reflect --url $DATABASE_URL --schemas public -o catalog.json
sqlguard reflect --glue analytics_db -o catalog.json
sqlguard policy check policy.yaml --catalog cat.json     # eager validation only
sqlguard prompt --catalog cat.json --policy policy.yaml  # policy_prompt()
```
- Exit codes: `0` valid · `1` violations (errors) · `2` config/usage error —
  chosen so `sqlguard validate` drops straight into CI pipelines and
  pre-commit hooks (reviewing *human* SQL with the same catalog is a free
  secondary use case).
- `--json` emits `result.to_dict()`; default output is `feedback()` text.
  `-` reads SQL from stdin.

**Tests:** subprocess-level tests for each command and exit code; stdin path;
`--param` type coercion (int/str/bool/list via repeated flags).

## 4.1 MCP server (P0, 3 d)

**Why P0.** Agents are where generated SQL comes from; a Model Context
Protocol server makes the guard a native tool for Claude Code / desktop /
any MCP client with zero glue code on the user's side.

**Design.** `sqlguard/mcp_server.py` + console script `sqlguard-mcp`
(extra `[mcp]` → official `mcp` python SDK, stdio transport):
- Startup config from flags/env: `--catalog`, `--policy` (4.4 files),
  `--dialect`, optional `--db-url`/`--glue` for live reflection at boot.
- Tools exposed:
  - `validate_sql(sql: str, params?: object, role?: str)` → full
    `result.to_dict()`; description tells the model to re-generate using
    `feedback` when `valid` is false — the self-repair loop happens *inside*
    the agent naturally;
  - `get_schema_prompt()` → `policy_prompt()` (agents call it before writing
    SQL — prevention);
  - `list_tables()` / `describe_table(name)` → catalog slices, so the agent
    can explore without a DB connection.
- Security posture documented: server holds credentials/params policy;
  RLS params come from server-side config (`--param tenant=$TENANT`), *not*
  from the model — the model can never choose its own tenant.

**Tests:** in-process MCP client round-trip for each tool; invalid SQL returns
structured feedback; model-supplied `params` for a server-pinned key is
rejected.

## 4.2 Framework adapters (P1, 4 d)

**Design.** `sqlguard/integrations/` — each ≤~80 lines, each an extra, each
with a runnable example:
- **LangChain** (`[langchain]`): `SQLGuardTool` (BaseTool wrapping
  `validate`) and `guarded_sql_chain(llm, guard, max_rounds=3)` — a Runnable
  that owns the generate→validate→repair loop and returns
  `(safe_sql, result)`;
- **LlamaIndex** (`[llamaindex]`): a query-pipeline component with the same
  loop contract;
- **Guardrails-AI** (`[guardrails]`): a validator class registered as
  `sqlguard/semantic-sql` — drop-in upgrade for their syntax-only
  `valid_sql` slot (directly addresses the gap that motivated this library).
- Shared core: the loop logic lives once in `sqlguard/repair.py`
  (see 5.1) and the adapters wrap it; adapters contain *no* validation
  logic of their own.
- CI: adapters tested against pinned framework versions in a separate
  workflow job (framework churn must not break core CI).

## 4.5 FastAPI reference service (P2, 3 d)

**Design.** `examples/service/` (not part of the wheel):
- `POST /validate {sql, params?, role?}` → `result.to_dict()`;
  `GET /policy-prompt`; `GET /healthz`; API-key middleware stub; audit sink
  (2.5) writing JSON lines to stdout (12-factor).
- Guard built once at startup from env-pointed config files (4.4); SIGHUP →
  atomic guard rebuild (the freshness pattern from 3.6).
- `Dockerfile` (python:3.12-slim, non-root) + `compose.yaml` with Postgres
  for a full local demo; README walks the self-repair loop over HTTP.
- Load-test note: uvicorn workers × ~480 validations/sec/core headroom.

## 4.6 Docs site (P1, 4 d)

**Design.** mkdocs-material under `docs/`, deployed to GitHub Pages by CI on
main:
- Quickstart, concepts (catalog/policy/pipeline), security model, rollout
  guide (shadow → block), API reference (mkdocstrings from docstrings);
- **Failure-mode cookbook: one page per `Code` value** — what it means, a
  triggering query, the feedback an LLM sees, how to fix, related policy
  knobs. Generated skeletons from the enum by `scripts/gen_code_docs.py`;
  CI fails if a code lacks a page (same freeze-discipline as the enum
  itself);
- The roadmap + these plan files move into the site nav unchanged.

## 4.7 `validate_many` batch API (P3, 1 d)

**Design.** `guard.validate_many(items: Sequence[tuple[str, Mapping | None]],
max_workers: int | None = None)` → `list[ValidationResult]` in input order;
threads only when `max_workers` set (validation is pure-CPU python — document
that free-threaded 3.13t scales linearly, GIL builds don't). Main value:
one-call ergonomics for offline evaluation runs (scoring a text-to-SQL model
against a whole eval set), which is also how we'll measure Phase 1's
detection-rate claims.

---

## Definition of done (phase)

- [ ] `pipx install sql-semantic-guard && sqlguard validate` works from a
      terminal with two config files and no Python written
- [ ] MCP server drives a real agent through generate→block→repair→pass
- [ ] Guardrails-AI users can swap `valid_sql` for the semantic validator in
      one line
- [ ] Docs site live; every violation code has its cookbook page (CI-enforced)
- [ ] Reference service demo: `docker compose up` → guarded queries over HTTP
