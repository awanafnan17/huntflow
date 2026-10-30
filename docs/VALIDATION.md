# Validation and current limits

This public source snapshot was reviewed against the current application tree. The preparation excluded credentials, databases, session cookies, private uploads, logs, backups and original Git history. A redacted Gitleaks directory scan found no remaining matches after synthetic test credentials were generalized. This is a source check, not proof that every integration is production safe.

## Checks run

- Python/Django system check under the existing Python 3.12 development environment, with a temporary test-only token pepper: **no issues**.
- `pytest -q tests/test_job_matching.py tests/test_daemon_controls.py tests/test_career.py`: **32 passed** in an isolated pytest test database; 19 warnings noted missing generated `staticfiles/` in the clean checkout.
- Focused model/configuration, provider-parser, synthetic-transport, external-action, tenant-isolation and public-release safety tests: **59 passed** in an isolated pytest database.
- Four additional job-transport tests initially could not run because ReportLab was missing from the reused local environment; they are not counted as passing.
- Follow-up synthetic-fixture and public-release regression checks: **11 passed**; the Git history secret scan also found no matches.
- Public source Gitleaks scan: **no findings** after replacing scanner-matched synthetic test literals.

The complete historical test suite, a fresh dependency install, container build and live account workflows were not run for this publication. Live LinkedIn messages, emails and job applications were never triggered while preparing it.

## What remains in development

- Provider pages and browser selectors change; the Easy Apply path supports known flows but cannot guarantee every form.
- Several utilities and regression tests originated from a single-operator deployment and need more generalization.
- Account/session handoff, multi-workspace isolation and deployment controls need review in the target environment before external use.
- A queued action, browser click or draft does not prove provider delivery; use verification evidence and reconcile uncertain outcomes.
- Model ranking and ATS heuristics are aids for review, not guarantees of sales, interviews or employment.
- No public hosted demo is included because the application requires private connected accounts and a database. The source can be run locally with synthetic data.
