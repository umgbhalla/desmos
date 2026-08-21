#!/bin/sh
# Oracle gestures, replayed by the verifier through the SDK:
#   session 1: <knowledge op=memory id=repo.rust.flags scope=repo kind=fact>
#              Rust compiler flags for this repo live in .cargo/config.toml
#   session 2 (fresh world, same cwd):
#              <knowledge op=memory>search rust compiler flags
#              -> repo.rust.flags ranked first
