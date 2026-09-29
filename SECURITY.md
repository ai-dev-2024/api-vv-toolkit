# Security policy

## Reporting a vulnerability

Please report vulnerabilities through a private GitHub security advisory on this repository
("Security" tab, then "Report a vulnerability"). Do not open a public issue.

Include the affected version, a minimal reproduction and the impact you observed. You should
receive an acknowledgement within a few days.

## Supported versions

Only the latest release receives fixes.

## Handling of secrets

- API credentials and LLM keys are read from environment variables named in the config file.
  They are never written to plans, results, reports or error messages.
- CLI validation errors print field locations and error types only, not input values.
- LLM provider errors are reduced to the exception type so that URLs, headers and response
  bodies are not echoed.
- The executor does not follow redirects and ignores proxy environment variables, so
  credentials are only sent to the base URL you pass.

## Scope notes

The demo API under `examples/demo_api/` is intentionally defective and must not be deployed.
