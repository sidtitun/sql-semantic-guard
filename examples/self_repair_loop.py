"""The self-repair loop: turn a validation failure into a corrected query.

This is the pattern that makes guardrails *productive* rather than merely
defensive: the structured, deterministic feedback goes straight back to the
model, which fixes exactly what was flagged.

The ``fake_llm`` here is a scripted stand-in so the example runs offline. In
production, replace it with a real model call (Claude, etc.), seeding the
system prompt with ``guard.policy_prompt()`` so the model knows the schema and
house rules up front — prevention beats repair.

    python examples/self_repair_loop.py
"""

from __future__ import annotations

from sqlguard import Catalog, ColumnRule, Policy, RLSRule, SQLGuard


def build_guard() -> SQLGuard:
    catalog = Catalog.from_dict(
        {
            "orders": {
                "columns": {
                    "id": "bigint",
                    "customer_id": "bigint",
                    "amount": "decimal(10,2)",
                    "status": "varchar(32)",
                    "ssn": {"type": "text", "tags": ["pii"]},
                    "created_at": "timestamp",
                },
                "row_count": 1_000_000,
                "total_bytes": 1 << 30,
            }
        }
    )
    return SQLGuard(
        catalog,
        Policy(
            rls=[RLSRule(table="orders", column="customer_id", param="customer_id")],
            column_rules=[ColumnRule(tags={"pii"}, action="deny", reason="PII")],
            default_limit=500,
        ),
        dialect="postgres",
    )


# A scripted model that "learns" from each round of feedback.
_SCRIPT = [
    # 1. hallucinated table + column, and tries to read a sensitive field
    "SELECT ssn, total FROM order_table WHERE customer_id = 1001",
    # 2. fixed the table, dropped ssn, but the column is still wrong
    "SELECT total FROM orders",
    # 3. correct
    "SELECT amount FROM orders WHERE status = 'shipped'",
]


def fake_llm(system_prompt: str, transcript: list[str], attempt: int) -> str:
    return _SCRIPT[min(attempt, len(_SCRIPT) - 1)]


def main() -> None:
    guard = build_guard()
    system_prompt = guard.policy_prompt()
    print("SYSTEM PROMPT SENT TO THE MODEL")
    print("-" * 78)
    print(system_prompt)
    print("-" * 78)

    transcript: list[str] = []
    tenant = {"customer_id": 1001}
    result = None

    for attempt in range(5):
        sql = fake_llm(system_prompt, transcript, attempt)
        print(f"\n── round {attempt + 1} ──")
        print("model:", sql)
        result = guard.validate(sql, params=tenant)
        if result.valid:
            print("guard: ✅ accepted")
            print("final:", result.sql)
            break
        feedback = result.feedback()
        print("guard: ⛔ rejected →")
        print("\n".join("   " + line for line in feedback.splitlines()))
        transcript.append(sql)
        transcript.append(feedback)

    assert result is not None and result.valid, "loop failed to converge"
    print("\nConverged in", attempt + 1, "round(s). Safe to execute:")
    print(result.sql)


if __name__ == "__main__":
    main()
