"""Analyze installed Python Lambda dependencies and render a self-contained HTML treemap."""

import csv
import json
import os
import posixpath
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LAMBDA_UNZIPPED_LIMIT_BYTES = 262_144_000  # 250 MB (AWS Lambda unzipped layer limit)

_NATIVE_BINARY_SUFFIXES = {".so", ".pyd", ".dll", ".dylib"}


def _is_native_binary(rel_path: str) -> bool:
    """Return True if the file path looks like a compiled native shared library."""
    name = posixpath.basename(rel_path).lower()
    if ".so." in name:
        return True
    _, ext = posixpath.splitext(name)
    return ext in _NATIVE_BINARY_SUFFIXES


def _parse_metadata(dist_info_dir: Path) -> Tuple[str, str]:
    """Extract (Name, Version) from a `.dist-info/METADATA` file."""
    fallback_name = dist_info_dir.name[: -len(".dist-info")].split("-", 1)[0]
    metadata_file = dist_info_dir / "METADATA"
    if not metadata_file.is_file():
        return fallback_name, ""

    name = ""
    version = ""
    with metadata_file.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.rstrip("\r\n")
            if stripped == "":
                break
            if stripped.startswith("Name:") and not name:
                name = stripped[5:].strip()
            elif stripped.startswith("Version:") and not version:
                version = stripped[8:].strip()
            if name and version:
                break

    return (name or fallback_name), version


def _normalize_rel_path(raw_path: str) -> Optional[str]:
    """Normalize a RECORD relative path and reject entries escaping target_dir."""
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/"):
        return None
    normalized = posixpath.normpath(cleaned)
    if normalized in ("", ".") or normalized.startswith("../") or normalized == "..":
        return None
    return normalized


def _insert_tree_file(root: Dict[str, Any], parts: List[str], size: int, is_native: bool) -> None:
    """Insert a file and accumulate size/file_count/native_bytes along its path."""
    root["size"] += size
    root["file_count"] += 1
    if is_native:
        root["native_bytes"] += size

    node = root
    for idx, part in enumerate(parts):
        children = node.setdefault("_children_map", {})
        child = children.get(part)
        if child is None:
            child = {
                "name": part,
                "size": 0,
                "file_count": 0,
                "native_bytes": 0,
            }
            children[part] = child
        child["size"] += size
        child["file_count"] += 1
        if is_native:
            child["native_bytes"] += size
        if idx < len(parts) - 1:
            node = child


def _finalize_tree(node: Dict[str, Any]) -> Dict[str, Any]:
    """Convert internal `_children_map` dicts into sorted `children` lists."""
    children_map = node.pop("_children_map", None)
    if children_map:
        children = [_finalize_tree(c) for c in children_map.values()]
        children.sort(key=lambda item: item["size"], reverse=True)
        node["children"] = children
    return node


