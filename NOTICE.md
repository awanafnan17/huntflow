# Attribution and public-release notes

HuntFlow is based on [OpenOutreach](https://github.com/eracle/OpenOutreach), originally authored by eracle and contributors. The Python package name remains `openoutreach`. Original application licensing is retained in `LICENCE.md` (GNU GPLv3), along with the upstream `LEGAL_NOTICE.md` and `PRIVACY_NOTICE.md`.

The bundled `packages/linkedin_agent_cli` package identifies eracle as author and declares an MIT license in `pyproject.toml`. Preserve upstream copyright and license notices when redistributing derived work.

HuntFlow adds career and application workflows, its dashboard, workspace controls, approvals, provider safety and operational tooling. This public release is a sanitized snapshot of the current application source from the owner's archive, published with fresh history. It does not publish the private restoration archive or its operational data. Some original notices describe upstream integrations and behavior; review the actual enabled configuration for your deployment.

Public-release preparation also generalized personal example values, replaced private correspondence and a profile fixture with synthetic data, removed personal fallback resumes, fixed lazy imports in the Ollama model builder, and tightened local host/staging-secret defaults. These changes do not constitute a complete production-hardening review.
