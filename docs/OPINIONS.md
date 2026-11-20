# Opinions

What the system believes, one line each, so a change can be checked against it. An opinion is a stance, not a
law: where it holds only in one part of the system, its scope says so. Opinions are not overstated; development
stays flexible, and the list is revisited by spot-checking the system once it is fully operational. Only a heavily
enforced opinion becomes a law in AGENTS.md. Laws and conventions already written there are not repeated here.

## Whole system
1. The developers never invent the world; the modeller may. A user may state any assumption, synthetic field or dataset of their own, and the run carries it as theirs. When testing, we are the modeller.
2. Empirical data is preferred, never required. A ballpark run on a lumped value (one storm total, a constant rate) is allowed with a warning.
3. Every number the system states shows where it came from: an engine default, a paper, a fetched field, or the user's word.
4. Declared, not implicit. Anything that reshapes data or a run (a clip, a fill, a fit, a widening) is a stated step someone can read, never a hidden setting.
5. Warnings and errors are generic and direct: a code, what happened, what to do. Each is written once and reused across the system and the SDK. They name things by codes and controlled identifiers, never by inserting free text a source or user supplied.
6. A failed fetch is visible through the fetch's own feedback; no extra mechanism enforces it.
7. The YAML is the auditable surface. Coupling to it (fetch, fields, SDK access) is good coupling.

## Simulations and operations
8. The system is for earth-science simulation first. A simulation (an engine run) is its own realm: an interface that takes a prescribed list of inputs and fills it. Operations (interpolation, compositing, delineation) exist to refine those inputs and are kept separate from simulations.
9. An engine may use an operation, but the operation lives a level up, generic and reusable. The engine offers the choice of algorithm rather than owning one.
10. Direction, not yet built: operations live in a domain-specific SDK, so a modeller reaches for them there instead of hunting through libraries or adding one-off tools.
11. Each engine is built whole around its own library and inputs first. Coupling to another engine comes only after both are finished, and never shapes either one.

## Inputs and data
12. Precedence for any value: stated by the user, then fetched from a cited source, then derived from fetched data, then a refusal. A derived value is offered, not applied: a reviewing user is asked with it shown; AUTO takes it and says so on the run.
13. Engine-native wins: its developers know the engine better than we do, and its defaults are what its users already expect.
14. What a gauge provides is never stated by hand.
15. A fetch keeps every field its source returns; code reads named fields the source's field map declares.
16. A measurement is empirical: a sensor's reading or an objective algorithm's retrieval. A model's output is an expected value under its assumptions.

## Responsibility between steps
17. Each step owns its own inputs. The mesh, bed and boundaries are built and checked before a run; a later step, calibration included, takes them as given rather than compensating for them.
18. A step's tool owns its usual surface. Calibration uses everything PEST++ supports; if PEST++ can calibrate something like the mesh, that door stays open.
19. A calibration target never feeds a boundary.

## Calibration (scope: PEST++ studies)
20. Sigma is the source's cited accuracy. Records without one still run, with a warning, in their own group; widening after the fit sets their weight.
21. An uncertain input becomes a parameter whose starting uncertainty is its stated accuracy, not a widened sigma.

## Compute and testing
22. Compute budgets are settings that apply to every solver; engine-specific limits stay engine-native. A PEST++ study is bounded by PEST++'s own settings.
23. A test that does not exercise the LLM is declarative: every input stated, data handed as real fetch chains.
