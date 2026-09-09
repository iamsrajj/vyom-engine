-- Farmer-selected soil type (from the NovosEdge soil picker in the "Draw
-- new field" modal, see web/index.html openSoilPicker()) -- a free-text
-- label like crop_type, not a foreign key, since the source list is an
-- external API rather than a table we own.
ALTER TABLE polygons ADD COLUMN soil_type VARCHAR;