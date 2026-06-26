# QueryGate — database scripts

The synthetic database, the deterministic seed, and the Layer-3 read-only role.

| File | What it does |
|---|---|
| `schema.sql` | The synthetic EHR/claims schema — 5 tables in the `app` schema (idempotent DDL). |
| `init_role.sql` | Layer 3: creates the least-privilege `querygate` role (SELECT only, read-only). |
| `docker-init-role.sh` | initdb hook that runs `init_role.sql` with the role password from the env. |
| `seed.py` | Loads the fixed, byte-identical dataset (`Faker(seed=42)` + time-stable date bands). |

## One-command bring-up (Docker)

```bash
cp .env.example .env          # first time only
docker compose up             # Postgres + schema + Layer-3 role + seeded data
```

On first init the `db` container runs `schema.sql` then the role/grants; once it is
healthy, the one-shot `seeder` service loads the data and exits. Re-running
`docker compose up` re-seeds (idempotent) without dropping the role.

Connect afterwards:

```bash
# read-only application role (this is what the server uses)
psql 'postgresql://querygate:querygate_pw@localhost:5432/querygate'
```

## Manual bring-up (no Docker)

Against any Postgres reachable as an admin role, in order:

```bash
psql "$DATABASE_URL" -f scripts/schema.sql
psql "$DATABASE_URL" -v pw="$QUERYGATE_DB_PASSWORD" -f scripts/init_role.sql
DATABASE_URL="$DATABASE_URL" python scripts/seed.py --reset
```

`seed.py` also applies `schema.sql` itself (idempotent), so a fresh DB only needs the
role step and the seed.

## Determinism

The dataset is byte-identical on every run: fixed `Faker`/`random` seeds (42), pinned
Faker version (`pyproject.toml`), fixed row counts, and **time-stable date bands** —
overdue follow-ups sit years in the past, not-due ones years in the future, with nothing
near "now". So "how many overdue" returns the same number today or years from now.
Expected counts are asserted in `tests/test_seed_and_role.py`.
