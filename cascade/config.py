"""Typed configuration for the whole system.

This module preserves the invariant that **no other module reads the process
environment**. Every tunable reaches the rest of the codebase as a validated
Pydantic model loaded here; a bare ``os.environ`` or ``os.getenv`` anywhere
else in ``cascade/`` is a bug and is caught by
``tests/unit/test_no_bare_environ.py``.

Precedence, highest first: explicit constructor arguments, then environment
variables (``CASCADE_`` prefix, ``__`` nesting delimiter), then the repo's
``.env``, then ``configs/base.yaml``. Secrets are environment-only and never
appear in YAML.

``.env`` is read here rather than only by the Makefile so that ``cascade
doctor`` and ``cascade db status`` work from a plain shell. Its location comes
from ``CASCADE_ENV_FILE``, which the test suite points at a nonexistent path:
a test whose result depended on a developer's machine-local ``.env`` would be
exactly the kind of environment coupling the determinism rules exist to
remove.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

__all__ = [
    "LLMMode",
    "LLMProvider",
    "Settings",
    "child_environment",
    "claude_cli_environment",
    "env_file_path",
    "load_settings",
    "repo_root",
]

REPO_ROOT = Path(__file__).resolve().parent.parent

# CASCADE_-prefixed variables that steer loading rather than populating a
# field, and so are legitimately absent from the model.
_NON_FIELD_ENV_VARS = frozenset({"CASCADE_CONFIG", "CASCADE_ENV_FILE"})

LLMMode = Literal["record", "replay", "live"]
# Who serves the model (ADR-0028, ADR-0031). The first three are SDK clients
# from the one `anthropic` package; `claude_code` is the Claude Code CLI run
# headless on the operator's machine. All four are reached only through
# LLMClient, so invariant 5's single door is unaffected by the choice.
LLMProvider = Literal["anthropic", "aws", "bedrock", "claude_code"]
Grounding = Literal["chronofence", "parametric_only"]
CacheTTL = Literal["5m", "1h"]


def repo_root() -> Path:
    """Return the repository root.

    Preserves the invariant that paths in config are resolved relative to the
    repository, not the caller's working directory, so a CLI invoked from a
    subdirectory reads the same config as one invoked from the root.
    """
    return REPO_ROOT


def _to_decimal(value: Any) -> Decimal:
    """Coerce a YAML scalar to ``Decimal`` without inheriting binary-float error.

    ``Decimal(0.1)`` is 0.1000000000000000055511151231257827..., which would
    make the cost meter's 6-decimal-place assertion fail for reasons that have
    nothing to do with the meter. Routing through ``str`` preserves the
    decimal literal the author wrote.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


