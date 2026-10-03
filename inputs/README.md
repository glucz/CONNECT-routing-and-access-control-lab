# Frozen preparation inputs

These files let `connect_lab.prepare` generate a two-server configuration without
access to a parent project or private database. They are historical price scenarios
from 29 September 2026, not current purchase recommendations.

`calibration_inputs.json` contains the archived price summaries and ordinary-path
range. `posted_prices.csv` contains the five selected datacenter provider rows used
to calculate the median per-GB price. It is a selected subset, not the full market
survey. URLs, retrieval dates and price excerpts preserve source attribution.

`provenance.json` identifies the original full input hashes and selection rule.
Newly generated configurations hash these bundled subset files; those hashes will
therefore differ from the original full-paper input hashes even though the selected
prices match. No private credentials or database connection are required.