def analyze_directory(target_dir: Path) -> Dict[str, Any]:
    """
    Walk `target_dir` and attribute files to Python distributions via `.dist-info/RECORD`
    and `METADATA`, building a hierarchical size tree per package.

    :param target_dir: Root directory containing installed Python packages.
    :return: JSON-serializable analysis summary and hierarchical package tree.
    """
    files_map: Dict[str, int] = {}
    for dirpath, _, filenames in os.walk(target_dir, followlinks=False):
        rel_dir = Path(dirpath).relative_to(target_dir).as_posix()
        for fname in filenames:
            full_path = Path(dirpath) / fname
            if full_path.is_symlink() or not full_path.is_file():
                continue
            rel_path = fname if rel_dir == "." else f"{rel_dir}/{fname}"
            files_map[rel_path] = full_path.stat().st_size

    claimed: set[str] = set()
    # Keyed by (dist_name.lower(), version) to merge cleanly if needed, storing display name
    pkg_buckets: Dict[str, Dict[str, Any]] = {}

    for dist_info_dir in sorted(target_dir.glob("*.dist-info")):
        if not dist_info_dir.is_dir():
            continue
        pkg_name, pkg_version = _parse_metadata(dist_info_dir)
        bucket_key = pkg_name.lower()
        bucket = pkg_buckets.setdefault(
            bucket_key,
            {
                "name": pkg_name,
                "version": pkg_version,
                "size": 0,
                "file_count": 0,
                "native_bytes": 0,
                "_children_map": {},
            },
        )
        if not bucket["version"] and pkg_version:
            bucket["version"] = pkg_version

        record_file = dist_info_dir / "RECORD"
        if record_file.is_file():
            with record_file.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                reader = csv.reader(fh)
                for row in reader:
                    if not row:
                        continue
                    rel_path = _normalize_rel_path(row[0])
                    if rel_path is None or rel_path in claimed:
                        continue
                    if rel_path in files_map:
                        claimed.add(rel_path)
                        size = files_map[rel_path]
                        _insert_tree_file(
                            bucket,
                            rel_path.split("/"),
                            size,
                            _is_native_binary(rel_path),
                        )

        # Also claim any files inside the .dist-info directory itself not listed in RECORD
        dist_prefix = f"{dist_info_dir.name}/"
        for rel_path, size in files_map.items():
            if rel_path.startswith(dist_prefix) and rel_path not in claimed:
                claimed.add(rel_path)
                _insert_tree_file(
                    bucket,
                    rel_path.split("/"),
                    size,
                    _is_native_binary(rel_path),
                )

    # Group any unrecorded files by top-level directory or "(root)"
    for rel_path, size in files_map.items():
        if rel_path in claimed:
            continue
        parts = rel_path.split("/")
        group_name = parts[0] if len(parts) > 1 else "(root)"
        bucket_key = f"__unclaimed__:{group_name.lower()}"
        bucket = pkg_buckets.setdefault(
            bucket_key,
            {
                "name": group_name,
                "version": "",
                "size": 0,
                "file_count": 0,
                "native_bytes": 0,
                "_children_map": {},
            },
        )
        _insert_tree_file(bucket, parts, size, _is_native_binary(rel_path))

    total_unzipped = sum(files_map.values())
    total_files = len(files_map)
    total_native = 0

    packages: List[Dict[str, Any]] = []
    for bucket in pkg_buckets.values():
        if bucket["file_count"] == 0:
            continue
        finalized = _finalize_tree(bucket)
        finalized["percent"] = (
            round(finalized["size"] / total_unzipped * 100, 2)
            if total_unzipped > 0
            else 0.0
        )
        total_native += finalized["native_bytes"]
        packages.append(finalized)

    packages.sort(key=lambda item: item["size"], reverse=True)

    return {
        "total_unzipped_bytes": total_unzipped,
        "quota_limit_bytes": LAMBDA_UNZIPPED_LIMIT_BYTES,
        "quota_percent": round(total_unzipped / LAMBDA_UNZIPPED_LIMIT_BYTES * 100, 2),
        "package_count": len(packages),
        "file_count": total_files,
        "native_binary_bytes": total_native,
        "packages": packages,
    }


