"""Secret removal shared by application receipts and the finance MCP bridge."""
import re


SENSITIVE_FIELD = re.compile(r"api[_-]?key|password|secret|authorization|cookie|token", re.I)
TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|password|secret|authorization|cookie|token)\s*[=:]\s*)([^\s,;]+)")


def configured_redactor(extra=()):
    import os
    from fin_agent.config import Config
    values = [value for key, value in vars(Config).items()
              if SENSITIVE_FIELD.search(key) and isinstance(value, str)]
    values.extend(value for key, value in os.environ.items() if SENSITIVE_FIELD.search(key))
    return Redactor([*values, *extra])


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = sorted({str(s) for s in secrets if s}, key=len, reverse=True)

    def text(self, value):
        value = str(value)
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = TOKEN.sub("[REDACTED]", value)
        return ASSIGNMENT.sub(r"\1[REDACTED]", value)

    def clean(self, value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if SENSITIVE_FIELD.search(key) else self.clean(item)
                    for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.clean(item) for item in value]
        return self.text(value) if isinstance(value, str) else value
