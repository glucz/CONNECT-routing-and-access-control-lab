# Published experiment reports

Four folders contain anonymized capacity, composition, rate/window and reconnect
reports. Each includes the frozen plan. These plans can be run with a newly prepared
private deployment configuration; they contain no deployed addresses or secrets.

`campaigns.csv` inventories ten original campaigns, including deployment failures,
smoke and pilot. Report directories are not counted as additional campaigns. The
four scientific blocks contain 671 retained valid runs from 672 attempts. One
reconnect attempt was excluded and retried; the summary preserves that distinction.

`manifest.json` records SHA-256 hashes of the published files. From the repository
root, run `python reproduce.py --output output/summary.csv` to verify the files and
reproduce the campaign table without private raw logs or network access.

`delivery.csv` contains cell-level outcomes. `aggregates.json` provides traffic and
cost measurements. Temporal `timeline.csv` files use within-run source/tunnel
ordinals, not addresses or session identifiers. `prices.csv` contains offline price
scenarios. See [RESULTS.md](../RESULTS.md) for interpretation and exclusions.

The repository deliberately omits individual request/proxy/origin JSONL logs and
deployed configurations. Report verification reproduces the summary from published
measurements; it does not claim to reconstruct every private raw-log observation.
