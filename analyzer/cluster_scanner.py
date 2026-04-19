"""
analyzer/cluster_scanner.py
Scans the live Kubernetes cluster and returns a rich topology snapshot.
Works with EKS, GKE, AKS — uses whatever kubeconfig context is active.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException
from config_loader import load_config


@dataclass
class ServiceNode:
    name: str
    namespace: str
    kind: str                        # Deployment | StatefulSet | DaemonSet
    replicas_desired: int
    replicas_ready: int
    containers: list[dict]           # [{name, image, resources}]
    labels: dict
    selectors: dict
    hpa_enabled: bool = False
    hpa_min: int = 1
    hpa_max: int = 1
    dependencies: list[str] = field(default_factory=list)   # service names this workload calls


@dataclass
class ClusterTopology:
    context: str
    namespace: str
    nodes: list[dict]                # cluster node info
    workloads: list[ServiceNode]
    services: list[dict]
    config_maps: list[str]
    persistent_volumes: list[dict]
    ingresses: list[dict]
    network_policies: list[dict]
    risk_summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


class ClusterScanner:
    def __init__(self, config_path: str = "config.yaml"):
        cfg = load_config(config_path)
        self.namespace = cfg["kubernetes"]["namespace"]
        kube_ctx = cfg["kubernetes"].get("context") or None
        kubeconfig = cfg["kubernetes"].get("kubeconfig", "~/.kube/config")

        try:
            k8s_config.load_kube_config(
                config_file=kubeconfig or None,
                context=kube_ctx or None,
            )
        except Exception:
            # Fallback for in-cluster execution
            k8s_config.load_incluster_config()

        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.networking = client.NetworkingV1Api()
        self.autoscaling = client.AutoscalingV2Api()

        # Resolve active context name
        contexts, active = k8s_config.list_kube_config_contexts(kubeconfig or None)
        self.active_context = (kube_ctx or active["name"]) if active else "unknown"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def scan(self) -> ClusterTopology:
        ns = None if self.namespace == "all" else self.namespace
        namespaces = self._list_namespaces() if ns is None else [self.namespace]

        all_workloads: list[ServiceNode] = []
        all_services: list[dict] = []
        all_ingresses: list[dict] = []
        all_net_policies: list[dict] = []

        hpa_map = self._build_hpa_map(namespaces)

        for namespace in namespaces:
            all_workloads += self._scan_deployments(namespace, hpa_map)
            all_workloads += self._scan_statefulsets(namespace, hpa_map)
            all_workloads += self._scan_daemonsets(namespace)
            all_services += self._scan_services(namespace)
            all_ingresses += self._scan_ingresses(namespace)
            all_net_policies += self._scan_network_policies(namespace)

        nodes = self._scan_nodes()
        pvs = self._scan_persistent_volumes()
        cms = self._scan_config_maps(namespaces)

        topology = ClusterTopology(
            context=self.active_context,
            namespace=self.namespace,
            nodes=nodes,
            workloads=all_workloads,
            services=all_services,
            config_maps=cms,
            persistent_volumes=pvs,
            ingresses=all_ingresses,
            network_policies=all_net_policies,
        )
        topology.risk_summary = self._compute_risk_summary(topology)
        return topology

    # ------------------------------------------------------------------
    # Workload scanners
    # ------------------------------------------------------------------

    def _scan_deployments(self, namespace: str, hpa_map: dict) -> list[ServiceNode]:
        nodes = []
        try:
            deps = self.apps.list_namespaced_deployment(namespace)
        except ApiException:
            return nodes

        for d in deps.items:
            hpa = hpa_map.get(f"{namespace}/{d.metadata.name}", {})
            nodes.append(ServiceNode(
                name=d.metadata.name,
                namespace=namespace,
                kind="Deployment",
                replicas_desired=d.spec.replicas or 1,
                replicas_ready=d.status.ready_replicas or 0,
                containers=self._extract_containers(d.spec.template.spec.containers),
                labels=d.metadata.labels or {},
                selectors=d.spec.selector.match_labels or {},
                hpa_enabled=bool(hpa),
                hpa_min=hpa.get("min", 1),
                hpa_max=hpa.get("max", 1),
            ))
        return nodes

    def _scan_statefulsets(self, namespace: str, hpa_map: dict) -> list[ServiceNode]:
        nodes = []
        try:
            sts = self.apps.list_namespaced_stateful_set(namespace)
        except ApiException:
            return nodes

        for s in sts.items:
            hpa = hpa_map.get(f"{namespace}/{s.metadata.name}", {})
            nodes.append(ServiceNode(
                name=s.metadata.name,
                namespace=namespace,
                kind="StatefulSet",
                replicas_desired=s.spec.replicas or 1,
                replicas_ready=s.status.ready_replicas or 0,
                containers=self._extract_containers(s.spec.template.spec.containers),
                labels=s.metadata.labels or {},
                selectors=s.spec.selector.match_labels or {},
                hpa_enabled=bool(hpa),
                hpa_min=hpa.get("min", 1),
                hpa_max=hpa.get("max", 1),
            ))
        return nodes

    def _scan_daemonsets(self, namespace: str) -> list[ServiceNode]:
        nodes = []
        try:
            ds_list = self.apps.list_namespaced_daemon_set(namespace)
        except ApiException:
            return nodes

        for ds in ds_list.items:
            nodes.append(ServiceNode(
                name=ds.metadata.name,
                namespace=namespace,
                kind="DaemonSet",
                replicas_desired=ds.status.desired_number_scheduled or 0,
                replicas_ready=ds.status.number_ready or 0,
                containers=self._extract_containers(ds.spec.template.spec.containers),
                labels=ds.metadata.labels or {},
                selectors=ds.spec.selector.match_labels or {},
            ))
        return nodes

    # ------------------------------------------------------------------
    # Supporting scanners
    # ------------------------------------------------------------------

    def _scan_nodes(self) -> list[dict]:
        result = []
        try:
            node_list = self.core.list_node()
        except ApiException:
            return result

        for n in node_list.items:
            conditions = {c.type: c.status for c in (n.status.conditions or [])}
            result.append({
                "name": n.metadata.name,
                "ready": conditions.get("Ready") == "True",
                "roles": [
                    k.replace("node-role.kubernetes.io/", "")
                    for k in (n.metadata.labels or {})
                    if "node-role.kubernetes.io" in k
                ],
                "capacity": {
                    "cpu": str(n.status.capacity.get("cpu", "?")),
                    "memory": str(n.status.capacity.get("memory", "?")),
                },
                "instance_type": (n.metadata.labels or {}).get(
                    "node.kubernetes.io/instance-type", "unknown"
                ),
                "zone": (n.metadata.labels or {}).get(
                    "topology.kubernetes.io/zone", "unknown"
                ),
            })
        return result

    def _scan_services(self, namespace: str) -> list[dict]:
        result = []
        try:
            svc_list = self.core.list_namespaced_service(namespace)
        except ApiException:
            return result

        for s in svc_list.items:
            result.append({
                "name": s.metadata.name,
                "namespace": namespace,
                "type": s.spec.type,
                "cluster_ip": s.spec.cluster_ip,
                "ports": [
                    {"port": p.port, "protocol": p.protocol, "name": p.name}
                    for p in (s.spec.ports or [])
                ],
                "selector": s.spec.selector or {},
            })
        return result

    def _scan_ingresses(self, namespace: str) -> list[dict]:
        result = []
        try:
            ing_list = self.networking.list_namespaced_ingress(namespace)
        except ApiException:
            return result

        for i in ing_list.items:
            rules = []
            for r in (i.spec.rules or []):
                paths = []
                if r.http:
                    for p in (r.http.paths or []):
                        paths.append({
                            "path": p.path,
                            "service": p.backend.service.name if p.backend.service else "",
                        })
                rules.append({"host": r.host, "paths": paths})
            result.append({"name": i.metadata.name, "namespace": namespace, "rules": rules})
        return result

    def _scan_network_policies(self, namespace: str) -> list[dict]:
        result = []
        try:
            np_list = self.networking.list_namespaced_network_policy(namespace)
        except ApiException:
            return result

        for np in np_list.items:
            result.append({
                "name": np.metadata.name,
                "namespace": namespace,
                "pod_selector": np.spec.pod_selector.match_labels or {},
            })
        return result

    def _scan_persistent_volumes(self) -> list[dict]:
        result = []
        try:
            pv_list = self.core.list_persistent_volume()
        except ApiException:
            return result

        for pv in pv_list.items:
            result.append({
                "name": pv.metadata.name,
                "capacity": pv.spec.capacity or {},
                "access_modes": pv.spec.access_modes or [],
                "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
                "status": pv.status.phase,
            })
        return result

    def _scan_config_maps(self, namespaces: list[str]) -> list[str]:
        names = []
        for ns in namespaces:
            try:
                cms = self.core.list_namespaced_config_map(ns)
                names += [f"{ns}/{cm.metadata.name}" for cm in cms.items]
            except ApiException:
                pass
        return names

    def _build_hpa_map(self, namespaces: list[str]) -> dict:
        hpa_map: dict = {}
        for ns in namespaces:
            try:
                hpas = self.autoscaling.list_namespaced_horizontal_pod_autoscaler(ns)
                for h in hpas.items:
                    key = f"{ns}/{h.spec.scale_target_ref.name}"
                    hpa_map[key] = {
                        "min": h.spec.min_replicas or 1,
                        "max": h.spec.max_replicas,
                    }
            except ApiException:
                pass
        return hpa_map

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _list_namespaces(self) -> list[str]:
        try:
            ns_list = self.core.list_namespace()
            return [n.metadata.name for n in ns_list.items]
        except ApiException:
            return ["default"]

    @staticmethod
    def _extract_containers(containers) -> list[dict]:
        result = []
        for c in (containers or []):
            resources = {}
            if c.resources:
                resources = {
                    "requests": {
                        "cpu": str((c.resources.requests or {}).get("cpu", "?")),
                        "memory": str((c.resources.requests or {}).get("memory", "?")),
                    },
                    "limits": {
                        "cpu": str((c.resources.limits or {}).get("cpu", "?")),
                        "memory": str((c.resources.limits or {}).get("memory", "?")),
                    },
                }
            result.append({
                "name": c.name,
                "image": c.image,
                "resources": resources,
                "env_count": len(c.env or []),
                "volume_mounts": len(c.volume_mounts or []),
            })
        return result

    @staticmethod
    def _compute_risk_summary(topology: ClusterTopology) -> dict:
        single_replica = [
            w.name for w in topology.workloads
            if w.replicas_desired == 1 and w.kind == "Deployment"
        ]
        no_resource_limits = [
            w.name for w in topology.workloads
            if any(not c.get("resources", {}).get("limits") for c in w.containers)
        ]
        no_hpa = [
            w.name for w in topology.workloads
            if not w.hpa_enabled and w.kind == "Deployment"
        ]
        not_ready = [
            w.name for w in topology.workloads
            if w.replicas_ready < w.replicas_desired
        ]
        return {
            "single_replica_workloads": single_replica,
            "workloads_without_resource_limits": no_resource_limits,
            "workloads_without_hpa": no_hpa,
            "degraded_workloads": not_ready,
            "total_workloads": len(topology.workloads),
            "total_nodes": len(topology.nodes),
        }
