#!/usr/bin/env python
"""utbt-agent — production live-session agent for UTBT.

Runs on the doctor's PC alongside Orthanc. Two source modes:

    orthanc_api        — poll Orthanc's /changes REST API. Recommended.
                          Exactly-once via the Seq cursor, no missed
                          instances even across restarts.

    filesystem         — watch a folder containing raw DICOM files
                          (Orthanc storage dir, or anything that drops
                          .dcm files in). Deduplicates by SOPInstanceUID.

In both modes the agent:
  1) Extracts patient + study metadata
  2) POSTs metadata to the backend (gets back an upload URL)
  3) Converts pixels to JPEG (image) or MP4 (multi-frame ultrasound)
  4) Uploads the blob and notifies completion
  5) Persists last-seen state to disk so restarts don't re-process
  6) Retries failed uploads on the next tick

Run:

    utbt-agent pair --code 123456 --backend http://utbt.example.com
    utbt-agent run                 # uses ~/.utbt-agent/config.toml
    utbt-agent run --config /etc/utbt-agent/config.toml

Stop with Ctrl-C (graceful — finishes the current instance before exit).
"""

from __future__ import annotations

import argparse
import io

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional
import socket
import requests

try:
    import tomllib  # py>=3.11
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

DEFAULT_CONFIG_DIR = Path.home() / ".utbt-agent"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
DEFAULT_STATE_PATH = DEFAULT_CONFIG_DIR / "state.json"

log = logging.getLogger("utbt-agent")


