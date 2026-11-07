import logging

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

logger = logging.getLogger(__name__)


def collect(namespace, pod):
    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    evidence = {"warnings": []}

    def pod_details():
        obj = core.read_namespaced_pod(pod, namespace, _request_timeout=20)
        return {
            "name": obj.metadata.name,
            "phase": obj.status.phase,
            "node": obj.spec.node_name,
            "containers": [
                {
                    "name": item.name,
                    "ready": item.ready,
                    "restarts": item.restart_count,
                    "state": client.ApiClient().sanitize_for_serialization(item.state),
                    "last_state": client.ApiClient().sanitize_for_serialization(item.last_state),
                }
                for item in (obj.status.container_statuses or [])
            ],
        }
    def event_details():
        selector = f"involvedObject.name={pod}" if pod else None
        events = core.list_namespaced_event(
            namespace, field_selector=selector, limit=100, _request_timeout=20
        ).items
        return [
            {"reason": e.reason, "message": e.message, "count": e.count, "last_seen": str(e.last_timestamp)}
            for e in events
        ]

    def deployment_details():
        deployments = apps.list_namespaced_deployment(namespace, limit=100, _request_timeout=20).items

        def updated(deployment):
            times = [str(condition.last_update_time or "") for condition in (deployment.status.conditions or [])]
            return max(times, default=str(deployment.metadata.creation_timestamp or ""))

        deployments.sort(key=updated, reverse=True)
        return [
            {
                "name": d.metadata.name,
                "created": str(d.metadata.creation_timestamp),
                "last_update": updated(d),
                "revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
                "generation": d.metadata.generation,
                "updated_replicas": d.status.updated_replicas,
                "available_replicas": d.status.available_replicas,
                "images": [c.image for c in d.spec.template.spec.containers],
            }
            for d in deployments[:10]
        ]

    collectors = [("events", event_details), ("recent_deployments", deployment_details)]
    if pod:
        collectors.insert(0, ("pod", pod_details))
    for name, collector in collectors:
        try:
            evidence[name] = collector()
        except Exception:
            logger.exception("Kubernetes %s collection failed", name)
            evidence["warnings"].append(f"Kubernetes {name} collection failed; inspect service logs.")
    return evidence
