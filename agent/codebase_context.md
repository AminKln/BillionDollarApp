# Codebase context

A short, human-maintained summary the agent includes with every diagnosis.
Not auto-generated, not auto-updated — treat it like a CLAUDE.md written for
an incident-response reader instead of a coding one. Update it when the
architecture changes or a new failure pattern becomes common enough to
name.

NOTE: this file currently describes a fictional "order-service" — the same
scenario used in fixtures.py and llm_fixtures.py. It's placeholder content
for testing the pipeline end-to-end, not a real system. Replace it once
there's an actual app to describe.

## Architecture

Order-service is a backend API fronted by an Application Load Balancer,
handling order lookups and creation for an e-commerce system. It talks to
a Postgres database for order data and a third-party payment gateway for
charges. Deployed on ECS.

## Key components

- OrderRepository — data access for orders, backed by Postgres. GetById is
  the hot path called on every order lookup.
- OrderService — validates and processes orders; calls OrderRepository and
  the payment gateway client.

## Known failure patterns

- DB connection pool exhaustion under load spikes shows up as Npgsql
  timeout exceptions in OrderRepository, not in the payment gateway client.
- Payment gateway 3rd-party outages show up as 5XXs with no corresponding
  DB errors — a strong signal it's external, not our code.