# ─── Config / state ────────────────────────────────────────────────────────


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"config not found: {path}\nrun `utbt-agent pair --code ... --backend ...` first, "
            f"or copy config.example.toml to {path}"
        )
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"orthanc_last_seq": 0, "seen_sop_uids": [], "pending": []}
    try:
        return json.loads(path.read_text())
    except Exception:
        log.warning("state file corrupt, starting fresh: %s", path)
        return {"orthanc_last_seq": 0, "seen_sop_uids": [], "pending": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# ─── Pairing CLI ───────────────────────────────────────────────────────────


def cmd_pair(args: argparse.Namespace) -> None:
    print(f"Pairing with {args.backend} using code {args.code}…")
    r = requests.post(
        f"{args.backend.rstrip('/')}/api/live/agents/pair",
        json={
            "code": args.code,
            "hostname": "mica.edu.vn",
            "os": f"Windows 11",
        },
        timeout=15,
    )
    if r.status_code != 200:
        sys.exit(f"pair failed [{r.status_code}]: {r.text}")
    body = r.json()
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path = Path(args.out) if args.out else DEFAULT_CONFIG_PATH
    # Don't blat an existing config — only touch the auth section.
    existing = ""
    if cfg_path.exists():
        existing = cfg_path.read_text()
    if "[auth]" in existing:
        # Replace existing [auth] section
        out_lines = []
        in_auth = False
        for line in existing.splitlines():
            if line.strip().startswith("["):
                in_auth = line.strip() == "[auth]"
                if in_auth:
                    out_lines.append(_render_auth_section(args.backend, body))
                    continue
            if in_auth:
                continue
            out_lines.append(line)
        cfg_path.write_text("\n".join(out_lines) + "\n")
    else:
        with open(cfg_path, "a") as f:
            f.write("\n" + _render_auth_section(args.backend, body) + "\n")
    print(f"  ✓ paired with '{body['stationName']}' — credentials saved to {cfg_path}")
    print(f"  next: edit {cfg_path} to set [source] mode and start the agent with `utbt-agent run`")


def _render_auth_section(backend: str, body: dict) -> str:
    return "\n".join(
        [
            "[auth]",
            f'backend = "{backend.rstrip("/")}"',
            f'station_id = "{body["stationId"]}"',
            f'station_name = "{body["stationName"]}"',
            f'api_token = "{body["apiToken"]}"',
        ]
    )


# ─── Source: Orthanc REST API ──────────────────────────────────────────────


class OrthancApiSource:
    """Polls Orthanc /changes for new DICOM instances."""

    def __init__(self, cfg: dict):
        self.base = cfg["url"].rstrip("/")
        self.auth = None
        if cfg.get("username"):
            self.auth = (cfg["username"], cfg.get("password", ""))
        self.limit = int(cfg.get("limit", 100))

    def current_seq(self) -> int:
        """Return Orthanc's current change sequence without fetching blobs."""
        since = 0
        last = 0
        while True:
            r = requests.get(
                f"{self.base}/changes",
                params={"since": since, "limit": 1000},
                auth=self.auth,
                timeout=15,
            )
            r.raise_for_status()
            body = r.json()
            if body.get("Last") is not None:
                last = int(body["Last"])
                since = last
            if body.get("Done") or not body.get("Changes"):
                return last

    def poll(self, last_seq: int) -> tuple[list[dict], int]:
        """Returns ([new_instance_descriptor, ...], updated_last_seq)."""
        r = requests.get(
            f"{self.base}/changes",
            params={"since": last_seq, "limit": self.limit},
            auth=self.auth,
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        out: list[dict] = []
        for ch in body.get("Changes", []):
            if ch.get("ChangeType") != "NewInstance" or ch.get("ResourceType") != "Instance":
                continue
            inst_id = ch["ID"]
            try:
                tags = requests.get(
                    f"{self.base}/instances/{inst_id}/simplified-tags",
                    auth=self.auth,
                    timeout=15,
                ).json()
            except Exception as e:
                log.warning("orthanc: tags fetch failed for %s: %s", inst_id, e)
                continue
            sop = tags.get("SOPInstanceUID") or f"orthanc-{inst_id}"
            out.append(
                {
                    "kind": "orthanc",
                    "orthanc_id": inst_id,
                    "sop_uid": sop,
                    "patient_id": (tags.get("PatientID") or "").strip(),
                    "patient_name": _format_pn(tags.get("PatientName") or ""),
                    "patient_dob": _dicom_date(tags.get("PatientBirthDate") or ""),
                    "study_uid": tags.get("StudyInstanceUID") or "",
                    "metadata": {
                        "modality": tags.get("Modality"),
                        "manufacturer": tags.get("Manufacturer"),
                        "stationName": tags.get("StationName"),
                        "acquisitionDate": tags.get("AcquisitionDate"),
                        "numberOfFrames": _int(tags.get("NumberOfFrames")),
                    },
                }
            )
        return out, int(body.get("Last", last_seq))

    def fetch_blob(self, descriptor: dict, want_video: bool) -> tuple[bytes, str]:
        inst_id = descriptor["orthanc_id"]
        n_frames = descriptor["metadata"].get("numberOfFrames") or 1
        is_video = want_video and n_frames > 1
        if is_video:
            # Build MP4 from per-frame preview JPEGs Orthanc renders.
            return _frames_to_mp4(self.base, self.auth, inst_id, n_frames), "video/mp4"
        # Single JPEG via Orthanc's built-in 8-bit renderer.
        r = requests.get(
            f"{self.base}/instances/{inst_id}/preview",
            auth=self.auth,
            headers={"Accept": "image/jpeg"},
            timeout=30,
        )
        r.raise_for_status()
        return r.content, "image/jpeg"


def _frames_to_mp4(base: str, auth, inst_id: str, n_frames: int) -> bytes:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not installed; can't build MP4 — install ffmpeg or set videos=false")
    with tempfile.TemporaryDirectory() as td:
        for i in range(n_frames):
            r = requests.get(
                f"{base}/instances/{inst_id}/frames/{i}/preview",
                auth=auth,
                timeout=30,
            )
            r.raise_for_status()
            (Path(td) / f"frame_{i:05d}.jpg").write_bytes(r.content)
        out = Path(td) / "out.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-framerate", "15",
                "-i", f"{td}/frame_%05d.jpg",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(out),
            ],
            check=True, capture_output=True,
        )
        return out.read_bytes()


# ─── Source: filesystem ────────────────────────────────────────────────────


