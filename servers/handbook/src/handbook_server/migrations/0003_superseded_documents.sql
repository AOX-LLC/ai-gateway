-- A document can be replaced by a newer edition. `superseded_by` names the edition that
-- replaces it, and a superseded document drops out of search: the server must not answer a
-- question from a policy that no longer applies. get_document still returns it, with a
-- pointer to the current edition, because an old edition is sometimes still the rule (an
-- order delivered before the new edition took effect).
--
-- The reference is deferred so that a whole seed can be inserted in one transaction in any
-- order. harborline-setup checks, before it writes, that a target exists, is not restricted
-- and is not itself superseded; the view below also refuses to name a restricted target.

ALTER TABLE documents
    ADD COLUMN superseded_by text
        REFERENCES documents (id) DEFERRABLE INITIALLY DEFERRED
        CHECK (superseded_by <> id);

-- Search reads only this view, so excluding superseded documents here covers every step of
-- every ranking. CREATE OR REPLACE keeps the role's grant on the view.
CREATE OR REPLACE VIEW searchable_chunks WITH (security_barrier = true) AS
SELECT c.id, c.document_id, d.title, d.category, d.classification,
       c.ordinal, c.heading, c.text, c.embedding, c.tsv
FROM chunks AS c
JOIN documents AS d ON d.id = c.document_id
WHERE d.classification <> 'restricted'
  AND d.superseded_by IS NULL;

-- The pointer is shown only when its target is published, so a bad row cannot reveal that a
-- restricted document exists. It is a subquery rather than a join so that the view stays a
-- plain single-table view: a write through it then reaches the privilege check, and the role
-- is refused there ("permission denied"), as it was before this column existed.
CREATE OR REPLACE VIEW published_documents WITH (security_barrier = true) AS
SELECT d.id, d.title, d.category, d.classification, d.updated, d.body,
       (SELECT s.id FROM documents AS s
         WHERE s.id = d.superseded_by AND s.classification <> 'restricted') AS superseded_by
FROM documents AS d
WHERE d.classification <> 'restricted';
