from django.conf import settings
from openai import OpenAI

_client = None


def get_openai_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = getattr(settings, "OPENAI_API_KEY", None)
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY não configurada no settings.")
        _client = OpenAI(api_key=api_key)
    return _client


def get_default_model_name() -> str:
    """
    Permite sobrescrever o modelo via settings.IAPARCELAMENTO_MODEL,
    senão usa um modelo leve por padrão.
    """
    return getattr(settings, "IAPARCELAMENTO_MODEL", "gpt-4.1-mini")
