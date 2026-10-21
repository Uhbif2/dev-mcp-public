"""Operator-private, authenticated opref -> pixel resolver for visual lenses.

The *public* device.screens API returns only an opaque Operator proof. This
module is NOT an MCP tool: a trusted Operator runner records screenshot bytes
after a signed backend receipt, and the trusted LensRunner receives ONLY a
bound load_frames callback. The directory and HMAC key must be accessible only
to the Operator/visual-verifier identity (enforced by OS ACLs).

Neither fleet_op input nor public evidence.ref can select a filesystem path.
No adb client is spawned here. The reserved device driver must actually capture
frames and supply them through the private fetch_frames callback.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from dev_mcp.fleet.lenses.engine import Frame, LensError, _frame
from dev_mcp.operator_catalog import CATALOG_VERSION, validate_operation
from .runtime import AdapterRuntime, BackendReceipt, NamedBackend, ResourceSnapshot, _digest

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REF = re.compile(r"opref:([0-9a-f]{64})\Z")
_MAX_FRAMES = 10
_MAX_IMAGE = 4 * 1024 * 1024
_MAX_META = 8192


class ScreenStoreError(ValueError):
    """A rejected or unavailable proof; never includes private paths."""


def _json(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _request_fingerprint(params: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json({
        "version": CATALOG_VERSION, "operation": "device.screens", "params": dict(params),
    })).hexdigest()


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _make_frame(data: bytes, *, opref: str, scenario: str,
                subject: str, env: str) -> Frame:
    if type(data) is not bytes or not 0 < len(data) <= _MAX_IMAGE:
        raise ScreenStoreError("invalid_frame")
    mime = ("image/png" if data.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg" if data.startswith(b"\xff\xd8") else "")
    frame = Frame(data, hashlib.sha256(data).hexdigest(), mime, scenario,
                  opref, subject, env)
    try:
        # Reuse the lens's actual pixel/CRC decoder, not an extension guess.
        _frame(frame, {"ref": opref, "subject_version": subject,
                       "env_version": env}, scenario)
    except (LensError, ValueError, TypeError):
        raise ScreenStoreError("invalid_frame_pixels") from None
    return frame


class PrivateScreenStore:
    """Immutable signed capture bundles addressed by sanitized Operator opref.

    Root and signing key are supplied by the trusted Operator launcher, not
    from fleet_op params or environment variables. The root must already exist
    and be writable only by the Operator/verifier identity.
    """

    def __init__(self, root: str | Path, hmac_key: bytes):
        folder = Path(root)
        if (not folder.is_absolute() or folder.is_symlink()
                or not folder.is_dir() or type(hmac_key) is not bytes
                or len(hmac_key) < 32):
            raise ScreenStoreError("private_store_not_configured")
        # POSIX test/runner: no other identity may traverse or read pixels.
        # On Windows the trusted Operator launcher must apply a private DACL;
        # Python's stat bits do not represent Windows access control lists.
        if os.name != "nt" and folder.stat().st_mode & 0o077:
            raise ScreenStoreError("private_store_permissions")
        self.root = folder.resolve()
        self._key = hmac_key

    def _signed(self, manifest: Mapping[str, Any]) -> str:
        return hmac.new(self._key, _json(manifest), hashlib.sha256).hexdigest()

    def _location(self, opref: str) -> Path:
        match = _REF.fullmatch(opref) if isinstance(opref, str) else None
        if match is None:
            raise ScreenStoreError("invalid_opref")
        return self.root / match.group(1)

    def _read_manifest(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_dir():
            raise ScreenStoreError("capture_unavailable")
        try:
            meta = path / "manifest.json"
            if meta.is_symlink() or meta.stat().st_size > _MAX_META:
                raise ScreenStoreError("untrusted_manifest")
            signed = json.loads(meta.read_bytes())
            if type(signed) is not dict or set(signed) != {"payload", "hmac"}:
                raise ScreenStoreError("untrusted_manifest")
            payload = signed["payload"]
            if type(payload) is not dict or not isinstance(signed["hmac"], str):
                raise ScreenStoreError("untrusted_manifest")
            if not hmac.compare_digest(self._signed(payload), signed["hmac"]):
                raise ScreenStoreError("untrusted_manifest")
            return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise ScreenStoreError("untrusted_manifest") from None

    def _read_frames(self, folder: Path, payload: Mapping[str, Any]) -> tuple[Frame, ...]:
        sizes = payload.get("frames")
        if type(sizes) is not list or not 1 <= len(sizes) <= _MAX_FRAMES:
            raise ScreenStoreError("invalid_frame_index")
        frames: list[Frame] = []
        for index, digest in enumerate(sizes):
            if not _valid_sha(digest):
                raise ScreenStoreError("invalid_frame_index")
            image = folder / f"{index:02d}.img"
            try:
                if image.is_symlink() or not 0 < image.stat().st_size <= _MAX_IMAGE:
                    raise ScreenStoreError("invalid_frame")
                raw = image.read_bytes()
            except OSError:
                raise ScreenStoreError("capture_unavailable") from None
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ScreenStoreError("frame_digest_mismatch")
            frames.append(_make_frame(raw, opref=payload["opref"],
                                      scenario=payload["scenario"],
                                      subject=payload["subject_version"],
                                      env=payload["env_version"]))
        return tuple(frames)

    def record_capture(
        self, *, params: Mapping[str, Any], receipt: BackendReceipt,
        frames: Sequence[bytes], expected_subject_version: str,
        expected_env_version: str,
        verify_backend: Callable[[BackendReceipt], bool],
    ) -> str:
        """Privileged capture-side write, after independent signed attestation.

        BackendReceipt.request_sha is the runtime's operation+params digest;
        public fingerprint includes the catalog version. Both are stored/bound.
        """
        try:
            args = dict(params)
            validate_operation("device.screens", args)
            count = args["count"]
            if (not 1 <= count <= _MAX_FRAMES or not args.get("execution_id")
                    or not _valid_sha(expected_subject_version)
                    or not _valid_sha(expected_env_version)
                    or not isinstance(receipt, BackendReceipt)
                    or receipt.operation != "device.screens"
                    or receipt.request_sha != _digest({"operation": "device.screens",
                                                      "params": args})
                    or receipt.subject_version != expected_subject_version
                    or receipt.env_version != expected_env_version
                    or receipt.outcome != "pass"
                    or receipt.fresh is not True
                    or receipt.metrics.get("screens") != count
                    or not callable(verify_backend)
                    or verify_backend(receipt) is not True):
                raise ScreenStoreError("capture_attestation_mismatch")
            opref = "opref:" + hashlib.sha256(receipt.ref.encode("utf-8")).hexdigest()
            items = tuple(frames)
            if len(items) != count:
                raise ScreenStoreError("capture_count_mismatch")
            validated = tuple(_make_frame(data, opref=opref,
                                          scenario=args["scenario"],
                                          subject=expected_subject_version,
                                          env=expected_env_version)
                              for data in items)
            payload = {
                "opref": opref, "request_fingerprint": _request_fingerprint(args),
                "scenario": args["scenario"], "subject_version": expected_subject_version,
                "env_version": expected_env_version,
                "frames": [frame.sha256 for frame in validated],
                # Only digest of signed receipt; never backend raw ref/path.
                "receipt_sha256": hashlib.sha256(_json(
                    {k: v for k, v in vars(receipt).items()
                     if isinstance(v, (str, bool, dict))}
                )).hexdigest(),
            }
            destination = self._location(opref)
            if destination.exists() or destination.is_symlink():
                existing = self._read_manifest(destination)
                if existing != payload or len(self._read_frames(destination, existing)) != count:
                    raise ScreenStoreError("opref_collision")
                return opref  # immutable idempotent replay
            staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=self.root))
            try:
                for index, frame in enumerate(validated):
                    with (staging / f"{index:02d}.img").open("xb") as out:
                        out.write(frame.data)
                        out.flush()
                        os.fsync(out.fileno())
                with (staging / "manifest.json").open("xb") as out:
                    out.write(_json({"payload": payload, "hmac": self._signed(payload)}))
                    out.flush()
                    os.fsync(out.fileno())
                os.rename(staging, destination)  # publish complete bundle atomically
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return opref
        except ScreenStoreError:
            raise
        except Exception:
            # No driver error, path, screenshot content, credentials in response.
            raise ScreenStoreError("capture_unavailable") from None

    def load_frames(self, proof: Mapping[str, Any], scenario: str) -> tuple[Frame, ...]:
        """Privileged LensRunner callback; NEVER register as a public MCP tool."""
        try:
            if (type(proof) is not dict or proof.get("kind") != "screen"
                    or proof.get("producer") != "operator"
                    or proof.get("operation") != "device.screens"
                    or proof.get("verdict") != "pass"
                    or not _valid_sha(proof.get("request_fingerprint"))
                    or not _valid_sha(proof.get("subject_version"))
                    or not _valid_sha(proof.get("env_version"))
                    or not isinstance(scenario, str)):
                raise ScreenStoreError("invalid_operator_proof")
            folder = self._location(proof.get("ref"))
            payload = self._read_manifest(folder)
            if any(payload.get(key) != proof.get(key) for key in (
                "opref", "subject_version", "env_version", "request_fingerprint"
            ) if key != "opref") or payload.get("opref") != proof.get("ref"):
                raise ScreenStoreError("capture_proof_mismatch")
            if payload.get("scenario") != scenario:
                raise ScreenStoreError("capture_scenario_mismatch")
            return self._read_frames(folder, payload)
        except ScreenStoreError:
            raise
        except Exception:
            raise ScreenStoreError("capture_unavailable") from None


class RecordingScreenBackend:
    """Operator-only bridge to an actual reserved capture driver.

    Delegate MUST return signed BackendReceipt. fetch_frames MUST retrieve real
    captured bytes via a private channel; never synthesize from metadata or
    expose its channel as an MCP endpoint. Trusted versions are resolved from
    server state, not requested by the fleet caller.
    """

    def __init__(
        self, delegate: NamedBackend, store: PrivateScreenStore,
        *, fetch_frames: Callable[[BackendReceipt, Mapping[str, Any]], Sequence[bytes]],
        trusted_versions: Callable[[Mapping[str, Any]], tuple[str, str]],
        verify_backend: Callable[[BackendReceipt], bool],
    ) -> None:
        self.delegate, self.store = delegate, store
        self.fetch_frames = fetch_frames
        self.trusted_versions = trusted_versions
        self.verify_backend = verify_backend

    def probe(self, action: str) -> ResourceSnapshot:
        if action != "device.screens":
            raise ScreenStoreError("wrong_operation")
        return self.delegate.probe(action)

    def run(self, action: str, params: Mapping[str, Any],
            browser_profile: Path | None) -> BackendReceipt:
        if action != "device.screens" or browser_profile is not None:
            raise ScreenStoreError("wrong_operation")
        receipt = self.delegate.run(action, params, None)
        if receipt.outcome == "pass":
            if (self.verify_backend(receipt) is not True or receipt.fresh is not True
                    or receipt.operation != "device.screens"
                    or receipt.request_sha != _digest(
                        {"operation": "device.screens", "params": dict(params)})):
                raise ScreenStoreError("capture_attestation_mismatch")
            subject, env = self.trusted_versions(params)
            if (receipt.subject_version != subject or receipt.env_version != env):
                raise ScreenStoreError("capture_version_mismatch")
            self.store.record_capture(
                params=params, receipt=receipt,
                frames=self.fetch_frames(receipt, params),
                expected_subject_version=subject, expected_env_version=env,
                verify_backend=self.verify_backend,
            )
        return receipt


class PinnedCaptureDriver:
    """Independently authenticated private capture transport.

    The trusted capture service returns a separately HMAC-signed bundle bound
    to the signed backend receipt, not arbitrary bytes supplied by fleet_op.
    Versions are read from Operator-owned files at EACH capture, allowing a
    changed deployment/device state to invalidate stale receipt versions.
    """

    def __init__(
        self, gateway: Any, capture_key: bytes,
        subject_version_file: str | Path, env_version_file: str | Path,
    ) -> None:
        if (type(capture_key) is not bytes or len(capture_key) < 32
                or not callable(getattr(gateway, "request", None))):
            raise ScreenStoreError("trusted_capture_driver_required")
        self.gateway, self.capture_key = gateway, capture_key
        self.subject_file = Path(subject_version_file)
        self.env_file = Path(env_version_file)
        self.trusted_versions({})

    @staticmethod
    def _version(path: Path) -> str:
        try:
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ScreenStoreError("trusted_version_unavailable")
            if not 0 < path.stat().st_size <= 128:
                raise ScreenStoreError("trusted_version_unavailable")
            value = path.read_text(encoding="ascii").strip()
            if not _valid_sha(value):
                raise ScreenStoreError("trusted_version_unavailable")
            return value
        except (OSError, UnicodeError):
            raise ScreenStoreError("trusted_version_unavailable") from None

    def trusted_versions(self, params: Mapping[str, Any]) -> tuple[str, str]:
        return self._version(self.subject_file), self._version(self.env_file)

    def fetch_frames(
        self, receipt: BackendReceipt, params: Mapping[str, Any],
    ) -> tuple[bytes, ...]:
        try:
            # Fixed named action. Ref is a backend-signed opaque identifier, not
            # a caller-chosen URL, filesystem path or shell command.
            response = self.gateway.request("capture", "device.screens", {
                "receipt_ref": receipt.ref, "receipt_request_sha": receipt.request_sha,
                "execution_id": params["execution_id"],
            })
            if type(response) is not dict or set(response) != {"payload", "hmac"}:
                raise ScreenStoreError("untrusted_capture")
            payload = response["payload"]
            signature = response["hmac"]
            if (type(payload) is not dict or set(payload) != {
                    "receipt_ref", "receipt_request_sha", "subject_version",
                    "env_version", "frames"} or type(signature) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", signature)):
                raise ScreenStoreError("untrusted_capture")
            expected = hmac.new(self.capture_key, _json(payload), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ScreenStoreError("untrusted_capture")
            current_subject, current_env = self.trusted_versions(params)
            if (payload["receipt_ref"] != receipt.ref
                    or payload["receipt_request_sha"] != receipt.request_sha
                    or payload["subject_version"] != current_subject
                    or payload["env_version"] != current_env
                    or receipt.subject_version != current_subject
                    or receipt.env_version != current_env):
                raise ScreenStoreError("capture_version_mismatch")
            encoded = payload["frames"]
            if type(encoded) is not list or len(encoded) != params["count"]:
                raise ScreenStoreError("capture_count_mismatch")
            frames = []
            for item in encoded:
                if (type(item) is not str or len(item) > (_MAX_IMAGE * 4 // 3 + 8)):
                    raise ScreenStoreError("invalid_frame")
                data = base64.b64decode(item, validate=True)
                if not 0 < len(data) <= _MAX_IMAGE:
                    raise ScreenStoreError("invalid_frame")
                frames.append(data)
            return tuple(frames)
        except ScreenStoreError:
            raise
        except (ValueError, KeyError, TypeError, OSError, binascii.Error):
            raise ScreenStoreError("untrusted_capture") from None


class UnavailableScreenBackend:
    """Fail closed when trusted capture configuration is missing or incomplete."""

    def probe(self, action: str) -> ResourceSnapshot:
        raise ScreenStoreError("capture_backend_unavailable")

    def run(self, action: str, params: Mapping[str, Any],
            browser_profile: Path | None) -> BackendReceipt:
        raise ScreenStoreError("capture_backend_unavailable")


def install_capture_bridge(
    runtime: AdapterRuntime, store: PrivateScreenStore,
    *, fetch_frames: Callable[[BackendReceipt, Mapping[str, Any]], Sequence[bytes]],
    trusted_versions: Callable[[Mapping[str, Any]], tuple[str, str]],
) -> Callable[[dict[str, Any], str], tuple[Frame, ...]]:
    """Operator-launcher opt-in, before make_adapters(runtime).

    The privileged device driver must provide the two callbacks; the public
    gateway only exposes signed metadata and cannot act as a pixel resolver.
    This function deliberately does not register a public read tool or launch
    ADB. The returned loader may be injected only into the trusted LensRunner
    process with private-root access, not serialized into fleet messages.
    """
    current = runtime.backends.get("device.screens")
    if current is None or isinstance(current, RecordingScreenBackend):
        raise ScreenStoreError("capture_backend_unavailable")
    if not callable(fetch_frames) or not callable(trusted_versions):
        raise ScreenStoreError("trusted_capture_driver_required")
    runtime.backends["device.screens"] = RecordingScreenBackend(
        current, store, fetch_frames=fetch_frames,
        trusted_versions=trusted_versions, verify_backend=runtime.verify_remote,
    )
    return store.load_frames
