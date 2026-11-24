import json
import logging

from app import kubernetes_client, loki, prometheus, vllm
from app.pagerduty import PagerDuty
from app.scope import incident_scope

logger = logging.getLogger(__name__)


def fit_evidence(evidence, max_chars):
    """Share the model's input budget across sources, so one cannot crowd out the rest."""
    sections = {
        name: json.dumps(evidence[name], default=str, ensure_ascii=True)
        for name in ("incident", "alert_summaries", "kubernetes", "prometheus", "loki")
        if name in evidence
    }
    # Reserve a quarter for scope, warnings, and JSON escaping of previews.
    remaining = max_chars * 3 // 4
    # Smallest first: whatever a small section leaves unused goes to the larger ones.
    for position, (name, serialized) in enumerate(sorted(sections.items(), key=lambda item: len(item[1]))):
        allowance = min(8000, remaining // (len(sections) - position))
        if len(serialized) > allowance:
            evidence[name] = {"truncated": True, "preview": serialized[:allowance]}
            evidence["collection_warnings"].append(f"{name} evidence truncated for model input.")
        remaining -= min(len(serialized), allowance)


def process(settings, event):
    incident = event["data"]
    incident_id = incident["id"]
    pagerduty = PagerDuty(settings)
    alerts = pagerduty.alerts(incident_id)
    namespace, pod = incident_scope(alerts)
    evidence = {
        "incident": {"id": incident_id, "title": incident.get("title", "")},
        "scope": {"namespace": namespace, "pod": pod},
        "alert_summaries": [
            {"summary": alert.get("summary"), "details": (alert.get("body") or {}).get("details")}
            for alert in alerts[:20]
        ],
        "collection_warnings": [],
    }
    if not namespace:
        evidence["collection_warnings"].append("No unambiguous namespace/pod scope in PagerDuty alert details.")
    else:
        sources = [("kubernetes", lambda: kubernetes_client.collect(namespace, pod))]
        if pod:
            sources.extend([
                ("prometheus", lambda: prometheus.collect(settings, namespace, pod)),
                ("loki", lambda: loki.collect(settings, namespace, pod)),
            ])
        else:
            evidence["collection_warnings"].append("Pod is missing; pod-specific metrics and logs were skipped.")
        for name, collect in sources:
            try:
                evidence[name] = collect()
                if name == "kubernetes":
                    evidence["collection_warnings"].extend(evidence[name].get("warnings", []))
            except Exception:
                logger.exception("Evidence collection failed for %s", name)
                evidence["collection_warnings"].append(f"{name} collection failed; inspect service logs.")
    fit_evidence(evidence, settings.max_evidence_chars)
    analysis = vllm.analyze(settings, evidence)
    warnings = "\n".join(f"- {warning}" for warning in evidence["collection_warnings"]) or "- None"
    note = (
        "AI-assisted analysis (human review required; no remediation was executed)\n"
        f"Webhook event: {event['id']}\n"
        f"Evidence collection warnings:\n{warnings}\n\n{analysis}"
    )
    pagerduty.add_note(incident_id, note[:25000])
