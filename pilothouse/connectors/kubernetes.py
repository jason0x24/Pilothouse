"""Kubernetes connector.

Live mode talks directly to the Kubernetes API server with a bearer token
(typically a ServiceAccount token mounted into the Pilothouse pod). That
avoids a kubectl shell-out and keeps the connector dependency-free beyond
httpx.

Tools (all read-only — pod manipulation belongs behind a separate gated
tool):

  * kubernetes_describe_pod     — pod spec + status + container statuses
  * kubernetes_get_pod_events   — Events scoped to a pod (recent only)
  * kubernetes_get_pod_logs     — recent log lines from one container
  * kubernetes_list_pods        — pods in a namespace, optionally filtered

Mock mode returns deterministic synthetic responses derived from the input
arguments — sufficient to drive the K8s investigator template end-to-end
without a cluster.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

import httpx

from ..config import get_settings
from .base import Connector, ToolContext, ToolResult


class KubernetesConnector(Connector):
    name = "kubernetes"

    def __init__(self) -> None:
        super().__init__()
        self._add(
            "kubernetes_describe_pod",
            "Describe a pod (spec + status + container statuses).",
            {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["namespace", "name"],
            },
            self._describe_pod,
        )
        self._add(
            "kubernetes_get_pod_events",
            "Get Events for a specific pod, newest first.",
            {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "name": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["namespace", "name"],
            },
            self._pod_events,
        )
        self._add(
            "kubernetes_get_pod_logs",
            "Tail logs from a container in a pod.",
            {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "name": {"type": "string"},
                    "container": {"type": "string", "default": ""},
                    "tail_lines": {"type": "integer", "default": 100},
                    "previous": {
                        "type": "boolean",
                        "default": False,
                        "description": "Read logs from the previous (crashed) container instance",
                    },
                },
                "required": ["namespace", "name"],
            },
            self._pod_logs,
        )
        self._add(
            "kubernetes_list_pods",
            "List pods in a namespace, optionally filtered by label selector.",
            {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "label_selector": {"type": "string", "default": ""},
                },
                "required": ["namespace"],
            },
            self._list_pods,
        )

    @property
    def live(self) -> bool:
        s = get_settings()
        return bool(s.kube_api_url and s.kube_token)

    def _client(self) -> httpx.AsyncClient:
        s = get_settings()
        verify: Any = True
        if s.kube_ca_path:
            verify = s.kube_ca_path
        return httpx.AsyncClient(
            base_url=s.kube_api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {s.kube_token}", "Accept": "application/json"},
            timeout=20,
            verify=verify,
        )

    async def _describe_pod(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_describe(params["namespace"], params["name"]))
        async with self._client() as client:
            r = await client.get(
                f"/api/v1/namespaces/{params['namespace']}/pods/{params['name']}"
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _pod_events(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(
                content=_mock_events(params["namespace"], params["name"], params.get("limit", 20))
            )
        field_selector = (
            f"involvedObject.namespace={params['namespace']},"
            f"involvedObject.name={params['name']}"
        )
        async with self._client() as client:
            r = await client.get(
                f"/api/v1/namespaces/{params['namespace']}/events",
                params={"fieldSelector": field_selector, "limit": params.get("limit", 20)},
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _pod_logs(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(
                content=_mock_logs(
                    params["namespace"],
                    params["name"],
                    params.get("container") or "",
                    params.get("tail_lines", 100),
                )
            )
        q: dict[str, Any] = {"tailLines": params.get("tail_lines", 100)}
        if params.get("container"):
            q["container"] = params["container"]
        if params.get("previous"):
            q["previous"] = "true"
        async with self._client() as client:
            r = await client.get(
                f"/api/v1/namespaces/{params['namespace']}/pods/{params['name']}/log",
                params=q,
            )
        return ToolResult(
            content=r.text if not r.is_error else r.json(), is_error=r.is_error
        )

    async def _list_pods(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(
                content=_mock_list_pods(params["namespace"], params.get("label_selector", ""))
            )
        q: dict[str, Any] = {}
        if params.get("label_selector"):
            q["labelSelector"] = params["label_selector"]
        async with self._client() as client:
            r = await client.get(f"/api/v1/namespaces/{params['namespace']}/pods", params=q)
        return ToolResult(content=r.json(), is_error=r.is_error)


# --- mock data fabrication ------------------------------------------------


def _seed(*parts: Any) -> random.Random:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:8], 16))


def _mock_describe(namespace: str, name: str) -> dict:
    rng = _seed("describe", namespace, name)
    restarts = rng.randint(3, 47)
    waiting_reasons = ["CrashLoopBackOff", "ImagePullBackOff", "ErrImageNeverPull"]
    last_state_reasons = ["OOMKilled", "Error", "Completed"]
    return {
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": name.split("-")[0], "version": f"v{rng.randint(1, 50)}"},
        },
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "image": f"registry.example/{name.split('-')[0]}:v{rng.randint(1, 50)}.{rng.randint(0, 30)}.{rng.randint(0, 200)}",
                    "resources": {
                        "limits": {"memory": "256Mi", "cpu": "500m"},
                        "requests": {"memory": "128Mi", "cpu": "100m"},
                    },
                }
            ],
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": restarts,
                    "state": {"waiting": {"reason": rng.choice(waiting_reasons)}},
                    "lastState": {
                        "terminated": {
                            "reason": rng.choice(last_state_reasons),
                            "exitCode": rng.choice([1, 137, 139]),
                            "finishedAt": "2026-05-13T14:18:02Z",
                        }
                    },
                }
            ],
        },
    }


def _mock_events(namespace: str, name: str, limit: int) -> dict:
    rng = _seed("events", namespace, name)
    events = []
    types = [
        ("Warning", "BackOff", "Back-off restarting failed container"),
        ("Warning", "Unhealthy", "Liveness probe failed: HTTP 500"),
        ("Normal", "Pulled", "Successfully pulled image"),
        ("Warning", "FailedScheduling", "0/3 nodes are available: insufficient memory"),
        ("Warning", "Killing", "Container app failed liveness probe, will be restarted"),
    ]
    for i in range(min(limit, 8)):
        t, reason, msg = rng.choice(types)
        events.append(
            {
                "type": t,
                "reason": reason,
                "message": msg,
                "count": rng.randint(1, 50),
                "firstTimestamp": f"2026-05-13T14:{14 + i:02d}:00Z",
                "lastTimestamp": f"2026-05-13T14:{20 + i:02d}:00Z",
                "involvedObject": {"kind": "Pod", "namespace": namespace, "name": name},
            }
        )
    return {"kind": "EventList", "items": events}


def _mock_logs(namespace: str, name: str, container: str, tail_lines: int) -> str:
    rng = _seed("logs", namespace, name, container)
    lines: list[str] = []
    templates = [
        "INFO  starting application version={ver}",
        "INFO  loaded config keys=12",
        "ERROR failed to open /etc/secrets/api_key: no such file or directory",
        "ERROR connection refused dialing 'pricing-service:8080'",
        "FATAL out of memory: Killed by oom-killer",
        "INFO  handled request path=/healthz status=200",
    ]
    ver = f"v{rng.randint(1, 50)}.{rng.randint(0, 30)}.{rng.randint(0, 200)}"
    for i in range(min(tail_lines, 20)):
        lines.append(
            f"2026-05-13T14:{20 + (i // 6):02d}:{(i * 11) % 60:02d}Z " + rng.choice(templates).format(ver=ver)
        )
    return "\n".join(lines)


def _mock_list_pods(namespace: str, label_selector: str) -> dict:
    rng = _seed("listpods", namespace, label_selector)
    items = []
    statuses = ["Running", "Running", "CrashLoopBackOff", "Pending", "Running"]
    for i in range(rng.randint(3, 8)):
        items.append(
            {
                "metadata": {
                    "name": f"checkout-7d8c-{rng.randrange(16**4):04x}",
                    "namespace": namespace,
                },
                "status": {"phase": rng.choice(statuses)},
            }
        )
    return {"kind": "PodList", "items": items}
