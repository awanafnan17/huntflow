# Configuration

## Local environment

Copy `.env.example` to `.env`. The Django settings loader reads simple `KEY=value` lines without shell interpolation. Do not put a shell command in the value field; generate the value first and paste it locally.

Generate private values in your terminal:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Use independently generated values for the Django signing key, token pepper and credential encryption key. Never share these values or commit `.env`.

| Setting | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django signing/session key; replace the development fallback |
| `DJANGO_DEBUG` | Local debugging only; disable for deployment |
| `DJANGO_ALLOWED_HOSTS` | Explicit hostnames accepted by Django |
| `CSRF_TRUSTED_ORIGINS` | Exact scheme/host origins for the deployment |
| `TOKEN_PEPPER` | Additional secret for token hashing |
| `HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY` | Dedicated credential encryption material; required for production use |
| `DATABASE_URL` | PostgreSQL connection URI; omission uses local SQLite under `data/` |
| `REDIS_URL` | Optional Redis cache endpoint |
| `OLLAMA_BASE_URL` | Ollama base URL; use `http://127.0.0.1:11434` on the host |
| `OLLAMA_MODEL` | ATS-generation model override; source fallback is `qwen2.5:1.5b` |
| `OLLAMA_REQUEST_TIMEOUT` | Timeout used by the Ollama model client |

Provider credentials, chosen `provider:model`, account settings, mailbox settings and campaign configuration are also stored through application models/UI. Do not assume that setting a generic `OPENAI_API_KEY` environment variable alone configures the saved `SiteConfig`.

## Local Ollama route

Install and run Ollama separately and download the desired model, for example `ollama pull qwen2.5:1.5b`. Set `OLLAMA_BASE_URL` to the correct host. Choose the matching `ollama:` model in HuntFlow's LLM configuration. The public repository contains no model files. Hardware capacity determines which model sizes are practical; no speed or quality benchmark is asserted here.

## Database and files

SQLite is intended for a local development checkout. Use PostgreSQL and an independently reviewed deployment configuration for concurrent workers. Keep all of the following private: database volumes/dumps, uploaded resumes, extracted candidate facts, application packages, browser state, screenshots, task logs, message history and tokens.

The credential service implements versioned AES-GCM envelopes and legacy compatibility. Encryption does not make the rest of a database safe to publish; the database may contain plaintext personal/business data and legacy fields.

## Docker deployment configuration

The provided production Compose file is a development/deployment starting point. Create your own ignored `.env.production` file containing the settings above, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and a matching `DATABASE_URL` whose host is `postgres`. Set `DJANGO_SETTINGS_MODULE=openoutreach.settings_production`, explicit public hostname/CSRF origins and HTTPS security settings. Redis and Ollama use their Compose service names internally.

Validate before building:

```bash
docker compose --env-file .env.production -f docker-compose.production.yml config --quiet
docker compose --env-file .env.production -f docker-compose.production.yml build
```

Review the Compose worker commands before bringing the full stack up: starting it can execute authorized pending tasks. No remote deployment or full Docker build was performed for this public release. Use separate staging data and independently generated secrets; never copy an operational database into a public repository.
