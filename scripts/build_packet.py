#!/usr/bin/env python3
"""Build a bounded advisor packet from explicitly selected workspace files."""

import argparse
import hashlib
import json
from pathlib import Path


def workspace_path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path outside workspace: {value}")
    return path


def read_text(path):
    data = path.read_bytes()
    if b"\0" in data:
        raise ValueError(f"Binary input: {path.name}")
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def build(root, spec, max_chars):
    round_id = spec["round_id"]
    if not isinstance(round_id, str) or not round_id.strip() or "\n" in round_id:
        raise ValueError("round_id must be a nonempty single line")
    request_path = workspace_path(root, spec["request"])
    request, request_hash = read_text(request_path)
    sections = [f"Round: {round_id}\n\n{request.rstrip()}\n",
                f"Request source: {request_path.relative_to(root)}; sha256={request_hash}\n"]
    source_paths = {request_path}
    ids = set()
    for source in spec.get("sources", []):
        evidence_id = source["id"]
        if not isinstance(evidence_id, str) or not evidence_id.strip() or "\n" in evidence_id:
            raise ValueError("Evidence id must be a nonempty single line")
        if evidence_id in ids:
            raise ValueError(f"Duplicate evidence id: {evidence_id}")
        ids.add(evidence_id)
        path = workspace_path(root, source["path"])
        source_paths.add(path)
        content, digest = read_text(path)
        lines = content.splitlines()
        start = source.get("start", 1)
        end = source.get("end", len(lines))
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError(f"Invalid line range for {source['path']}: {start}:{end}")
        excerpt = "\n".join(f"{i + 1}: {lines[i]}" for i in range(start - 1, end))
        fence = "```"
        while fence in excerpt:
            fence += "`"
        sections.append(f"\nEvidence {evidence_id}: {path.relative_to(root)}:{start}-{end}\n"
                        f"Full file sha256={digest}\n{fence}text\n{excerpt}\n{fence}\n")
    packet = "\n".join(sections)
    if len(packet) > max_chars:
        raise ValueError(f"Packet has {len(packet)} characters; budget is {max_chars}. "
                         "Narrow sources or explicitly raise --max-chars; nothing was truncated.")
    return packet, source_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--spec", required=True, help="Workspace-relative JSON input list")
    parser.add_argument("--output", required=True, help="New workspace-relative packet path")
    parser.add_argument("--max-chars", type=int, default=24000)
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.max_chars <= 0:
            raise ValueError("--max-chars must be positive")
        spec_path = workspace_path(root, args.spec)
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        packet, inputs = build(root, spec, args.max_chars)
        output = workspace_path(root, args.output)
        if output in inputs or output == spec_path:
            raise ValueError("Output cannot replace an input")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(packet)
        print(json.dumps({"path": str(output.relative_to(root)), "characters": len(packet),
                          "sha256": hashlib.sha256(packet.encode()).hexdigest()}, ensure_ascii=False))
    except (KeyError, TypeError, ValueError, OSError, UnicodeError) as exc:
        parser.exit(2, f"Packet error: {exc}\n")


if __name__ == "__main__":
    main()
