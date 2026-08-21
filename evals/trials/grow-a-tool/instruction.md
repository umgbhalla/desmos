# grow-a-tool

Between two turns, write an extension file under
`.desmos/extensions/greet.py` whose `load(api)` registers a `<greet>`
tag. Score 1.0 iff the tag is unknown before the file exists and
dispatches successfully on the very next turn boundary with **no
explicit reload**: the ambient stat-sweep reload in
`desmos.kernel.loop.install_resources` must pick it up.
