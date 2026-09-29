# API V&V report

| Requirement | Priority | Status | Test cases → latest verdict |
| --- | --- | --- | --- |
| REQ-001: The health endpoint returns its status and service version header. | must | verified | TC-cdaa34c36bf2 → passed |
| REQ-002: A valid widget can be created. | must | verified | TC-49e39d3e97cf → passed |
| REQ-003: A widget name longer than eight characters is rejected. | must | failed | TC-fcf6e146d3f7 → failed |
| REQ-004: A widget must include a name. | must | verified | TC-79866774821e → passed |
| REQ-005: A widget name must be a string. | must | verified | TC-118246c9cbae → passed |
| REQ-006: Widget roles are restricted to the documented enum. | must | verified | TC-e19504014f59 → passed |
| REQ-007: The secure endpoint rejects unauthenticated requests. | must | verified | TC-bc42e8332169 → passed |
| REQ-008: Deleting a widget returns 204 with no body. | must | failed | TC-13777f5b2f2f → failed |
| REQ-009: Reading a widget returns a schema-valid representation. | must | verified | TC-2cefd5bb874c → passed |
| REQ-010: Reading absent widget 999 returns a documented not-found response. | must | verified | TC-bf3867a1d253 → passed |
| REQ-011: A one-character widget name is accepted. | must | verified | TC-8b8c2d496cb1 → passed |
| REQ-012: The health endpoint responds within one second in this local demo. | should | verified | TC-cdaa34c36bf2 → passed |

Untraced tests: TC-19262f360851, TC-60a2c5a91e13, TC-5b33e7a9b87d, TC-1d5e23f34061, TC-3672bfd2665c, TC-34c607faab34, TC-fe713f895f52, TC-db1e9b99ffe5, TC-0581effea7c6, TC-954d1ad56930, TC-20a07aed3239, TC-b38f98f72adc, TC-0e220c58dd1d, TC-482de01b6b54, TC-a45a03da342f, TC-8f08efa84c7e, TC-1ea5c02b9cc8, TC-c87250a04bff, TC-1fcb60261aea, TC-1494271a2eb9

Operations without requirements: none

## Execution findings

- TC-fcf6e146d3f7 (failed): status: expected 422, got 201
- TC-13777f5b2f2f (failed): status: expected 204, got 200; response status is undocumented
