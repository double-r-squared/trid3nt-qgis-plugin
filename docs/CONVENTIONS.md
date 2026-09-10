# Code documentation conventions

Each fact lives in exactly one place, and the checkable place beats the readable
one: names and types carry what the code is, an inline comment carries the
constraint at the line it governs, the docstring carries the caller's contract,
the SysML model carries structure and requirements, `docs/` carries method and
maps, and the tests carry the behaviour. Prose that repeats one of those is a
maintenance liability.

## The docstring

A docstring says what the symbol is, what it refuses, a constraint the signature
cannot carry, or the input/output contract where the types under-specify it.
Nothing else belongs there.

| surface | budget |
|---|---|
| function, method, class | 3 content lines |
| module | 5 content lines (what lives here + one module-wide constraint) |
| LLM-facing (a `register_tool` body, a docstring assigned through `__doc__`) | 1000 characters - the routing budget the model actually reads |

**Content lines** are the non-blank lines between the delimiters. The opening
and closing `"""` lines and a blank separator do not count, so a one-line
docstring is one content line.

A longer docstring that is a genuine contract carries a marker in the comment
block directly above the symbol:

    # docstring-exempt: <the contract the limit cannot hold>

`docs/validation/docstring-exemptions.md` is rendered from those markers and the
suite diffs it. Past roughly ten entries the limit gets re-argued rather than
routed around.

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
- **Why-essays and rationale**: the decision record holds those, or nothing does.
- **Per-field roll-calls** of a typed model the reader can read off the type.

A path a comment, docstring or README does name has to exist: a reference that
stopped resolving is a claim the reader cannot check.

## Enforcement

`tests/hygiene/` is the sweep guard, not the census: `test_docstring_standard.py`
holds the limits, the routing budget, the exemption ledger and the disallowed
classes; `test_history_markers.py` sweeps every comment and docstring in every
product tree; `test_dead_references.py` resolves every named module, script and
path, in the product trees, the directory maps, the manual, `AGENTS.md` and this
document - an agent told to obey a law cannot follow a path that is not there. A
path is read whole: one starting at a tree resolves as written, and one written
package-relative (`workflows/...`, `tools/...`, `net/...`) resolves under the
package roots, but only when it ends in a file suffix - without that the pattern
reads ordinary prose as a path, and a guard that fires on English is worse than
the class it catches. A guard is a grep after a file has been read end to end - it catches a
class coming back, it never stands in for reading.