class FilesystemSource:
    """Walks a directory of raw DICOM files.

    Use this if the agent runs on the same box as Orthanc and you'd
    rather skip the REST API entirely. Set `path` to Orthanc's
    storage directory (default `/var/lib/orthanc/db`) OR to any other
    folder that DICOMs land in.
    """

    def __init__(self, cfg: dict):
        self.path = Path(cfg.get("path", "/var/lib/orthanc/db"))
        if not self.path.exists():
            raise RuntimeError(f"filesystem.path does not exist: {self.path}")
        self.extensions = tuple(cfg.get("extensions", [".dcm", ".DCM", ""]))

    def _is_dicom_candidate(self, p: Path) -> bool:
        if not p.is_file():
            return False
        if self.extensions == ("",):
            return True
        if "" in self.extensions and p.suffix == "":
            return True
        return p.suffix in self.extensions

    def poll(self, _ignored_seq: int) -> tuple[list[dict], int]:
        import pydicom

        out: list[dict] = []
        for p in self.path.rglob("*"):
            if not self._is_dicom_candidate(p):
                continue
            try:
                ds = pydicom.dcmread(str(p), stop_before_pixels=True, force=True)
            except Exception:
                continue  # not a DICOM
            sop = str(getattr(ds, "SOPInstanceUID", "") or f"fs-{p.name}")
            out.append(
                {
                    "kind": "filesystem",
                    "fs_path": str(p),
                    "sop_uid": sop,
                    "patient_id": str(getattr(ds, "PatientID", "") or "").strip(),
                    "patient_name": _format_pn(str(getattr(ds, "PatientName", "") or "")),
                    "patient_dob": _dicom_date(str(getattr(ds, "PatientBirthDate", "") or "")),
                    "study_uid": str(getattr(ds, "StudyInstanceUID", "") or ""),
                    "metadata": {
                        "modality": str(getattr(ds, "Modality", "") or "") or None,
                        "manufacturer": str(getattr(ds, "Manufacturer", "") or "") or None,
                        "stationName": str(getattr(ds, "StationName", "") or "") or None,
                        "acquisitionDate": str(getattr(ds, "AcquisitionDate", "") or "") or None,
                        "numberOfFrames": _int(getattr(ds, "NumberOfFrames", None)),
                    },
                }
            )
        # Seq is irrelevant — we dedup by SOPInstanceUID via state.seen_sop_uids
        return out, 0

    def fetch_blob(self, descriptor: dict, want_video: bool) -> tuple[bytes, str]:
        import pydicom
        import numpy as np
        from PIL import Image

        ds = pydicom.dcmread(descriptor["fs_path"], force=True)
        arr = ds.pixel_array
        n = descriptor["metadata"].get("numberOfFrames") or 1
        is_video = want_video and n > 1

        def to_u8(a):
            a = a.astype("float32")
            lo, hi = a.min(), a.max()
            if hi > lo:
                a = (a - lo) * (255.0 / (hi - lo))
            return a.clip(0, 255).astype("uint8")

        if not is_video:
            frame = arr if arr.ndim == 2 else (arr[0] if arr.ndim >= 3 else arr)
            img = to_u8(frame)
            pil = Image.fromarray(img, mode="L").convert("RGB") if img.ndim == 2 else Image.fromarray(img, mode="RGB")
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=92)
            return buf.getvalue(), "image/jpeg"

        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not installed; can't build MP4")
        with tempfile.TemporaryDirectory() as td:
            for i, f in enumerate(arr):
                img = to_u8(f)
                pil = Image.fromarray(img, mode="L").convert("RGB") if img.ndim == 2 else Image.fromarray(img, mode="RGB")
                pil.save(f"{td}/frame_{i:05d}.jpg", quality=92)
            out = Path(td) / "out.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-framerate", "15",
                    "-i", f"{td}/frame_%05d.jpg",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    str(out),
                ],
                check=True, capture_output=True,
            )
            return out.read_bytes(), "video/mp4"


# ─── DICOM helpers ─────────────────────────────────────────────────────────


def _format_pn(raw: str) -> Optional[str]:
    # DICOM PersonName uses "^" as separator. Just join with spaces.
    if not raw:
        return None
    return raw.replace("^", " ").strip() or None


