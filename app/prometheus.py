import json

import httpx


def collect(settings, namespace, pod):
    ns = json.dumps(namespace)
    name = json.dumps(pod)
    labels = f"namespace={ns},pod={name},container!=\"\",container!=\"POD\""
    queries = {
        "cpu_cores": f"sum(rate(container_cpu_usage_seconds_total{{{labels}}}[5m]))",
        "memory_bytes": f"sum(container_memory_working_set_bytes{{{labels}}})",
    }
    result = {}
    with httpx.Client(timeout=settings.http_timeout) as client:
        for key, query in queries.items():
            response = client.get(f"{settings.prometheus_url}/api/v1/query", params={"query": query})
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                raise ValueError(f"Prometheus query failed: {payload.get('error', 'unknown error')}")
            result[key] = payload["data"]["result"]
    return result
