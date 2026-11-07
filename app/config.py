import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    pagerduty_token: str
    pagerduty_from_email: str
    webhook_secret: str
    prometheus_url: str
    loki_url: str
    vllm_base_url: str
    vllm_model: str
    vllm_api_key: str
    pagerduty_api_url: str
    database_path: str
    http_timeout: float
    lookback_minutes: int
    max_attempts: int

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"{name} must be set")
            return value

        timeout = float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
        lookback = int(os.getenv("LOOKBACK_MINUTES", "30"))
        attempts = int(os.getenv("MAX_ATTEMPTS", "5"))
        if timeout <= 0 or lookback <= 0 or attempts <= 0:
            raise ValueError("Timeout, lookback, and max attempts must be positive")
        return cls(
            pagerduty_token=required("PAGERDUTY_API_TOKEN"),
            pagerduty_from_email=required("PAGERDUTY_FROM_EMAIL"),
            webhook_secret=required("PAGERDUTY_WEBHOOK_SECRET"),
            prometheus_url=required("PROMETHEUS_URL").rstrip("/"),
            loki_url=required("LOKI_URL").rstrip("/"),
            vllm_base_url=required("VLLM_BASE_URL").rstrip("/"),
            vllm_model=required("VLLM_MODEL"),
            vllm_api_key=os.getenv("VLLM_API_KEY", ""),
            pagerduty_api_url=os.getenv("PAGERDUTY_API_URL", "https://api.pagerduty.com").rstrip("/"),
            database_path=os.getenv("DATABASE_PATH", "./data/jobs.db"),
            http_timeout=timeout,
            lookback_minutes=lookback,
            max_attempts=attempts,
        )
