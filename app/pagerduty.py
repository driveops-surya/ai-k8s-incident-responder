import hashlib
import hmac
import re

import httpx


def verify_signature(body: bytes, header: str, secret: str) -> bool:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return any(
        hmac.compare_digest(part.strip()[3:], digest)
        for part in header.split(",")
        if re.fullmatch(r"v1=[0-9a-f]{64}", part.strip())
    )


class PagerDuty:
    def __init__(self, settings):
        self.settings = settings
        self.headers = {
            "Authorization": f"Token token={settings.pagerduty_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
            "From": settings.pagerduty_from_email,
        }

    def alerts(self, incident_id):
        alerts = []
        offset = 0
        with httpx.Client(timeout=self.settings.http_timeout, headers=self.headers) as client:
            while True:
                response = client.get(
                    f"{self.settings.pagerduty_api_url}/incidents/{incident_id}/alerts",
                    params={"limit": 100, "offset": offset},
                )
                response.raise_for_status()
                data = response.json()
                page = data["alerts"]
                alerts.extend(page)
                if not data.get("more"):
                    return alerts
                if not page or len(alerts) >= 1000:
                    raise ValueError("PagerDuty alert pagination exceeded safe bounds")
                offset += len(page)

    def add_note(self, incident_id, text):
        response = httpx.post(
            f"{self.settings.pagerduty_api_url}/incidents/{incident_id}/notes",
            headers=self.headers,
            json={"note": {"content": text}},
            timeout=self.settings.http_timeout,
        )
        response.raise_for_status()
