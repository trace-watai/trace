"""Public results: the retained evidence hosted in Supabase for anonymous reads.

``schema`` is the Python side of the SQL contract in ``supabase/migrations/``,
``postgrest`` talks to the hosted tables over HTTP with the standard library,
``retained`` lays the retained tree out as one runs directory, and ``rows``
turns what RunReader returns into table rows. The read side is
``trace_harness.run_reader_supabase``. See ``docs/public_results.md``.
"""
