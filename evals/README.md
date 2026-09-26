# SQLGuard evaluation corpus

Run the frozen behavior gate after installing the package:

```sh
python -m pip install -e ".[dev]"
python -m evals.run
```

The corpus measures four deterministic guard properties: safe-query
acceptance (the checklist's recall proxy), false blocks among known-safe
queries, rejection of known-unsafe queries, and byte-stable revalidation of
rewritten SQL. It does **not** measure whether SQL correctly answers a natural
language question, and it is not a benchmark of any LLM or text-to-SQL model.

Each case has a stable ID and an explicit expected verdict in
[`corpus.py`](corpus.py). Keep cases representative and readable; do not
weaken a policy or alter an expected verdict merely to make the gate pass.
When a behavior is intentionally changed, update the corpus and its rationale
in the same reviewed change. The gate requires at least 50 safe and 10 unsafe
cases so the 2% false-block threshold has meaningful resolution.

Thresholds are deliberately explicit in `corpus.py`: safe-query recall >=95%,
false-block rate <=2%, unsafe-query rejection =100%, and rewrite idempotence
>=95%. These are release acceptance targets for this corpus, not claims about
production traffic; deployments should add a privacy-reviewed representative
corpus before relying on those rates as operational estimates.
