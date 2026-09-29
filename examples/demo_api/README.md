# Demo API

A small Starlette app (`app.py`) with an OpenAPI 3.1 contract (`openapi.yaml`) and twelve
requirements (`requirements.yaml`). It exists to show the toolkit finding real contract
violations. Do not deploy it.

Run it end to end from the repository root:

```bash
make demo
```

## Seeded defects

The app contains exactly two deliberate deviations from its contract. Everything else
conforms to the spec.

| # | Defect | Contract | Implementation | Requirement | Case that catches it |
| --- | --- | --- | --- | --- | --- |
| 1 | Off-by-one length validation | `NewWidget.name` has `maxLength: 8` | validates against `maxLength: 9`, so a 9-character name is accepted | REQ-003 | `boundary:name:maxLength:above` (schema-boundary generator): expected 422, got 201 |
| 2 | Wrong status code | `DELETE /widgets/{widget_id}` returns `204` | returns `200` with an empty body | REQ-008 | `happy-path` for `deleteWidget`: expected 204, got 200, and 200 is undocumented |

Neither defect is hidden from the toolkit by special-casing: the cases come from the
generic generators, and the requirements trace to them through their `covers` selectors.

## Files

- `config.yaml`: executor settings, credential environment variable (`API_VV_DEMO_KEY`) and
  the mock LLM provider.
- `mock_responses.yaml`: authored model-output fixtures for the `mock` provider. One proposal is
  valid (REQ-010, widget 999 returns 404); the other names an operation that does not exist
  and is rejected by the validation gate. These are hand-written, not captured from a live model.
