"""units -- tiny shared module so unit conversions used for both display
(vyom/api/farms.py's area_acre field) and billing (vyom/farm_pricing.py,
which prices per acre while farms are stored in hectares) never drift out
of sync by having two separate literal constants."""

HA_TO_ACRE = 2.4710538147
