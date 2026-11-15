#!/usr/bin/env python3
"""Copy and organize dataset 2 without writing to the raw recording tree."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from zoneinfo import ZoneInfo


SOURCE_RELATIVE = Path("dataset/raw/202606 300米海上饱和数据采集/300米录音")
STAGE_RELATIVE = Path("dataset/processed/300m录音")
FINAL_RELATIVE = Path("dataset/processed/heliox_dataset2_processed1")
BUFFER_SIZE = 8 * 1024 * 1024
PATH_ACTIONS = {"copy_file", "rename_file", "move_file", "parent_directory_renamed"}


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def describe_stat(path: Path) -> dict:
    info = path.stat()
    return {
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "mode": info.st_mode,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def progress(phase: str, **values) -> None:
    print(json.dumps({"time": now(), "phase": phase, **values}, ensure_ascii=False), flush=True)


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.sequence = 0
        self.run_id = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S%z")
        self.stream = path.open("x", encoding="utf-8", buffering=1)

    def event(self, action: str, **fields) -> int:
        self.sequence += 1
        record = {
            "schema_version": 1,
            "run_id": self.run_id,
            "seq": self.sequence,
            "timestamp": now(),
            "action": action,
            "status": "completed",
            **fields,
        }
        self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return self.sequence

    def close(self) -> None:
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()

    def reopen(self, path: Path) -> None:
        self.path = path
        self.stream = path.open("a", encoding="utf-8", buffering=1)


class Organizer:
    def __init__(self, repo: Path, baseline: dict):
        self.repo = repo.resolve()
        self.source = self.repo / SOURCE_RELATIVE
        self.stage = self.repo / STAGE_RELATIVE
        self.final = self.repo / FINAL_RELATIVE
        self.output = self.stage
        self.baseline = baseline
        self.records: list[dict] = []
        self.journal: Journal | None = None
        self.started = time.monotonic()

    def relative(self, path: Path) -> str:
        return self.repo.name + "/" + path.relative_to(self.repo).as_posix()

    def ensure_output_path(self, path: Path) -> None:
        # Resolving paths also prevents a symlink from redirecting a mutation to raw data.
        if not path.resolve().is_relative_to(self.output.resolve()):
            raise ValueError(f"Mutation outside the current output directory: {path}")

    def mkdir(self, path: Path, reason: str) -> None:
        self.ensure_output_path(path)
        if path.is_dir():
            return
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
        self.mkdir(path.parent, reason)
        path.mkdir()
        self.journal.event("mkdir", entity_type="directory", from_path=None,
                           to_path=self.relative(path), reason=reason)

    def file_fields(self, record: dict) -> dict:
        return {key: record[key] for key in (
            "file_id", "original_path", "sha256", "size_bytes", "role", "depth_m",
            "device", "device_assignment", "original_device_directory",
        )}

    def check_source_inventory(self) -> None:
        paths = sorted(self.source.rglob("*"))
        if any(path.is_symlink() for path in paths):
            raise ValueError("Source contains symlinks")
        files = {p.relative_to(self.source).as_posix(): p for p in paths if p.is_file()}
        expected = {item["relative_path"]: item for item in self.baseline["files"]}
        if files.keys() != expected.keys():
            raise ValueError("Source file inventory differs from its baseline")
        for name, path in files.items():
            expected_stat = {key: expected[name][key] for key in describe_stat(path)}
            if describe_stat(path) != expected_stat:
                raise ValueError(f"Source file metadata changed: {name}")
        directories = {str(p.relative_to(self.source)): describe_stat(p)
                       for p in [self.source] + [p for p in paths if p.is_dir()]}
        if directories != self.baseline["directories"]:
            raise ValueError("Source directory inventory or metadata changed")

    def preflight(self) -> None:
        if self.stage.exists() or self.stage.is_symlink() or self.final.exists() or self.final.is_symlink():
            raise FileExistsError("The staging or final output directory already exists; refusing to merge or overwrite")
        if self.source.is_symlink() or self.baseline["source_root"] != self.relative(self.source):
            raise ValueError("Source path does not match the supplied baseline")
        self.check_source_inventory()
        self.depths = sorted(self.source.iterdir(), key=lambda p: (float(re.match(r"^(\d+(?:\.\d+)?)米", p.name)[1])
                             if re.match(r"^(\d+(?:\.\d+)?)米", p.name) else float("inf"), p.name))
        if not self.depths or any(not p.is_dir() or not re.match(r"^(\d+(?:\.\d+)?)米", p.name) for p in self.depths):
            raise ValueError("Unexpected non-depth entry at the source root")
        self.aliases = []
        self.new_directory_depths = []
        for depth in self.depths:
            for directory in sorted(p for p in depth.iterdir() if p.is_dir()):
                if directory.name.startswith("老机"):
                    canonical = "老机"
                elif directory.name.startswith("新机"):
                    canonical = "新机"
                    if any(p.is_file() and p.suffix.lower() == ".wav" for p in depth.iterdir()):
                        self.new_directory_depths.append(depth.name)
                else:
                    raise ValueError(f"Cannot assign a device to {directory.relative_to(self.source)}")
                if directory.name != canonical:
                    target = directory.with_name(canonical)
                    if target.exists():
                        raise ValueError(f"Device directory normalization would collide: {target}")
                    self.aliases.append((directory.relative_to(self.source), target.relative_to(self.source)))
        for index, item in enumerate(sorted(self.baseline["files"], key=lambda v: v["relative_path"]), 1):
            original = Path(item["relative_path"])
            parts = original.parts
            if len(parts) < 2:
                raise ValueError(f"Unexpected source file: {original}")
            depth_m = re.match(r"^(\d+(?:\.\d+)?)米", parts[0])[1]
            if len(parts) == 2:
                device, assignment, device_dir = "new", "depth_root_as_new_per_approved_rule", None
            else:
                device_dir = parts[1]
                if device_dir.startswith("老机"):
                    device = "old"
                elif device_dir.startswith("新机"):
                    device = "new"
                else:
                    raise ValueError(f"Unrecognized device directory: {original}")
                assignment = "explicit_directory_label"
            if original.suffix.lower() == ".wav":
                role = "background_noise" if "背景音" in original.stem or "底噪" in original.stem else "recording"
            elif original.suffix.lower() in {".doc", ".docx"}:
                role = "source_document"
            else:
                raise ValueError(f"Unrecognized file type: {original}")
            record = {
                "file_id": f"f{index:06d}", "original_path": self.relative(self.source / original),
                "source_relative_path": original.as_posix(), "current_relative_path": original.as_posix(),
                "sha256": item["sha256"], "size_bytes": item["size_bytes"], "depth_m": depth_m,
                "device": device, "device_assignment": assignment, "original_device_directory": device_dir,
                "role": role, "background_kind": "decompression" if "减压" in original.stem else "ambient",
            }
            if role == "source_document":
                record["final_relative_path"] = (Path("metadata/source_documents") / original).as_posix()
            elif role == "recording":
                inside = Path(*parts[2:]) if device_dir else Path(parts[-1])
                record["final_relative_path"] = (Path(parts[0]) / ("老机" if device == "old" else "新机") / inside).as_posix()
            self.records.append(record)
        groups = defaultdict(list)
        for record in self.records:
            if record["role"] == "background_noise":
                groups[(record["depth_m"], record["device"], record["background_kind"])].append(record)
        for (depth_m, device, kind), records in groups.items():
            base = f"bg_noise_{depth_m}m_{device}" + ("_decompression" if kind == "decompression" else "")
            for index, record in enumerate(records, 1):
                name = base + (f"_{index:02d}" if len(records) > 1 else "") + ".wav"
                record["final_relative_path"] = (Path("background_noise") / name).as_posix()
                record["background_filename"] = name
        used = set()
        # Explicitly labeled device files claim their original names before loose root files.
        order = sorted(self.records, key=lambda r: (r["original_device_directory"] is None, r["source_relative_path"]))
        for record in order:
            target = Path(record["final_relative_path"])
            if target.as_posix().casefold() in used:
                if record["role"] != "recording" or record["device"] == "old":
                    raise ValueError(f"Cannot preserve the original filename without a collision: {target}")
                suffix = "__from_depth_root" if record["original_device_directory"] is None else "__" + record["file_id"]
                candidate = target.with_name(target.stem + suffix + target.suffix)
                number = 2
                while candidate.as_posix().casefold() in used:
                    candidate = target.with_name(target.stem + suffix + f"_{number:02d}" + target.suffix)
                    number += 1
                record["final_relative_path"] = candidate.as_posix()
                record["collision_renamed"] = True
                target = candidate
            used.add(target.as_posix().casefold())
        if len(used) != len(self.records):
            raise ValueError("Output mapping is not one-to-one")
        self.total_bytes = sum(r["size_bytes"] for r in self.records)
        if self.total_bytes != self.baseline["total_bytes"] or len(self.records) != self.baseline["total_files"]:
            raise ValueError("Baseline summary mismatch")
        if shutil.disk_usage(self.stage.parent).free < self.total_bytes + 100 * 1024 * 1024:
            raise OSError("Insufficient output disk space")
        progress("preflight_complete", source_files=len(self.records), total_bytes=self.total_bytes,
                 roles=dict(Counter(r["role"] for r in self.records)),
                 renamed_recording_collisions=sum(r.get("collision_renamed", False) for r in self.records))

    def relocate_file(self, record: dict, target_relative: Path, action: str, reason: str) -> None:
        source = self.output / record["current_relative_path"]
        target = self.output / target_relative
        self.ensure_output_path(source)
        self.ensure_output_path(target)
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        self.mkdir(target.parent, reason)
        source.rename(target)
        self.journal.event(action, entity_type="file", **self.file_fields(record),
                           from_path=self.relative(source), to_path=self.relative(target), reason=reason)
        record["current_relative_path"] = target_relative.as_posix()

    def directory_file_updates(self, before: Path, after: Path, directory_seq: int) -> None:
        for record in self.records:
            current = self.output / record["current_relative_path"]
            if current.is_relative_to(before):
                target = after / current.relative_to(before)
                self.journal.event("parent_directory_renamed", entity_type="file", **self.file_fields(record),
                                   from_path=self.relative(current), to_path=self.relative(target),
                                   directory_event_seq=directory_seq, physical_operation=False,
                                   reason="path_changed_by_parent_directory_rename")
                record["current_relative_path"] = target.relative_to(self.output).as_posix()

    def copy_source(self) -> None:
        for relative in sorted(self.baseline["directories"], key=lambda p: (len(Path(p).parts), p)):
            if relative != ".":
                self.mkdir(self.output / relative, "copy_source_directory_structure")
        copied = 0
        phase_started = time.monotonic()
        for index, record in enumerate(self.records, 1):
            source = self.source / record["source_relative_path"]
            target = self.output / record["current_relative_path"]
            self.ensure_output_path(target)
            digest = hashlib.sha256()
            with source.open("rb") as reader, target.open("xb") as writer:
                while chunk := reader.read(BUFFER_SIZE):
                    writer.write(chunk)
                    digest.update(chunk)
            shutil.copystat(source, target, follow_symlinks=False)
            if digest.hexdigest() != record["sha256"] or target.stat().st_size != record["size_bytes"]:
                raise ValueError(f"Copy did not match baseline: {record['source_relative_path']}")
            self.journal.event("copy_file", entity_type="file", **self.file_fields(record),
                               from_path=self.relative(source), to_path=self.relative(target),
                               reason="independent_byte_copy_from_read_only_source")
            copied += record["size_bytes"]
            if index % 10 == 0 or index == len(self.records):
                progress("copy", files_done=index, files_total=len(self.records), bytes_done=copied,
                         elapsed_seconds=round(time.monotonic() - phase_started, 1))
        self.check_source_inventory()

    def organize(self) -> None:
        backgrounds = [r for r in self.records if r["role"] == "background_noise"]
        for record in backgrounds:
            target = Path(record["current_relative_path"]).with_name(record["background_filename"])
            self.relocate_file(record, target, "rename_file", "rename_background_in_its_copied_source_directory")
        self.journal.event("background_rename_complete", file_count=len(backgrounds))
        self.mkdir(self.output / "background_noise", "collect_background_noise")
        for record in backgrounds:
            self.relocate_file(record, Path(record["final_relative_path"]), "move_file", "collect_background_noise")
        self.journal.event("background_collection_complete", file_count=len(backgrounds))
        for before_relative, after_relative in self.aliases:
            before, after = self.output / before_relative, self.output / after_relative
            self.ensure_output_path(before)
            self.ensure_output_path(after)
            if after.exists() or after.is_symlink():
                raise FileExistsError(after)
            before.rename(after)
            seq = self.journal.event("rename_directory", entity_type="directory", from_path=self.relative(before),
                                     to_path=self.relative(after), reason="normalize_device_directory_name",
                                     original_directory_note=before_relative.name)
            self.directory_file_updates(before, after, seq)
        for depth in self.depths:
            for device in ("老机", "新机"):
                self.mkdir(self.output / depth.name / device, "ensure_both_device_directories_exist")
        for record in self.records:
            if record["current_relative_path"] != record["final_relative_path"]:
                reason = "archive_source_document" if record["role"] == "source_document" else "organize_recording_by_device"
                if record.get("collision_renamed"):
                    reason += "; preserve_same_named_sources_without_overwriting"
                self.relocate_file(record, Path(record["final_relative_path"]), "move_file", reason)
        progress("organization_complete", background_files=len(backgrounds),
                 document_files=sum(r["role"] == "source_document" for r in self.records))

    def check_layout(self, generated_files: set[str]) -> None:
        expected = {r["current_relative_path"] for r in self.records}
        actual = {p.relative_to(self.output).as_posix() for p in self.output.rglob("*") if p.is_file()}
        if actual != expected | generated_files:
            raise ValueError(f"Unexpected output files: missing={expected - actual}, extra={actual - expected - generated_files}")
        if any(p.is_symlink() for p in self.output.rglob("*")):
            raise ValueError("Output contains symlinks")
        for depth in self.depths:
            entries = sorted((self.output / depth.name).iterdir())
            if {p.name for p in entries} != {"老机", "新机"} or not all(p.is_dir() for p in entries):
                raise ValueError(f"Depth directory not normalized: {depth.name}")
        for record in self.records:
            path = self.output / record["current_relative_path"]
            if path.stat().st_size != record["size_bytes"]:
                raise ValueError(f"Output size mismatch: {path}")
            if record["role"] == "background_noise" and path.parent != self.output / "background_noise":
                raise ValueError("Uncollected background file")
            if record["device"] == "old" and record["role"] == "recording":
                original = Path(record["source_relative_path"])
                if path.name != original.name or path.relative_to(self.output / original.parts[0] / "老机") != Path(*original.parts[2:]):
                    raise ValueError("An old-device recording filename or internal structure changed")

    def verify_hashes(self) -> None:
        self.check_source_inventory()
        phase_started = time.monotonic()
        for label in ("source_after", "output"):
            for index, record in enumerate(self.records, 1):
                path = (self.source / record["source_relative_path"] if label == "source_after"
                        else self.output / record["current_relative_path"])
                if sha256(path) != record["sha256"]:
                    raise ValueError(f"{label} SHA-256 mismatch: {record['source_relative_path']}")
                if index % 10 == 0 or index == len(self.records):
                    progress("verify_" + label, files_done=index, files_total=len(self.records),
                             elapsed_seconds=round(time.monotonic() - phase_started, 1))
        self.check_source_inventory()
        self.journal.event("payload_integrity_verified", file_count=len(self.records), total_bytes=self.total_bytes,
                           source_sha256_unchanged=True, output_sha256_matches_source=True,
                           source_paths_sizes_mtimes_ctimes_modes_unchanged=True)

    def check_journal(self) -> int:
        self.journal.stream.flush()
        events = [json.loads(line) for line in self.journal.path.read_text(encoding="utf-8").splitlines()]
        if [e["seq"] for e in events] != list(range(1, len(events) + 1)):
            raise ValueError("Journal sequence is discontinuous")
        by_id = defaultdict(list)
        for event in events:
            for key in ("from_path", "to_path", "original_path", "final_path", "path"):
                value = event.get(key)
                if value is not None and (not value.startswith(self.repo.name + "/") or ".." in Path(value).parts):
                    raise ValueError("Journal contains a nonconforming relative path")
            if event.get("file_id") and event["action"] in PATH_ACTIONS:
                by_id[event["file_id"]].append(event)
        for record in self.records:
            cursor = record["original_path"]
            history = by_id[record["file_id"]]
            if not history or history[0]["action"] != "copy_file":
                raise ValueError("Journal is missing an initial copy")
            for event in history:
                if event["from_path"] != cursor:
                    raise ValueError(f"File history is discontinuous: {record['file_id']}")
                cursor = event["to_path"]
            if cursor != self.relative(self.output / record["current_relative_path"]):
                raise ValueError("Journal does not reach a file's actual final path")
        rename_end = next(e["seq"] for e in events if e["action"] == "background_rename_complete")
        collection = [e for e in events if e["action"] == "move_file" and e.get("reason") == "collect_background_noise"]
        if any(e["seq"] <= rename_end for e in collection):
            raise ValueError("Background collection started before all backgrounds were renamed")
        return len(events)

    def rename_root(self) -> None:
        if self.final.exists() or self.final.is_symlink():
            raise FileExistsError(self.final)
        self.journal.close()
        self.stage.rename(self.final)
        self.output = self.final
        self.journal.reopen(self.final / "file_changes.jsonl")
        seq = self.journal.event("rename_directory", entity_type="directory", from_path=self.relative(self.stage),
                                 to_path=self.relative(self.final), reason="requested_final_dataset_name")
        for record in self.records:
            relative = record["current_relative_path"]
            self.journal.event("parent_directory_renamed", entity_type="file", **self.file_fields(record),
                               from_path=self.relative(self.stage / relative), to_path=self.relative(self.final / relative),
                               directory_event_seq=seq, physical_operation=False,
                               reason="path_changed_by_final_dataset_directory_rename")
            self.journal.event("file_finalized", entity_type="file", **self.file_fields(record),
                               final_path=self.relative(self.final / relative), source_sha256_after=record["sha256"],
                               output_sha256=record["sha256"], content_unchanged=True)

    def write_readme(self, validation: dict) -> None:
        counts = Counter(r["role"] for r in self.records)
        collision_records = [r for r in self.records if r.get("collision_renamed")]
        empty = [self.relative(self.output / d.name / device) for d in self.depths for device in ("老机", "新机")
                 if not any((self.output / d.name / device).rglob("*.wav"))]
        lines = [
            "# 300 米饱和潜水录音目录整理记录", "",
            f"处理完成时间：{now()}。", "",
            f"原始目录：`{self.relative(self.source)}`。",
            f"中间目录：`{self.relative(self.stage)}`。",
            f"最终目录：`{self.relative(self.final)}`。", "",
            "## 处理结果", "",
            f"保留 {len(self.records)} 份原始文件，共 {self.total_bytes:,} 字节（{self.total_bytes / 1024**3:.3f} GiB）。",
            f"包含 {len(self.depths)} 个深度目录、{counts['recording']} 份非背景 WAV、{counts['background_noise']} 份背景 WAV、{counts['source_document']} 份 Word 文档。",
            "生成的 README、JSONL 日志及 metadata 中的清单、脚本、校验报告不计入上述原始文件数量。", "",
            "## 整理规则", "",
            "1. 读取 raw 中的源文件，独立复制到 `dataset/processed/300m录音/`；没有使用指向原文件的硬链接或符号链接。",
            "2. 在每份背景音的副本原所在目录内重命名；全部背景音重命名完成后，才统一移动到 `background_noise/`。",
            "3. 原深度目录名保持不变，每个深度下仅保留 `老机/`、`新机/` 两个目录。",
            "4. 已标注老机、新机的目录按标注归类；`老机-更好`、`老机-信号更好`、`新机-信号更好` 统一为标准设备目录名，原备注保存在日志和下表中。",
            "5. 按本次确认的规则，深度根部散放 WAV 归新机。该标签是整理规则的归类结果，不代表通过音频鉴定了硬件型号。",
            "6. 已有设备目录内的文件优先保留原名；根部录音移入新机时若重名，增加 `__from_depth_root` 后缀。内容相同的文件也保留各自独立副本，不去重。",
            "7. 老机非背景录音的文件名和内部相对结构保持不变；新机已有的说话人子目录也保留。",
            "8. `.doc`、`.docx` 集中到 `metadata/source_documents/<原始相对路径>`，保留原文件名及来源结构。",
            "9. 整理并通过完整性校验后，实际将中间根目录更名为 `heliox_dataset2_processed1`。",
            "10. 音频和文档内容按原始字节保留，文件大小、修改时间和权限从源文件复制。", "",
            "## 背景音命名", "",
            "普通背景音和底噪：`bg_noise_<深度>m_<new|old>.wav`；减压背景音增加 `_decompression`。",
            "同一深度、设备、类型存在多份时，对所有同组文件增加 `_01`、`_02` 等序号，按原始相对路径排序分配。",
            "识别的原文件名包括 `背景音.wav`、`减压背景音.wav`、`300米底噪.wav`；每份背景音的原位置、设备归类依据和各中间位置可按 file_id 在 JSONL 中追溯。", "",
            "## 设备归类需要留意的原始结构", "",
            "以下深度已有显式新机目录，根部仍有 WAV；本次继续执行已确认的“根部归新机”规则。两组数据完整保留，没有将根部录音自行推断为老机：", "",
        ]
        lines.extend(f"- `{name}`" for name in sorted(set(self.new_directory_depths)))
        lines.extend(["", f"因重名而增加来源后缀的非背景录音共有 {len(collision_records)} 份：", "",
                      "| 原始相对路径 | 最终数据集内相对路径 |", "| --- | --- |"])
        lines.extend(f"| {r['source_relative_path']} | {r['final_relative_path']} |" for r in collision_records)
        lines.extend(["", "## 设备目录名称归一化", "", "| 原设备目录 | 整理后设备目录 |", "| --- | --- |"])
        lines.extend(f"| {before.as_posix()} | {after.as_posix()} |" for before, after in self.aliases)
        lines.extend(["", "## 各深度文件统计", "", "| 深度（米） | 新机非背景 WAV | 老机非背景 WAV | 背景 WAV | Word 文档 |", "| ---: | ---: | ---: | ---: | ---: |"])
        for depth in self.depths:
            rows = [r for r in self.records if Path(r["source_relative_path"]).parts[0] == depth.name]
            tally = Counter((r["role"], r["device"]) for r in rows)
            lines.append(f"| {rows[0]['depth_m']} | {tally['recording', 'new']} | {tally['recording', 'old']} | "
                         f"{sum(r['role'] == 'background_noise' for r in rows)} | {sum(r['role'] == 'source_document' for r in rows)} |")
        lines.extend(["", "## 空设备目录", "", "以下设备目录没有非背景录音，仅创建目录以统一结构：", ""])
        lines.extend(f"- `{path}`" for path in empty)
        lines.extend([
            "", "## file_changes.jsonl", "",
            "UTF-8 编码，每行一个 JSON 对象；按实际执行顺序追加。路径统一为 `heliox_2nd/dataset/...`，以项目目录名开头，不含 `/mnt/d/`。", "",
            "- `seq`、`timestamp`、`run_id`：操作序号、带时区的时间和本次处理标识。",
            "- `action`、`entity_type`：操作类型以及文件或目录。",
            "- `file_id`、`original_path`：原始文件的稳定追踪标识和原始位置；相同内容的不同来源文件有不同 file_id。",
            "- `from_path`、`to_path`：本次操作前后路径，包含复制、就地重命名、移动和父目录更名产生的中间路径。",
            "- `sha256`、`size_bytes`：原始文件的内容摘要和字节数。",
            "- `role`、`depth_m`、`device`、`device_assignment`、`original_device_directory`：文件用途、深度、设备和设备归类依据。",
            "- `reason`：本次操作的原因；原设备目录中的备注保存在 `original_directory_note`。",
            "- `directory_event_seq`：父目录更名的实际操作序号；对应的逐文件 `parent_directory_renamed` 记录带 `physical_operation: false`，表示路径随父目录改变，没有再次逐文件搬运。",
            "- `file_finalized`：每份源文件的最终路径和核对结果。", "",
            "追溯单个文件时，按 file_id 筛选并按 seq 排序；`copy_file`、`rename_file`、`move_file`、`parent_directory_renamed` 构成完整且连续的路径链。",
            "日志也记录目录创建、设备目录更名、最终根目录更名、校验及生成说明文件等事件。", "",
            "## 校验", "",
            f"校验状态：**{validation['status']}**。详细结果见 `metadata/validation.json`。",
            "- 300 个原始文件均有唯一对应产物，无覆盖、丢弃或内容去重。" if len(self.records) == 300 else f"- {len(self.records)} 个原始文件均有唯一对应产物。",
            "- 原始数据总字节数与全部对应产物总字节数一致。",
            "- 原始目录处理前后，全部文件的 SHA-256、路径、大小、mtime、ctime、权限以及目录清单一致。",
            "- 整理后的每份产物均完整计算 SHA-256，与对应原始文件基线一致。",
            "- SHA-256 校验在最终根目录更名前完成；随后只更名父目录，并检查最终路径、文件大小及日志链。",
            "- 56 份背景音均已归集。" if counts["background_noise"] == 56 else "- 所有已识别背景音均已归集。",
            "- 各深度下恰有老机、新机两个目录；老机非背景录音原名和内部结构保持一致。",
            "- 每份文件日志从原始位置连续连接到最终位置，涵盖设备目录归一化及最终根目录更名。", "",
            "访问时间 atime 不作为不变性校验字段，因为读取文件可能由文件系统更新访问时间；处理脚本没有对原目录执行写入或元数据设置。", "",
            "## 附带资料", "",
            "- `metadata/source_manifest.json`：处理前的完整文件 SHA-256、大小、元数据和目录快照。",
            "- `metadata/validation.json`：处理后完整性校验结果。",
            "- `metadata/organize_heliox_dataset2.py`：本次处理脚本副本；项目中同一脚本位于 `scripts/audio_processing/organize_heliox_dataset2.py`。",
            "- `metadata/source_documents/`：全部原始 Word 资料。", "",
            "脚本仅依赖 Python 3.10+ 标准库，入口参数为 `--baseline <source_manifest.json>`；中间或最终输出目录已存在时会拒绝执行，避免覆盖已有结果。", "",
        ])
        path = self.output / "README.md"
        with path.open("x", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
        self.journal.event("generate_file", entity_type="file", to_path=self.relative(path),
                           sha256=sha256(path), reason="document_processing_rules_provenance_and_validation")

    def run(self) -> dict:
        self.preflight()
        self.stage.mkdir()
        self.journal = Journal(self.stage / "file_changes.jsonl")
        self.journal.event("mkdir", entity_type="directory", from_path=None,
                           to_path=self.relative(self.stage), reason="create_requested_staging_directory")
        try:
            self.copy_source()
            self.organize()
            self.check_layout({"file_changes.jsonl"})
            self.mkdir(self.output / "metadata", "save_provenance_metadata")
            write_json(self.output / "metadata/source_manifest.json", self.baseline)
            script_copy = self.output / "metadata/organize_heliox_dataset2.py"
            with Path(__file__).open("rb") as source_stream, script_copy.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream)
            generated = {"file_changes.jsonl", "metadata/source_manifest.json", "metadata/organize_heliox_dataset2.py"}
            for relative in sorted(generated - {"file_changes.jsonl"}):
                self.journal.event("generate_file", entity_type="file", to_path=self.relative(self.output / relative),
                                   sha256=sha256(self.output / relative), reason="save_processing_provenance")
            self.check_layout(generated)
            self.check_journal()
            self.verify_hashes()
            self.rename_root()
            self.check_layout(generated)
            self.check_source_inventory()
            journal_events_checked = self.check_journal()
            validation = {
                "schema_version": 1, "status": "passed", "validated_at": now(),
                "source_root": self.relative(self.source), "staging_root": self.relative(self.stage),
                "final_root": self.relative(self.final), "source_file_count": len(self.records),
                "output_payload_file_count": len(self.records), "source_bytes": self.total_bytes,
                "output_payload_bytes": sum((self.output / r["current_relative_path"]).stat().st_size for r in self.records),
                "depth_directory_count": len(self.depths), "role_counts": dict(Counter(r["role"] for r in self.records)),
                "recording_counts_by_device": dict(Counter(r["device"] for r in self.records if r["role"] == "recording")),
                "collision_recordings_preserved": sum(r.get("collision_renamed", False) for r in self.records),
                "source_sha256_before_after_equal": True, "all_output_sha256_match_source": True,
                "source_file_paths_sizes_mtimes_ctimes_modes_unchanged": True,
                "source_directory_snapshot_unchanged": True, "source_atime_excluded_from_comparison": True,
                "all_file_path_chains_continuous_to_final": True, "journal_events_checked": journal_events_checked,
                "all_backgrounds_renamed_before_collection": True, "depth_layout_valid": True,
                "old_device_recording_names_and_internal_structure_preserved": True,
                "root_directory_actually_renamed": not self.stage.exists() and self.final.is_dir(),
                "payload_sha256_verification_phase": "after_organization_before_final_parent_directory_rename",
            }
            write_json(self.output / "metadata/validation.json", validation)
            self.journal.event("generate_file", entity_type="file", to_path=self.relative(self.output / "metadata/validation.json"),
                               sha256=sha256(self.output / "metadata/validation.json"), reason="save_integrity_verification_result")
            self.write_readme(validation)
            generated |= {"metadata/validation.json", "README.md"}
            self.check_layout(generated)
            self.check_journal()
            self.journal.event("processing_complete", source_files=len(self.records),
                               output_payload_files=len(self.records), total_bytes=self.total_bytes,
                               final_path=self.relative(self.final), validation_status="passed")
            self.journal.close()
            progress("complete", **validation, elapsed_seconds=round(time.monotonic() - self.started, 1))
            return validation
        except BaseException as error:
            if self.journal and not self.journal.stream.closed:
                self.journal.event("processing_failed", error_type=type(error).__name__, message=str(error))
                self.journal.close()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="Read-only source snapshot JSON with SHA-256 values")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    Organizer(repo, baseline).run()


if __name__ == "__main__":
    main()
