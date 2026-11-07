import json
import time

import httpx


def collect(settings, namespace, pod):
    end = time.time_ns()
    query = f'{{namespace={json.dumps(namespace)},pod={json.dumps(pod)}}}'
    response = httpx.get(
        f"{settings.loki_url}/loki/api/v1/query_range",
        params={
            "query": query,
            "start": end - settings.lookback_minutes * 60 * 1_000_000_000,
            "end": end,
            "limit": 100,
            "direction": "backward",
        },
        timeout=settings.http_timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError(f"Loki query failed: {payload.get('error', 'unknown error')}")
    return [
        {"timestamp_ns": timestamp, "line": line[:2000]}
        for stream in payload["data"]["result"]
        for timestamp, line in stream["values"]
    ][:100]
