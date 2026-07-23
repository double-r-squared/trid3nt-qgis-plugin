# Roadmap - PROPOSAL ONLY (not approved)

From research.md section 5. Recorded so the thinking is concrete; NATE
decides if/when/what.

A. Post-run diagnostics reader - per-engine parser of the run's own
   self-report into one normalized envelope. Machine tier. Everything else
   consumes it.
B. Metric acceptance gate as a decision card - metrics vs observations with
   published bands and a SUGGESTED verdict; human confirms.
C. Review-checklist runner - the 24-item checklist as one declarative table;
   the responsibility-cut classification is the routing key (auto-run /
   pre-fill-and-ask / explicit human sign-off).
D. Calibration guardrail - calibration is a playground workflow over atomic
   primitives (param-write, run, metric) with run-count cost surfaced
   through the granularity gate. MODFLOW/PEST++ first if ever; SFINCS
   auto-calibration is R&D, not shippable.
