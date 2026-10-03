# Sensitivity results

Status: complete. Cells: 120. Valid-run delivery prediction mismatches: 0.

delivery.csv separates 99% and complete delivery; missing/invalid runs remain explicit. A prediction mismatch is a finding, not an instruction to overwrite observations.

prices.csv is an offline scenario sweep. Candidates must deliver the criterion in every planned repetition. Winners are only among sampled candidates, not an exhaustive market minimum. Empty winners are not proof of impossibility, especially when sampled_group_complete is false. Dedicated-address costs reserve a whole address-month per used clean address; there is no assumed sharing, profitability, or provider billing measurement.

Interface counters include background/control traffic; choose the physical interface and do not sum virtual duplicates. These measurements are synthetic mechanism sensitivity, not discovered service policies.
