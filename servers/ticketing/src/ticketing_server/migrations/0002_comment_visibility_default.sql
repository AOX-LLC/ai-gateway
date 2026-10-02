-- A comment is public unless staff say otherwise. The server's role may not insert the
-- visibility column at all (see harborline_setup.ticketing), so every comment a tool
-- creates takes this default and the server can never write an internal one.
ALTER TABLE comments ALTER COLUMN visibility SET DEFAULT 'public';
