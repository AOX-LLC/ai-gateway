-- Harborline Supply Co. is fictional. This schema belongs to the handbook server alone.
-- harborline-setup creates the pgvector extension in this schema before it runs this file
-- (that needs the database owner), and grants the server's role (handbook_app) SELECT on
-- the two views at the end, and nothing else. The mcp-common runner runs this with the
-- handbook schema first on the search path.

CREATE TABLE documents (
    id             text  PRIMARY KEY CHECK (id ~ '^DOC-[0-9]{3}$'),
    title          text  NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    category       text  NOT NULL CHECK (category IN ('hr', 'returns', 'shipping', 'security', 'expenses')),
    classification text  NOT NULL CHECK (classification IN ('general', 'restricted')),
    updated        date  NOT NULL,
    body           text  NOT NULL
);

CREATE TABLE chunks (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id text   NOT NULL REFERENCES documents (id),
    ordinal     integer NOT NULL,
    heading     text   NOT NULL,
    text        text   NOT NULL,
    embedding   handbook.vector(256) NOT NULL,
    -- Keyword search over the heading and the text.
    tsv         tsvector GENERATED ALWAYS AS (
                    to_tsvector('english', coalesce(heading, '') || ' ' || text)
                ) STORED,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX chunks_by_tsv ON chunks USING gin (tsv);
-- No vector index: a few hundred chunks are scanned exactly, which is faster than an
-- approximate index would be and never misses a neighbour.

-- The only objects the server's role may read. Restricted documents are left out here, in
-- the database, so the role cannot reach their text even if a query is wrong. The views
-- run with their owner's rights and are security barriers, so a condition a caller adds
-- is never evaluated against a restricted row.
CREATE VIEW published_documents WITH (security_barrier = true) AS
SELECT id, title, category, classification, updated, body
FROM documents
WHERE classification <> 'restricted';

CREATE VIEW searchable_chunks WITH (security_barrier = true) AS
SELECT c.id, c.document_id, d.title, d.category, d.classification,
       c.ordinal, c.heading, c.text, c.embedding, c.tsv
FROM chunks AS c
JOIN documents AS d ON d.id = c.document_id
WHERE d.classification <> 'restricted';