Money = Annotated[Decimal, BeforeValidator(_to_decimal)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StudyConfig(_Model):
    name: str
    salt: str
    manifest_sha256: str | None = None


class ModelsConfig(_Model):
    agent: str
    compiler: str
    embedding: str
    temperature: float
    compiler_temperature: float
    use_batch_api: bool
    agent_max_tokens: int
    compiler_max_tokens: int


class PricingEntry(_Model):
    input_per_mtok: Money
    output_per_mtok: Money


class PricingMultipliers(_Model):
    batch: Money
    cache_read: Money
    cache_write_5m: Money
    cache_write_1h: Money


class PromptCacheConfig(_Model):
    enabled: bool
    ttl: CacheTTL
    min_prefix_tokens: int
    enforce_min_prefix: bool


class ActivationConfig(_Model):
    salience_threshold: float
    forced_interval: int
    max_active_per_step: int


class MemoryConfig(_Model):
    recent_observations: int
    summary_interval: int


class KernelConfig(_Model):
    steps: int
    max_step_delta: float
    contest_gamma: float
    # Characters of each retrieved chunk carried into an agent's cacheable
    # prefix. Bounded so the prefix has a stable length, and large enough that
    # the prefix clears the provider's 4,096-token cache floor (ADR-0001).
    evidence_chars: int
    activation: ActivationConfig
    memory: MemoryConfig


class Channel(_Model):
    noise_sigma: float
    lag: int
    quantized: bool


class ApertureConfig(_Model):
    hop1: Channel
    hop2: Channel
    public: Channel


class RetrievalConfig(_Model):
    k_agent: int
    k_compiler: int
    target_p95_ms: float
    partition_granularity: Literal["month", "quarter"]
    # HNSW's query-time breadth, pinned into `chronofence_search` by migration
    # 014 the way `ivfflat.probes` was by 007. An integration test asserts the
    # deployed function and this value agree. Build parameters, unlike
    # IVFFlat's `lists`, do not depend on the row count -- which is what
    # retires ADR-0012's drift class (ADR-0026). `max_k` bounds the candidate
    # pool the function draws with a constant limit.
    hnsw_m: int = Field(ge=2, le=100)
    hnsw_ef_construction: int = Field(ge=4, le=1000)
    hnsw_ef_search: int = Field(ge=1, le=1000)
    max_k: int = Field(gt=0)
    bench_queries: int = Field(gt=0)
    bench_recall_k: int = Field(gt=0)
    bench_recall_sample: float = Field(gt=0.0, le=1.0)
    target_recall_at_k: float = Field(gt=0.0, le=1.0)
    poison_pill_count: int = Field(gt=0)
    signature_similarity_threshold: float = Field(gt=0.0, le=1.0)


class EnsembleConfig(_Model):
    replicates: int
    ablation_replicates: int
    ablation_scenarios: int
    bootstrap_b: int
    sigma_multimodal_threshold: float


class BudgetConfig(_Model):
    phase_ceiling_usd: dict[str, Money]
    abort_on_breach: bool


class FlagsConfig(_Model):
    causal_decomposition: bool
    information_asymmetry: bool
    grounding: Grounding
    # Panel size for the `causal_decomposition: false` arm (Appendix C factor
    # A off, "generic persona panel"). Configuration rather than a constant so
    # it can be set to the decomposition arm's measured mean actor count --
    # otherwise the two arms differ in panel size as well as in structure and
    # the reported delta contains both.
    panel_actors: int


class LLMConfig(_Model):
    mode: LLMMode
    # Not part of the cache key (ADR-0029): the key carries the logical model,
    # and the provider only decides where the request is sent and how it is
    # billed.
    provider: LLMProvider
    cache_dir: str
    prompt_rev: str
    max_retries: int
    timeout_s: float


class AWSProviderConfig(_Model):
    """Routing for Claude Platform on AWS (ADR-0028).

    Routing only. Identity is the ambient IAM principal resolved by the
    standard AWS credential chain -- an instance or task role in the cloud, a
    profile on a workstation -- so no credential ever appears in this file.

    Every routing value is passed to the SDK explicitly. Left unset, the SDK
    falls back to ``AWS_REGION`` and ``ANTHROPIC_AWS_WORKSPACE_ID`` from
    whatever shell it runs in, which would decide where the study's spend
    lands without that choice appearing in any reviewed file.
    """

    region: str | None
    workspace_id: str | None
    base_url: str | None
    profile: str | None
    # Empty by design. Rates are read from the provider's live pricing page at
    # the time a provider is enabled, never transcribed from memory: a wrong
    # rate is a silent ledger error that surfaces only at reconciliation.
    pricing: dict[str, PricingEntry]


class BedrockProviderConfig(_Model):
    """Routing for Amazon Bedrock (ADR-0028). Same identity rule as AWS."""

    region: str | None
    base_url: str | None
    profile: str | None
    # Logical model -> Bedrock model id. Absent entries render as
    # ``anthropic.<logical>``, the documented Bedrock form; an entry overrides
    # it without a code change when a deployment publishes something else.
    model_ids: dict[str, str]
    pricing: dict[str, PricingEntry]


class ClaudeCodeProviderConfig(_Model):
    """The Claude Code CLI, run headless under a Claude subscription (ADR-0031).

    Local-only by design: it authenticates as the person logged in to Claude
    Code on this machine, which is not an identity that belongs inside cloud
    infrastructure. The deployed design uses ``aws`` or ``bedrock``.
    """

    executable: str
    # Where the CLI runs. Null means a fresh temporary directory per client.
    # It must not sit below any CLAUDE.md: the CLI auto-discovers them upward
    # from its working directory, and this repository's build contract is
    # ~27k tokens that would be injected into every forecasting call.
    workdir: str | None


class ProvidersConfig(_Model):
    aws: AWSProviderConfig
    bedrock: BedrockProviderConfig
    claude_code: ClaudeCodeProviderConfig


SSLMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


class DatabaseConfig(_Model):
    host: str
    port: int
    name: str
    admin_user: str
    sim_user: str
    eval_user: str
    # Always written into the connection URL, even when it equals libpq's own
    # default: an explicit parameter outranks an ambient PGSSLMODE, so the
    # shell cannot weaken a deployment that asked for verify-full (ADR-0034).
    sslmode: SSLMode
    # CA bundle for verify-ca / verify-full. For Aurora, the RDS global bundle.
    sslrootcert: str | None
    # `iam` replaces the two application roles' stored secrets with short-lived
    # RDS tokens signed by the ambient IAM principal (ADR-0034). Identity is
    # ambient; routing -- the region the token is signed for -- is explicit, as
    # in ADR-0028. The admin role always uses its stored secret: on Aurora that
    # secret is generated and rotated by RDS itself, and granting rds_iam to a
    # role disables its password, which would break that rotation.
    auth: Literal["stored", "iam"]
    iam_region: str | None

    @model_validator(mode="after")
    def _coherent(self) -> DatabaseConfig:
        """Refuse configurations that would fail late or weaken silently."""
        if self.sslmode in ("verify-ca", "verify-full") and not self.sslrootcert:
            raise ValueError(
                f"database.sslmode={self.sslmode!r} needs database.sslrootcert: without a CA "
                "bundle libpq cannot verify anything and the connection fails at the first use"
            )
        if self.auth == "iam":
            if not self.iam_region:
                raise ValueError(
                    "database.auth='iam' needs database.iam_region: the token is signed for a "
                    "region, and that must not come from an ambient AWS_REGION (ADR-0028)"
                )
            if self.sslmode not in ("verify-ca", "verify-full"):
                raise ValueError(
                    "database.auth='iam' needs sslmode verify-ca or verify-full: an IAM token "
                    "is a bearer credential, and sending one to an unverified server hands it "
                    "to whoever answers"
                )
        return self


class LangfuseConfig(_Model):
    enabled: bool
    host: str


class LedgerConfig(_Model):
    """Scenario-registry constraints (spec §3.1).

    These are *inclusion constraints*, not results. The measurement contract
    forbids writing a target metric into a report path; a constraint that
    defines which questions may enter the set is the opposite -- it has to be
    explicit, versioned and asserted, or the set is not reproducible.
    """

    target_scenarios: int
    yes_rate_min: float
    yes_rate_max: float
    max_domain_share: float
    min_volume: float
    cutoff_fraction: float
    source_cache_dir: str
    curated_dir: str
    polymarket_pages: int
    polymarket_page_size: int
    manifold_pages: int
    manifold_page_size: int
    metaculus_pages: int
    metaculus_page_size: int


class CorpusConfig(_Model):
    """Evidence-corpus ingest settings (spec §3.2)."""

    target_chunks: int
    # `target_chunks` is a floor that `corpus verify` asserts; nothing ever made
    # it a stopping rule, so an unbounded build walks every generated unit --
    # measured at M10: ~1,460 CC-NEWS files, ~140 GB, against 27 GB of free
    # disk, on a project whose git store was once destroyed by a full disk.
    # The build now stops starting new units at this many stored chunks. It is
    # a ceiling on *work*, not a result: coverage is still judged by
    # `corpus coverage`, and raising this and re-running resumes where it left.
    max_chunks: int
    # Stop starting new units below this much free space on the volume holding
    # the repository (with Docker Desktop, the same disk the database grows
    # on). Null disables the check, for a remote database.
    min_free_disk_gb: float | None

    @model_validator(mode="after")
    def _ceiling_clears_the_floor(self) -> CorpusConfig:
        if self.max_chunks < self.target_chunks:
            raise ValueError(
                f"corpus.max_chunks ({self.max_chunks:,}) is below corpus.target_chunks "
                f"({self.target_chunks:,}): the build would stop before `corpus verify` "
                "could ever pass"
            )
        return self

    # SEC EDGAR requires a contact address in the User-Agent; see
    # cascade/corpus/fetch.py. Put a real address here before a long ingest.
    contact: str
    start_year: int
    end_year: int
    chunk_max_tokens: int
    chunk_overlap_tokens: int
    embed_batch_size: int
    min_body_chars: int
    requests_per_second: float
    max_documents_per_unit: int
    write_batch_size: int
    enabled_sources: tuple[str, ...]
    gdelt_max_records: int
    fetch_workers: int
    ccnews_max_files: int
    ccnews_max_records_per_file: int
    coverage_lookback_months: int
    coverage_min_chunks: int
    wikipedia_max_articles_per_scenario: int


class PathsConfig(_Model):
    checkpoints: str
    reports: str
    graphs: str


class _YamlSource(PydanticBaseSettingsSource):
    """Feed ``configs/base.yaml`` into the settings chain as the lowest priority."""

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._path = path

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Whole-document sources implement __call__ instead; this is required
        # by the ABC but never consulted for our usage.
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        if not self._path.is_file():
            raise FileNotFoundError(f"config file not found: {self._path}")
        with self._path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        if not isinstance(loaded, dict):
            raise ValueError(f"config file {self._path} must contain a YAML mapping")
        return loaded


class Settings(BaseSettings):
    """Root configuration object. Constructed once, passed explicitly thereafter."""

    model_config = SettingsConfigDict(
        env_prefix="CASCADE_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    study: StudyConfig
    models: ModelsConfig
    pricing: dict[str, PricingEntry]
    pricing_multipliers: PricingMultipliers
    prompt_cache: PromptCacheConfig
    kernel: KernelConfig
    aperture: ApertureConfig
    retrieval: RetrievalConfig
    ensemble: EnsembleConfig
    budget: BudgetConfig
    flags: FlagsConfig
    llm: LLMConfig
    providers: ProvidersConfig
    ledger: LedgerConfig
    corpus: CorpusConfig
    database: DatabaseConfig
    langfuse: LangfuseConfig
    paths: PathsConfig

    # --- Secrets: environment only, never YAML ------------------------------
    anthropic_api_key: SecretStr | None = Field(default=None)
    # Metaculus rejects unauthenticated API requests (see
    # cascade/ledger/sources/metaculus.py). Absent, the loader reports the
    # source as unavailable rather than contributing silently.
    metaculus_token: SecretStr | None = Field(default=None)
    db_admin_password: SecretStr | None = Field(default=None)
    db_sim_password: SecretStr | None = Field(default=None)
    db_eval_password: SecretStr | None = Field(default=None)
    langfuse_public_key: SecretStr | None = Field(default=None)
    langfuse_secret_key: SecretStr | None = Field(default=None)

    @model_validator(mode="after")
    def _reject_unrecognised_overrides(self) -> Settings:
        """Fail on a ``CASCADE_``-prefixed variable that binds to nothing.

        ``extra="forbid"`` catches a typo *inside* a known section
        (``CASCADE_KERNEL__STEPZ``), but pydantic-settings harvests only the
        variables that match a field, so a typo in the **root** segment is
        silently discarded: ``CASCADE_ENSEMBLE_REPLICATES`` (single underscore
        where the nesting delimiter belongs) sets nothing, reports nothing, and
        leaves the study running on the default while the operator believes it
        is overridden. That is precisely the silent-default failure the
        no-bare-environ rule exists to prevent, so it is an error here.

        Only the root segment is checked, because sections keyed by a dict --
        ``CASCADE_BUDGET__PHASE_CEILING_USD__SIMULATE`` -- have legitimately
        dynamic leaves.
        """
        known = {name.upper() for name in type(self).model_fields}
        offenders = sorted(
            name
            for name in os.environ
            if name.startswith("CASCADE_")
            and name not in _NON_FIELD_ENV_VARS
            and name[len("CASCADE_") :].split("__", 1)[0] not in known
        )
        if offenders:
            raise ValueError(
                f"unrecognised CASCADE_ environment variable(s): {', '.join(offenders)}. "
                f"Known top-level settings: {', '.join(sorted(known))}. "
                "Nested keys use a double underscore, e.g. "
                "CASCADE_ENSEMBLE__REPLICATES. A variable that binds to nothing "
                "would leave the study on its default value without saying so."
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Both paths are resolved at call time, not at class-creation time, so
        # a test can redirect either one without re-importing this module.
        yaml_path = Path(os.environ.get("CASCADE_CONFIG", REPO_ROOT / "configs" / "base.yaml"))
        env_path = Path(os.environ.get("CASCADE_ENV_FILE", REPO_ROOT / ".env"))
        del dotenv_settings  # replaced by the explicitly-located source below
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(
                settings_cls,
                env_file=env_path,
                env_file_encoding="utf-8",
                env_prefix="CASCADE_",
                env_nested_delimiter="__",
                case_sensitive=False,
                # `.env` legitimately carries two namespaces: CASCADE_* settings
                # and LANGFUSE_*/compose-only infrastructure values. Only the
                # first belongs to this model. "only_existing" reads it exactly
                # as the environment source does -- prefix applied, unknown keys
                # left alone -- so a compose variable is not mistaken for a
                # typo'd setting. `extra="forbid"` still rejects a real typo in
                # a CASCADE_-prefixed name.
                dotenv_filtering="only_existing",
            ),
            _YamlSource(settings_cls, yaml_path),
        )

    # --- Derived accessors --------------------------------------------------

    def cache_path(self) -> Path:
        return self._resolve(self.llm.cache_dir)

    def checkpoint_path(self) -> Path:
        return self._resolve(self.paths.checkpoints)

    def report_path(self) -> Path:
        return self._resolve(self.paths.reports)

    def graph_path(self) -> Path:
        return self._resolve(self.paths.graphs)

    def source_cache_path(self) -> Path:
        return self._resolve(self.ledger.source_cache_dir)

    def curated_path(self) -> Path:
        return self._resolve(self.ledger.curated_dir)

    def _resolve(self, raw: str) -> Path:
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else REPO_ROOT / candidate

    def pricing_table(self) -> dict[str, PricingEntry]:
        """The price table of the active provider (ADR-0028).

        Preserves the invariant that a call is priced at the rate of whoever
        served it. Bedrock is partner-operated and priced separately, so
        falling back to the first-party table for it would book every Bedrock
        call at the wrong rate and reconcile against nothing.
        """
        provider = self.llm.provider
        if provider == "anthropic":
            return self.pricing
        if provider == "aws":
            return self.providers.aws.pricing
        if provider == "bedrock":
            return self.providers.bedrock.pricing
        # A subscription bills no tokens, so every model it serves costs zero
        # in the ledger -- which is the truth, and what makes the M8 ledger
        # reconciliation report "nothing to reconcile" rather than inventing a
        # spend. The CLI's notional list-price cost is kept on each recording.
        zero = PricingEntry(input_per_mtok=Decimal(0), output_per_mtok=Decimal(0))
        return {self.models.agent: zero, self.models.compiler: zero}

    def price_for(self, model: str) -> PricingEntry:
        """Return the active provider's price table entry for ``model``.

        Raises rather than defaulting: an unpriced model silently costing $0
        would corrupt the cost ledger, which blocks the report at M8.
        """
        table = self.pricing_table()
        try:
            return table[model]
        except KeyError:
            known = ", ".join(sorted(table)) or "none"
            section = (
                "pricing"
                if self.llm.provider == "anthropic"
                else f"providers.{self.llm.provider}.pricing"
            )
            raise KeyError(
                f"no pricing entry for model {model!r} under provider "
                f"{self.llm.provider!r}; known models: {known}. Add it to {section} "
                "in configs/base.yaml -- an unpriced model breaks the cost ledger."
            ) from None

    def phase_ceiling(self, phase: str) -> Decimal:
        try:
            return self.budget.phase_ceiling_usd[phase]
        except KeyError:
            known = ", ".join(sorted(self.budget.phase_ceiling_usd))
            raise KeyError(
                f"no budget ceiling configured for phase {phase!r}; known phases: {known}"
            ) from None

    def _database_user(self, role: Literal["admin", "sim", "eval"]) -> str:
        return {
            "admin": self.database.admin_user,
            "sim": self.database.sim_user,
            "eval": self.database.eval_user,
        }[role]

    def database_secret(self, role: Literal["admin", "sim", "eval"]) -> str:
        """The credential ``role`` logs in with: its password, or a fresh IAM token.

        Preserves the invariant that a credential is produced in exactly one
        place. A token is minted per call and is valid for 15 minutes, which
        only has to cover the connect: an established session outlives it.
        """
        if self.database.auth == "iam" and role != "admin":
            return _rds_auth_token(
                host=self.database.host,
                port=self.database.port,
                user=self._database_user(role),
                region=self.database.iam_region or "",
            )
        password = {
            "admin": self.db_admin_password,
            "sim": self.db_sim_password,
            "eval": self.db_eval_password,
        }[role]
        return password.get_secret_value() if password else ""

    def database_url(
        self, role: Literal["admin", "sim", "eval"], *, with_secret: bool = True
    ) -> str:
        """Build a libpq URL for one of the three roles.

        Kept here rather than in the DB layer so credentials never need an
        environment read outside this module. The secret is percent-encoded:
        an RDS-generated password or an IAM token carries ``#``, ``?``, ``%``
        and ``&``, any of which would silently re-parse the URL around it.

        ``with_secret=False`` is for anything that lands in a process's
        argument list, where every local user can read it; the caller passes
        :meth:`database_secret` through ``PGPASSWORD`` instead.
        """
        user = quote(self._database_user(role), safe="")
        userinfo = user
        if with_secret:
            userinfo = f"{user}:{quote(self.database_secret(role), safe='')}"
        params = [f"sslmode={self.database.sslmode}"]
        if self.database.sslrootcert:
            params.append(f"sslrootcert={quote(self.database.sslrootcert, safe='/')}")
        return (
            f"postgresql://{userinfo}@{self.database.host}:"
            f"{self.database.port}/{self.database.name}?{'&'.join(params)}"
        )


def _rds_auth_token(*, host: str, port: int, user: str, region: str) -> str:
    """Mint an RDS IAM authentication token for ``user`` (ADR-0034).

    boto3 is imported lazily -- it lives in the ``aws`` extra, and nothing
    local needs it. Signing is local computation over the ambient IAM
    credentials; no request is sent.
    """
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "database.auth='iam' needs boto3: install the `aws` extra (uv sync --extra aws)"
        ) from exc
    client = boto3.client("rds", region_name=region)
    return str(client.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user))


