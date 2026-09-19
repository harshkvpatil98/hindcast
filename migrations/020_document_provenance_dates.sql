-- 020: when a document's text was knowable (ADR-0044).
--
-- `published_at` stays the one column the time lock filters on. From here it
-- means *when the stored text was knowable*: the later of the date the page
-- states and the time its text was fetched. Two columns keep the evidence for
-- that value, so a reader can see which rule set it and by how much:
--
--   * `stated_published_at` -- what the page said about itself;
--   * `crawled_at`          -- when the text was fetched (CC-NEWS `WARC-Date`).
--
-- Both are NULL for rows written before this migration until `cascade corpus
-- redate` has re-read their crawl files, and `crawled_at` stays NULL for
-- sources whose text is fixed at its stated date (a Wikipedia revision, a
-- Federal Register document).
--
-- Adding nullable columns without a default is a catalogue change; no row is
-- rewritten. Forward-only.

BEGIN;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS stated_published_at timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS crawled_at timestamptz;

COMMENT ON COLUMN documents.published_at IS
    'When the stored text was knowable: max(stated_published_at, crawled_at). The time lock filters on this (ADR-0044).';
COMMENT ON COLUMN documents.stated_published_at IS
    'The publication date the document states about itself.';
COMMENT ON COLUMN documents.crawled_at IS
    'When the stored text was fetched (CC-NEWS WARC-Date); NULL where the text is fixed at the stated date.';

COMMIT;
