#!/usr/bin/env python3
"""
nsx_sync_edge_nodes.py

Comprehensive NSX Edge Node & Virtual Network Appliance (VNA) Health Remediation.

Features
--------
1. NSX Edge Transport Node Synchronization:
   - Triggers "Sync Edge Node Configuration" on every edge node across all NSX
     edge clusters via the NSX Manager REST API:
       POST /api/v1/transport-nodes/{id}?action=refresh_node_configuration
                                         &resource_type=EdgeNode
   - Pulls realized state from vSphere/edge CLI back into NSX Manager, resolving
     DEGRADED/failed states after lab power cycles, reverts, or storage outages.
   - Verifies pre- and post-sync deployment state and connectivity status.

2. Virtual Network Appliance (VNA) NIC & Host Remediation:
   - Targets VNA VMs (default: vna-wld01-01a) that experience disconnected virtual
     NICs on boot.
   - Step A: Discovers the VM and inspects virtual network adapters.
   - Step B: Attempts to connect disconnected NICs on the current host.
   - Step C: vMotions the VM to the designated healthy ESXi host
             (default: esx-06a.site-a.vcf.lab).
   - Step D: Reconnects all virtual NICs post-vMotion (connected=True, startConnected=True)
             and verifies healthy network link and guest IP state.

Requirements
------------
- Python 3.8+ (Standard library).
- Optional: pyVmomi (if installed, used for native vSphere SOAP API; otherwise
  falls back automatically to vSphere REST API).

Usage
-----
    python3 nsx_sync_edge_nodes.py                       # Run both Edge sync and VNA remediation
    python3 nsx_sync_edge_nodes.py --vna-only            # Remediate VNA VM(s) only
    python3 nsx_sync_edge_nodes.py --edge-only           # Sync NSX Edge nodes only
    python3 nsx_sync_edge_nodes.py --vna-vm vna-wld01-01a --target-host esx-06a.site-a.vcf.lab
    python3 nsx_sync_edge_nodes.py --dry-run             # Inspect without modifying
    python3 nsx_sync_edge_nodes.py --verbose             # Detailed debug/API output
"""

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── Optional pyVmomi Import ───────────────────────────────────────────────────

try:
    from pyVmomi import vim
    from pyVim.connect import SmartConnect, Disconnect
    PYVMOMI_AVAILABLE = True
except ImportError:
    PYVMOMI_AVAILABLE = False


# ── Configuration Defaults ────────────────────────────────────────────────────

NSX_HOST         = "nsx-wld01-a.site-a.vcf.lab"
NSX_USER         = "admin"

VCENTER_HOST     = "vc-wld01-a.site-a.vcf.lab"
VCENTER_USER     = "administrator@vsphere.local"
VCENTER_PORT     = 443

DEFAULT_VNA_VM   = "vna-wld01-01a"
DEFAULT_TARGET_HOST = "esx-06a.site-a.vcf.lab"

PASSWORD_FILE    = "/home/holuser/creds.txt"
RECHECK_WAIT     = 45   # seconds to wait before re-reading NSX edge state post-sync


# ── Logging ───────────────────────────────────────────────────────────────────

COLORS = {
    "INFO":    "\033[36m",   # Cyan
    "SUCCESS": "\033[32m",   # Green
    "WARN":    "\033[33m",   # Yellow
    "ERROR":   "\033[31m",   # Red
    "API":     "\033[35m",   # Magenta
    "HEADER":  "\033[1;34m", # Bold Blue
    "RESET":   "\033[0m",
}

_verbose = False


def log(msg: str, level: str = "INFO") -> None:
    ts    = time.strftime("%Y-%m-%d %H:%M:%S")
    color = COLORS.get(level, "")
    reset = COLORS["RESET"]
    print(f"{color}[{ts}] [{level}] {msg}{reset}", flush=True)


def log_api(method: str, url: str, status: int, body: str = "") -> None:
    color = COLORS["API"]
    reset = COLORS["RESET"]
    ok    = status < 300
    status_color = COLORS["SUCCESS"] if ok else COLORS["ERROR"]
    print(
        f"{color}[API] {method} {url}{reset}  "
        f"{status_color}→ HTTP {status}{reset}",
        flush=True,
    )
    if body and _verbose:
        try:
            parsed = json.loads(body)
            print(json.dumps(parsed, indent=2), flush=True)
        except Exception:
            print(body[:500], flush=True)
    elif body and not ok:
        print(f"  Response: {body[:300]}", flush=True)


def fail(msg: str) -> None:
    log(msg, "ERROR")
    sys.exit(1)


# ── Password Helper ───────────────────────────────────────────────────────────

def load_password(path: str) -> str:
    candidate_paths = [
        path,
        "/lmchol/home/holuser/Desktop/PASSWORD.txt",
        "/lmchol/holuser/Desktop/PASSWORD.txt",
        os.path.expanduser("~/Desktop/PASSWORD.txt"),
    ]
    for p in candidate_paths:
        if p and os.path.isfile(p):
            try:
                pw = open(p).read().strip()
                if pw:
                    log(f"Password loaded from {p}")
                    return pw
            except Exception:
                pass
    fail(f"Password file not found or empty (checked: {path})")
    return ""