@lru_cache(maxsize=8)
def _cached_settings(overlay: str | None) -> Settings:
    # Zero-argument construction is correct: the YAML settings source supplies
    # the required fields. The pydantic mypy plugin understands this; without
    # it the call reads as fifteen missing arguments.
    base = Settings()
    if overlay is None:
        return base
    overlay_path = REPO_ROOT / "configs" / "ablations" / f"{overlay}.yaml"
    if not overlay_path.is_file():
        raise FileNotFoundError(f"ablation overlay not found: {overlay_path}")
    with overlay_path.open("r", encoding="utf-8") as handle:
        patch = yaml.safe_load(handle) or {}
    merged = _deep_merge(base.model_dump(mode="python"), patch)
    return Settings(**merged)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay ``patch`` onto ``base`` without mutating either.

    Iterates in sorted key order (invariant 7). Nothing here depends on the
    order today, but an unsorted walk over a mapping is the nondeterminism
    vector the invariant exists to remove, and the static check in
    ``tests/unit/test_invariants.py`` does not special-case config.
    """
    out = dict(base)
    for key, value in sorted(patch.items()):
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out


def env_file_path() -> Path:
    """Where :class:`Settings` reads its secrets from.

    Exposed so a caller that needs to hand the location to a *child* process --
    `cascade trace replay` spawns one interpreter per replay -- does not have
    to read the environment itself. Invariant: only this module does that, and
    the check is textual, so the way to keep it honest is to give every
    legitimate need a function here rather than an exemption there.
    """
    return Path(os.environ.get("CASCADE_ENV_FILE", REPO_ROOT / ".env"))


def child_environment(**overrides: str) -> dict[str, str]:
    """The environment a Cascade subprocess should run with.

    Inherits the parent's -- a child needs PATH, HOME and the rest -- then
    applies ``overrides`` and pins ``CASCADE_ENV_FILE`` to the file this
    process actually resolved. A child that re-derived that from ambient
    environment would work from a shell and fail wherever the environment is
    deliberately controlled, which is exactly where determinism is checked.
    """
    env = dict(os.environ)
    env["CASCADE_ENV_FILE"] = str(env_file_path())
    env.update(overrides)
    return env


# Variables that attach a process to a running Claude Code session. A child CLI
# that inherited them would try to report into *this* session rather than run
# as an independent headless call.
_CLAUDE_SESSION_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_EFFORT")

# Credentials that would silently switch the CLI from the subscription to
# per-token API billing -- the "#1 auth trap" in the SDK's own documentation.
# The pay-as-you-go path is `llm.provider: anthropic`; keeping the two apart is
# what makes switching between them a configuration change and not a surprise.
_CLI_CREDENTIAL_OVERRIDES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def claude_cli_environment() -> dict[str, str]:
    """The environment ``claude -p`` runs with (ADR-0031).

    Inherits PATH, HOME and the keychain access the subscription login needs,
    then removes the session-attachment variables and any API credential, and
    turns extended thinking off. Thinking is off on the API path, so leaving
    the CLI's default on would change the model's behaviour between providers
    (measured: 235 of 254 output tokens were thinking on a one-line answer).
    """
    env = {
        name: value
        for name, value in sorted(os.environ.items())
        if not name.startswith(_CLAUDE_SESSION_PREFIXES) and name not in _CLI_CREDENTIAL_OVERRIDES
    }
    env["MAX_THINKING_TOKENS"] = "0"
    return env


def load_settings(overlay: str | None = None) -> Settings:
    """Load configuration, optionally overlaid with an ablation cell.

    ``overlay`` names a file in ``configs/ablations/`` without its extension.
    Results are cached so repeated calls in one process return an identical
    object -- config is immutable within a run by construction.
    """
    return _cached_settings(overlay)
