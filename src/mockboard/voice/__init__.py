"""The voice loop (phase 2): Pipecat 1.5.0 around the same brain the text CLI uses.

* `lines`: sentence splitting and playback accounting (pure Python, no Pipecat).
* `brain_processor`, `strategies`, `pipeline`, `fakes`, `server`: need the `voice` extra.

This package imports nothing from Pipecat itself; import the submodules directly.
"""