# ── Result Tracking Dataclasses ───────────────────────────────────────────────

@dataclass
class EdgeNodeResult:
    node_id:          str
    display_name:     str           = ""
    cluster_name:     str           = ""
    initial_status:   str           = "UNKNOWN"
    initial_state:    str           = "UNKNOWN"
    synced:           bool          = False
    sync_error:       Optional[str] = None
    final_status:     str           = "-"
    final_state:      str           = "-"
    action:           str           = "NONE"


@dataclass
class VnaNicInfo:
    label:           str
    mac:             str           = ""
    network:         str           = ""
    connected:       bool          = False
    start_connected: bool          = False


@dataclass
class VnaResult:
    vm_name:          str
    initial_host:     str           = "UNKNOWN"
    final_host:       str           = "UNKNOWN"
    initial_nics:     List[VnaNicInfo] = field(default_factory=list)
    final_nics:       List[VnaNicInfo] = field(default_factory=list)
    initial_connected:bool          = False
    final_connected:  bool          = False
    vmotion_performed:bool          = False
    action:           str           = "NONE"
    error:            Optional[str] = None


# ── NSX REST API Client ───────────────────────────────────────────────────────

class NsxClient:
    """Minimal NSX Manager REST API client (stdlib only, no third-party deps)."""

    def __init__(self, host: str, user: str, password: str) -> None:
        self.host = host
        self.base = f"https://{host}"
        cred      = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {cred}"
        self._ctx  = ssl.create_default_context()
        self._ctx.check_hostname = False   # lab uses self-signed certs
        self._ctx.verify_mode    = ssl.CERT_NONE

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        log_response_body: bool = False,
    ) -> dict:
        url      = self.base + path
        data     = json.dumps(body).encode() if body else None
        req      = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("Content-Type",  "application/json")
        req.add_header("Accept",        "application/json")

        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                raw        = resp.read()
                status     = resp.status
                body_txt   = raw.decode(errors="replace")
                log_api(method, url, status, body_txt if log_response_body else "")
                return json.loads(raw) if raw else {}

        except urllib.error.HTTPError as exc:
            body_txt = exc.read().decode(errors="replace")
            log_api(method, url, exc.code, body_txt)
            raise RuntimeError(f"HTTP {exc.code}: {body_txt[:300]}")

        except urllib.error.URLError as exc:
            log(f"Connection error {method} {url}: {exc.reason}", "ERROR")
            raise RuntimeError(f"Connection error: {exc.reason}")

    def get(self, path: str) -> dict:
        return self._request("GET", path)

    def post(self, path: str, body: Optional[dict] = None,
             log_response_body: bool = True) -> dict:
        return self._request("POST", path, body, log_response_body=log_response_body)

    def get_paged(self, base_path: str, page_size: int = 100) -> list:
        sep     = "&" if "?" in base_path else "?"
        results: list = []
        cursor: Optional[str] = None
        while True:
            path = base_path + f"{sep}page_size={page_size}"
            if cursor:
                path += f"&cursor={cursor}"
            data = self.get(path)
            results.extend(data.get("results", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return results


def get_node_connectivity(client: NsxClient, node_id: str) -> str:
    """GET /api/v1/transport-nodes/{id}/status"""
    try:
        data = client.get(f"/api/v1/transport-nodes/{node_id}/status")
        return (
            data.get("node_status", {}).get("overall_status")
            or data.get("overall_status")
            or "UNKNOWN"
        )
    except Exception as exc:
        log(f"    Could not read connectivity status for {node_id}: {exc}", "WARN")
        return "UNKNOWN"


def get_node_deployment_state(client: NsxClient, node_id: str) -> str:
    """GET /api/v1/transport-nodes/{id}/state"""
    try:
        data  = client.get(f"/api/v1/transport-nodes/{node_id}/state")
        state = data.get("node_deployment_state", {}).get("state", "UNKNOWN")
        details = data.get("node_deployment_state", {}).get("details", [])
        if details and _verbose:
            for d in details:
                log(f"    State detail: {d.get('state')} — {d.get('failure_message','')}", "WARN")
        return state
    except Exception as exc:
        log(f"    Could not read deployment state for {node_id}: {exc}", "WARN")
        return "UNKNOWN"


def check_node(client: NsxClient, r: EdgeNodeResult) -> None:
    conn  = get_node_connectivity(client, r.node_id)
    state = get_node_deployment_state(client, r.node_id)
    if r.synced:
        r.final_status = conn
        r.final_state  = state
    else:
        r.initial_status = conn
        r.initial_state  = state


def refresh_edge_node(client: NsxClient, node_id: str) -> None:
    """POST /api/v1/transport-nodes/{id}?action=refresh_node_configuration&resource_type=EdgeNode"""
    path = (
        f"/api/v1/transport-nodes/{node_id}"
        f"?action=refresh_node_configuration&resource_type=EdgeNode"
    )
    client.post(path, body=None, log_response_body=True)


# ── vCenter Client (pyVmomi Primary & REST Fallback) ──────────────────────────

class VCenterManager:
    """Unified vCenter management client supporting pyVmomi and REST API fallback."""

    def __init__(self, host: str, user: str, password: str, port: int = 443):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.si = None
        self.content = None
        self.rest_session_id = None
        self.connected_user = user
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def connect(self) -> bool:
        users_to_try = [self.user]
        if "@" in self.user:
            prefix, domain = self.user.split("@", 1)
            alt_domain = "wld.sso" if domain == "vsphere.local" else "vsphere.local"
            users_to_try.append(f"{prefix}@{alt_domain}")

        # 1. Try pyVmomi if available
        if PYVMOMI_AVAILABLE:
            for u in users_to_try:
                try:
                    log(f"Connecting to vCenter https://{self.host}:{self.port} (pyVmomi) as {u}...")
                    self.si = SmartConnect(
                        host=self.host,
                        user=u,
                        pwd=self.password,
                        port=self.port,
                        sslContext=self._ctx,
                    )
                    self.content = self.si.RetrieveContent()
                    self.connected_user = u
                    log(f"Connected to vCenter {self.host} as {u} (pyVmomi)", "SUCCESS")
                    return True
                except Exception as exc:
                    log(f"pyVmomi login as {u} failed: {exc}", "WARN")

        # 2. Try vSphere REST API fallback
        log(f"Attempting vSphere REST API connection to https://{self.host}...", "INFO")
        for u in users_to_try:
            cred = base64.b64encode(f"{u}:{self.password}".encode()).decode()
            endpoints = ["/api/session", "/rest/com/vmware/cis/session", "/rest/vcenter/session"]
            for ep in endpoints:
                url = f"https://{self.host}{ep}"
                req = urllib.request.Request(url, method="POST")
                req.add_header("Authorization", f"Basic {cred}")
                try:
                    with urllib.request.urlopen(req, context=self._ctx, timeout=20) as resp:
                        token = resp.headers.get("vmware-api-session-id")
                        if not token:
                            raw = resp.read().decode(errors="replace").strip().strip('"')
                            if raw and "{" not in raw:
                                token = raw
                        if token:
                            self.rest_session_id = token
                            self.connected_user = u
                            log(f"Connected to vCenter {self.host} via REST API as {u}", "SUCCESS")
                            return True
                except Exception:
                    continue

        fail(f"Unable to authenticate to vCenter {self.host} with provided credentials.")
        return False

    def disconnect(self) -> None:
        if self.si and PYVMOMI_AVAILABLE:
            try:
                Disconnect(self.si)
            except Exception:
                pass

    # ── pyVmomi Task Helper ───────────────────────────────────────────────────

    def wait_for_task(self, task, timeout_s: int = 300) -> Any:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            state = task.info.state
            if state == vim.TaskInfo.State.success:
                return task.info.result
            if state == vim.TaskInfo.State.error:
                err_msg = task.info.error.msg if hasattr(task.info.error, "msg") else str(task.info.error)
                raise RuntimeError(f"vCenter Task failed: {err_msg}")
            time.sleep(2)
        raise TimeoutError(f"vCenter Task timed out after {timeout_s}s")

    # ── pyVmomi VM & Host Discovery ───────────────────────────────────────────

    def find_vms(self, name_pattern: str) -> list:
        if self.content:
            container = self.content.viewManager.CreateContainerView(
                self.content.rootFolder, [vim.VirtualMachine], True
            )
            vms = []
            pat = name_pattern.lower()
            for vm in container.view:
                if pat in vm.name.lower():
                    vms.append(vm)
            container.Destroy()
            return vms
        elif self.rest_session_id:
            try:
                url = f"https://{self.host}/api/vcenter/vm"
                req = urllib.request.Request(url)
                req.add_header("vmware-api-session-id", self.rest_session_id)
                with urllib.request.urlopen(req, context=self._ctx, timeout=20) as resp:
                    data = json.loads(resp.read())
                    vms = data if isinstance(data, list) else data.get("value", [])
                    return [v for v in vms if name_pattern.lower() in v.get("name", "").lower()]
            except Exception as exc:
                log(f"Failed to list VMs via REST API: {exc}", "ERROR")
                return []
        return []

    def find_host(self, host_name: str) -> Any:
        if self.content:
            container = self.content.viewManager.CreateContainerView(
                self.content.rootFolder, [vim.HostSystem], True
            )
            target = None
            hn = host_name.lower().strip()
            for h in container.view:
                if hn == h.name.lower() or hn in h.name.lower():
                    target = h
                    break
            container.Destroy()
            return target
        elif self.rest_session_id:
            try:
                url = f"https://{self.host}/api/vcenter/host"
                req = urllib.request.Request(url)
                req.add_header("vmware-api-session-id", self.rest_session_id)
                with urllib.request.urlopen(req, context=self._ctx, timeout=20) as resp:
                    data = json.loads(resp.read())
                    hosts = data if isinstance(data, list) else data.get("value", [])
                    hn = host_name.lower().strip()
                    for h in hosts:
                        if hn == h.get("name", "").lower() or hn in h.get("name", "").lower():
                            return h
            except Exception as exc:
                log(f"Failed to list hosts via REST API: {exc}", "ERROR")
        return None

    # ── pyVmomi NIC Inspection & Reconfiguration ──────────────────────────────

    def get_vm_nics(self, vm: Any) -> List[VnaNicInfo]:
        nics = []
        if self.content and hasattr(vm, "config"):
            if not vm.config or not vm.config.hardware:
                return nics
            for dev in vm.config.hardware.device:
                if isinstance(dev, vim.vm.device.VirtualEthernetCard):
                    conn = dev.connectable.connected if dev.connectable else False
                    start_conn = dev.connectable.startConnected if dev.connectable else False
                    label = dev.deviceInfo.label if dev.deviceInfo else "Network Adapter"
                    summary = dev.deviceInfo.summary if dev.deviceInfo else ""
                    mac = dev.macAddress or ""
                    nics.append(VnaNicInfo(
                        label=label,
                        mac=mac,
                        network=summary,
                        connected=conn,
                        start_connected=start_conn,
                    ))
        elif self.rest_session_id and isinstance(vm, dict):
            vm_id = vm.get("vm")
            try:
                url = f"https://{self.host}/api/vcenter/vm/{vm_id}/hardware/ethernet"
                req = urllib.request.Request(url)
                req.add_header("vmware-api-session-id", self.rest_session_id)
                with urllib.request.urlopen(req, context=self._ctx, timeout=20) as resp:
                    data = json.loads(resp.read())
                    eths = data if isinstance(data, list) else data.get("value", [])
                    for e in eths:
                        nics.append(VnaNicInfo(
                            label=e.get("label", f"NIC {e.get('nic')}"),
                            mac=e.get("mac_address", ""),
                            network=str(e.get("backing", {}).get("network", "")),
                            connected=(e.get("state") == "CONNECTED"),
                            start_connected=bool(e.get("start_connected", False)),
                        ))
            except Exception as exc:
                log(f"Failed to get NICs for VM {vm_id} via REST: {exc}", "WARN")
        return nics

    def reconfigure_nics(
        self,
        vm: Any,
        connect: bool = True,
        start_connected: bool = True,
        dry_run: bool = False,
    ) -> Tuple[bool, int]:
        """Ensures all virtual NICs on the VM have connected=True and startConnected=True."""
        if self.content and hasattr(vm, "config"):
            changes = []
            for dev in vm.config.hardware.device:
                if isinstance(dev, vim.vm.device.VirtualEthernetCard):
                    curr_conn = dev.connectable.connected if dev.connectable else False
                    curr_start = dev.connectable.startConnected if dev.connectable else False
                    if not curr_conn or not curr_start:
                        dev_spec = vim.vm.device.VirtualDeviceSpec()
                        dev_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
                        dev_spec.device = dev
                        dev_spec.device.connectable.connected = connect
                        dev_spec.device.connectable.startConnected = start_connected
                        changes.append(dev_spec)

            if not changes:
                log(f"  All NICs on VM '{vm.name}' already connected (startConnected={start_connected})", "SUCCESS")
                return True, 0

            if dry_run:
                log(f"  → DRY RUN: Would reconfigure {len(changes)} NIC(s) on '{vm.name}' (connected={connect}, startConnected={start_connected})", "WARN")
                return True, len(changes)

            spec = vim.vm.ConfigSpec()
            spec.deviceChange = changes
            task = vm.ReconfigVM_Task(spec=spec)
            self.wait_for_task(task)
            log(f"  Reconfigured {len(changes)} NIC(s) on VM '{vm.name}' → connected={connect}, startConnected={start_connected}", "SUCCESS")
            return True, len(changes)

        elif self.rest_session_id and isinstance(vm, dict):
            vm_id = vm.get("vm")
            nics = self.get_vm_nics(vm)
            changed = 0
            for nic in nics:
                if not nic.connected or not nic.start_connected:
                    nic_id = nic.label.replace("Network adapter ", "")
                    if dry_run:
                        log(f"  → DRY RUN: Would connect REST NIC {nic.label} on VM {vm_id}", "WARN")
                        changed += 1
                        continue
                    try:
                        url_conn = f"https://{self.host}/api/vcenter/vm/{vm_id}/hardware/ethernet/{nic_id}/connect"
                        req_conn = urllib.request.Request(url_conn, method="POST")
                        req_conn.add_header("vmware-api-session-id", self.rest_session_id)
                        with urllib.request.urlopen(req_conn, context=self._ctx, timeout=20):
                            pass
                        changed += 1
                    except Exception as exc:
                        log(f"  REST connect NIC {nic.label} failed: {exc}", "WARN")
            return True, changed

        return False, 0

    # ── pyVmomi vMotion (Relocate) ────────────────────────────────────────────

    def vmotion_vm(self, vm: Any, target_host: Any, dry_run: bool = False) -> bool:
        if self.content and hasattr(vm, "runtime"):
            current_host = vm.runtime.host
            curr_name = current_host.name if current_host else "Unknown"
            target_name = target_host.name if hasattr(target_host, "name") else str(target_host)

            if current_host and (curr_name.lower() == target_name.lower() or target_name.lower() in curr_name.lower()):
                log(f"  VM '{vm.name}' is already running on target host '{curr_name}'", "INFO")
                return True

            log(f"  Initiating vMotion for '{vm.name}' ({curr_name} → {target_name})...", "WARN")
            if dry_run:
                log(f"  → DRY RUN: Would call RelocateVM_Task(host={target_name})", "WARN")
                return True

            relocate_spec = vim.vm.RelocateSpec()
            relocate_spec.host = target_host

            # Map resource pool if crossing compute clusters
            if vm.resourcePool:
                curr_parent = current_host.parent if current_host else None
                target_parent = target_host.parent if hasattr(target_host, "parent") else None
                if curr_parent != target_parent and hasattr(target_parent, "resourcePool"):
                    relocate_spec.pool = target_parent.resourcePool

            task = vm.RelocateVM_Task(
                spec=relocate_spec,
                priority=vim.VirtualMachine.MovePriority.defaultPriority,
            )
            self.wait_for_task(task, timeout_s=300)
            log(f"  vMotion completed! VM '{vm.name}' is now on '{target_name}'", "SUCCESS")
            return True

        elif self.rest_session_id and isinstance(vm, dict) and isinstance(target_host, dict):
            vm_id = vm.get("vm")
            host_id = target_host.get("host")
            host_name = target_host.get("name", host_id)
            log(f"  Initiating REST vMotion for VM '{vm.get('name')}' to '{host_name}'...", "WARN")
            if dry_run:
                log(f"  → DRY RUN: Would POST /api/vcenter/vm/{vm_id}/relocate", "WARN")
                return True
            try:
                url = f"https://{self.host}/api/vcenter/vm/{vm_id}/relocate"
                body = json.dumps({"placement": {"host": host_id}}).encode()
                req = urllib.request.Request(url, data=body, method="POST")
                req.add_header("vmware-api-session-id", self.rest_session_id)
                req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, context=self._ctx, timeout=60):
                    log(f"  REST vMotion completed for VM '{vm.get('name')}'", "SUCCESS")
                    return True
            except Exception as exc:
                log(f"  REST vMotion error: {exc}", "ERROR")
                return False

        return False


# ── VNA Remediation Workflow ──────────────────────────────────────────────────

def remediate_vna_vm(
    vc: VCenterManager,
    vm: Any,
    target_host_name: str,
    force_vmotion: bool,
    dry_run: bool,
) -> VnaResult:
    vm_name = vm.name if hasattr(vm, "name") else vm.get("name", "Unknown-VNA")
    current_host_name = "Unknown"
    if hasattr(vm, "runtime") and vm.runtime.host:
        current_host_name = vm.runtime.host.name
    elif isinstance(vm, dict):
        current_host_name = vm.get("host", "Unknown")

    result = VnaResult(
        vm_name=vm_name,
        initial_host=current_host_name,
        final_host=current_host_name,
    )

    print()
    log(f"── Processing VNA VM: {vm_name} ──", "HEADER")
    log(f"  Current ESXi Host: {current_host_name}")

    # Inspect Initial NIC State
    initial_nics = vc.get_vm_nics(vm)
    result.initial_nics = initial_nics
    all_initially_connected = bool(initial_nics and all(n.connected and n.start_connected for n in initial_nics))
    result.initial_connected = all_initially_connected

    for nic in initial_nics:
        status_str = "CONNECTED" if (nic.connected and nic.start_connected) else "DISCONNECTED"
        level = "SUCCESS" if (nic.connected and nic.start_connected) else "WARN"
        log(f"    NIC [{nic.label}] (MAC: {nic.mac}) → {status_str} (connected={nic.connected}, startConnected={nic.start_connected})", level)

    # Step A: Attempt NIC Reconnection on Current Host
    nics_were_disconnected = not all_initially_connected
    if nics_were_disconnected:
        log("  Step A: Disconnected NICs detected on boot. Attempting reconnection on current host...", "WARN")
        try:
            vc.reconfigure_nics(vm, connect=True, start_connected=True, dry_run=dry_run)
            time.sleep(2)
        except Exception as exc:
            log(f"  Initial NIC connection attempt failed: {exc}", "WARN")

    # Step B: Determine if vMotion to target host is required
    target_host_obj = vc.find_host(target_host_name)
    needs_vmotion = False

    if not target_host_obj:
        log(f"  Target host '{target_host_name}' not found in vCenter inventory. Skipping vMotion.", "WARN")
    else:
        actual_target_name = target_host_obj.name if hasattr(target_host_obj, "name") else target_host_obj.get("name", target_host_name)
        is_already_on_target = (
            current_host_name.lower() == actual_target_name.lower()
            or actual_target_name.lower() in current_host_name.lower()
        )

        if force_vmotion or nics_were_disconnected or not is_already_on_target:
            needs_vmotion = not is_already_on_target

        if needs_vmotion:
            log(f"  Step B: Performing vMotion to designated target host '{actual_target_name}'...", "WARN")
            try:
                vmotion_ok = vc.vmotion_vm(vm, target_host_obj, dry_run=dry_run)
                if vmotion_ok:
                    result.vmotion_performed = True
                    result.final_host = actual_target_name
                    log(f"  vMotion to '{actual_target_name}' succeeded.", "SUCCESS")
                else:
                    log(f"  vMotion to '{actual_target_name}' failed.", "ERROR")
            except Exception as exc:
                result.error = f"vMotion error: {exc}"
                log(f"  vMotion exception: {exc}", "ERROR")
        else:
            log(f"  VM is already residing on healthy target host '{actual_target_name}'.", "SUCCESS")

    # Step C: Reconnect NICs post-vMotion & Verify
    log("  Step C: Reconnecting virtual NICs post-vMotion / ensuring link state...", "INFO")
    try:
        vc.reconfigure_nics(vm, connect=True, start_connected=True, dry_run=dry_run)
        time.sleep(3)
    except Exception as exc:
        log(f"  Post-vMotion NIC reconnection failed: {exc}", "ERROR")
        if not result.error:
            result.error = f"Post-vMotion NIC error: {exc}"

    # Final NIC State Check
    final_nics = vc.get_vm_nics(vm)
    result.final_nics = final_nics
    all_finally_connected = bool(final_nics and all(n.connected and n.start_connected for n in final_nics))
    result.final_connected = all_finally_connected

    log("  Final NIC Link Verification:")
    for nic in final_nics:
        status_str = "CONNECTED" if (nic.connected and nic.start_connected) else "DISCONNECTED"
        level = "SUCCESS" if (nic.connected and nic.start_connected) else "ERROR"
        log(f"    NIC [{nic.label}] (MAC: {nic.mac}) → {status_str} (connected={nic.connected}, startConnected={nic.start_connected})", level)

    # Guest Info / IP Check (pyVmomi)
    if hasattr(vm, "guest") and vm.guest:
        ip = vm.guest.ipAddress or "N/A"
        tools_status = vm.guest.toolsRunningStatus or "unknown"
        log(f"  Guest IP: {ip} | VMware Tools: {tools_status}", "INFO")

    if all_finally_connected:
        result.action = "HEALTHY"
    elif dry_run:
        result.action = "DRY_RUN"
    else:
        result.action = "PARTIAL" if result.vmotion_performed else "ERROR"

    return result


# ── Argument Parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "NSX Edge Node Configuration Sync & VNA Health Remediation.\n"
            "----------------------------------------------------------\n"
            f"NSX Manager : {NSX_HOST}\n"
            f"vCenter     : {VCENTER_HOST}\n"
            f"Target Host : {DEFAULT_TARGET_HOST}\n"
            f"VNA VM      : {DEFAULT_VNA_VM}\n"
            f"Password    : {PASSWORD_FILE}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--nsx-host", default=NSX_HOST,
        help=f"NSX Manager FQDN or IP (default: {NSX_HOST})",
    )
    p.add_argument(
        "--nsx-user", default=NSX_USER,
        help=f"NSX admin username (default: {NSX_USER})",
    )
    p.add_argument(
        "--vc-host", default=VCENTER_HOST,
        help=f"vCenter FQDN or IP (default: {VCENTER_HOST})",
    )
    p.add_argument(
        "--vc-user", default=VCENTER_USER,
        help=f"vCenter SSO username (default: {VCENTER_USER})",
    )
    p.add_argument(
        "--vc-port", type=int, default=VCENTER_PORT,
        help=f"vCenter HTTPS port (default: {VCENTER_PORT})",
    )
    p.add_argument(
        "--vna-vm", default=DEFAULT_VNA_VM,
        help=f"VNA VM name pattern or substring (default: {DEFAULT_VNA_VM})",
    )
    p.add_argument(
        "--target-host", default=DEFAULT_TARGET_HOST,
        help=f"Target ESXi host for vMotion (default: {DEFAULT_TARGET_HOST})",
    )
    p.add_argument(
        "--force-vmotion", action="store_true",
        help="Force vMotion to target host even if NICs connected on current host",
    )
    p.add_argument(
        "--skip-edge", action="store_true",
        help="Skip NSX Edge node synchronization",
    )
    p.add_argument(
        "--skip-vna", action="store_true",
        help="Skip VNA VM NIC & vMotion remediation",
    )
    p.add_argument(
        "--edge-only", action="store_true",
        help="Alias for --skip-vna (only sync NSX Edge nodes)",
    )
    p.add_argument(
        "--vna-only", action="store_true",
        help="Alias for --skip-edge (only remediate VNA VMs)",
    )
    p.add_argument(
        "--password-file", default=PASSWORD_FILE,
        help=f"Path to password file (default: {PASSWORD_FILE})",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be called without modifying changes",
    )
    p.add_argument(
        "--wait", type=int, default=RECHECK_WAIT, metavar="SECONDS",
        help=f"Seconds to wait before re-checking edge state after sync (default: {RECHECK_WAIT})",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Dump full JSON response bodies and detailed API outputs",
    )
    return p.parse_args()


