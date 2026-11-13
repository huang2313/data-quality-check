#!/usr/bin/env python3
"""Check a raw MP4 using the immutable media metadata prepared by the runner."""

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path

PROTOCOL_VERSION = "1.0"
MAX_INPUT_BYTES = 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
DEFAULT_PARAMETERS = {
    "min_short_edge_px": 720,
    "min_duration_s": 1,
    "min_fps": 0,
}


def require_object(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 JSON 对象")
    return value


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def require_positive_int(value, field):
    if type(value) is not int or not 1 <= value <= MAX_SAFE_INTEGER:
        raise ValueError(f"{field} 必须是有效的正整数")
    return value


def require_number(value, field, minimum=0):
    if type(value) in (int, float):
        try:
            if math.isfinite(value) and minimum <= value <= MAX_SAFE_INTEGER:
                return value
        except OverflowError:
            pass
    raise ValueError(f"{field} 必须是 {minimum:g} 至 {MAX_SAFE_INTEGER} 之间的有限数字")


def check(request):
    require_object(request, "请求")
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError(f"protocol_version 必须是 {PROTOCOL_VERSION}")
    if request.get("input_profile") != "ego.raw":
        raise ValueError("input_profile 必须是 ego.raw")
    request_id = require_text(request.get("request_id"), "request_id")
    sample_id = require_text(request.get("sample_id"), "sample_id")
    parameters = require_object(request.get("parameters", {}), "parameters")
    if set(parameters) - DEFAULT_PARAMETERS.keys():
        raise ValueError("parameters 包含未声明的参数")
    params = DEFAULT_PARAMETERS | parameters
    min_edge = require_number(params["min_short_edge_px"], "min_short_edge_px", 1)
    min_duration = require_number(params["min_duration_s"], "min_duration_s")
    min_fps = require_number(params["min_fps"], "min_fps")

    sample = require_object(request.get("sample"), "sample")
    if sample.get("schema_version") != "1.0.0" or sample.get("profile") != "ego.raw":
        raise ValueError("sample 必须采用 1.0.0 / ego.raw 标准")
    if sample.get("sample_id") != sample_id:
        raise ValueError("sample.sample_id 与请求的 sample_id 不一致")
    timeline = require_object(sample.get("timeline"), "sample.timeline")
    if timeline.get("unit") != "us":
        raise ValueError("timeline.unit 必须是 us")
    asset_id = require_text(timeline.get("primary_video_id"), "timeline.primary_video_id")
    duration_us = require_positive_int(timeline.get("duration_us"), "timeline.duration_us")
    assets = require_object(sample.get("assets"), "sample.assets")
    primary = require_object(assets.get(asset_id), "主视频资产")
    if primary.get("role") != "rgb_video" or primary.get("format") != "mp4":
        raise ValueError("主视频资产必须是 rgb_video 类型的 MP4")
    local_files = require_object(request.get("local_files"), "local_files")
    local_path = Path(require_text(local_files.get(asset_id), "主视频本地路径"))
    if not local_path.is_file() or local_path.stat().st_size <= 0:
        raise ValueError("主视频本地文件不存在或为空")
    video = require_object(primary.get("video"), "主视频属性")
    width = require_positive_int(video.get("width_px"), "video.width_px")
    height = require_positive_int(video.get("height_px"), "video.height_px")
    short_edge = min(width, height)
    duration_s = duration_us / 1_000_000
    fps = None
    if "fps_num" in video or "fps_den" in video:
        numerator = require_positive_int(video.get("fps_num"), "video.fps_num")
        denominator = require_positive_int(video.get("fps_den"), "video.fps_den")
        fps = numerator / denominator

    issues = []

    def issue(code, severity, message, metric, observed, threshold, unit):
        issues.append({
            "code": code, "severity": severity, "message": message,
            "asset_id": asset_id,
            "evidence": {"metric": metric, "observed": observed,
                         "minimum": threshold, "unit": unit},
        })

    if short_edge < min_edge:
        issue("VIDEO_SHORT_EDGE_BELOW_MIN", "error",
              f"视频短边 {short_edge} px，低于 {min_edge:g} px",
              "short_edge_px", short_edge, min_edge, "px")
    if duration_s < min_duration:
        issue("VIDEO_DURATION_BELOW_MIN", "error",
              f"视频时长 {duration_s:g} 秒，低于 {min_duration:g} 秒",
              "duration_s", duration_s, min_duration, "s")
    if min_fps > 0:
        if fps is None:
            issue("VIDEO_FPS_UNAVAILABLE", "warning", "无法读取视频帧率，最低帧率检查未完成",
                  "fps", None, min_fps, "fps")
        elif fps < min_fps:
            issue("VIDEO_FPS_BELOW_MIN", "error",
                  f"视频平均帧率 {fps:.6g} fps，低于 {min_fps:g} fps",
                  "fps", fps, min_fps, "fps")

    # Missing a requested measurement must remain incomplete, even if another
    # threshold failed; it cannot satisfy the platform's publication gate.
    if min_fps > 0 and fps is None:
        status = "skip"
    elif issues:
        status = "fail"
    else:
        status = "pass"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "sample_id": sample_id,
        "status": status,
        "issues": issues,
        "metrics": {
            "width_px": width, "height_px": height, "short_edge_px": short_edge,
            "duration_us": duration_us, "duration_s": duration_s, "fps": fps,
            "min_short_edge_px": min_edge, "min_duration_s": min_duration,
            "min_fps": min_fps,
        },
    }


def reject_nonfinite(token):
    raise ValueError(f"无效 JSON 数字 {token}")


def read_request(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("输入文件超过 1 MiB")
    return json.loads(data.decode("utf-8"), parse_constant=reject_nonfinite)


def write_result(path, result):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="标准请求 JSON")
    parser.add_argument("--output", required=True, type=Path, help="标准结果 JSON")
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve():
            raise ValueError("输入和输出不能是同一个文件")
        result = check(read_request(args.input))
        write_result(args.output, result)
    except (OSError, ValueError) as error:
        print(f"视频规格检查执行失败：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
