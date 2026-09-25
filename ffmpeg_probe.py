from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _language(line: str) -> str:
    match = re.search(r"Stream #\S+\(([^)]+)\):", line)
    return match.group(1).casefold() if match else "und"


def _channels(line: str) -> int:
    lowered = line.casefold()
    if re.search(r"\bmono\b", lowered):
        return 1
    if re.search(r"\bstereo\b", lowered):
        return 2

    match = re.search(r"\b(\d+)\.(\d+)(?:\([^)]*\))?\b", lowered)
    if match:
        return int(match.group(1)) + int(match.group(2))
    return 0


def probe_media_file_ffmpeg(ffmpeg: str, path: Path):
    """Read media headers with ffmpeg when ffprobe is unavailable."""
    if not path.exists():
        return {"path": path, "error": "file does not exist"}
    if not path.is_file():
        return {"path": path, "error": "path is not a regular file"}
    if path.is_symlink():
        return {"path": path, "error": "refusing to probe symlink"}

    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"path": path, "error": str(exc)}

    output = completed.stderr or ""
    duration = None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if match:
        duration = (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + float(match.group(3))
        )

    video = None
    audio = []
    subtitles = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if "Stream #" not in line:
            continue

        if " Video: " in line and video is None:
            codec = re.search(r"Video:\s*([^,\s]+)", line)
            profile = re.search(r"Video:\s*[^,(\s]+\s*\(([^)]+)\)", line)
            pix_fmt = re.search(r"Video:\s*[^,]+,\s*([^,\s(]+)", line)
            resolution = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)

            pixel_format = pix_fmt.group(1) if pix_fmt else ""
            bit_depth = None
            depth = re.search(r"p(10|12|16)(?:le|be)?$", pixel_format.casefold())
            if depth:
                bit_depth = int(depth.group(1))
            elif pixel_format:
                bit_depth = 8

            lowered = line.casefold()
            if "dolby vision" in lowered or "dovi" in lowered:
                hdr = "Dolby Vision"
            elif "smpte2084" in lowered:
                hdr = "HDR/PQ"
            elif "arib-std-b67" in lowered:
                hdr = "HLG"
            elif "bt2020" in lowered:
                hdr = "HDR/BT2020"
            else:
                hdr = "SDR/unknown"

            video = {
                "codec": codec.group(1) if codec else "?",
                "profile": profile.group(1) if profile else "",
                "width": int(resolution.group(1)) if resolution else 0,
                "height": int(resolution.group(2)) if resolution else 0,
                "pix_fmt": pixel_format,
                "bit_depth": bit_depth,
                "hdr": hdr,
            }

        elif " Audio: " in line:
            codec = re.search(r"Audio:\s*([^,\s]+)", line)
            layout = re.search(
                r"\b(mono|stereo|\d+\.\d+(?:\([^)]*\))?)\b",
                line,
                flags=re.IGNORECASE,
            )
            audio.append(
                {
                    "language": _language(line),
                    "codec": codec.group(1).casefold() if codec else "?",
                    "channels": _channels(line),
                    "layout": layout.group(1) if layout else "",
                    "title": "",
                }
            )

        elif " Subtitle: " in line:
            codec = re.search(r"Subtitle:\s*([^,\s]+)", line)
            lowered = line.casefold()
            subtitles.append(
                {
                    "language": _language(line),
                    "codec": codec.group(1).casefold() if codec else "?",
                    "forced": "forced" in lowered,
                    "hearing_impaired": (
                        "hearing impaired" in lowered
                        or "hearing_impaired" in lowered
                    ),
                    "title": "",
                }
            )

    if video is None and not audio and not subtitles:
        tail = " | ".join(
            line.strip()
            for line in output.splitlines()[-3:]
            if line.strip()
        )
        return {
            "path": path,
            "error": tail or "ffmpeg returned no stream metadata",
        }

    try:
        size = path.stat().st_size
    except OSError:
        size = None

    return {
        "path": path,
        "error": None,
        "size": size,
        "duration": duration,
        "video": video,
        "audio": audio,
        "subtitles": subtitles,
    }
