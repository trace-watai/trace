"""Public results: the retained evidence hosted in Supabase for anonymous reads.

``schema`` is the Python side of the SQL contract in ``supabase/migrations/``,
``postgrest`` talks to the hosted tables over HTTP with the standard library,
``retained`` lays the retained tree out as one runs directory, ``rows`` turns
what RunReader returns into table rows, ``upload`` pushes them from CI, and
``secret_scan`` refuses to publish a tree with a key in it. The read side is
``trace_harness.run_reader_supabase``. See ``docs/public_results.md``.
"""
