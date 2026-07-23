# The responsibility cut

The central design principle (NATE): there is a line between what the system
can validate about a simulation and what only a human can. The system must
never pretend to be on the wrong side of it.

TIER 1 - MACHINE-ENFORCED (the honesty floor extended to physics):
deterministic checks run automatically after every solve; failures ride the
result as typed warnings, never silently pass. Examples grounded in the AWS
flood-model-review webinar and the engines' own self-reports:
- mass balance / continuity error (engine-reported or derived-and-labeled)
- volume sanity (modeled volume vs supplied forcing over the catchment)
- duration sufficiency (peak occurring at the end of the window)
- trapped-water / missing downstream boundary symptoms
- parameter-vs-data cross-checks (assigned imperviousness vs fetched landcover;
  buildings present in an urban domain)
- convergence / instability / dry-cell diagnostics

TIER 2 - MACHINE-ASSISTED, HUMAN-JUDGED: the system computes and renders;
the human judges. Computed-vs-observed at gauges (metrics: NSE/KGE/PBIAS/
CSI with published bands shown alongside), dynamic visual review (animation
+ velocity vectors, one click), residual maps. The number is machine; "good
enough for this purpose" is human.

TIER 3 - HUMAN-ONLY, SYSTEM-PROMPTED: local knowledge, flow-path
reasonableness, event selection, stakeholder input, boundary-condition
correctness (no published tool even lints BCs). The system's whole job here
is to ASK - explicit unchecked sign-offs on a review card, typed
RequiresHumanReview, never auto-passed.

Corollaries:
- A failing tier-1 check is stated on the result. No silent green.
- Tier-2 verdicts are suggestions with sources, not gates.
- Tier-3 items can never be satisfied by the LLM on the user's behalf.
