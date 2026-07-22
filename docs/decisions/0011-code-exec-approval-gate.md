# 0011 - code execution is user-gated and honestly timed out

Decision: code_exec_request emits an approval card (rationale + verbatim
code preview, Run/Deny) that the plugin renders inline; the reply rides the
existing tool-payload-confirmation envelope. An unanswered request times
out (default 180s) into a typed error so the turn always completes - no
gate may hang a turn forever.