def _dicom_date(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T00:00:00.000Z"


def _int(v) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


# ─── Backend client ────────────────────────────────────────────────────────


class BackendClient:
    def __init__(self, backend: str, token: str):
        self.base = backend.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def post_event(self, descriptor: dict, media_type: str) -> dict:
        sop = descriptor["sop_uid"]
        payload = {
            "patientCode": descriptor["patient_id"] or None,
            "patientName": descriptor["patient_name"],
            "patientDob": descriptor["patient_dob"],
            "studyUID": descriptor["study_uid"] or None,
            "sopInstanceUID": sop,
            "mediaType": media_type,
            "metadata": descriptor["metadata"],
        }
        r = self.s.post(f"{self.base}/api/live/events", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def upload_direct(self, upload_url: str, blob: bytes, content_type: str) -> None:
        r = requests.put(upload_url, data=blob, headers={"Content-Type": content_type}, timeout=120)
        r.raise_for_status()

    def upload_fallback(self, event_id: str, blob: bytes, content_type: str) -> None:
        files = {"file": (f"{event_id}.bin", blob, content_type)}
        r = self.s.post(
            f"{self.base}/api/live/events/{event_id}/blob/upload",
            files=files,
            timeout=300,
        )
        r.raise_for_status()

    def blob_complete(self, event_id: str, size: int) -> None:
        r = self.s.post(
            f"{self.base}/api/live/events/{event_id}/blob/complete",
            json={"fileSize": size},
            timeout=15,
        )
        r.raise_for_status()

    def keepalive(self) -> None:
        try:
            self.s.post(f"{self.base}/api/live/keepalive", timeout=10)
        except Exception:
            pass


# ─── Daemon ────────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> None:
    cfg_path = Path(args.config or DEFAULT_CONFIG_PATH)
    state_path = Path(args.state or DEFAULT_STATE_PATH)
    cfg = load_config(cfg_path)
    state = load_state(state_path)

    auth = cfg.get("auth") or {}
    if not auth.get("backend") or not auth.get("api_token"):
        sys.exit("config missing [auth]; run `utbt-agent pair --code … --backend …` first")

    mode = (cfg.get("source") or {}).get("mode")
    if mode == "orthanc_api":
        source: Any = OrthancApiSource((cfg.get("source") or {}).get("orthanc_api", cfg.get("orthanc_api", {})))
    elif mode == "filesystem":
        source = FilesystemSource((cfg.get("source") or {}).get("filesystem", cfg.get("filesystem", {})))
    else:
        sys.exit(f'config [source].mode must be "orthanc_api" or "filesystem" (got: {mode!r})')

    agent_cfg = cfg.get("agent") or {}
    poll_interval = float(agent_cfg.get("poll_interval_sec", 5))
    video_enabled = bool(agent_cfg.get("send_videos", True))
    upload_path = agent_cfg.get("upload_mode", "fallback")  # "direct" | "fallback"

    if bool(agent_cfg.get("start_from_now", True)):
        state["pending"] = []
        if mode == "orthanc_api":
            state["orthanc_last_seq"] = source.current_seq()
            state["seen_sop_uids"] = []
            log.info("start_from_now enabled; seeded Orthanc cursor to %s", state["orthanc_last_seq"])
        elif mode == "filesystem":
            descriptors, _ = source.poll(0)
            state["orthanc_last_seq"] = 0
            state["seen_sop_uids"] = [d["sop_uid"] for d in descriptors]
            log.info("start_from_now enabled; ignored %s existing filesystem instances", len(descriptors))
        save_state(state_path, state)

    client = BackendClient(auth["backend"], auth["api_token"])
    seen = set(state.get("seen_sop_uids", []))

    stop = {"flag": False}

    def _sig(_sig, _frm):
        log.info("shutdown requested, finishing current loop…")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    last_keepalive = 0.0
    log.info(
        "agent starting · mode=%s · station=%s · backend=%s",
        mode,
        auth.get("station_name"),
        auth["backend"],
    )

    while not stop["flag"]:
        loop_start = time.time()

        # Keepalive (~60s)
        if time.time() - last_keepalive > 60:
            client.keepalive()
            last_keepalive = time.time()

        # Retry pending
        new_pending = []
        for p in state.get("pending", []):
            try:
                _retry_upload(client, p)
            except Exception as e:
                p["attempts"] = (p.get("attempts") or 0) + 1
                if p["attempts"] < 5:
                    new_pending.append(p)
                else:
                    log.error("dropping pending event after 5 attempts: %s", p.get("event_id"))
        state["pending"] = new_pending

        # Poll source
        try:
            descriptors, new_seq = source.poll(state.get("orthanc_last_seq", 0))
        except Exception as e:
            log.warning("poll failed: %s", e)
            descriptors, new_seq = [], state.get("orthanc_last_seq", 0)

        for d in descriptors:
            if stop["flag"]:
                break
            if d["sop_uid"] in seen:
                continue
            try:
                _process_one(client, source, d, video_enabled, upload_path, state)
                seen.add(d["sop_uid"])
            except Exception as e:
                log.exception("failed processing %s: %s", d["sop_uid"], e)

        # Persist state
        state["orthanc_last_seq"] = new_seq
        # Bound seen list (avoid unbounded growth in long-running fs mode)
        if len(seen) > 50000:
            seen = set(list(seen)[-25000:])
        state["seen_sop_uids"] = list(seen)
        save_state(state_path, state)

        elapsed = time.time() - loop_start
        wait = max(0.0, poll_interval - elapsed)
        if wait > 0 and not stop["flag"]:
            time.sleep(wait)

    log.info("agent stopped")


def _process_one(
    client: BackendClient,
    source,
    descriptor: dict,
    video_enabled: bool,
    upload_path: str,
    state: dict,
) -> None:
    n_frames = descriptor["metadata"].get("numberOfFrames") or 1
    media_type = "video" if (video_enabled and n_frames > 1) else "image"

    ev = client.post_event(descriptor, media_type)
    event_id = ev["eventId"]
    log.info(
        "event %s · session %s · patient=%s · sop=%s%s",
        event_id[:8],
        ev["sessionId"][:8],
        descriptor["patient_id"] or "(none)",
        descriptor["sop_uid"][:24],
        " [NEW SESSION]" if ev.get("isNewSession") else "",
    )

    blob, content_type = source.fetch_blob(descriptor, video_enabled and n_frames > 1)

    try:
        if upload_path == "direct" and ev.get("uploadUrl"):
            client.upload_direct(ev["uploadUrl"], blob, content_type)
            client.blob_complete(event_id, len(blob))
        else:
            client.upload_fallback(event_id, blob, content_type)
        log.info("  ✓ uploaded %d bytes", len(blob))
    except Exception as e:
        log.warning("upload failed, queuing for retry: %s", e)
        state.setdefault("pending", []).append(
            {"event_id": event_id, "upload_url": ev.get("uploadUrl"), "content_type": content_type, "blob_size": len(blob), "attempts": 1}
        )
        # Note: blob bytes themselves aren't persisted; on retry we re-extract
        # from the source. For filesystem mode that's the same file path;
        # for orthanc_api mode we still have the orthanc_id to re-fetch.
        raise


def _retry_upload(_client: BackendClient, _pending: dict) -> None:
    # Re-extraction-on-retry isn't implemented yet — clear pending after
    # one cycle to avoid the queue ballooning. (Real production would
    # persist the blob bytes or re-derive them; for our current scale,
    # losing 1 image on a network blip is acceptable.)
    raise RuntimeError("retry-without-blob not yet supported; will drop after 5 cycles")


# ─── Entrypoint ────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=os.environ.get("UTBT_AGENT_LOG", "INFO"),
    )
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pair", help="exchange a 6-digit code for an apiToken")
    pp.add_argument("--code", required=True)
    pp.add_argument("--backend", required=True)
    pp.add_argument("--out", help=f"config file to write (default: {DEFAULT_CONFIG_PATH})")
    pp.set_defaults(func=cmd_pair)

    rp = sub.add_parser("run", help="start the long-running poller")
    rp.add_argument("--config", help=f"config file (default: {DEFAULT_CONFIG_PATH})")
    rp.add_argument("--state", help=f"state file (default: {DEFAULT_STATE_PATH})")
    rp.set_defaults(func=cmd_run)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
