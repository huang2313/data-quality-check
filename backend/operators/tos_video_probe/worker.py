#!/usr/bin/env python3
"""Download one TOS video over the internal endpoint and upload its ffprobe report."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import tos
from tos.exceptions import TosClientError, TosServerError


MIB = 1024 * 1024
MAX_REPORT_BYTES = 512 * MIB
MIN_REPORT_SPACE = 8 * MIB
VIDEO_FORMATS = "mov,matroska,avi,mpeg,mpegts,flv,ogg,asf,mxf,dv,rm,3dostr,bink,roq,ivf,h264,hevc,vvc"


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class TosObject:
    uri: str
    bucket: str
    key: str


def parse_tos_object(value: str) -> TosObject:
    try:
        parts = urlsplit(value)
        key = unquote(parts.path.removeprefix("/"), errors="strict")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a valid tos://bucket/object URI") from exc
    if (
        parts.scheme != "tos"
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", parts.netloc)
        or not key
        or "?" in value
        or "#" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value + key)
    ):
        raise argparse.ArgumentTypeError("Expected a tos://bucket/object URI without query or fragment")
    return TosObject(value, parts.netloc, key)


def log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def make_clients() -> tuple[tos.TosClientV2, tos.TosClientV2, str]:
    region = os.environ.get("TOS_REGION", "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", region):
        raise ProbeError("TOS_REGION is required and must be a valid region name")
    host = f"tos-{region}.ivolces.com"
    endpoint = f"https://{host}"
    configured = os.environ.get("TOS_ENDPOINT", "").strip()
    if configured and configured not in (host, endpoint, endpoint + "/"):
        raise ProbeError(f"TOS_ENDPOINT must use the internal HTTPS endpoint {endpoint}")
    access_key = os.environ.get("TOS_ACCESS_KEY")
    secret_key = os.environ.get("TOS_SECRET_KEY")
    session_token = os.environ.get("TOS_SESSION_TOKEN")
    if not access_key or not secret_key:
        raise ProbeError("TOS_ACCESS_KEY and TOS_SECRET_KEY are required")
    output_access_key = os.environ.get("TOS_OUTPUT_ACCESS_KEY")
    output_secret_key = os.environ.get("TOS_OUTPUT_SECRET_KEY")
    output_token = os.environ.get("TOS_OUTPUT_SESSION_TOKEN")
    if any((output_access_key, output_secret_key, output_token)) and not (output_access_key and output_secret_key):
        raise ProbeError("TOS_OUTPUT_ACCESS_KEY and TOS_OUTPUT_SECRET_KEY must be supplied together")

    def create_client(ak: str, sk: str, token: str | None) -> tos.TosClientV2:
        client = tos.TosClientV2(
            ak=ak, sk=sk, security_token=token or None, region=region, endpoint=endpoint,
            follow_redirect_times=0, connection_time=10, socket_timeout=120,
        )
        client.session.trust_env = False
        return client

    source_client = create_client(access_key, secret_key, session_token)
    output_client = source_client
    if output_access_key and output_secret_key:
        output_client = create_client(output_access_key, output_secret_key, output_token)
    return source_client, output_client, endpoint


def download_video(client: tos.TosClientV2, source: TosObject, path: Path) -> dict:
    head = client.head_object(source.bucket, source.key)
    if head.status_code != 200 or not head.content_length or not head.etag:
        raise ProbeError("TOS source must have a positive object size and an ETag")
    if shutil.disk_usage(path.parent).free < head.content_length + MIN_REPORT_SPACE:
        raise ProbeError(f"Insufficient local disk for source object of {head.content_length} bytes")

    log("download_started", bytes_expected=head.content_length)
    response = client.get_object_to_file(source.bucket, source.key, str(path), if_match=head.etag)
    actual_size = path.stat().st_size
    if (
        response.status_code != 200
        or response.etag != head.etag
        or response.version_id != head.version_id
        or actual_size != head.content_length
    ):
        raise ProbeError(f"Full TOS download failed verification after writing {actual_size} bytes")

    log("downloaded", bytes_downloaded=actual_size, request_id=response.request_id)
    return {
        "uri": source.uri,
        "bytes": actual_size,
        "etag": head.etag,
        "version_id": head.version_id,
        "last_modified": head.last_modified.isoformat() if head.last_modified else None,
        "content_type": head.content_type,
        "download_request_id": response.request_id,
    }


def probe_video(executable: str, path: Path, detail: str, report: dict, output: Path) -> str:
    # ffprobe receives only the local file and does not inherit storage credentials.
    environment = {"PATH": os.defpath, "LC_ALL": "C.UTF-8"}
    help_output = subprocess.run(
        [executable, "-h"], capture_output=True, check=True, env=environment, timeout=10
    )
    command = [
        executable, "-hide_banner", "-v", "error",
        "-protocol_whitelist", "file", "-format_whitelist", VIDEO_FORMATS,
        "-print_format", "json=compact=1", "-show_program_version", "-show_format",
        "-show_streams", "-show_chapters", "-show_programs", "-show_error",
        "-count_frames", "-count_packets",
    ]
    if b"-show_stream_groups " in help_output.stdout + help_output.stderr:
        command.append("-show_stream_groups")
    if detail in ("frames", "all"):
        command.append("-show_frames")
    if detail in ("packets", "all"):
        command.append("-show_packets")
    command.extend(("-i", path.name))

    with output.open("wb") as destination, tempfile.TemporaryFile(dir=path.parent) as diagnostics:
        destination.write(json.dumps(report, ensure_ascii=False).encode("utf-8")[:-1])
        destination.write(b',"ffprobe":')
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=diagnostics, cwd=path.parent, env=environment
        ) as process:
            assert process.stdout is not None
            try:
                while block := process.stdout.read(MIB):
                    if destination.tell() + len(block) > MAX_REPORT_BYTES:
                        raise ProbeError("ffprobe report exceeds 512 MiB. Select a smaller detail mode")
                    destination.write(block)
                status_code = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()

        diagnostics.seek(0)
        errors = diagnostics.read(4097)
        warnings = errors[:4096].decode("utf-8", errors="replace").strip()
        if status_code != 0:
            raise ProbeError(f"ffprobe exited with code {status_code}: {warnings or 'no diagnostics'}")
        status = "completed_with_warnings" if errors else "completed"
        result = {"status": status, "diagnostics": warnings, "diagnostics_truncated": len(errors) > 4096}
        footer = b',"probe":' + json.dumps(result, ensure_ascii=False).encode("utf-8") + b"}\n"
        if destination.tell() + len(footer) > MAX_REPORT_BYTES:
            raise ProbeError("ffprobe report exceeds 512 MiB. Select a smaller detail mode")
        destination.write(footer)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=parse_tos_object, help="Source video TOS URI")
    parser.add_argument("--output", required=True, type=parse_tos_object, help="Output JSON TOS URI")
    parser.add_argument("--detail", choices=("summary", "frames", "packets", "all"), default="summary")
    args = parser.parse_args()
    if not args.output.key.lower().endswith(".json"):
        parser.error("--output must be a TOS object ending in .json")
    if (args.input.bucket, args.input.key) == (args.output.bucket, args.output.key):
        parser.error("--input and --output must be different TOS objects")

    stage = "setup"
    try:
        executable = shutil.which("ffprobe")
        if not executable:
            raise ProbeError("ffprobe is required in the worker image")
        source_client, output_client, endpoint = make_clients()
        log("worker_ready", tos_endpoint=endpoint, detail=args.detail)
        with tempfile.TemporaryDirectory(prefix="tos-video-probe-") as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "input.video"
            output_path = directory / "report.json"
            stage = "download"
            source = download_video(source_client, args.input, source_path)
            stage = "probe"
            report = {
                "schema_version": 1,
                "worker": {
                    "scheduler_batch_id": os.environ.get("SCHEDULER_BATCH_ID"),
                    "scheduler_task_id": os.environ.get("SCHEDULER_TASK_ID"),
                    "operator_git_commit": os.environ.get("OPERATOR_GIT_COMMIT"),
                },
                "source": source,
                "output_uri": args.output.uri,
                "tos_endpoint": endpoint,
                "detail": args.detail,
            }
            status = probe_video(str(Path(executable).resolve()), source_path, args.detail, report, output_path)
            report_size = output_path.stat().st_size
            log("probed", status=status, report_bytes=report_size)
            stage = "upload"
            response = output_client.put_object_from_file(
                args.output.bucket, args.output.key, str(output_path),
                content_type="application/json", forbid_overwrite=True,
            )
            if response.status_code != 200:
                raise ProbeError(f"Unexpected TOS upload status {response.status_code}")
            log("uploaded", output_uri=args.output.uri, report_bytes=report_size, request_id=response.request_id)
    except TosServerError as exc:
        log("failed", stage=stage, status_code=exc.status_code, code=exc.code, request_id=exc.request_id)
        return 1
    except TosClientError as exc:
        log(
            "failed", stage=stage, error="TOS client could not complete the internal endpoint request",
            cause_type=type(exc.cause).__name__ if exc.cause else None,
        )
        return 1
    except ProbeError as exc:
        log("failed", stage=stage, error=str(exc))
        return 1
    except OSError as exc:
        log("failed", stage=stage, error=exc.strerror or type(exc).__name__)
        return 1
    except subprocess.SubprocessError as exc:
        log("failed", stage=stage, error=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
