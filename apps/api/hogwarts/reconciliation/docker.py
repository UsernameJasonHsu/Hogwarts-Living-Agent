"""Exact Linux daemon adapter. Discovery never authorizes adoption or deletion."""

import json
import re

from sqlalchemy import select

from hogwarts.models import IdempotencyRecord, PrototypeRun, SandboxInstance
from hogwarts.qa.contracts import QAJournal
from hogwarts.qa.driver import container_argv, environment_entries
from hogwarts.reconciliation.contracts import Resource, deny
from hogwarts.service import DomainError, digest, utc
from hogwarts.workspaces.manager import token_hash


class DockerResourceBackend:
    def __init__(self, controller, qa=None):
        self.controller, self.qa = controller, qa
        self.targets = {}

    @staticmethod
    def _container_fingerprint(info):
        # Avoid recording environment or daemon host paths in the public journal.
        config = dict(info["Config"])
        config["Env"] = environment_entries(config.get("Env", []))
        host = dict(info["HostConfig"])
        for name in ("MaskedPaths", "ReadonlyPaths", "CapDrop", "SecurityOpt"):
            if isinstance(host.get(name), list):
                host[name] = sorted(host[name])
        if isinstance(host.get("Ulimits"), list):
            host["Ulimits"] = sorted(host["Ulimits"], key=lambda x: x["Name"])
        mounts = sorted(info.get("Mounts", []), key=lambda x: (x["Destination"], x.get("Name", "")))
        return digest(
            {
                "Id": info["Id"],
                "Created": info["Created"],
                "Name": info["Name"],
                "Image": info["Image"],
                "Config": config,
                "HostConfig": host,
                "Mounts": mounts,
            }
        )

    @staticmethod
    def _volume_fingerprint(info):
        return digest(
            {k: info.get(k) for k in ("Name", "CreatedAt", "Driver", "Scope", "Options", "Labels")}
        )

    def _attachments(self, driver, name):
        raw = driver.call(
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            "volume=" + name,
            "--format",
            "{{.ID}}",
        ).stdout
        ids = raw.decode().splitlines()
        if len(ids) > 1000 or any(not re.fullmatch(r"[0-9a-f]{64}", x) for x in ids):
            deny("cleanup_attachment_inventory_invalid")
        return set(ids)

    def _observe(self, spec):
        d, row, profile, role = spec
        if role == "holder":
            info = d.inspect(row)
            if info:
                d.verify(row, profile)
        elif role == "seed":
            info = d.seed_info(row)
            if info:
                h, c = info["HostConfig"], info["Config"]
                mounts = info.get("Mounts", [])
                if (
                    info["Image"] != profile.image
                    or c["User"] != "10001:10001"
                    or c["Entrypoint"] != ["/usr/local/bin/python"]
                    or c["Cmd"]
                    != [
                        "-I",
                        "-B",
                        "/opt/hogwarts/runner.py",
                        "--seed",
                        str(int(utc(row.expires_at).timestamp())),
                    ]
                    or h["NetworkMode"] != "none"
                    or not h["ReadonlyRootfs"]
                    or h["Privileged"]
                    or h.get("Binds")
                    or h.get("Devices")
                    or h.get("CapAdd")
                    or h["CapDrop"] != ["ALL"]
                    or h["SecurityOpt"] != ["no-new-privileges"]
                    or h["PidMode"]
                    or h["IpcMode"] != "none"
                    or h["UTSMode"]
                    or h["CgroupnsMode"] != "private"
                    or h.get("VolumesFrom")
                    or h["Memory"] != 128 * 1024 * 1024
                    or h["PidsLimit"] != 8
                    or h["NanoCpus"] != int(profile.cpus * 1e9)
                    or len(mounts) != 1
                    or mounts[0]["Type"] != "volume"
                    or mounts[0]["Name"] != d.volume(row)
                    or mounts[0]["Destination"] != "/workspace"
                    or not mounts[0]["RW"]
                ):
                    deny("cleanup_seed_configuration_conflict")
        elif role == "reader":
            info = d.reader_info(row)
        elif role == "job":
            info = d.job_info(row)
            if info:
                if row.job_id and info["Id"] != row.job_id:
                    deny("cleanup_qa_job_identity_changed")
                # Full inherited security verification needs the holder. Missing
                # holder is quarantined rather than guessing job configuration.
                d.verify_job(row, profile)
                if info["Config"]["Cmd"] != container_argv(row, profile)[1:]:
                    deny("cleanup_qa_command_changed")
        elif role == "input":
            info = d.volume_info(row)
        elif role == "output":
            info = d.output_info(row)
        else:
            deny("cleanup_unknown_resource_role")
        return info

    def inventory(self, s, run, ws):
        self.targets = {}
        c = self.controller
        # Health is checked even for an empty inventory: daemon unavailable is not absence.
        c.driver.call("info", "--format", "{{.OSType}}")
        specs = []
        sb = s.scalar(select(SandboxInstance).where(SandboxInstance.run_id == run.id))
        if sb:
            if (
                not ws
                or sb.workspace_id != ws.id
                or sb.owner_id != ws.owner_id
                or sb.lease_hash != token_hash(run.lease_token or "")
            ):
                deny("cleanup_sandbox_binding_mismatch", 403)
            profile = c._profile(sb)
            for role in ("holder", "seed", "input"):
                specs.append((c.driver, sb, profile, role))
            if hasattr(c.driver, "output_info"):
                specs.extend((c.driver, sb, profile, role) for role in ("reader", "output"))
        qrows = list(
            s.scalars(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.owner_id == run_owner(s, run),
                    IdempotencyRecord.key.like("qa-runner.v1:%"),
                )
            )
        )
        for qr in qrows:
            if qr.response.get("run_id") != run.id:
                continue
            j = QAJournal.model_validate_json(json.dumps(qr.response))
            if not self.qa:
                deny("cleanup_qa_backend_required")
            if (
                j.qa_id != qr.id
                or j.workspace_id != ws.id
                or j.sandbox_id != sb.id
                or j.worker_id != run.worker_id
                or j.lease_hash != token_hash(run.lease_token or "")
                or j.controller_id != self.qa.driver.controller_id
            ):
                deny("cleanup_qa_binding_mismatch", 403)
            profile = self.qa.registry.get(j.profile_name)
            if profile.fingerprint != j.profile_sha256:
                deny("cleanup_qa_profile_changed", 403)
            row = self.qa._resource(j)
            # QAJournal.container_id is the execution job, not the input holder.
            row.job_id, row.container_id = j.container_id, None
            specs.extend(
                (self.qa.driver, row, profile, role) for role in ("holder", "seed", "job", "input")
            )
        resources, observed, known_containers = [], [], set()
        for spec in specs:
            d, row, profile, role = spec
            key = "docker:" + d.controller_id + ":" + row.id + ":" + role
            try:
                info = self._observe(spec)
                if info is None:
                    continue
                kind = "volume" if role in {"input", "output"} else "container"
                identifier = info["Name"] if kind == "volume" else info["Id"]
                if kind == "container":
                    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
                        deny("cleanup_invalid_container_identity")
                    known_containers.add(identifier)
                fp = (
                    self._volume_fingerprint(info)
                    if kind == "volume"
                    else self._container_fingerprint(info)
                )
                item = Resource(
                    key=key,
                    kind=kind,
                    identifier=identifier,
                    fingerprint=fp,
                    disposition="CLEANUP_ELIGIBLE",
                    reason="identity_confirmed",
                )
                observed.append((item, spec, info))
                self.targets[key] = (spec, item)
            except DomainError as e:
                resources.append(
                    Resource(
                        key=key,
                        kind="unknown",
                        identifier=row.id,
                        fingerprint=digest(e.code),
                        disposition="QUARANTINE",
                        reason=e.code,
                    )
                )
        for item, spec, _info in observed:
            if (
                item.kind == "volume"
                and self._attachments(spec[0], item.identifier) - known_containers
            ):
                item = item.model_copy(
                    update={"disposition": "QUARANTINE", "reason": "foreign_volume_attachment"}
                )
            resources.append(item)
        # Bounded discovery identifies unknown resources but NEVER adopts them.
        known_ids = {x.identifier for x in resources if x.kind == "container"}
        known_volumes = {x.identifier for x in resources if x.kind == "volume"}
        valid_runs = set(s.scalars(select(PrototypeRun.id)))
        drivers = {c.driver.controller_id: c.driver}
        if self.qa:
            drivers[self.qa.driver.controller_id] = self.qa.driver
        for controller_id, d in drivers.items():
            for kind, fmt, known in (
                ("container", "{{.ID}}", known_ids),
                ("volume", "{{.Name}}", known_volumes),
            ):
                args = [kind, "ls"] + (["-a", "--no-trunc"] if kind == "container" else [])
                args += ["--filter", "label=hogwarts.controller=" + controller_id, "--format", fmt]
                ids = d.call(*args).stdout.decode().splitlines()
                if len(ids) > 1000:
                    deny("cleanup_inventory_limit")
                for identifier in ids:
                    if identifier in known:
                        continue
                    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,199}", identifier):
                        deny("cleanup_invalid_discovery_identity")
                    info = json.loads(d.call(kind, "inspect", identifier).stdout)[0]
                    labels = (
                        info.get("Labels") if kind == "volume" else info["Config"].get("Labels")
                    )
                    other_run = (labels or {}).get("hogwarts.run")
                    if other_run != run.id and other_run in valid_runs:
                        continue
                    resources.append(
                        Resource(
                            key="unknown-" + kind + ":" + identifier,
                            kind="unknown",
                            identifier=identifier,
                            fingerprint=digest(labels),
                            disposition="QUARANTINE",
                            reason="unknown_daemon_ownership",
                        )
                    )
        return resources

    def remove(self, resource):
        saved = self.targets.get(resource.key)
        if not saved or saved[1] != resource:
            deny("cleanup_target_not_bound")
        spec, _ = saved
        d = spec[0]
        info = self._observe(spec)
        if not info:
            deny("cleanup_target_disappeared")
        fp = (
            self._volume_fingerprint(info)
            if resource.kind == "volume"
            else self._container_fingerprint(info)
        )
        if fp != resource.fingerprint:
            deny("cleanup_target_replaced")
        if resource.kind == "container":
            if info["Id"] != resource.identifier:
                deny("cleanup_target_replaced")
            d.call("container", "rm", "-f", resource.identifier)
        elif resource.kind == "volume":
            if self._attachments(d, resource.identifier):
                deny("cleanup_volume_still_attached")
            d.call("volume", "rm", resource.identifier)
        else:
            deny("cleanup_invalid_delete_kind", 403)


def run_owner(s, run):
    from hogwarts.models import Proposal

    return s.get(Proposal, run.proposal_id).owner_id
