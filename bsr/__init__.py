"""Banana Split Redux media engine.

Layers (kept independent on purpose):
    media      - probing and packet indexing (no decoding)
    detect     - black-frame analysis (decodes for analysis only; never exports)
    boundaries - which presentation times can be cut by stream copy, proven by decode
    planner    - user decisions -> concrete segments with per-track treatment
    export     - writes outputs (copy mode never creates an encoder)
    validate   - independent checks on the written files
"""

ENGINE_VERSION = "0.1.0"
