# Agent Operational & Testing Guidelines

## Database Safety & Verification Policy
1. **Never Create Debug Fixtures in Operational DB**: All automated verifications and testing scripts must execute inside pytest's isolated test database (`pytest` / `@pytest.mark.django_db`) or an explicitly isolated temporary database.
2. **No Ad-Hoc Writes**: Do not use `python -c` or `manage.py shell` to write throwaway `User`, `Workspace`, `Campaign`, `Lead`, `Deal`, or `ProviderWriteIntent` objects directly into `data/db.sqlite3`.
3. **Read-Only Inspection Permitted**: Read-only queries to check system status, health, and configuration are permitted.
4. **Provider Safety & Live LinkedIn Guard**: Never execute live LinkedIn network access or bypass safety stops (`HIGH_VOLUME_DATA_ACCESS_WARNING`, `SAFETY_STOP`).
