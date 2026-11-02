"""One settings object for the whole app, loaded from environment variables (and `.env`).

Every region, model id, threshold and feature flag lives here so that nothing is
hard-coded elsewhere. Secrets are read from the environment only; AWS credentials are
never read here at all, because boto3 uses the default credential chain.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


# P8: placeholder voices for the five panel members, one table per Timbre model. They use only
# voices the voice lab already offers (`voice/lab.py` VOICES, from Gnani's available-voices.md):
# v2.5 has three of them for five members, so voices repeat, but never between the Chairperson (who
# speaks every hand-off) and a member, and never between two neighbouring members. Which voice
# suits which member is the owner's choice by ear from `make voice-lab` (Milestone E, Ruling 39);
# until then these are stand-ins. Persona ids, not names: the names live in the persona files.
# UNVERIFIED: that any of these voices sounds right for its member, and Poorvi on a male-titled
# member ("Shri Rathore"): v2.5 offers one verified male voice here, which the Chairperson has.
DEFAULT_PANEL_VOICES: dict[str, dict[str, str]] = {
    "timbre-v2.5": {
        "chairman": "Vikrant", "administrator": "Urmila", "economist": "Poorvi",
        "humanities": "Urmila", "ips_officer": "Poorvi",
    },
    "timbre-v3.0": {
        "chairman": "Avinash", "administrator": "Vasudha", "economist": "Yashraj",
        "humanities": "Vasudha", "ips_officer": "Yashraj",
    },
}


class Settings(BaseSettings):
    """Application settings. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Gnani speech (Prisma STT, Timbre TTS) ---
    gnani_api_key: SecretStr | None = None
    gnani_stt_url: str = "wss://api.vachana.ai/stt/v3/stream"
    gnani_tts_ws_url: str = "wss://api.vachana.ai/api/v1/tts"
    gnani_tts_sse_url: str = "https://api.vachana.ai/api/v1/tts/sse"
    stt_lang_code: str = "en-IN"
    stt_sample_rate: int = 16000
    stt_format: str = "verbatim"
    stt_max_speech_s: float = 15.0
    # Ruling 27: Prisma model. `prisma-v2.5` is the default and the model the contest names;
    # `prisma-v3.0` is allowed for an A/B on the owner's clips (CTO_NOTES_05 §4 suggests
    # STT_LANG_CODE=hi-IN,en-IN with STT_FORMAT=codemix for that arm). The submission names
    # whichever ships. The server silently ignores unknown values, so we validate here.
    stt_model: str = Field(default="prisma-v2.5", pattern=r"^prisma-v(2\.5|3\.0)$")
    # Prisma's own VAD (headers, fixed per connection). UNVERIFIED: these are Gnani's
    # documented defaults, sent explicitly so the trace shows them; probes P2/P3 set them.
    stt_vad_threshold: float | None = Field(default=0.7, ge=0, le=1)
    # Ruling 34: 300 ms (Gnani's default is 500) so Prisma closes a segment sooner after the
    # candidate stops; the first real run measured finals ~1.3 s after speech end at 500.
    # UNVERIFIED at 300: measure (summary's `final_wait_ms`).
    stt_min_silence_ms: int | None = Field(default=300, ge=100, le=2000)
    stt_min_speech_ms: int | None = Field(default=100, ge=50, le=2000)
    stt_min_segment_ms: int | None = Field(default=120, ge=100, le=1000)
    # Ruling 30 / CTO_NOTES_05 should-fix 2: the docs close a session after 60 s with no
    # detected speech; a candidate thinking or a long board turn can hit that. 300 s is our
    # choice (documented range 5-900). UNVERIFIED: whether board audio echo counts as speech.
    stt_idle_timeout_s: int = Field(default=300, ge=5, le=900)
    # Prisma closes a stream at 15 minutes, so the service moves to a fresh socket before that: at
    # `stt_rotate_after_s` when the candidate is not speaking, and at `stt_rotate_hard_s` whatever
    # they are doing. The panel's 17-minute interview (P7) is the first session long enough to need
    # it. Lower both in `.env` to watch a rotation in a short session. UNVERIFIED: rotation has
    # only run against our fake Prisma server, never a live one.
    stt_rotate_after_s: float = Field(default=12 * 60.0, gt=0, lt=900)
    stt_rotate_hard_s: float = Field(default=14 * 60.0, gt=0, lt=900)
    # Per-topic context for Prisma v3.0 (`keyterms`, `topic`); ignored on v2.5, where the
    # docs offer only single-word boosting, which stays off unless STT_BIAS_SCORE > 0
    # (probe rule P6: do not rely on boosting until `settings_updated` is seen).
    stt_context_enabled: bool = True
    stt_bias_score: float = Field(default=0.0, ge=0, le=2)
    # Ruling 28/31: after Smart Turn ends the user's turn the voice loop sends `commit` and
    # waits at most this long for the transcripts it closes. UNVERIFIED until probe P2.
    stt_commit_wait_ms: int = Field(default=1500, gt=0)
    # Ruling 34 (supersedes relying on commit): after the turn stops, wait at most this long
    # for Prisma's own final for the speech still in flight. Measured in the first real run:
    # every commit replied "none" and finals came 1.28-1.76 s after speech end. Start 1.8 s.
    stt_final_wait_ms: int = Field(default=1800, gt=0)
    # CTO_NOTES_05 §4.4: what `commit` does with no utterance open is undocumented; if no
    # `processing` follows within this window, nothing was open. UNVERIFIED.
    stt_commit_noop_ms: int = Field(default=300, gt=0)
    # Ruling 38: "timbre-v2.5" or "timbre-v3.0" (both in tts-websocket.md, read Oct 31, 2026).
    # Voices are not shared between models: TTS_VOICE must come from the model's catalog.
    tts_model: str = "timbre-v2.5"
    # Ruling 8: Timbre language for the board's voice; Hinglish by default. Ruling 25: the value
    # must be one that BOTH gnani-vachana 0.7.9 (SUPPORTED_TTS_LANGUAGES in gnani/tts/client.py:
    # auto, bn/en/gu/hi/kn/ml/mr/pa/ta/te-IN; it refuses "hi-en") and Timbre accept. Gnani's
    # tts-websocket.md (checked Oct 30, 2026) lists "hi-en" (Hinglish) and describes "auto" as
    # "Auto-detect from script (recommended for mixed content)", so "auto" is the one value
    # in both lists meant for Hinglish text.
    # UNVERIFIED: how "auto" voices Latin-script Hinglish compared with "hi-IN"/"en-IN" (and
    # with "hi-en", which only the timbre probe can send); the owner decides by ear.
    tts_language: str = "auto"
    # Ruling 38: Timbre's output rate and the pipeline's output rate (Timbre offers 8000,
    # 16000, 22050, 24000, 44100, 48000; the voice lab compares 16 and 24 kHz).
    tts_sample_rate: int = 16000
    # Ruling 38: how the board's lines go to Timbre. "sentence": one request per sentence as
    # the actor streams it (lowest latency, prosody resets at each sentence); "line": one
    # request per whole guarded line (waits for the actor to finish the line); "first_rest":
    # the first sentence alone, then the rest of the line as one request. The owner picks by
    # ear in the voice lab (`make voice-lab`). UNVERIFIED: whole-line prosody and latency.
    tts_request_unit: str = "sentence"
    # The board's single Timbre voice for the local voice app (persona voice ids are still
    # placeholders until the owner picks them by ear, CTO_NOTES_03 T4).
    tts_voice: str = "Poorvi"
    # P8 (CTO_NOTES_06 section 6): the panel's voices, persona id -> Timbre voice, as JSON in
    # TTS_PANEL_VOICES. Unset: the placeholder table for TTS_MODEL (`DEFAULT_PANEL_VOICES`, below).
    # `{}`: every member speaks with TTS_VOICE, as before P8. A member left out of the table also
    # uses TTS_VOICE. Every voice is checked against the model's catalog before a session starts.
    tts_panel_voices: dict[str, str] | None = None
    # Ruling 30 (CTO_NOTES_05 should-fix 5): if a Timbre request gives no audio within this
    # time, reconnect and resend once, then show the line as text only. UNVERIFIED (T1/T2).
    tts_first_byte_timeout_s: float = Field(default=3.0, gt=0)
    # Ruling 30 (CTO_NOTES_05 must-fix 4): one Timbre request at a time; the next sentence
    # waits at most this long for the previous request's `complete`. UNVERIFIED.
    tts_request_timeout_s: float = Field(default=20.0, gt=0)

    # --- Voice loop turn-taking (V3/V4; Pipecat 1.5.0 Silero + Smart Turn v3) ---
    # Silero VAD (Pipecat's VADParams defaults; Pipecat recommends stop 0.2 s with Smart Turn).
    voice_vad_confidence: float = Field(default=0.7, ge=0, le=1)
    voice_vad_start_secs: float = Field(default=0.2, gt=0)
    voice_vad_stop_secs: float = Field(default=0.2, gt=0)
    voice_vad_min_volume: float = Field(default=0.6, ge=0, le=1)
    # Smart Turn v3's silence fallback (Pipecat default 3 s) and Pipecat's user-turn stop
    # timeout (default 5 s). UNVERIFIED on Hinglish: decided from the false cut-in rate (V9).
    voice_smart_turn_stop_secs: float = Field(default=3.0, gt=0)
    voice_user_turn_stop_timeout_s: float = Field(default=5.0, gt=0)
    # Long turns (a debate opening or closing statement, `session.expects_long_turn`): a
    # pause must also last this long after Smart Turn says "complete", and the silence
    # fallbacks are longer. UNVERIFIED: our choice, CTO_NOTES_04 §7.
    voice_long_turn_extra_pause_s: float = Field(default=1.2, ge=0)
    voice_long_turn_smart_turn_stop_secs: float = Field(default=5.0, gt=0)
    voice_long_turn_stop_timeout_s: float = Field(default=8.0, gt=0)
    # Barge-in (V4): while the board is audibly speaking, the candidate's speech must last
    # this long (after Silero's own start delay) before it counts, so coughs do not cut the
    # board off. Before the board's first audio, resumed speech reopens the turn after the
    # shorter threshold (ruling 31). UNVERIFIED: both are our choice until live runs.
    voice_barge_in_min_speech_ms: int = Field(default=500, ge=0)
    voice_resume_min_speech_ms: int = Field(default=150, ge=0)
    # The UPSC panel's interview limit is 17 minutes (owner's decision, Ruling 47). At the wrap-up
    # the session asks the director to close with the next finished turn (an in-flight question
    # stands); at the hard stop it ends, with the Chairperson's time line if no close was spoken.
    # Prisma's 15-minute cap is per socket, not per session: the stream rotates to a fresh socket
    # at 12 to 14 minutes (`PrismaSTTService`), which this longer session now reaches.
    # UNVERIFIED: rotation has never run in a live session.
    voice_wrap_up_s: float = Field(default=960.0, gt=0)
    voice_hard_stop_s: float = Field(default=1020.0, gt=0)
    # P9: at the hard stop a candidate who is in the middle of speaking is given this long to finish
    # the sentence before the turn is closed for them; the Chairperson's close then follows their
    # answer. 0 closes at once, as P7 did. Our choice, unmeasured.
    voice_hard_stop_grace_s: float = Field(default=20.0, ge=0)
    # P9: how long the likely-questions route waits for Nova's hints before it shows the standard
    # ones. A guess until an approved real run.
    likely_questions_timeout_s: float = Field(default=20.0, gt=0)
    # Debate keeps the 10-minute stop it always had (ADR-009: debate is unchanged).
    voice_debate_max_s: float = Field(default=600.0, gt=0)
    # Defect D: a WebRTC connection that is not `connected` within this time is closed and
    # gets no session (no Prisma or Timbre connection is ever opened for it). Localhost ICE
    # takes well under a second; our choice.
    voice_connect_timeout_s: float = Field(default=15.0, gt=0)

    # --- AWS Bedrock / Nova Pro ---
    aws_region: str = "ap-southeast-2"
    nova_model_id: str = "amazon.nova-pro-v1:0"
    # Execution lanes (CTO_NOTES_02 M5). Every Bedrock call runs on one of three lanes, each
    # with its own small thread pool and its own boto3 client, so slow background work can
    # never take the threads or connections the critical path needs:
    #   actor      - the board's spoken line and the opener (the critical path)
    #   fast       - the fast analysis graph's listener (between end of turn and the actor)
    #   background - fact checks (librarian) and the judge
    bedrock_actor_workers: int = Field(default=4, ge=1)
    bedrock_fast_workers: int = Field(default=4, ge=1)
    bedrock_background_workers: int = Field(default=8, ge=1)
    # botocore's connection pool per lane client (botocore's default is 10). Must be at least
    # the lane's worker count, or threads would queue for a connection (validated below).
    bedrock_max_pool_connections: int = Field(default=16, ge=1)
    # Background lane: generous timeouts; retries come only from the graph RetryPolicy.
    bedrock_connect_timeout_s: float = 5.0
    bedrock_read_timeout_s: float = 30.0
    # Actor lane (ruling 13): short timeouts, so a hung call ends in the fallback line quickly.
    # Ruling 14: the actor gets ONE attempt and no retry anywhere (botocore's is off, and it
    # has no graph RetryPolicy). A failed or late actor call is answered with the speaking
    # persona's cached fallback line instead, because a short natural pause beats seconds of
    # silence while a retry runs.
    # UNVERIFIED: both timeouts until the Nova probe measures TTFT from EC2 (should-fix 6).
    bedrock_actor_connect_timeout_s: float = Field(default=2.0, gt=0)
    bedrock_actor_read_timeout_s: float = Field(default=4.0, gt=0)
    # Ruling 14: the actor's time budget, measured from the request to its first token. If
    # no token has arrived by then, the call is abandoned and the persona's fallback line is
    # spoken. A stream that stalls after its first token ends by the read timeout above and
    # takes the same fallback. Must stay below the read timeout to matter.
    # UNVERIFIED: placeholder until the Nova probe measures the actor's TTFT from EC2.
    actor_first_token_budget_s: float = Field(default=2.5, gt=0)
    # Ruling 17 (CTO_NOTES_03 §5.2): one exception to "the actor is never retried". When on,
    # an actor call that fails with a ThrottlingException within
    # `actor_fast_throttle_window_ms` of its request, before any token arrived, is retried
    # ONCE, inside what is left of the first-token budget. A throttle that comes back that
    # fast leaves room for a second try; a slow failure does not. Off by default: the Nova
    # probe's throttle counts and throttle timings (rule N3) decide whether to turn it on.
    actor_fast_throttle_retry: bool = False
    actor_fast_throttle_window_ms: int = Field(default=300, gt=0)
    # Fast lane (ruling 13): the listener. Converse returns nothing until generation ends, so
    # the read timeout must cover one whole listener call. botocore retries stay off here;
    # the fast graph's RetryPolicy retries, capped by `fast_graph_max_attempts`.
    # UNVERIFIED: both until the --app-prompts Nova probe times real listener calls.
    bedrock_fast_connect_timeout_s: float = Field(default=2.0, gt=0)
    bedrock_fast_read_timeout_s: float = Field(default=5.0, gt=0)
    # Nova's "Choosing a tool" page recommends this stop sequence whenever any tool choice
    # is set (not only the forced `tool` mode), so the model stops after the tool call.
    nova_tool_stop_sequences: list[str] = Field(default_factory=lambda: ["</tool>"])
    # Greedy decoding for forced tool calls (Nova "Troubleshooting tool calls" page). Sent as
    # additionalModelRequestFields.inferenceConfig.topK. Set to None to omit it.
    nova_tool_top_k: int | None = 1
    structured_max_tokens: int = 1200
    # A truncated structured call (stopReason "max_tokens") is retried once with double the
    # limit, never above this ceiling. The Nova Pro model card gives a 10K-token output
    # maximum (checked in CTO_NOTES_03 §1), so 5000 is safely inside it.
    structured_max_tokens_ceiling: int = 5000
    # The judge writes nine rationales plus evidence; a Hindi (Devanagari) report may need
    # more tokens than an English one (UNVERIFIED for Nova's tokenizer).
    judge_max_tokens: int = 2500
    actor_max_tokens: int = 120

    # --- Brain tuning (all deterministic thresholds) ---
    numeric_rel_tolerance: float = 0.05
    numeric_abs_tolerance_pp: float = 1.0
    # Must be >= 1 (validated below): fact checks run in the background and the move never
    # waits for this turn's verdicts (ruling 9), so a same-turn challenge cannot exist.
    fact_challenge_min_delay_turns: int = 1
    strong_streak_to_move_on: int = 3
    reassure_latency_s: float = 4.0
    interrupt_after_words: int = 150
    actor_max_words: int = 30
    # Background graphs (fact check, report): retries on throttling/timeouts.
    graph_retry_max_attempts: int = 3
    graph_retry_initial_interval_s: float = 0.5
    # LangGraph's jitter adds uniform(0, 1) SECONDS to each retry (langgraph 1.2.17), which
    # spreads retries of throttled background calls; off only to make offline tests faster.
    graph_retry_jitter: bool = True
    # Fast analysis graph (ruling 13): fewer, quicker retries. UNVERIFIED: until probe data.
    fast_graph_max_attempts: int = Field(default=2, ge=1)
    fast_graph_retry_initial_interval_s: float = Field(default=0.1, ge=0)
    # Off: up to a whole extra second per retry is unaffordable on the hot path, and with at
    # most one fast retry there is little herd to spread.
    fast_graph_retry_jitter: bool = False
    # How long the fast graph waits for one listener call before abandoning it for the move
    # (ruling 13). The call itself keeps running; if it returns, its claims still go to the
    # fact checker. This keeps a hung call from blocking the session's later segments; the
    # end-of-turn deadline abandons a turn's unfinished analyses sooner.
    # UNVERIFIED: must sit above a normal listener call's latency (Nova probe).
    fast_listener_timeout_s: float = Field(default=3.0, gt=0)
    # End-of-turn deadline for the move in the voice path (ruling 9): `end_of_turn` waits at
    # most this long for the turn's fast analyses, then uses a deterministic fallback move.
    # UNVERIFIED: placeholder until the Nova and Prisma probes run from EC2 (CTO_NOTES_02
    # should-fix 6); it must be set from probe data, not by guessing.
    move_deadline_ms: int = Field(default=1500, gt=0)
    # Text mode has no voice latency budget (the candidate is typing), so the CLI uses a
    # generous deadline; it still bounds a hung listener call.
    text_move_deadline_s: float = Field(default=30.0, gt=0)
    # How long `finish()` waits for background fact checks before writing the report.
    fact_check_drain_timeout_s: float = Field(default=30.0, ge=0)
    # Bounded LRU of real librarian votes shared by all sessions (ruling 10).
    librarian_vote_cache_size: int = Field(default=2048, ge=1)
    # Approximate budget for the cached system prefix (persona pack + fact cards).
    # Nova's prompt cache supports at most 20K tokens; we keep headroom.
    cache_prefix_token_budget: int = 18000

    # --- Prompt versions (prompts/<id>.v<N>.md) ---
    # UPSC is the panel (P7). The old board's listener.v3, actor.v3 and opener.v1 stay on disk,
    # never edited, and are no longer used.
    prompt_listener_version: int = 4  # v3 + the candidate's request, English-only notes
    prompt_librarian_version: int = 1
    prompt_actor_version: int = 8  # v7, but the briefing is background for every member (Nova next)
    prompt_judge_version: int = 1
    prompt_opener_version: int = 2  # no greeting: the ritual line is said by code
    prompt_summary_version: int = 1  # likely-question hints (P9); not in a session's trace map
    prompt_agenda_plan_version: int = 3  # v2, plus a briefing the planner must not copy into a seed
    # Debate mode (ADR-009, CTO_NOTES_04).
    prompt_shared_debate_version: int = 1
    prompt_listener_debate_version: int = 2  # Round 20: Devanagari
    prompt_actor_debate_version: int = 1
    prompt_judge_debate_version: int = 1
    prompt_motion_screen_version: int = 1
    prompt_case_file_version: int = 1
    report_language: str = Field(default="en", pattern=r"^(en|hi)$")

    # --- Panel agenda planning (P6, CTO_NOTES_06 §2, CTO_NOTES_12 §7). Nova only rewrites seed
    # text; the template agenda always exists and stands in if the plan is late, bad or missing.
    agenda_plan_enabled: bool = True
    # How long the opening waits for the plan before the interview starts on the templates. A late
    # plan still upgrades the segments not yet begun. UNVERIFIED: Nova's time for the call; this is
    # a guess until an approved real run.
    agenda_plan_wait_s: float = Field(default=1.5, ge=0)

    # --- Eval gate (ENGINEERING.md §5): `make eval-gate` exits non-zero when a dev-split run
    # catches fewer planted errors than this share, or makes more false corrections than this
    # count. These are our own choices, not industry figures; the recall floor is a
    # placeholder for the owner to set once the frozen golden dev set exists.
    eval_gate_min_planted_recall: float = Field(default=0.8, ge=0, le=1)
    eval_gate_max_false_corrections: int = Field(default=0, ge=0)

    # --- Data locations ---
    cards_dir: Path = REPO_ROOT / "data" / "cards"

    # --- Feature flags: every innovation defaults to the safe setting (off). ---
    # None of these features is built in phase 1; the flags exist so later phases plug in
    # behind them. FEATURE_BOARD_INTERRUPT gates the strategist's `interrupt` rule.
    feature_dual_decode: bool = False
    feature_board_interrupt: bool = False
    feature_echo_filter: bool = False
    feature_prisma_partials: bool = False
    feature_second_prompt_vote: bool = False
    # Debate mode (ruling 21): custom motions go through the fail-closed safety screen.
    # Off by default and off in the public demo: only the 13 curated motions are offered.
    feature_custom_motions: bool = False

    # --- Observability ---
    trace_dir: Path = Path("traces")  # per-turn JSONL traces (git-ignored)
    report_dir: Path = Path("runs/reports")
    langfuse_enabled: bool = False

    @model_validator(mode="after")
    def _rotation_order(self) -> Settings:
        if self.stt_rotate_after_s > self.stt_rotate_hard_s:
            raise ValueError("stt_rotate_after_s must not be above stt_rotate_hard_s")
        return self

    @model_validator(mode="after")
    def _wrap_up_comes_before_the_hard_stop(self) -> Settings:
        if self.voice_wrap_up_s >= self.voice_hard_stop_s:
            raise ValueError("voice_wrap_up_s must be below voice_hard_stop_s")
        return self

    @model_validator(mode="after")
    def _pools_cover_workers(self) -> Settings:
        most = max(
            self.bedrock_actor_workers, self.bedrock_fast_workers, self.bedrock_background_workers
        )
        if self.bedrock_max_pool_connections < most:
            raise ValueError(
                f"bedrock_max_pool_connections ({self.bedrock_max_pool_connections}) must be >= "
                f"the largest lane's worker count ({most}), or lane threads queue for sockets"
            )
        return self

    @field_validator("tts_sample_rate")
    @classmethod
    def _timbre_rate(cls, v: int) -> int:
        if v not in (8000, 16000, 22050, 24000, 44100, 48000):  # tts-websocket.md
            raise ValueError("TTS_SAMPLE_RATE must be 8000, 16000, 22050, 24000, 44100 or 48000")
        return v

    @field_validator("tts_request_unit")
    @classmethod
    def _tts_unit(cls, v: str) -> str:
        if v not in ("sentence", "line", "first_rest"):
            raise ValueError("TTS_REQUEST_UNIT must be sentence, line or first_rest")
        return v

    @field_validator("fact_challenge_min_delay_turns")
    @classmethod
    def _fact_challenge_is_a_turn_late(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                "fact_challenge_min_delay_turns must be >= 1: verdicts come from the background "
                "fact-check graph, which the move never waits for (ruling 9)"
            )
        return v

    def time_limits(self, mode: str = "upsc_interview") -> tuple[float | None, float]:
        """(wrap-up seconds or None, hard stop seconds) of a voice session in this mode: the panel's
        16 and 17 minutes, or debate's single 10-minute stop (debate has no wrap-up)."""
        if mode == "debate":
            return None, self.voice_debate_max_s
        return self.voice_wrap_up_s, self.voice_hard_stop_s

    def panel_voices(self) -> dict[str, str]:
        """persona id -> voice for the panel (P8). `TTS_PANEL_VOICES` wins, `{}` included."""
        if self.tts_panel_voices is not None:
            return dict(self.tts_panel_voices)
        return dict(DEFAULT_PANEL_VOICES.get(self.tts_model, {}))

    def prompt_versions(self, mode: str = "upsc_interview") -> dict[str, int]:
        """The prompt id -> version map recorded in every trace line, for one mode. Another mode
        (ADR-009) lists its own prompts."""
        if mode != "upsc_interview":
            return self._mode_prompt_versions(mode)
        return {
            "listener": self.prompt_listener_version,
            "librarian": self.prompt_librarian_version,
            "actor": self.prompt_actor_version,
            "judge": self.prompt_judge_version,
            "opener": self.prompt_opener_version,
            "agenda_plan": self.prompt_agenda_plan_version,
        }

    def _mode_prompt_versions(self, mode: str) -> dict[str, int]:
        if mode == "debate":
            return {
                "shared_debate": self.prompt_shared_debate_version,
                "listener_debate": self.prompt_listener_debate_version,
                "actor_debate": self.prompt_actor_debate_version,
                "judge_debate": self.prompt_judge_debate_version,
                "librarian": self.prompt_librarian_version,  # the fact-check graph is shared
                "motion_screen": self.prompt_motion_screen_version,
                "case_file": self.prompt_case_file_version,
            }
        raise ValueError(f"no prompt versions for mode {mode!r}")

    def feature_flags(self) -> dict[str, bool]:
        """All feature flags by name, for logging and the report footer."""
        return {
            name.removeprefix("feature_"): bool(getattr(self, name))
            for name in type(self).model_fields
            if name.startswith("feature_")
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached after the first call)."""
    return Settings()
