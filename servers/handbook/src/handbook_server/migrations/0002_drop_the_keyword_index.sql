-- The GIN index on chunks.tsv could never be used. The server reads chunks through the
-- security_barrier view searchable_chunks, and the planner will not push the keyword
-- operator (`tsv @@ query`) below a security barrier: the operator is not leakproof, so it
-- could expose rows the view hides. Every keyword search is therefore a scan of the view,
-- which is about 150 chunks and takes roughly a millisecond.
--
-- Revisit when the handbook is far larger (thousands of chunks). Then either make the
-- restricted-document exclusion cheap to apply before the keyword filter, or index a
-- published-only copy of the chunks; do not drop the barrier to get the index used.
DROP INDEX IF EXISTS chunks_by_tsv;
