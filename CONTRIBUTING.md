# Contributing

HuntFlow is under active development. Keep changes focused and document which user-visible workflow they improve.

1. Create a virtual environment and install `requirements/local.txt`.
2. Use a fresh local database; do not copy the maintainer's operational state.
3. Run tests under pytest's isolated database and mock external providers.
4. Keep credentials, personal candidate data, real correspondence and browser profiles out of fixtures and commits.
5. Validate tenant ownership, action authorization and idempotency when modifying transport code.
6. Record which checks passed and any behavior that was not tested.

```bash
python -m compileall -q openoutreach packages tests
python manage.py check
python -m pytest tests/test_llm.py tests/test_conf.py tests/api/test_voyager.py
```

Read `AGENTS.md` for database and provider safety requirements. Never bypass a provider safety stop to make a test pass. End-to-end tests involving live third-party accounts require a separately designed, authorized test plan.
