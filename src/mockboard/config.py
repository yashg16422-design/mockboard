"""One settings object for the whole app, loaded from environment variables (and `.env`).

Every region, model id, threshold and feature flag lives here so that nothing is
hard-coded elsewhere. Secrets are read from the environment only; AWS credentials are
never read here at all, because boto3 uses the default credential chain.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    tts_model: str = "timbre-v2.5"
    # Ruling 8: Timbre language for the board's voice; Hinglish by default.
    # UNVERIFIED: the exact language-code string Timbre expects; the timbre probe settles it.
    tts_language: str = "hi-en"
    tts_sample_rate: int = 16000

    # --- AWS Bedrock / Nova Pro ---
    aws_region: str = "ap-southeast-2"
    nova_model_id: str = "amazon.nova-pro-v1:0"
    bedrock_connect_timeout_s: float = 5.0
    bedrock_read_timeout_s: float = 30.0
    # Nova's tool-use guide recommends a stop sequence when forcing a tool, so the model
    # does not keep generating after the tool call.
    nova_tool_stop_sequences: list[str] = Field(default_factory=lambda: ["</tool>"])
    # Greedy decoding for forced tool calls (Nova "Troubleshooting tool calls" page). Sent as
    # additionalModelRequestFields.inferenceConfig.topK. Set to None to omit it.
    nova_tool_top_k: int | None = 1
    structured_max_tokens: int = 1200
    # A truncated structured call (stopReason "max_tokens") is retried once with double the
    # limit, never above this ceiling. UNVERIFIED: Nova Pro's per-request output maximum.
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
    max_turns_per_topic: int = 4
    strong_streak_to_move_on: int = 3
    reassure_latency_s: float = 4.0
    interrupt_after_words: int = 150
    actor_max_words: int = 30
    graph_retry_max_attempts: int = 3
    graph_retry_initial_interval_s: float = 0.5
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
    prompt_listener_version: int = 2
    prompt_librarian_version: int = 1
    prompt_actor_version: int = 2
    prompt_judge_version: int = 1
    prompt_opener_version: int = 1
    report_language: str = Field(default="en", pattern=r"^(en|hi)$")

    # --- Data locations ---
    cards_dir: Path = REPO_ROOT / "data" / "cards"
    topics: list[str] = Field(default_factory=lambda: ["tourism", "internal_security"])

    # --- Feature flags: every innovation defaults to the safe setting (off). ---
    # None of these features is built in phase 1; the flags exist so later phases plug in
    # behind them. FEATURE_BOARD_INTERRUPT gates the strategist's `interrupt` rule.
    feature_dual_decode: bool = False
    feature_board_interrupt: bool = False
    feature_echo_filter: bool = False
    feature_prisma_partials: bool = False
    feature_second_prompt_vote: bool = False

    # --- Observability ---
    trace_dir: Path = Path("traces")  # per-turn JSONL traces (git-ignored)
    report_dir: Path = Path("runs/reports")
    langfuse_enabled: bool = False

    @field_validator("fact_challenge_min_delay_turns")
    @classmethod
    def _fact_challenge_is_a_turn_late(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                "fact_challenge_min_delay_turns must be >= 1: verdicts come from the background "
                "fact-check graph, which the move never waits for (ruling 9)"
            )
        return v

    def prompt_versions(self) -> dict[str, int]:
        """The prompt id -> version map recorded in every trace line."""
        return {
            "listener": self.prompt_listener_version,
            "librarian": self.prompt_librarian_version,
            "actor": self.prompt_actor_version,
            "judge": self.prompt_judge_version,
            "opener": self.prompt_opener_version,
        }

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
