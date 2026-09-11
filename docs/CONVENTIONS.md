# Code documentation conventions

Each fact lives in exactly one place, and the checkable place beats the readable
one: names and types carry what the code is, an inline comment carries the
constraint at the line it governs, the docstring carries the caller's contract,
the SysML model carries structure and requirements, `docs/` carries method and
maps, and the tests carry the behaviour. Prose that repeats one of those is a
maintenance liability. The RULINGS record - what was decided and why - is kept
outside the repo, so a constraint that only a ruling states has to be lifted into
one of the places above before the ruling can be its only home.

PROOF IS TRANSIENT, so a packet is never one of those places. A rendered packet
lands under `run/proof/<template>/<run-id>/`, which git does not carry and the
renderer sweeps to a seven-day TTL on every write; it exists to be DELIVERED,
and the delivery is the record. Prose that cites a packet path is citing
something that will be gone - state the fact where the fact lives, and let the
acceptance render its own packet. The only durable images are the doc-sized
figures a generated template page embeds, which are committed beside it.

A drawing of LIVE STRUCTURE is the model's, regenerated: `docs/model/` is
derived from its own `.sysml` sources and the suite fails while a view is stale,
so a second hand-drawn copy of the same system is drift with nothing to catch
it. A spec draws the MECHANISM it is committing to and cites the model for the
structure it builds on.

## The spec

A spec is ONE self-contained HTML page under `docs/specs/`, written before the
work it commits to. One numbered section per concern; a vocabulary table when a
term is load-bearing; the real surface quoted as code rather than paraphrased;
mermaid for every diagram; plain language in place of pattern names. Everything
on the page is buildable - what is excluded is marked out of scope where it
would have gone, so no reader mistakes an intention for a commitment. The header
carries a revision line.

Self-contained means the page loads nothing: styles and any highlighter are
inline, so the page renders on the day it is read the way it rendered on the day
it was written.

## The docstring

A docstring says what the symbol is, what it refuses, a constraint the signature
cannot carry, or the input/output contract where the types under-specify it.
Nothing else belongs there.

Shape, as guidance rather than a gate: three content lines for a function, a
method or a class, five for a module - what lives here plus one module-wide
constraint. An LLM-facing docstring (a `register_tool` body, a docstring
assigned through `__doc__`) is product material the model reads, so it is rich
and front-loaded inside the roughly 1000 characters that survive truncation.
Content lines are the non-blank lines between the delimiters. A docstring that
needs more than the shape allows is a genuine contract and states it; one that
needs a paragraph to explain a private helper needs a better name or a split.

## The comment block

A full-line comment block has **no length cap**. It is the constraint at its
point of use, read by whoever changes that line - a different reader from the
caller at the docstring. A derivation, a ladder or a unit convention that
governs a line belongs there; what governs nothing goes, because git is the
archive.

## Never, in a docstring or a comment

- **History**: dates, "used to", change narration, what a wave or a fold moved.
- **Spec notation**: job, task, sprint, ADR, wave N, milestone N, FR-N, OQ-N, section marks.
- **Attribution**: a person's name, and the memory filenames.
- **Usage narrative and examples**: `>>>` blocks, `Example:`, `Usage:`.
- **Architecture and neighbour references**: `see <module>.py`, "defined in ...".
- **Why-essays and rationale**: the rulings record holds them, or nothing does.
- **Per-field roll-calls** of a typed model the reader can read off the type.

A path a comment, docstring or README does name has to exist: a reference that
stopped resolving is a claim the reader cannot check.

## Enforcement

The guards are LINTS, not tests: they read the tree rather than the product's
behaviour, so they live in `dev/lint/` and run with `make lint`. The history
sweep reads every comment and docstring in every product tree; the dead-path
lint resolves every named module, script and path, in the product trees, the
directory maps, the manual, `AGENTS.md` and this document - an agent told to
obey a law cannot follow a path that is not there; the map lint holds a package
README to its package; the template-page lint holds a generated page to its
declaration; the template-grammar lint holds a recipe to values, defining no
function; the module-vocabulary lint holds every tree below the template layer
to the engine's own words, naming no template and no question; the banner lint
holds the ruler comments out. A path is read whole:
one starting at a tree resolves as written, and one written package-relative
(`workflows/...`, `tools/...`, `net/...`) resolves under the package roots, but
only when it ends in a file suffix - without that the pattern reads ordinary
prose as a path, and a guard that fires on English is worse than the class it
catches. A guard is a grep after a file has been read end to end - it catches a
class coming back, it never stands in for reading.
