"""Model-provider adapters and AI agents (pure library: no DB imports).

Public API::

    from app.ai import (
        ModelConfig, Capabilities, ProviderError, build_provider, check_capabilities,
        run_modeling_agent, CurrentModel, BuildFeedback, AgentOutcome,
        run_intake_agent, to_feature_summary, IntakeOutcome, IntakeOverrides, ImagePart,
    )

See PROVIDER_NOTES.md for protocol choices per provider.
"""

from .intake_agent import (
    IntakeIncomplete,
    IntakeOutcome,
    IntakeOverrides,
    IntakeResult,
    run_intake_agent,
    to_feature_summary,
)
from .modeling_agent import (
    AgentOutcome,
    AgentReply,
    BuildFeedback,
    CurrentModel,
    OpError,
    run_modeling_agent,
)
from .providers.base import (
    Capabilities,
    ConnectionTest,
    ImagePart,
    Message,
    ModelConfig,
    ModelProvider,
    ProviderError,
    StructuredResult,
    resolve_secret,
)
from .providers.fake import FakeProvider
from .registry import (
    REQUIRED_CAPABILITIES,
    ConfigStatus,
    build_provider,
    build_simulated_provider,
    check_capabilities,
    config_status,
    require_capabilities,
    select_config,
)
from .trace import AgentStep, AiCallRecord

__all__ = [
    "AgentOutcome", "AgentReply", "AgentStep", "AiCallRecord", "BuildFeedback", "Capabilities",
    "ConfigStatus", "ConnectionTest", "CurrentModel", "FakeProvider", "ImagePart", "IntakeIncomplete",
    "IntakeOutcome", "IntakeOverrides", "IntakeResult", "Message", "ModelConfig", "ModelProvider",
    "OpError", "ProviderError", "REQUIRED_CAPABILITIES", "StructuredResult", "build_provider",
    "build_simulated_provider", "check_capabilities", "config_status", "require_capabilities",
    "resolve_secret", "run_intake_agent", "run_modeling_agent", "select_config", "to_feature_summary",
]