# ── Main Orchestration ────────────────────────────────────────────────────────

def main() -> None:
    global _verbose
    args = parse_args()
    _verbose = args.verbose

    run_edge = not (args.skip_edge or args.vna_only)
    run_vna  = not (args.skip_vna or args.edge_only)

    password = load_password(args.password_file)

    if args.dry_run:
        log("DRY RUN mode enabled — no modifying actions will be executed", "WARN")

    edge_results: List[EdgeNodeResult] = []
    vna_results:  List[VnaResult]      = []

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 1: VNA VM NIC & vMotion Remediation
    # ══════════════════════════════════════════════════════════════════════════
    if run_vna:
        print()
        log("====================================================================", "HEADER")
        log(" PHASE 1: Virtual Network Appliance (VNA) Remediation ", "HEADER")
        log("====================================================================", "HEADER")
        log(f"Target vCenter : https://{args.vc_host}:{args.vc_port}")
        log(f"Target VNA VM  : {args.vna_vm}")
        log(f"Target ESXi    : {args.target_host}")

        vc = VCenterManager(
            host=args.vc_host,
            user=args.vc_user,
            password=password,
            port=args.vc_port,
        )

        try:
            vc.connect()
            vms = vc.find_vms(args.vna_vm)

            # Auto-fallback to Management vCenter if not found on Workload vCenter
            if not vms and "wld" in args.vc_host:
                alt_vc = args.vc_host.replace("wld01", "mgmt").replace("wld", "mgmt")
                log(f"VM '{args.vna_vm}' not found on {args.vc_host}; checking {alt_vc}...", "WARN")
                vc.disconnect()
                vc = VCenterManager(host=alt_vc, user=args.vc_user, password=password, port=args.vc_port)
                try:
                    vc.connect()
                    vms = vc.find_vms(args.vna_vm)
                except Exception as ex:
                    log(f"Failed connecting to fallback vCenter {alt_vc}: {ex}", "WARN")

            if not vms:
                log(f"No VMs matching '{args.vna_vm}' found in vCenter inventory.", "WARN")
            else:
                log(f"Found {len(vms)} matching VNA VM(s): {', '.join([v.name if hasattr(v, 'name') else v.get('name') for v in vms])}")
                for vm in vms:
                    res = remediate_vna_vm(
                        vc=vc,
                        vm=vm,
                        target_host_name=args.target_host,
                        force_vmotion=args.force_vmotion,
                        dry_run=args.dry_run,
                    )
                    vna_results.append(res)

        except Exception as exc:
            log(f"VNA remediation encountered error: {exc}", "ERROR")
        finally:
            vc.disconnect()

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 2: NSX Edge Node Configuration Synchronization
    # ══════════════════════════════════════════════════════════════════════════
    if run_edge:
        print()
        log("====================================================================", "HEADER")
        log(" PHASE 2: NSX Edge Node Configuration Synchronization ", "HEADER")
        log("====================================================================", "HEADER")
        log(f"Connecting to NSX Manager: https://{args.nsx_host} (user: {args.nsx_user})")

        nsx_client = NsxClient(args.nsx_host, args.nsx_user, password)
        try:
            info = nsx_client.get("/api/v1/node")
            ver  = info.get("node_version", "unknown")
            log(f"NSX Manager reachable — version {ver}", "SUCCESS")
        except Exception as exc:
            log(f"Cannot reach NSX Manager: {exc}", "ERROR")
            if not run_vna:
                fail(f"NSX Manager connection failed: {exc}")

        log("\nFetching NSX edge clusters...")
        try:
            clusters = nsx_client.get_paged("/api/v1/edge-clusters")
        except Exception as exc:
            clusters = []
            log(f"Failed to list edge clusters: {exc}", "ERROR")

        if not clusters:
            log("No edge clusters found or query failed.", "WARN")
        else:
            log(f"Found {len(clusters)} edge cluster(s)")

            for cluster in clusters:
                cluster_id   = cluster.get("id", "")
                cluster_name = cluster.get("display_name", cluster_id)
                members      = cluster.get("members", [])

                print()
                log(f"Edge Cluster: {cluster_name} ({len(members)} member node(s))", "HEADER")

                if not members:
                    log("  No members — skipping", "WARN")
                    continue

                for member in members:
                    node_id = member.get("transport_node_id", "")
                    if not node_id:
                        log("  Member entry has no transport_node_id — skipping", "WARN")
                        continue

                    # Resolve display name
                    display_name = node_id
                    try:
                        tn = nsx_client.get(f"/api/v1/transport-nodes/{node_id}")
                        display_name = tn.get("display_name", node_id)
                    except Exception:
                        pass

                    result = EdgeNodeResult(
                        node_id=node_id,
                        display_name=display_name,
                        cluster_name=cluster_name,
                    )

                    # Pre-sync Status Check
                    log(f"\n  Node: {display_name} (id: {node_id})")
                    log("  Checking pre-sync status...")
                    check_node(nsx_client, result)

                    conn_level  = "SUCCESS" if result.initial_status == "UP" else "WARN"
                    state_level = "SUCCESS" if result.initial_state  == "NODE_READY" else "WARN"
                    log(f"    Connectivity : {result.initial_status}", conn_level)
                    log(f"    Deploy state : {result.initial_state}",  state_level)

                    # Dry Run
                    if args.dry_run:
                        log(
                            f"  → DRY RUN: would POST /api/v1/transport-nodes/{node_id}"
                            f"?action=refresh_node_configuration&resource_type=EdgeNode",
                            "WARN",
                        )
                        result.action = "DRY_RUN"
                        edge_results.append(result)
                        continue

                    # Trigger Sync
                    log("  → Sending: Sync Edge Node Configuration", "WARN")
                    try:
                        refresh_edge_node(nsx_client, node_id)
                        result.synced = True
                        result.action = "SYNCED"
                        log("  Sync accepted by NSX Manager", "SUCCESS")
                    except Exception as exc:
                        result.sync_error = str(exc)
                        result.action     = "ERROR"
                        log(f"  Sync call failed: {exc}", "ERROR")

                    edge_results.append(result)

            # Wait and Re-check Edge Nodes
            synced_nodes = [r for r in edge_results if r.synced]
            if synced_nodes:
                print()
                log(f"Waiting {args.wait}s for edge sync operations to propagate...")
                time.sleep(args.wait)

                log("Re-checking node state after sync...")
                for r in synced_nodes:
                    check_node(nsx_client, r)
                    conn_level  = "SUCCESS" if r.final_status == "UP"       else "WARN"
                    state_level = "SUCCESS" if r.final_state  == "NODE_READY" else "WARN"
                    log(f"\n  {r.display_name}")
                    log(f"    Connectivity : {r.initial_status} → {r.final_status}", conn_level)
                    log(f"    Deploy state : {r.initial_state} → {r.final_state}",   state_level)
                    if r.final_state not in ("NODE_READY", "SUCCESS") and r.final_status != "UP":
                        log(
                            f"    Still not healthy after sync — may need redeployment "
                            f"(POST /api/v1/transport-nodes/{r.node_id}?action=redeploy)",
                            "WARN",
                        )

    # ══════════════════════════════════════════════════════════════════════════
    # FINAL SUMMARY REPORT
    # ══════════════════════════════════════════════════════════════════════════
    RESET = COLORS["RESET"]
    ACTION_COLOR = {
        "HEALTHY": "\033[32m",
        "SYNCED":  "\033[32m",
        "DRY_RUN": "\033[33m",
        "PARTIAL": "\033[33m",
        "ERROR":   "\033[31m",
        "NONE":    "\033[37m",
    }

    all_ok = True

    if vna_results:
        print()
        log("════════════════════ VNA REMEDIATION SUMMARY ════════════════════", "HEADER")
        VN = 24; VH = 26; VM = 10; VC = 16; VA = 10
        vna_hdr = (
            f"{'VM Name':<{VN}} {'Host (Before → After)':<{VH}} "
            f"{'vMotion':<{VM}} {'NICs Connected':<{VC}} {'Status':<{VA}}"
        )
        print(vna_hdr)
        print("-" * len(vna_hdr))
        for vr in vna_results:
            color = ACTION_COLOR.get(vr.action, "")
            host_str = f"{vr.initial_host.split('.')[0]} → {vr.final_host.split('.')[0]}"
            vmot_str = "YES" if vr.vmotion_performed else "NO"
            nic_str  = f"{'YES' if vr.initial_connected else 'NO'} → {'YES' if vr.final_connected else 'NO'}"
            print(
                f"{color}"
                f"{vr.vm_name:<{VN}} "
                f"{host_str:<{VH}} "
                f"{vmot_str:<{VM}} "
                f"{nic_str:<{VC}} "
                f"{vr.action:<{VA}}"
                f"{RESET}"
            )
            if vr.error:
                print(f"  {'':>{VN}}  Error: {vr.error[:100]}")
            if vr.action in ("ERROR", "PARTIAL"):
                all_ok = False

    if edge_results:
        print()
        log("════════════════════ EDGE SYNC SUMMARY ══════════════════════════", "HEADER")
        CN = 36; CC = 26; CA = 9; CS = 10; CD = 16
        hdr = (
            f"{'Node':<{CN}} {'Cluster':<{CC}} {'Action':<{CA}} "
            f"{'Conn':<{CS}} {'Deploy State':<{CD}}"
        )
        print(hdr)
        print("-" * len(hdr))

        for r in edge_results:
            color = ACTION_COLOR.get(r.action, "")
            conn  = f"{r.initial_status}→{r.final_status}" if r.synced else r.initial_status
            state = f"{r.initial_state}→{r.final_state}"   if r.synced else r.initial_state
            print(
                f"{color}"
                f"{r.display_name:<{CN}} "
                f"{r.cluster_name:<{CC}} "
                f"{r.action:<{CA}} "
                f"{conn:<{CS}} "
                f"{state:<{CD}}"
                f"{RESET}"
            )
            if r.sync_error:
                print(f"  {'':>{CN}}  Error: {r.sync_error[:100]}")
            if r.action == "ERROR":
                all_ok = False

    print()
    if args.dry_run:
        log("Dry run complete — no changes were made.", "WARN")
    elif all_ok:
        log("All remediation tasks completed successfully!", "SUCCESS")
    else:
        log("One or more tasks had warnings or errors — check details above.", "WARN")
        sys.exit(1)


if __name__ == "__main__":
    main()
