# Moved to public_results.md

The API plan now lives in [public_results.md](public_results.md), in its
Local API section. The hosted public results in Supabase supersede a FastAPI
wrapper for public reads, and the wrapper stays the plan for a local server if
one is ever needed.

This file stays so that older links still resolve, from ADR-0001 and from the
docstrings of `RunReader` and `ArtifactStore`, which #205 leaves unchanged.
