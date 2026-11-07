import re

_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")


def incident_scope(alerts):
    scopes = set()
    for alert in alerts:
        details = (alert.get("body") or {}).get("details") or {}
        if not isinstance(details, dict):
            return None, None
        labels = details.get("labels")
        labels = labels if isinstance(labels, dict) else details
        namespace, pod = labels.get("namespace"), labels.get("pod")
        if not isinstance(namespace, str) or not namespace or not _NAME.fullmatch(namespace) or len(namespace) > 63:
            return None, None
        if pod and (not isinstance(pod, str) or not _NAME.fullmatch(pod) or len(pod) > 253):
            return None, None
        scopes.add((namespace, pod or ""))
    if len(scopes) != 1:
        return None, None
    namespace, pod = scopes.pop()
    return namespace, pod or None
