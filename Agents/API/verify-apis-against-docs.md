# Rule: verify external APIs against official docs

Fetch and cite the official documentation before writing or delegating code against an external
API; say which claims came from docs and which from memory.

**Why:** on 2026-10-01 the user-account rewrite was built from memory and shipped a `/room` call to
Talk's removed `api/v1`, which 404'd on first start. The maintainer then asked for sources, and the answer
was that there were none.

**How to apply:** for Nextcloud Talk use `talk-user-api-reference.md` and refresh it first; give
delegated agents the doc URLs or that file, not just a description. Keep a "not verified" list
rather than presenting remembered behaviour as fact.
