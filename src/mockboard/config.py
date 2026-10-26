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
    # Actor lane (ruling 13): short timeouts, so a hung call ends in the filler line quickly.
    # The actor has no graph RetryPolicy, so botocore's own retry is its only retry
    # (ENGINEERING §9: at most one retry on the hot path). For ConverseStream botocore can
    # only retry before the stream starts, never after words were spoken.
    # UNVERIFIED: all three until the Nova probe measures TTFT from EC2 (should-fix 6).
    bedrock_actor_connect_timeout_s: float = Field(default=2.0, gt=0)
    bedrock_actor_read_timeout_s: float = Field(default=4.0, gt=0)
    bedrock_actor_max_attempts: int = Field(default=2, ge=1, le=3)
    # Fast lane (ruling 13): the listener. Converse returns nothing until generation ends, so
    # the read timeout must cover one whole listener call. botocore retries stay off here;
    # the fast graph's RetryPolicy retries, capped by `fast_graph_max_attempts`.
    # UNVERIFIED: both until the --app-prompts Nova probe times real listener calls.
    bedrock_fast_connect_timeout_s: float = Field(default=2.0, gt=0)
    bedrock_fast_read_timeout_s: float = Field(default=5.0, gt=0)
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
    # Background graphs (fact check, report): retries on throttling/timeouts.
    graph_retry_max_attempts: int = 3
    graph_retry_initial_interval_s: float = 0.5
    # Fast analysis graph (ruling 13): fewer, quicker retries. UNVERIFIED: until probe data.
    fast_graph_max_attempts: int = Field(default=2, ge=1)
    fast_graph_retry_initial_interval_s: float = Field(default=0.1, ge=0)
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
