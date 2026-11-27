"""Central content-processing policy for configured language models."""

from app.core.config import Settings
from app.providers.endpoint_security import UnsafeEndpointError, normalize_base_url, policy_from_settings
from app.providers.registry import get_provider


def model_allowed_for_content(provider: str, base_url: str | None, settings: Settings) -> bool:
    """In local-only mode, accept only explicitly trusted custom-provider origins for outbound requests.

    The HTTP transport rechecks every DNS answer and connection for a private address. This
    configuration-level check blocks built-in cloud providers and unlisted custom endpoints
    before content, credentials, model verification or evaluation requests can leave the process.
    """
    if not settings.llm_local_only:
        return True
    if get_provider(provider) is not None or not base_url:
        return False
    try:
        endpoint = normalize_base_url(base_url)
    except UnsafeEndpointError:
        return False
    return policy_from_settings(settings).trusts(endpoint)
