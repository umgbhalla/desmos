#!/bin/sh
# Oracle: what an ideal agent does mid-session. The verifier replays this
# end-to-end against the SDK, so this script is the documented gesture.
mkdir -p .desmos/extensions
cat > .desmos/extensions/greet.py <<'EOF'
def load(api):
    api.register_tool("greet", "say hello", lambda body, **a: "hello " + body)
EOF
# next turn: <greet>world</greet>  ->  hello world (ambient reload)
