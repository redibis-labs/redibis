"""
redibis.config
==============
Single configuration surface for CLI, library, and (via adapters) the web session.

``RedibisConfig`` round-trips to YAML with deep-merge over defaults. Legacy
Workflow A/B types (``PipelineConfig``, ``GEPipelineConfig``) live in
``redibis.pipeline_config`` and project from ``RedibisConfig``. Session/scan
adapters (``ScanConfig``, ``GlobalConfig``) project from it without changing
behaviour this milestone.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional, Union, get_type_hints

import yaml

from redibis.pii.thresholds import DEFAULT_EQUATION, EQUATION_MODES, Thresholds
from redibis.quality.rule_set import QualityRuleSet

_VALID_SCAN_TYPES = frozenset({"profile", "quality", "pii"})
_VALID_AUTOMERGE = frozenset({"none", "pii", "quality", "both"})
_VALID_PII_ENGINES = frozenset({"regex", "gliner", "ner", "llm", "both"})
_VALID_SOURCE_ENGINES = frozenset({"none", "hive", "postgres", "oracle", "jdbc"})
_VALID_MEMORY_STORES = frozenset({"pgvector", "memory"})
_VALID_CATALOG_BACKENDS = frozenset({"openmetadata", "atlas", "datahub"})
_VALID_RAI_MODES = frozenset({"block", "warn", "report"})

PathLike = Union[str, Path]


class ConfigError(ValueError):
    """Raised when configuration is invalid or cannot be loaded."""


# ── Nested config blocks ─────────────────────────────────────────────────────

@dataclass
class OpenMetadataConfig:
    """OpenMetadata profiler settings (Phase 5)."""
    include_frequent_values: bool = True
    max_frequent_values: int = 10


@dataclass
class CatalogPushConfig:
    tags: bool = True
    glossary: bool = True
    contract: bool = True
    quality: bool = True
    masked_samples: bool = False
    lifecycle_diff: bool = True


@dataclass
class OpenMetadataCatalogConfig:
    host: str = "http://localhost:8585"
    jwt_env: str = "OM_BOT_JWT"
    service_name: str = "redibis"
    default_schema: str = "default"  # Redibis-managed path only
    entity_mode: str = "mixed"  # enrich_existing | create_if_missing | mixed
    fqn_map: dict = field(default_factory=dict)
    search_services: list = field(default_factory=list)
    min_version: str = "1.12.4"  # CVE / capability floor
    publish_threshold: float = 0.60
    confirm_threshold: float = 0.85
    delete_after_negatives: int = 2
    resolve_cache_ttl_hours: int = 168  # entity FQN cache TTL (default 7 days)


@dataclass
class AtlasCatalogConfig:
    host: str = "http://localhost:21000"
    username_env: str = "ATLAS_USERNAME"
    password_env: str = "ATLAS_PASSWORD"
    cluster: str = "redibis"
    entity_type: str = "hive_table"


@dataclass
class DataHubCatalogConfig:
    host: str = "http://localhost:8080"
    token_env: str = "DATAHUB_GMS_TOKEN"
    platform: str = "redibis"
    env: str = "PROD"
    actor: str = "urn:li:corpuser:redibis"


@dataclass
class CatalogConfig:
    """Data-governance catalog push (backend-neutral)."""
    backend: str = "openmetadata"
    push: CatalogPushConfig = field(default_factory=CatalogPushConfig)
    openmetadata: OpenMetadataCatalogConfig = field(default_factory=OpenMetadataCatalogConfig)
    atlas: AtlasCatalogConfig = field(default_factory=AtlasCatalogConfig)
    datahub: DataHubCatalogConfig = field(default_factory=DataHubCatalogConfig)


@dataclass
class ProfilingMetadataConfig:
    """Tier A — catalog metadata (off by default)."""
    enabled: bool = False


@dataclass
class ProfilingPushdownConfig:
    """Tier B — pushdown aggregates for catalog gaps (off by default)."""
    enabled: bool = False
    approx_distinct: bool = True


@dataclass
class ProfilingConfig:
    engine: str = "great_expectations"
    triage_threshold: float = 0.0
    arabic_threshold: float = 0.05
    openmetadata: OpenMetadataConfig = field(default_factory=OpenMetadataConfig)
    metadata: ProfilingMetadataConfig = field(default_factory=ProfilingMetadataConfig)
    pushdown: ProfilingPushdownConfig = field(default_factory=ProfilingPushdownConfig)


@dataclass
class JdbcConfig:
    host: str = ""
    port: int = 0
    database: str = ""
    credential_ref: str = ""


@dataclass
class SourceConfig:
    """Source connection for catalog / pushdown tiers."""
    engine: str = "none"
    spark_session_injected: bool = True
    jdbc: JdbcConfig = field(default_factory=JdbcConfig)


@dataclass
class MemoryConfig:
    """Column memory / enrichment learning loop (off by default)."""
    enabled: bool = False
    store: str = "pgvector"
    dsn_ref: str = ""
    embedding_provider: str = "sentence_transformers"
    embedding_model: str = "all-MiniLM-L6-v2"
    top_k: int = 5
    min_similarity: float = 0.0
    domain: str = ""
    async_writes: bool = True


@dataclass
class GlinerConfig:
    """Deprecated — use ``NERConfig`` / ``pii.ner.model_path`` instead."""

    model_id: str = ""
    device: str = "cpu"
    batch_size: int = 8


@dataclass
class NERConfig:
    type: str = "gliner"
    model_path: str = ""
    labels: list[str] = field(default_factory=list)
    label_groups: list[list[str]] = field(default_factory=list)
    models: list[dict] = field(default_factory=list)
    max_models: int = 4
    max_passes: int = 8
    device: str = "cpu"
    threshold: float = 0.3
    batch_size: int = 8
    always_run: bool = False


@dataclass
class LLMConfig:
    provider: str = "gemini"
    model_name: str = "gemini-3.5-flash"
    api_key: str = ""
    endpoint_url: Optional[str] = None
    temperature: float = 0.0
    enabled: bool = False
    # Free-text PII scanning (Text Gateway + /api/pii/text/*) sends raw pasted
    # text to the model. False (default) restricts that path to local
    # providers (ollama/vllm/demo) or an endpoint that resolves to a private
    # host. Set True to explicitly permit a cloud provider — RAI residency
    # policy (``rai.block_external_raw_pii`` / ``rai.enforce``) still applies.
    allow_external_raw_text: bool = False


@dataclass
class PIILoggingConfig:
    """Per-pattern decision logging during PII detection."""
    log_regex_hits: bool = True
    max_regex_hits_logged: int = 20


@dataclass
class PIITuningConfig:
    """LLM/agent tuning bundle output on scan."""
    bundle: bool = False


@dataclass
class LearnedClassifierConfig:
    """Structured learned classifier consume seam (Phase 2). Disabled by default."""

    enabled: bool = False
    promoted: bool = False
    model_path: str = ""
    type: str = "gbt"  # gbt | encoder | null
    min_score: float = 0.90
    residency: str = "portable"  # portable | local — LocalLlmInferBackend requires local


@dataclass
class DeviceIdConfig:
    imei_valid_rate_min: float = 0.85
    verify_rbi: bool = True
    tac_database: str = ""


@dataclass
class SubscriberIdConfig:
    home_mcc: str = "602"
    roaming_mncs: dict = field(default_factory=dict)
    imsi_valid_rate_min: float = 0.90


@dataclass
class GeoConfig:
    require_pair: bool = True
    geofence: str = "egypt"
    geofence_rate_min: float = 0.80
    min_decimal_places: int = 4
    detect_scaled_integers: bool = True
    confidence_min: float = 0.75


@dataclass
class NationalIdEgyptConfig:
    """Egyptian National ID structural validator + column gate (operator-tunable)."""

    enabled: bool = True
    max_age: int = 110
    min_birth_year: int = 1900
    verify_check_digit: bool = False
    valid_rate_min: float = 0.85
    exclude_name_patterns: list[str] = field(default_factory=list)


@dataclass
class PIIConfig:
    engines: str = "both"
    equation_mode: str = DEFAULT_EQUATION
    thresholds: Thresholds = field(default_factory=Thresholds)
    sample_size: int = 100
    msisdn_valid_rate_min: float = 0.80
    use_phonenumbers: bool = True
    default_region: str = "EG"
    phone_gate_conf: float = 0.10
    geo_require_pair: bool = True
    geo_egypt_geofence: bool = False
    msisdn_prefixes: list[str] = field(
        default_factory=lambda: ["010", "011", "012", "015"]
    )
    national_id_egypt: NationalIdEgyptConfig = field(
        default_factory=NationalIdEgyptConfig
    )
    device_id: DeviceIdConfig = field(default_factory=DeviceIdConfig)
    subscriber_id: SubscriberIdConfig = field(default_factory=SubscriberIdConfig)
    geo: GeoConfig = field(default_factory=GeoConfig)
    negative_context_tokens: Optional[dict] = None
    ner: NERConfig = field(default_factory=NERConfig)
    models_dir: str = "/models"
    gliner: GlinerConfig = field(default_factory=GlinerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    learned: LearnedClassifierConfig = field(default_factory=LearnedClassifierConfig)
    gliner_always_run: bool = False
    regex_overrides: Optional[dict] = None
    selected_columns: Optional[list[str]] = None
    logging: PIILoggingConfig = field(default_factory=PIILoggingConfig)
    tuning: PIITuningConfig = field(default_factory=PIITuningConfig)

    def ner_always_run(self) -> bool:
        """True when NER must run even if regex confidence is high."""
        return self.ner.always_run or self.gliner_always_run

    def __post_init__(self) -> None:
        # Keep Thresholds floors in sync with dedicated config blocks.
        nid_cfg = self.national_id_egypt
        if nid_cfg is not None and hasattr(self.thresholds, "nid_min"):
            self.thresholds.nid_min = float(nid_cfg.valid_rate_min)
        if self.device_id is not None and hasattr(self.thresholds, "imei_min"):
            self.thresholds.imei_min = float(self.device_id.imei_valid_rate_min)
        if self.subscriber_id is not None and hasattr(self.thresholds, "imsi_min"):
            self.thresholds.imsi_min = float(self.subscriber_id.imsi_valid_rate_min)
        if self.geo is not None and hasattr(self.thresholds, "geo_min"):
            self.thresholds.geo_min = float(self.geo.confidence_min)
            # Mirror legacy top-level flag from the nested geo block.
            self.geo_require_pair = bool(self.geo.require_pair)


@dataclass
class QualityPublishConfig:
    """Where ``redibis quality-monitor`` sends validate-only run results.

    ``sinks`` is authoritative when non-empty (e.g. ``[openmetadata, console]``).
    Left empty, it falls back to the legacy ``catalog.push.quality`` toggle so
    existing configs keep working unchanged (see
    ``redibis.quality.sinks.registry.resolve_sink_names``).
    """
    sinks: list[str] = field(default_factory=list)


@dataclass
class QualityConfig:
    generate_ge_docs: bool = True
    rule_set: QualityRuleSet = field(default_factory=QualityRuleSet)
    publish: QualityPublishConfig = field(default_factory=QualityPublishConfig)


@dataclass
class MaskingConfig:
    roles_path: Optional[str] = None
    default_locale: str = "default"


@dataclass
class AuthConfig:
    """Dashboard authentication. JSON file is the default store."""

    enabled: bool = True
    backend: str = "json"  # json | db
    store_path: str = "./configs/auth/users.json"
    db_url: str = ""
    session_hours: float = 12
    bootstrap_admin_user: str = ""
    # Empty + empty store → local default admin/admin (not for production).
    bootstrap_admin_password: str = ""


@dataclass
class StorageConfig:
    backend: str = "local"
    endpoint_url: Optional[str] = None
    runs_bucket: str = "pii-reports"
    contracts_bucket: str = "active-contracts"
    pii_runs_bucket: str = "pii-contracts"
    quality_runs_bucket: str = "quality-contracts"


@dataclass
class ContractConfig:
    auto_create: bool = True
    auto_write: bool = False
    automerge: str = "none"
    validate: bool = True


@dataclass
class SimilarContextConfig:
    """Similar-column memory hints in enrichment context."""
    enabled: bool = True
    k: int = 5


@dataclass
class EnrichMultistepConfig:
    """YAML-driven LangGraph enrichment workflow settings."""
    enabled: bool = False
    steps_file: str = ""
    rai_enabled: Optional[bool] = None
    review_enabled: bool = True
    review_max_changes: int = 25


@dataclass
class EnrichContextConfig:
    """Resolved enrichment context profile (pack + custom overlays)."""
    pack: str = ""
    custom_dir: str = ""


@dataclass
class EnrichConfig:
    """Full-contract LLM enrichment settings."""
    include_pii_evidence: bool = False
    similar_context: SimilarContextConfig = field(default_factory=SimilarContextConfig)
    max_attempts: int = 3
    multistep: EnrichMultistepConfig = field(default_factory=EnrichMultistepConfig)
    context: EnrichContextConfig = field(default_factory=EnrichContextConfig)


@dataclass
class SynthesisConfig:
    """Contract Synthesis (portable ODCS v3.1) — out of Redibis merger."""

    enabled: bool = True
    analysis_mode: str = "deterministic"  # deterministic | assisted
    odcs_version: str = "v3.1.0"
    bump_kind: str = "minor"
    output_dir: str = ""


@dataclass
class EvidenceBundleConfig:
    """Per-table evidence bundle (``evidence_bundle.json``) — see
    ``docs/internal/EVIDENCE_BUNDLE_PLAN.md``. ``sample_mode: raw`` means the
    bundle carries literal source values; fence it accordingly (invariant 23).
    """
    enabled: bool = True
    sample_mode: str = "raw"  # raw | masked | none
    sample_n: int = 10
    top_values_n: int = 20
    format_masks_n: int = 10
    numeric_histogram_bins: int = 20
    length_histogram_bins: int = 20
    include_profile_groups: list[str] = field(default_factory=list)  # empty = all applicable


@dataclass
class EvidenceAccessPolicy:
    """Who may read exact restricted LLM evidence from the local spool."""

    steward_role: str = "data_steward"
    require_actor: bool = True
    require_reason: bool = True
    audit_enabled: bool = True


@dataclass
class EvidenceConfig:
    """Local governed spool for exact restricted evidence (never the runs bucket)."""

    restricted_spool_dir: Path = field(
        default_factory=lambda: Path("./reports/_restricted_evidence"),
    )
    access: EvidenceAccessPolicy = field(default_factory=EvidenceAccessPolicy)


@dataclass
class ReportConfig:
    formats: list[str] = field(default_factory=lambda: ["profile", "quality", "pii"])
    output_dir: Path = field(default_factory=lambda: Path("./reports"))
    evidence_bundle: EvidenceBundleConfig = field(default_factory=EvidenceBundleConfig)


@dataclass
class ClassificationConfig:
    """Multi-domain classification policy engine settings."""
    enabled: bool = False
    policy_pack: str = "telecom"
    policy_path: Optional[str] = None
    default_jurisdiction: str = ""
    edge_rules_enabled: bool = True
    per_run_overlay_allowed: bool = True


@dataclass
class RAIConfig:
    """Responsible-AI middleware — advisory by default in open-source builds.

    Enterprise deployments should set ``hard_block_external_pii: true`` and
    ``enforce: true`` via YAML (see ``config/examples/enterprise-rai.yaml``).
    """

    enabled: bool = True
    mode: str = "report"
    enforce: bool = False
    block_external_raw_pii: bool = True
    hard_block_external_pii: bool = False
    allowed_models: list[str] = field(default_factory=list)
    denied_models: list[str] = field(default_factory=list)
    log_prompts: bool = True
    default_residency: str = "local"


@dataclass
class OtelConfig:
    """OpenTelemetry SDK settings (optional ``redibis[otel]`` extra)."""

    enabled: bool = False
    exporter: str = "file"
    otlp_endpoint: str = ""
    metrics: bool = True
    service_name: str = "redibis"


@dataclass
class ObservabilityConfig:
    """Logging, per-run log persistence, and optional OTel."""

    log_level: str = "INFO"
    log_format: str = ""
    log_dir: str = ""
    log_samples: bool = False
    persist_run_log: bool = True
    decision_log: bool = True
    llm_debug: bool = False
    llm_log_prompts: bool = False
    module_levels: dict = field(default_factory=dict)
    otel: OtelConfig = field(default_factory=OtelConfig)


@dataclass
class AgentsConfig:
    """Agentic pipeline board + in-app execution settings."""
    # Master switch + local-dev-friendly defaults (opt out in REDIBIS_CONFIG / YAML).
    enabled: bool = True
    runs_dir: str = "./agent_runs"
    sample_dir: str = ""
    auto_approve_writes: bool = True
    dynamic_tools_dir: str = "./dynamic_tools"
    allow_external_codegen: bool = True
    codegen_default_target: str = "ranger"
    # Hosted codegen service URL (vendor gateway). Empty = local codegen only.
    codegen_service_url: str = ""
    codegen_provenance_secret: str = ""
    # Single-table runs: "langgraph" (durable HITL) or "batch" (legacy loop).
    single_table_executor: str = "langgraph"
    # Multi-table batch: "langgraph" (per-table checkpoint) or "sequential" (legacy loop).
    batch_executor: str = "langgraph"
    # Approved dynamic tools: True = bounded sandbox exec when a tool is approved.
    dynamic_sandbox_enabled: bool = True
    # AG-UI / CopilotKit chat endpoint at /api/copilotkit/agent (requires redibis[copilotkit]).
    copilotkit_enabled: bool = True
    # Optional default LLM for IntentPlanner (copilot + /api/agents/plan when body omits provider).
    planner_provider: str = ""
    planner_model: str = ""
    # Codegen routing: "local" (default OSS engine) or "remote" (vendor URL + token).
    codegen_mode: str = "local"
    # Output validator smart-retry budget (enrich/contract critique loop).
    max_retries: int = 2
    # Planner structural repair attempts when validate_spec fails.
    max_plan_repairs: int = 2


@dataclass
class PackConfig:
    """Runtime pack-store / signature policy (deployment-local; not exported in packs)."""

    require_signature: bool = False
    trust_store: str = ""


@dataclass
class GatewayGuardThresholds:
    """Score at/above which a guard analyser flags ``decision.action=block``."""

    toxicity_block: float = 0.75
    prompt_injection_block: float = 0.75


@dataclass
class TextGatewayConfig:
    """Text Gateway (``/gateway``) safety analysers.

    Heuristic checks (regex-based, local, no network) always run and are
    free. LLM-backed checks are opt-in per analyser because they add
    latency/cost and route through ``gateway.toxicity`` /
    ``gateway.prompt_injection`` capability roles (Settings → LLM →
    Capability roles) — any LiteLLM-compatible model, general-purpose or a
    dedicated moderation/safety model, may be bound to either role.
    """

    toxicity_llm_enabled: bool = False
    prompt_injection_llm_enabled: bool = False
    heuristic_toxicity: bool = True
    heuristic_prompt_injection: bool = True
    thresholds: GatewayGuardThresholds = field(default_factory=GatewayGuardThresholds)
    # Deterministic spoken/obfuscated PII expanders (Arabic digits, spaced
    # emails, separator-tolerant telecom IDs). Gateway enables by default;
    # admin ``/api/pii/text/*`` stays opt-in for backward compatibility.
    obfuscation_preprocess: bool = True
    obfuscation_expanders: list = field(default_factory=list)


@dataclass
class PackSourceConfig:
    """Deployment reference to a portable ``.rdbpack`` (not exported inside packs)."""

    path: str = ""
    mode: str = "overlay"


@dataclass
class RedibisConfig:
    """Root configuration object shared by CLI and library code."""

    table: str = ""
    scan_types: list[str] = field(default_factory=lambda: ["profile", "quality", "pii"])
    source: SourceConfig = field(default_factory=SourceConfig)
    profiling: ProfilingConfig = field(default_factory=ProfilingConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    pii: PIIConfig = field(default_factory=PIIConfig)
    masking: MaskingConfig = field(default_factory=MaskingConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    contract: ContractConfig = field(default_factory=ContractConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    rai: RAIConfig = field(default_factory=RAIConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    enrich: EnrichConfig = field(default_factory=EnrichConfig)
    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    behavior: "BehaviorConfig" = field(default_factory=lambda: _make_behavior_config())
    pack: PackConfig = field(default_factory=PackConfig)
    packs: list = field(default_factory=list)  # list[PackSourceConfig | dict]
    text_gateway: TextGatewayConfig = field(default_factory=TextGatewayConfig)

    @classmethod
    def default(cls) -> "RedibisConfig":
        return cls()

    @classmethod
    def load(cls) -> "RedibisConfig":
        """Load from the default config file path, or return defaults if not found."""
        import os
        config_path = os.environ.get("REDIBIS_CONFIG")
        if config_path:
            try:
                return cls.from_yaml(config_path)
            except Exception:
                pass
        return cls.default()

    def validate(self) -> "RedibisConfig":
        """Raise ``ConfigError`` when field values are out of range."""
        from redibis.profiling import PROFILER_REGISTRY

        engine = (self.profiling.engine or "great_expectations").lower()
        if engine not in PROFILER_REGISTRY:
            raise ConfigError(
                f"unknown profiler engine {self.profiling.engine!r}; "
                f"choices: {sorted(PROFILER_REGISTRY)}"
            )

        mode = (self.pii.equation_mode or DEFAULT_EQUATION).lower()
        if mode not in EQUATION_MODES:
            raise ConfigError(
                f"unknown equation mode {self.pii.equation_mode!r}; "
                f"choices: {sorted(EQUATION_MODES)}"
            )

        engines = (self.pii.engines or "both").lower()
        if engines not in _VALID_PII_ENGINES:
            raise ConfigError(
                f"unknown pii engines {self.pii.engines!r}; "
                f"choices: {sorted(_VALID_PII_ENGINES)}"
            )

        automerge = (self.contract.automerge or "none").lower()
        if automerge not in _VALID_AUTOMERGE:
            raise ConfigError(
                f"unknown automerge {self.contract.automerge!r}; "
                f"choices: {sorted(_VALID_AUTOMERGE)}"
            )

        scan_types = {t.lower() for t in (self.scan_types or [])}
        unknown = scan_types - _VALID_SCAN_TYPES
        if unknown:
            raise ConfigError(
                f"unknown scan_types {sorted(unknown)}; "
                f"choices: {sorted(_VALID_SCAN_TYPES)}"
            )

        catalog_backend = (self.catalog.backend or "openmetadata").lower()
        if catalog_backend not in _VALID_CATALOG_BACKENDS:
            raise ConfigError(
                f"unknown catalog backend {self.catalog.backend!r}; "
                f"choices: {sorted(_VALID_CATALOG_BACKENDS)}"
            )

        source_engine = (self.source.engine or "none").lower()
        if source_engine not in _VALID_SOURCE_ENGINES:
            raise ConfigError(
                f"unknown source engine {self.source.engine!r}; "
                f"choices: {sorted(_VALID_SOURCE_ENGINES)}"
            )

        memory_store = (self.memory.store or "pgvector").lower()
        if memory_store not in _VALID_MEMORY_STORES:
            raise ConfigError(
                f"unknown memory store {self.memory.store!r}; "
                f"choices: {sorted(_VALID_MEMORY_STORES)}"
            )

        rai_mode = (self.rai.mode or "report").lower()
        if rai_mode not in _VALID_RAI_MODES:
            raise ConfigError(
                f"unknown rai.mode {self.rai.mode!r}; "
                f"choices: {sorted(_VALID_RAI_MODES)}"
            )

        auth_backend = (self.auth.backend or "json").lower()
        if auth_backend not in ("json", "db"):
            raise ConfigError(
                f"unknown auth.backend {self.auth.backend!r}; choices: json, db"
            )
        import os as _os

        db_url = (self.auth.db_url or _os.environ.get("REDIBIS_AUTH_DB") or "").strip()
        if auth_backend == "db" and not db_url:
            raise ConfigError(
                "auth.backend is 'db' but auth.db_url / REDIBIS_AUTH_DB is empty; "
                "refusing to fall back to JSON"
            )
        if self.memory.enabled:
            embed_prov = (self.memory.embedding_provider or "sentence_transformers").lower()
            store_kind = (self.memory.store or "pgvector").lower()
            if embed_prov in ("local", "hash") and store_kind != "memory":
                raise ConfigError(
                    "memory.embedding_provider 'local'/'hash' is a test-only non-semantic "
                    "hash embedder; use 'sentence_transformers' with embedding_model "
                    "(e.g. all-MiniLM-L6-v2) for production retrieval, or store=memory "
                    "for the in-process test double"
                )
        mode = (self.synthesis.analysis_mode or "deterministic").lower()
        if mode not in ("deterministic", "assisted"):
            raise ConfigError(
                f"unknown synthesis.analysis_mode {self.synthesis.analysis_mode!r}; "
                "choices: deterministic, assisted"
            )
        return self

    @classmethod
    def from_yaml(cls, path: PathLike) -> "RedibisConfig":
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
        raw = _migrate_legacy_catalog_keys(raw)
        merged = deep_merge(_config_to_dict(cls.default()), raw)
        return _dict_to_config(cls, merged).validate()

    @classmethod
    def from_dict(cls, data: dict) -> "RedibisConfig":
        raw = _migrate_legacy_catalog_keys(dict(data or {}))
        merged = deep_merge(_config_to_dict(cls.default()), raw)
        return _dict_to_config(cls, merged).validate()

    def to_dict(self) -> dict:
        return _config_to_dict(self)

    def to_yaml(self, path: PathLike) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(self.to_dict(), default_flow_style=False, sort_keys=False,
                           allow_unicode=True),
            encoding="utf-8",
        )

    @classmethod
    def dump_default_yaml(cls, path: PathLike) -> None:
        cls.default().to_yaml(path)


# ── YAML helpers ─────────────────────────────────────────────────────────────

def _migrate_legacy_catalog_keys(raw: dict) -> dict:
    """Accept ``catalog.openmetadata.mode`` for one release → ``entity_mode``."""
    import warnings

    cat = raw.get("catalog")
    if not isinstance(cat, dict):
        return raw
    om = cat.get("openmetadata")
    if not isinstance(om, dict) or "mode" not in om:
        return raw
    om = dict(om)
    legacy = om.pop("mode")
    if "entity_mode" not in om:
        om["entity_mode"] = legacy
    warnings.warn(
        "catalog.openmetadata.mode is deprecated; use "
        "catalog.openmetadata.entity_mode",
        DeprecationWarning,
        stacklevel=3,
    )
    cat = dict(cat)
    cat["openmetadata"] = om
    out = dict(raw)
    out["catalog"] = cat
    return out


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = dict(base)
    for key, val in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _config_to_dict(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Thresholds):
        return asdict(obj)
    if isinstance(obj, QualityRuleSet):
        return obj.to_dict()
    if is_dataclass(obj):
        return {f.name: _config_to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [_config_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _config_to_dict(v) for k, v in obj.items()}
    return obj


def _dict_to_config(cls, data: dict) -> Any:
    if not is_dataclass(cls):
        return data
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        if val is None:
            kwargs[f.name] = None
            continue
        ft = hints.get(f.name, f.type)
        if f.name == "packs":
            items = []
            for item in val or []:
                if isinstance(item, PackSourceConfig):
                    items.append(item)
                elif isinstance(item, dict):
                    items.append(
                        PackSourceConfig(
                            path=str(item.get("path") or ""),
                            mode=str(item.get("mode") or "overlay"),
                        )
                    )
                else:
                    items.append(PackSourceConfig(path=str(item)))
            kwargs[f.name] = items
            continue
        if f.name == "output_dir" or ft is Path or (isinstance(ft, type) and issubclass(ft, Path)):
            kwargs[f.name] = Path(val) if not isinstance(val, Path) else val
        elif ft is Thresholds or (isinstance(ft, type) and issubclass(ft, Thresholds)):
            kwargs[f.name] = Thresholds(**val) if isinstance(val, dict) else val
        elif ft is QualityRuleSet or (isinstance(ft, type) and issubclass(ft, QualityRuleSet)):
            kwargs[f.name] = QualityRuleSet.from_dict(val)
        elif isinstance(ft, type) and is_dataclass(ft):
            kwargs[f.name] = _dict_to_config(ft, val if isinstance(val, dict) else {})
        elif getattr(ft, "__origin__", None) is list:
            kwargs[f.name] = list(val or [])
        else:
            kwargs[f.name] = val
    return cls(**kwargs)


def _scan_types_to_flags(scan_types: list[str]) -> tuple[bool, bool, bool]:
    types = {t.lower() for t in (scan_types or [])}
    if not types:
        types = {"profile", "quality", "pii"}
    run_profile = "profile" in types or "quality" in types
    run_quality = "quality" in types
    run_pii = "pii" in types
    return run_pii, run_profile, run_quality


def apply_global_settings_pii(
    pii: PIIConfig,
    global_settings: dict | None,
) -> PIIConfig:
    """Overlay ``global_settings['pii']`` onto a ``PIIConfig`` (Settings UI / JSON)."""
    from dataclasses import replace

    block = (global_settings or {}).get("pii") or {}
    if not isinstance(block, dict) or not block:
        return pii
    valid = {f.name for f in fields(PIIConfig)}
    overrides = {k: v for k, v in block.items() if k in valid}
    return replace(pii, **overrides) if overrides else pii


def load_global_settings_optional() -> dict:
    """Best-effort read of ``global_settings.json`` (no webapp import)."""
    import json
    import os

    configs_dir = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    path = Path(configs_dir) / "global_settings.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def resolve_pii_config(
    explicit: PIIConfig | None = None,
    *,
    global_settings: dict | None = None,
) -> PIIConfig:
    """``PIIConfig`` with optional global-settings overlay (``pii.sample_size``, etc.)."""
    base = explicit or PIIConfig()
    gs = global_settings if global_settings is not None else load_global_settings_optional()
    return apply_global_settings_pii(base, gs)


def _flags_to_scan_mode(run_pii: bool, run_profile: bool, run_quality: bool) -> str:
    if run_pii and run_quality:
        return "both"
    if run_pii:
        return "pii"
    if run_quality:
        return "quality"
    if run_profile:
        return "profile"
    return "none"


# ── BehaviorConfig lazy import helper (avoids circular imports) ───────────────

def _make_behavior_config():
    """Lazy factory to avoid circular import at module load time."""
    try:
        from redibis.behavior.config import BehaviorConfig
        return BehaviorConfig()
    except ImportError:
        # Fallback: return a minimal namespace object so config loads cleanly
        # even before the behavior package is fully installed.
        class _FallbackBehaviorConfig:
            enabled = False
            mode = "disabled"
            engine_modes: dict = {}
            policies: list = []
            allow_per_run_overlay: bool = False
            plugin_allowlist: list = []
            fail_mode: str = "baseline_with_warning"
            max_rules_per_context: int = 200
            max_eval_ms: int = 50
        return _FallbackBehaviorConfig()


# Re-export BehaviorConfig for YAML round-trip (TYPE_CHECKING-safe)
try:
    from redibis.behavior.config import BehaviorConfig as BehaviorConfig  # noqa: F401
except ImportError:
    pass
