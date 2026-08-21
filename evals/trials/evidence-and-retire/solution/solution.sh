#!/bin/sh
# Oracle gestures, replayed by the verifier through the SDK:
#   <harness op=register name=shout doc=uppercase>def handle(body, **a): ...
#   <shout>hi</shout>   (N times; each dispatch lands a result event)
#   <harness op=refine>              -> census: N calls, 0 errors
#   <harness op=refine tombstone=shout reason=eval-retire>
#   <shout>hi</shout>                -> "<shout> was retired ..."
