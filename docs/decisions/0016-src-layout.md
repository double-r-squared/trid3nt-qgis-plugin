# 0016 - server uses the src/ layout

Decision: the package lives at server/src/<package>/ so imports resolve
only through the installed (editable) package, never accidentally from the
working directory. Standard packaging safety; the nesting is intentional.
