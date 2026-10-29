"""Speech services for the voice loop (Gnani Prisma STT and Timbre TTS).

* `gnani_protocol`: the Gnani wire protocol (headers, 1,024-byte framing, message parsers),
  shared with the probes. Imports no Pipecat, so it works without the `voice` extra.
* `prisma_stt`: `PrismaSTTService`, our own Pipecat STT service (ruling 24). Needs the extra.
* `timbre_tts`: `TimbreTTSService`, the stock Gnani TTS plus our interruption fix (ruling 25).
  Needs the extra.

This package imports nothing from Pipecat itself; import the submodules directly.
"""