def render_treemap_html(
    analysis: Dict[str, Any],
    *,
    zip_bytes: int,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Render a self-contained interactive HTML treemap report with zero external CDN dependencies.

    :param analysis: Output dictionary from :func:`analyze_directory`.
    :param zip_bytes: Compressed `.zip` archive size in bytes.
    :param metadata: Optional build metadata (`runtime`, `architecture`, `engine`, etc.).
    :return: Complete HTML5 document as a UTF-8 string.
    """
    payload = {
        "analysis": analysis,
        "zip_bytes": zip_bytes,
        "metadata": metadata or {},
    }
    safe_json = (
        json.dumps(payload, separators=(",", ":"))
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )

    return _HTML_TEMPLATE.replace("__LAYER_DATA_JSON__", safe_json)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lambda Layer Size Analysis</title>
<style>
  :root {
    --bg: #0b1020;
    --surface: #121931;
    --surface-2: #182242;
    --border: #26335c;
    --text: #e6ecff;
    --muted: #93a3c8;
    --accent: #6366f1;
    --ok: #10b981;
    --warn: #f59e0b;
    --danger: #f43f5e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px;
    background: var(--bg);
    color: var(--text);
    font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.45;
  }
  .container { max-width: 1280px; margin: 0 auto; }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 20px;
  }
  h1 { margin: 0; font-size: 1.45rem; font-weight: 700; letter-spacing: -0.02em; }
  .subtitle { color: var(--muted); font-size: 0.88rem; margin-top: 2px; }
  .badges { display: flex; flex-wrap: wrap; gap: 8px; }
  .badge {
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 18px;
    margin-bottom: 16px;
  }
  .quota-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 8px;
    font-size: 0.9rem;
  }
  .progress-track {
    width: 100%;
    height: 12px;
    background: var(--surface-2);
    border-radius: 999px;
    overflow: hidden;
    border: 1px solid var(--border);
  }
  .progress-fill { height: 100%; transition: width 0.25s ease; }
  .kpis {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
  }
  .kpi-label { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }
  .kpi-value { font-size: 1.3rem; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }
  .kpi-sub { color: var(--muted); font-size: 0.78rem; margin-top: 2px; }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
  }
  .breadcrumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 0.88rem; }
  .crumb {
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 4px 10px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.82rem;
  }
  .crumb:hover { border-color: var(--accent); }
  .crumb.active { background: var(--accent); border-color: var(--accent); cursor: default; }
  .search-input {
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 12px;
    border-radius: 8px;
    font-size: 0.86rem;
    min-width: 240px;
    outline: none;
  }
  .search-input:focus { border-color: var(--accent); }
  #treemap {
    position: relative;
    width: 100%;
    height: 460px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  .tile {
    position: absolute;
    border: 1px solid rgba(11, 16, 32, 0.75);
    padding: 6px 8px;
    overflow: hidden;
    cursor: pointer;
    transition: filter 0.12s ease, opacity 0.12s ease;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }
  .tile:hover { filter: brightness(1.14); z-index: 2; }
  .tile.dimmed { opacity: 0.25; }
  .tile-title {
    font-size: 0.8rem;
    font-weight: 600;
    color: #ffffff;
    text-shadow: 0 1px 2px rgba(0,0,0,0.55);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tile-size {
    font-size: 0.72rem;
    color: rgba(255,255,255,0.88);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  #tooltip {
    position: fixed;
    pointer-events: none;
    background: rgba(11, 16, 32, 0.96);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 0.8rem;
    color: var(--text);
    box-shadow: 0 10px 28px rgba(0,0,0,0.45);
    z-index: 50;
    display: none;
    max-width: 320px;
  }
  .tt-name { font-weight: 700; margin-bottom: 4px; word-break: break-word; }
  .tt-row { display: flex; justify-content: space-between; gap: 16px; color: var(--muted); }
  .tt-row strong { color: var(--text); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); }
  th {
    color: var(--muted);
    font-weight: 600;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    cursor: pointer;
    user-select: none;
  }
  th:hover { color: var(--text); }
  tbody tr { cursor: pointer; }
  tbody tr:hover { background: var(--surface-2); }
  .num { text-align: right; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .bar-cell { width: 120px; }
  .mini-bar { height: 7px; background: var(--surface-2); border-radius: 999px; overflow: hidden; }
  .mini-bar > div { height: 100%; background: var(--accent); }
  .empty-state {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--muted);
    font-size: 0.9rem;
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>Lambda Layer Size Analysis</h1>
      <div class="subtitle" id="subtitle">Generated by lambda_deps_builder</div>
    </div>
    <div class="badges" id="badges"></div>
  </header>

  <section class="card">
    <div class="quota-header">
      <span><strong>250 MB AWS Lambda Unzipped Quota</strong></span>
      <span id="quota-summary"></span>
    </div>
    <div class="progress-track">
      <div class="progress-fill" id="quota-bar"></div>
    </div>
  </section>

  <section class="kpis" id="kpis"></section>

  <section class="card">
    <div class="toolbar">
      <div class="breadcrumbs" id="breadcrumbs"></div>
      <input class="search-input" id="search" type="search" placeholder="Filter packages or files...">
    </div>
    <div id="treemap"></div>
  </section>

  <section class="card">
    <div class="toolbar" style="margin-bottom: 6px;">
      <strong>Package Breakdown</strong>
      <span class="subtitle" id="table-count"></span>
    </div>
    <div style="overflow-x: auto;">
      <table>
        <thead>
          <tr>
            <th data-sort="name">Package</th>
            <th data-sort="version">Version</th>
            <th class="num" data-sort="size">Unzipped</th>
            <th class="num" data-sort="percent">% Layer</th>
            <th class="num" data-sort="file_count">Files</th>
            <th class="num" data-sort="native_bytes">Native (.so)</th>
            <th class="bar-cell">Share</th>
          </tr>
        </thead>
        <tbody id="pkg-tbody"></tbody>
      </table>
    </div>
  </section>
</div>

<div id="tooltip"></div>

<script id="layer-data" type="application/json">__LAYER_DATA_JSON__</script>
<script>
(function () {
  "use strict";
  const payload = JSON.parse(document.getElementById("layer-data").textContent);
  const analysis = payload.analysis;
  const zipBytes = payload.zip_bytes || 0;
  const meta = payload.metadata || {};

  const PALETTE = [
    "#4f46e5", "#0891b2", "#059669", "#d97706", "#db2777",
    "#7c3aed", "#2563eb", "#0d9488", "#ea580c", "#9333ea"
  ];

  function fmtBytes(bytes) {
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    const val = bytes / Math.pow(1024, i);
    return (i === 0 ? val : val.toFixed(2)) + " " + units[i];
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // Header metadata
  const badgesEl = document.getElementById("badges");
  const badgePairs = [
    ["arch", meta.architecture],
    ["runtime", meta.runtime],
    ["engine", meta.engine],
    ["build", meta.build_duration_sec !== undefined ? meta.build_duration_sec + "s" : null]
  ];
  badgePairs.forEach(function (pair) {
    if (pair[1]) {
      badgesEl.appendChild(el("span", "badge", pair[0] + ": " + pair[1]));
    }
  });
  if (meta.object_key) {
    document.getElementById("subtitle").textContent = "Artifact: " + meta.object_key;
  }

  // Quota bar
  const pct = Math.min(100, analysis.quota_percent || 0);
  const quotaBar = document.getElementById("quota-bar");
  quotaBar.style.width = pct + "%";
  quotaBar.style.background = pct > 85 ? "var(--danger)" : pct >= 60 ? "var(--warn)" : "var(--ok)";
  const remaining = Math.max(0, analysis.quota_limit_bytes - analysis.total_unzipped_bytes);
  document.getElementById("quota-summary").textContent =
    fmtBytes(analysis.total_unzipped_bytes) + " / " + fmtBytes(analysis.quota_limit_bytes) +
    " (" + analysis.quota_percent + "% used, " + fmtBytes(remaining) + " headroom)";

  // KPIs
  const kpisEl = document.getElementById("kpis");
  const compRatio = analysis.total_unzipped_bytes > 0
    ? ((1 - zipBytes / analysis.total_unzipped_bytes) * 100).toFixed(1) + "% smaller zipped"
    : "n/a";
  const topPkg = analysis.packages[0];
  const kpis = [
    ["Unzipped Size", fmtBytes(analysis.total_unzipped_bytes), analysis.quota_percent + "% of 250 MB limit"],
    ["Zipped Size", fmtBytes(zipBytes), compRatio],
    ["Packages", String(analysis.package_count), analysis.file_count + " total files"],
    ["Native Binaries", fmtBytes(analysis.native_binary_bytes), ".so / .pyd / .dll"],
    ["Largest Package", topPkg ? topPkg.name : "-", topPkg ? fmtBytes(topPkg.size) + " (" + topPkg.percent + "%)" : ""]
  ];
  kpis.forEach(function (item) {
    const card = el("div", "card");
    card.style.marginBottom = "0";
    card.appendChild(el("div", "kpi-label", item[0]));
    card.appendChild(el("div", "kpi-value", item[1]));
    card.appendChild(el("div", "kpi-sub", item[2]));
    kpisEl.appendChild(card);
  });

  // Navigation & search state
  const rootNode = {
    name: "All Packages",
    size: analysis.total_unzipped_bytes,
    file_count: analysis.file_count,
    native_bytes: analysis.native_binary_bytes,
    children: analysis.packages
  };
  let pathStack = [rootNode];
  let searchQuery = "";
  let sortCol = "size";
  let sortAsc = false;

  // Squarified treemap layout
  function squarify(items, x, y, w, h) {
    const valid = items.filter(function (d) { return d.size > 0; });
    const total = valid.reduce(function (s, d) { return s + d.size; }, 0);
    if (!valid.length || total <= 0 || w <= 0 || h <= 0) return [];

    const rects = [];
    const areaScale = (w * h) / total;
    const scaled = valid.map(function (d) {
      return { node: d, area: d.size * areaScale };
    });

    function worstAspect(row, side) {
      if (!row.length || side <= 0) return Infinity;
      const sum = row.reduce(function (s, r) { return s + r.area; }, 0);
      let maxA = 0, minA = Infinity;
      for (let i = 0; i < row.length; i++) {
        if (row[i].area > maxA) maxA = row[i].area;
        if (row[i].area < minA) minA = row[i].area;
      }
      const s2 = sum * sum;
      const w2 = side * side;
      return Math.max((w2 * maxA) / s2, s2 / (w2 * minA));
    }

    let cx = x, cy = y, cw = w, ch = h;
    let row = [];
    let idx = 0;

    function layoutRow(r) {
      const sum = r.reduce(function (s, it) { return s + it.area; }, 0);
      if (cw >= ch) {
        const rw = sum / ch;
        let ry = cy;
        for (let i = 0; i < r.length; i++) {
          const rh = r[i].area / rw;
          rects.push({ node: r[i].node, x: cx, y: ry, w: rw, h: rh });
          ry += rh;
        }
        cx += rw;
        cw -= rw;
      } else {
        const rh = sum / cw;
        let rx = cx;
        for (let i = 0; i < r.length; i++) {
          const rw = r[i].area / rh;
          rects.push({ node: r[i].node, x: rx, y: cy, w: rw, h: rh });
          rx += rw;
        }
        cy += rh;
        ch -= rh;
      }
    }

    while (idx < scaled.length) {
      const item = scaled[idx];
      const side = Math.min(cw, ch);
      const nextRow = row.concat([item]);
      if (!row.length || worstAspect(nextRow, side) <= worstAspect(row, side)) {
        row = nextRow;
        idx++;
      } else {
        layoutRow(row);
        row = [];
      }
    }
    if (row.length) layoutRow(row);
    return rects;
  }

  const tooltip = document.getElementById("tooltip");
  function showTooltip(ev, node) {
    tooltip.textContent = "";
    const title = el("div", "tt-name", node.name + (node.version ? " v" + node.version : ""));
    tooltip.appendChild(title);
    const layerPct = analysis.total_unzipped_bytes > 0
      ? ((node.size / analysis.total_unzipped_bytes) * 100).toFixed(2) + "%"
      : "0%";
    const quotaPct = ((node.size / analysis.quota_limit_bytes) * 100).toFixed(2) + "%";
    const rows = [
      ["Size", fmtBytes(node.size)],
      ["% of Layer", layerPct],
      ["% of 250 MB Quota", quotaPct],
      ["Files", String(node.file_count || 0)],
      ["Native (.so)", fmtBytes(node.native_bytes || 0)]
    ];
    rows.forEach(function (r) {
      const row = el("div", "tt-row");
      row.appendChild(el("span", null, r[0]));
      row.appendChild(el("strong", null, r[1]));
      tooltip.appendChild(row);
    });
    tooltip.style.display = "block";
    moveTooltip(ev);
  }
  function moveTooltip(ev) {
    const pad = 14;
    const tx = Math.min(window.innerWidth - 330, ev.clientX + pad);
    const ty = Math.min(window.innerHeight - 150, ev.clientY + pad);
    tooltip.style.left = Math.max(8, tx) + "px";
    tooltip.style.top = Math.max(8, ty) + "px";
  }
  function hideTooltip() {
    tooltip.style.display = "none";
  }

  function renderBreadcrumbs() {
    const bc = document.getElementById("breadcrumbs");
    bc.textContent = "";
    pathStack.forEach(function (node, idx) {
      const btn = el("button", "crumb" + (idx === pathStack.length - 1 ? " active" : ""), node.name);
      btn.type = "button";
      if (idx < pathStack.length - 1) {
        btn.addEventListener("click", function () {
          pathStack = pathStack.slice(0, idx + 1);
          renderAll();
        });
      }
      bc.appendChild(btn);
    });
  }

  function renderTreemap() {
    const container = document.getElementById("treemap");
    container.textContent = "";
    const current = pathStack[pathStack.length - 1];
    const children = (current.children || []).slice();
    if (!children.length) {
      container.appendChild(el("div", "empty-state", "No sub-items inside " + current.name));
      return;
    }

    const rect = container.getBoundingClientRect();
    const rects = squarify(children, 0, 0, rect.width, rect.height);
    const q = searchQuery.trim().toLowerCase();

    rects.forEach(function (r, idx) {
      const tile = el("div", "tile");
      tile.style.left = r.x + "px";
      tile.style.top = r.y + "px";
      tile.style.width = Math.max(0, r.w) + "px";
      tile.style.height = Math.max(0, r.h) + "px";
      tile.style.background = PALETTE[idx % PALETTE.length];

      if (q && !r.node.name.toLowerCase().includes(q)) {
        tile.classList.add("dimmed");
      }

      if (r.w > 46 && r.h > 24) {
        tile.appendChild(el("div", "tile-title", r.node.name));
        if (r.h > 40) {
          tile.appendChild(el("div", "tile-size", fmtBytes(r.node.size)));
        }
      }

      tile.addEventListener("mouseenter", function (ev) { showTooltip(ev, r.node); });
      tile.addEventListener("mousemove", moveTooltip);
      tile.addEventListener("mouseleave", hideTooltip);
      tile.addEventListener("click", function () {
        if (r.node.children && r.node.children.length > 0) {
          hideTooltip();
          pathStack.push(r.node);
          renderAll();
        }
      });

      container.appendChild(tile);
    });
  }

  function renderTable() {
    const tbody = document.getElementById("pkg-tbody");
    tbody.textContent = "";
    const q = searchQuery.trim().toLowerCase();
    const filtered = analysis.packages.filter(function (p) {
      return !q || p.name.toLowerCase().includes(q);
    });
    filtered.sort(function (a, b) {
      const av = a[sortCol] || "";
      const bv = b[sortCol] || "";
      if (typeof av === "number" && typeof bv === "number") {
        return sortAsc ? av - bv : bv - av;
      }
      return sortAsc
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });

    document.getElementById("table-count").textContent =
      filtered.length + " of " + analysis.packages.length + " packages";

    filtered.forEach(function (pkg) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", null, pkg.name));
      tr.appendChild(el("td", null, pkg.version || "-"));
      tr.appendChild(el("td", "num", fmtBytes(pkg.size)));
      tr.appendChild(el("td", "num", pkg.percent + "%"));
      tr.appendChild(el("td", "num", String(pkg.file_count)));
      tr.appendChild(el("td", "num", fmtBytes(pkg.native_bytes)));

      const barTd = el("td", "bar-cell");
      const miniBar = el("div", "mini-bar");
      const fill = document.createElement("div");
      fill.style.width = Math.min(100, pkg.percent) + "%";
      miniBar.appendChild(fill);
      barTd.appendChild(miniBar);
      tr.appendChild(barTd);

      tr.addEventListener("click", function () {
        if (pkg.children && pkg.children.length > 0) {
          pathStack = [rootNode, pkg];
          renderAll();
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
      tbody.appendChild(tr);
    });
  }

  function renderAll() {
    renderBreadcrumbs();
    renderTreemap();
    renderTable();
  }

  document.getElementById("search").addEventListener("input", function (ev) {
    searchQuery = ev.target.value;
    renderTreemap();
    renderTable();
  });

  document.querySelectorAll("th[data-sort]").forEach(function (th) {
    th.addEventListener("click", function () {
      const col = th.getAttribute("data-sort");
      if (sortCol === col) {
        sortAsc = !sortAsc;
      } else {
        sortCol = col;
        sortAsc = col === "name" || col === "version";
      }
      renderTable();
    });
  });

  window.addEventListener("resize", renderTreemap);
  renderAll();
})();
</script>
</body>
</html>
"""
