# Copyright (c) 2026 AI伙伴计划
# SPDX-License-Identifier: MIT

"""Generic schematic drawing runner — parses design doc markdown tables and drives draw_engine.py.

Usage:
    python scripts/generic_runner.py [design_doc_path] [--dry-run] [--module N]

The design doc must contain structured tables:
  - 附录 C: region coordinate table (x_min, x_max, y_min, y_max)
  - 附录 B: module summary (region assignment)
  - Per-module: 器件清单 (6 cols), IC引脚连线 (4 cols), 无源器件连线 (3 cols)

Workflow (two-phase):
  Phase 1 — Generate Manifest:
    1. Parse design doc → modules
    2. Resolve LCSC IDs
    3. Place ALL ICs on canvas (needed for pin query)
    4. Query all IC pin coordinates from EDA
    5. Compute layout + connections for each module
    6. Save complete manifest JSON (coordinates, nets, wire stubs)
    7. Print human-readable preview

  Phase 2 — Draw:
    1. Read manifest JSON
    2. For each module: place passives + draw wire stubs (ICs auto-deduped)
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from draw_engine import DrawEngine, LayoutCalculator, Region

# Override via environment variable
LIB_UUID = os.environ.get("EDA_LIB_UUID", "your-library-uuid-here")


# ── File utilities ──────────────────────────────────────

def find_design_doc(doc_path: str = None) -> str:
    """Find design document. Searches for *Design*.md or Hardware_Design_*.md."""
    if doc_path and os.path.exists(doc_path):
        return doc_path
    # Search common naming patterns
    for pattern in ["Hardware_Design_*.md", "*Design*.md", "*设计*.md"]:
        candidates = sorted(Path.cwd().glob(pattern))
        if candidates:
            return str(candidates[-1])
    sys.exit("No design document found. Run /hardware-design first.")


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.readlines()


# ── Markdown table parser ───────────────────────────────

def parse_markdown_table(lines: list[str], start_idx: int) -> tuple[list[dict], int]:
    """Parse a markdown table starting at start_idx.

    Returns (rows, next_idx). Each row is {header: value}.
    """
    i = start_idx
    while i < len(lines) and not lines[i].strip().startswith("|"):
        i += 1
    if i >= len(lines):
        return [], i

    header_line = lines[i].strip()
    headers = [h.strip() for h in header_line.strip("|").split("|")]
    i += 1

    if i < len(lines) and re.match(r'^\|[\s\-:|]+\|$', lines[i].strip()):
        i += 1

    rows = []
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        row = {}
        for j, header in enumerate(headers):
            if j < len(cells):
                row[header] = cells[j]
        rows.append(row)
        i += 1

    return rows, i


# ── Section parsers ─────────────────────────────────────

def parse_region_table(lines: list[str]) -> dict[str, Region]:
    """Parse 附录 C region coordinate table → {name: Region}."""
    for i, line in enumerate(lines):
        if "附录 C" in line and "区域坐标" in line:
            rows, _ = parse_markdown_table(lines, i + 1)
            regions = {}
            for row in rows:
                name = row.get("区域", "")
                if name:
                    regions[name] = Region(
                        name=name,
                        x_min=int(row.get("x_min", 0)),
                        x_max=int(row.get("x_max", 0)),
                        y_min=int(row.get("y_min", 0)),
                        y_max=int(row.get("y_max", 0)),
                    )
            return regions
    sys.exit("附录 C (region coordinates) not found in design doc.")


def parse_layout_config(path: str) -> tuple[dict[str, Region], dict[int, str]]:
    """Parse a JSON layout config file.

    Format:
        {
          "regions": {
            "Q1": {"x_min": 50, "x_max": 780, "y_min": 880, "y_max": 1605},
            ...
          },
          "module_regions": {
            "1": "Q1", "2": "Q2", ...
          }
        }

    Returns (regions_dict, module_to_region_map).
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    regions = {}
    for name, r in data.get("regions", {}).items():
        regions[name] = Region(
            name=name,
            x_min=r["x_min"],
            x_max=r["x_max"],
            y_min=r["y_min"],
            y_max=r["y_max"],
        )

    module_regions = {}
    for num_str, region_name in data.get("module_regions", {}).items():
        module_regions[int(num_str)] = region_name

    return regions, module_regions


def parse_module_summary(lines: list[str]) -> dict[int, str]:
    """Parse 附录 B module summary → {module_number: region_name}.
    Used as fallback when no --layout is provided.
    """
    region_map = {}
    for i, line in enumerate(lines):
        if "附录 B" in line and "模块汇总" in line:
            rows, _ = parse_markdown_table(lines, i + 1)
            for row in rows:
                num_str = row.get("#", "")
                region_cell = row.get("单页区域", "")
                if num_str and region_cell:
                    region_map[int(num_str)] = region_cell.split()[0]
            break
    return region_map


def parse_all_modules(lines: list[str]) -> list[dict]:
    """Parse all module sections.

    Returns list of:
        {number, name, region, components, ic_pin_nets, passive_nets}
    """
    modules = []
    current_module = None
    table_context = None

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        m = re.match(r'^###\s+模块\s+(\d+)[：:]\s*(.+)', line)
        if m:
            if current_module:
                modules.append(current_module)
            current_module = {
                "number": int(m.group(1)),
                "name": m.group(2).strip(),
                "components": [],
                "ic_pin_nets": [],
                "passive_nets": [],
            }
            table_context = None
            i += 1
            continue

        if current_module:
            if "器件清单" in line and ("结构化" in line or "供绘制" in line):
                table_context = "components"
                i += 1
                continue
            if "连线清单" in line:
                table_context = None
                i += 1
                continue
            if "IC 引脚连线" in line or "连接器引脚连线" in line:
                table_context = "ic_pins"
                i += 1
                continue
            if "无源器件连线" in line:
                table_context = "passive_nets"
                i += 1
                continue
            if "测试点连线" in line:
                table_context = "tp_nets"
                i += 1
                continue

            if table_context and line.strip().startswith("|") and not re.match(r'^\|[\s\-:]+\|$', line.strip()):
                rows, next_i = parse_markdown_table(lines, i)

                if table_context == "components":
                    for row in rows:
                        des = row.get("位号", "")
                        if des:
                            current_module["components"].append({
                                "des": des,
                                "uuid": row.get("LCSC UUID", ""),
                                "package": row.get("封装", ""),
                                "value": row.get("值", ""),
                                "type": row.get("类型", "OTHER"),
                                "side": row.get("side", "right"),
                            })

                elif table_context == "ic_pins":
                    for row in rows:
                        pin_ref = row.get("器件引脚", "")
                        net = row.get("Net名", "")
                        if pin_ref and net and "." in pin_ref:
                            ic_des, pin_name = pin_ref.split(".", 1)
                            current_module["ic_pin_nets"].append({
                                "ic_des": ic_des,
                                "pin_name": pin_name,
                                "net": net,
                            })

                elif table_context == "passive_nets":
                    for row in rows:
                        des = row.get("位号", "")
                        p1n = row.get("Pin1 Net", "")
                        p2n = row.get("Pin2 Net", "")
                        if des:
                            current_module["passive_nets"].append({
                                "des": des,
                                "pin1_net": p1n,
                                "pin2_net": p2n,
                            })

                elif table_context == "tp_nets":
                    for row in rows:
                        des = row.get("位号", "")
                        net = row.get("Net名", "")
                        if des:
                            current_module["passive_nets"].append({
                                "des": des,
                                "pin1_net": net,
                                "pin2_net": "",
                            })

                table_context = None
                i = next_i
                continue

        i += 1

    if current_module:
        modules.append(current_module)

    return modules


# ── Data utilities ──────────────────────────────────────

def is_valid_device_uuid(val: str) -> bool:
    return bool(val) and len(val) >= 32 and not val.startswith("C")


def collect_lcsc_ids(modules: list[dict]) -> list[str]:
    ids = set()
    for mod in modules:
        for c in mod["components"]:
            uid = c.get("uuid", "")
            if uid and uid.startswith("C"):
                ids.add(uid)
    return list(ids)


def adapt_to_module_def(mod: dict) -> dict:
    """Convert parsed module to compute_module() format.

    - Splits components by type (IC/CONN/passive/TP)
    - ic_pin_nets → netlist (combine ic_des+pin_name, _generate_connections resolves name→number)
    - passive_nets (pin1/pin2) → [{des, net, side}, ...]
    """
    components = mod.get("components", [])

    ics = []
    conns = []
    tps = []
    passives = []

    for c in components:
        ctype = c.get("type", "OTHER")
        entry = {"des": c["des"], "uuid": c["uuid"], "lib_uuid": c.get("lib_uuid", "")}
        if ctype == "IC":
            ics.append(entry)
        elif ctype == "CONN":
            conns.append(entry)
        elif ctype == "TP":
            tps.append(entry)
        else:
            entry["type"] = ctype
            entry["side"] = c.get("side", "right")
            passives.append(entry)

    netlist = []
    for entry in mod.get("ic_pin_nets", []):
        netlist.append({
            "pin": f"{entry['ic_des']}.{entry['pin_name']}",
            "net": entry["net"],
        })

    passive_nets_out = []
    for entry in mod.get("passive_nets", []):
        des = entry["des"]
        p1n = entry.get("pin1_net", "")
        p2n = entry.get("pin2_net", "")
        # Handle compound designators like "R3+R3b" → split into individual entries
        des_list = [d.strip() for d in des.split("+")] if "+" in des else [des]
        for single_des in des_list:
            if p1n:
                if p1n.startswith("(") or p1n.startswith("（"):
                    print(f"  [ERROR] M{mod['number']}: {single_des} pin1_net='{p1n}' — "
                          f"net名使用括号视为注释，请改为实际net名")
                else:
                    passive_nets_out.append({"des": single_des, "net": p1n, "side": "left"})
            if p2n:
                if p2n.startswith("(") or p2n.startswith("（"):
                    print(f"  [ERROR] M{mod['number']}: {single_des} pin2_net='{p2n}' — "
                          f"net名使用括号视为注释，请改为实际net名")
                else:
                    passive_nets_out.append({"des": single_des, "net": p2n, "side": "right"})

    # ── Cross-validation: BOM passives vs passive_nets ──
    passive_des_from_bom = {c["des"] for c in passives}
    passive_des_from_nets = {e["des"] for e in passive_nets_out}
    missing_nets = passive_des_from_bom - passive_des_from_nets
    if missing_nets:
        print(f"  [WARN] M{mod['number']}: 器件清单中有但无源连线表缺失: "
              f"{sorted(missing_nets)} — 放置后将无出线")

    return {
        "ics": ics,
        "passives": passives,
        "connectors": conns,
        "test_points": tps,
        "netlist": netlist,
        "passive_nets": passive_nets_out,
        "power_nets": [],
    }


# ── Phase 1: Manifest generation ────────────────────────

def generate_manifest(modules: list[dict], regions: dict[str, Region],
                      region_map: dict[int, str], engine: DrawEngine,
                      calc: LayoutCalculator) -> dict:
    """Generate complete layout manifest (coordinates + nets + wires) for all modules.

    Steps:
      1. Resolve LCSC IDs
      2. Compute layout positions for all modules
      3. Place ALL ICs on canvas → query pin coordinates from EDA
      4. Re-generate wire connections per module with correct pin data
      5. Assemble and return the master manifest dict
    """

    # ── Resolve LCSC ──
    all_lcsc = collect_lcsc_ids(modules)
    print(f"Resolving {len(all_lcsc)} unique LCSC IDs...")
    lcsc_map = engine.resolve_lcsc_ids(all_lcsc) if all_lcsc else {}

    unresolved = []
    for mod in modules:
        for c in mod["components"]:
            uid = c.get("uuid", "")
            if uid in lcsc_map:
                info = lcsc_map[uid]
                c["uuid"] = info["uuid"]
                c["lib_uuid"] = info.get("libraryUuid", "")
            elif uid and uid.startswith("C"):
                unresolved.append(f"{c['des']}:{uid}")
    print(f"Resolved: {len(lcsc_map)}/{len(all_lcsc)}")
    if unresolved:
        print(f"  [WARN] Unresolved LCSC: {unresolved}")

    # ── Step 1: Compute layout positions for all modules ──
    staged = []
    for mod in modules:
        region_name = region_map.get(mod["number"], "")
        region = regions.get(region_name)
        if not region:
            print(f"  [WARN] M{mod['number']} has no region, skipping")
            continue

        # Filter components with invalid UUIDs
        valid_comps = [c for c in mod["components"] if is_valid_device_uuid(c["uuid"])]
        skipped = [c for c in mod["components"] if not is_valid_device_uuid(c["uuid"])]
        if skipped:
            print(f"  M{mod['number']}: skipping {[c['des'] for c in skipped]} (no valid UUID)")
        mod["components"] = valid_comps

        if not valid_comps:
            print(f"  M{mod['number']}: no valid components, skipping")
            continue

        module_def = adapt_to_module_def(mod)
        all_chip_designators = [c["des"] for c in (
            module_def["ics"] + module_def["connectors"])]
        try:
            manifest = calc.compute_module(module_def, region, all_chip_designators)
        except Exception as e:
            print(f"  [INFO] M{mod['number']}: compute_module pin query skipped "
                  f"(will re-generate in step 4)")
            manifest = calc.compute_placement_only(module_def, region)

        staged.append({
            "number": mod["number"],
            "name": mod["name"],
            "region": region_name,
            "module_def": module_def,
            "all_chip_designators": all_chip_designators,
            "manifest": manifest,
        })

    # ── Step 2: Place chips with estimated heights, query actual heights ──
    all_chips = []
    for s in staged:
        all_chips.extend(s["manifest"]["ics"])
        for p in s["manifest"]["passives"]:
            if p["des"] in [c["des"] for c in s["module_def"]["connectors"]]:
                all_chips.append(p)
    if all_chips:
        print(f"\nPass 1: placing {len(all_chips)} ICs/connectors (rough heights)...")
        engine.place_components(all_chips)

    all_designators = list(set(
        d for s in staged for d in s["all_chip_designators"]))
    pin_maps_pass1 = engine.query_all_ic_pins(all_designators) if all_designators else {}

    # Compute actual heights from pin Y ranges
    actual_heights = {}
    for des, pins in pin_maps_pass1.items():
        if pins:
            ys = [p["y"] for p in pins]
            actual_heights[des] = calc.snap(max(ys) - min(ys) + 20)
    if actual_heights:
        for des, h in actual_heights.items():
            est = calc._height_for("IC")
            if abs(h - est) > 20:
                print(f"  Height corrected: {des} {est}→{h}")

    # ── Step 3: Re-layout with actual heights ──
    if actual_heights:
        print("Pass 2: re-computing layout with actual IC heights...")
        for s in staged:
            for ic_entry in s["module_def"]["ics"]:
                if ic_entry["des"] in actual_heights:
                    ic_entry["height"] = actual_heights[ic_entry["des"]]
            for conn_entry in s["module_def"]["connectors"]:
                if conn_entry["des"] in actual_heights:
                    conn_entry["height"] = actual_heights[conn_entry["des"]]

        # Delete all components AND wires, then re-place at corrected positions.
        # Wires must be cleared too — old wires at wrong positions would otherwise
        # persist and cause duplicate net labels on the same pins.
        engine._exec(
            "var cs=await eda.sch_PrimitiveComponent.getAll(undefined,true);"
            "for(var i=0;i<cs.length;i++){try{await eda.sch_PrimitiveComponent.delete(cs[i].primitiveId||cs[i].uuid)}catch(e){}}"
            "var ws=await eda.sch_PrimitiveWire.getAll(undefined,true);"
            "for(var j=0;j<ws.length;j++){try{await eda.sch_PrimitiveWire.delete(ws[j].primitiveId||ws[j].uuid)}catch(e){}}"
            "return 'ok';"
        )

        for s in staged:
            s["manifest"] = calc.compute_placement_only(s["module_def"],
                                                         regions[s["region"]])
        all_to_place = []
        for s in staged:
            all_to_place.extend(s["manifest"]["ics"])
            all_to_place.extend(s["manifest"]["passives"])
        if all_to_place:
            print(f"  Re-placing {len(all_to_place)} components at corrected positions...")
            engine.place_components(all_to_place)

    # ── Step 4: Query pins at final positions, generate connections ──
    all_passive_designators = list(set(
        d for s in staged for d in [p["des"] for p in s["module_def"].get("passives", [])]))
    all_query_designators = list(set(all_designators + all_passive_designators))
    pin_maps = engine.query_all_ic_pins(all_query_designators) if all_query_designators else {}
    if pin_maps:
        total_pins = sum(len(v) for v in pin_maps.values())
        print(f"Queried {total_pins} pins from {len(pin_maps)} components")

    for s in staged:
        wires, _, _ = calc._generate_connections(
            s["module_def"], pin_maps,
            s["manifest"]["ics"],
            s["manifest"]["passives"],
            []
        )
        s["manifest"]["wires"] = wires
        s["manifest"]["ports"] = []
        s["manifest"]["flags"] = []

    # ── Step 5: Assemble master manifest ──
    modules_out = []
    for s in staged:
        m = s["manifest"]
        modules_out.append({
            "number": s["number"],
            "name": s["name"],
            "region": s["region"],
            "ics": m["ics"],
            "passives": m["passives"],
            "wires": m["wires"],
            "ports": m["ports"],
            "flags": m["flags"],
        })

    return {
        "modules": modules_out,
        "unresolved_lcsc": unresolved,
    }


def print_manifest_preview(manifest: dict):
    """Print human-readable layout preview."""
    print(f"\n{'=' * 60}")
    print(f"  LAYOUT MANIFEST PREVIEW")
    print(f"{'=' * 60}")

    for mod in manifest["modules"]:
        m_ics = mod["ics"]
        m_passives = [p for p in mod["passives"] if "type" not in p or p.get("type") != "TP"]
        m_tps = [p for p in mod["passives"] if p.get("type") == "TP"]
        m_wires = mod["wires"]
        total = len(m_ics) + len(m_passives) + len(m_tps)

        print(f"\n  Module {mod['number']}: {mod['name']} → {mod['region']}")
        print(f"  Components: {total} total "
              f"(ICs: {len(m_ics)}, Passives: {len(m_passives)}, TPs: {len(m_tps)})")
        print(f"  Wires: {len(m_wires)}")

        if m_ics:
            print(f"  ICs:")
            for c in m_ics:
                print(f"    {c['des']:6s} @ ({c['x']:4d}, {c['y']:4d})")

        if m_passives:
            print(f"  Passives:")
            for c in m_passives[:8]:
                print(f"    {c['des']:6s} @ ({c['x']:4d}, {c['y']:4d})")
            if len(m_passives) > 8:
                print(f"    ... +{len(m_passives) - 8} more")

        if m_tps:
            print(f"  Test Points:")
            for c in m_tps:
                print(f"    {c['des']:6s} @ ({c['x']:4d}, {c['y']:4d})")

        if m_wires:
            print(f"  Sample wires:")
            for w in m_wires[:5]:
                print(f"    ({w['x1']}, {w['y1']}) → ({w['x2']}, {w['y2']})  {w['net']}")
            if len(m_wires) > 5:
                print(f"    ... +{len(m_wires) - 5} more")

    total_ics = sum(len(m["ics"]) for m in manifest["modules"])
    total_passives = sum(len([p for p in m["passives"] if p.get("type") != "TP"]) for m in manifest["modules"])
    total_tps = sum(len([p for p in m["passives"] if p.get("type") == "TP"]) for m in manifest["modules"])
    total_wires = sum(len(m["wires"]) for m in manifest["modules"])

    print(f"\n{'=' * 60}")
    print(f"  TOTALS: {len(manifest['modules'])} modules, "
          f"{total_ics} ICs, {total_passives} passives, {total_tps} TPs, "
          f"{total_wires} wires")
    if manifest.get("unresolved_lcsc"):
        print(f"  UNRESOLVED: {manifest['unresolved_lcsc']}")
    print(f"{'=' * 60}")


# ── Phase 2: Draw from manifest ─────────────────────────

def draw_from_manifest(manifest: dict, engine: DrawEngine,
                       calc: LayoutCalculator, module_filter: int = None):
    """Execute drawing for all modules from a pre-generated manifest."""
    for mod in manifest["modules"]:
        if module_filter and mod["number"] != module_filter:
            continue

        print(f"\n{'=' * 60}")
        print(f"  Module {mod['number']}: {mod['name']} → {mod['region']}")

        total = len(mod["ics"]) + len(mod["passives"])
        print(f"  Components: {total} total "
              f"(ICs: {len(mod['ics'])}, Passives: {len(mod['passives'])}), "
              f"Wires: {len(mod['wires'])}")

        try:
            result = engine.draw_module(mod, {})

            placed = (len(result.get("placed_ics", [])) +
                       len(result.get("placed_passives", [])))
            wires = result.get("wires", 0)
            missing = result.get("missing", [])

            print(f"  Result: {placed} placed, {wires} wires", end="")
            if missing:
                print(f", MISSING: {missing}", end="")
            print()

            engine.save()
        except Exception as e:
            print(f"  [ERROR] M{mod['number']}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print("  ALL MODULES COMPLETE")


def mark_unconnected_pins(engine: DrawEngine):
    """Detect unconnected pins and mark them No-Connect (收尾阶段).

    Runs after drawing when --mark-nc is set. Delegates to draw_engine.py's
    built-in detect → mark → verify.
    """
    print("\n" + "=" * 60)
    print("  NO-CONNECT MARKING")

    unconnected = engine.detect_unconnected_pins()
    if not unconnected:
        print("  No unconnected pins detected.")
        return

    total = sum(len(v) for v in unconnected.values())
    print(f"  Detected {total} unconnected pins across {len(unconnected)} ICs")

    result = engine.mark_no_connect(unconnected)
    print(f"  Marked: {result.get('marked', 0)}, Failed: {result.get('failed', 0)}")

    verify = engine.verify_no_connect(list(unconnected.keys()))
    print(f"  Verified NC: {verify.get('total_nc', 0)} pins")

    engine.save()
    print("=" * 60)


# ── Main ────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Generic schematic drawing runner")
    ap.add_argument("doc", nargs="?", help="Path to design doc (default: auto-search)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Generate manifest JSON only, do not draw")
    ap.add_argument("--module", type=int, help="Process only specific module number")
    ap.add_argument("--layout", help="Path to layout config JSON (regions + module→region map)")
    ap.add_argument("--from-manifest", type=str,
                    help="Draw directly from a pre-generated manifest JSON (skip Phase 1)")
    ap.add_argument("--mark-nc", action="store_true",
                    help="After drawing, auto-detect and mark unconnected pins as No-Connect")
    ap.add_argument("--manifest-out", type=str, default="layout_manifest.json",
                    help="Output path for generated manifest JSON (default: layout_manifest.json)")
    ap.add_argument("--lib-uuid", type=str, default=LIB_UUID,
                    help=f"EDA library UUID (default: {LIB_UUID})")
    ap.add_argument("--bridge-url", type=str, default="http://localhost:49620/execute",
                    help="Bridge server URL (default: http://localhost:49620/execute)")
    args = ap.parse_args()

    lib_uuid = args.lib_uuid
    bridge_url = args.bridge_url

    # ── Mode: Draw from existing manifest ──
    if args.from_manifest:
        if not os.path.exists(args.from_manifest):
            sys.exit(f"Manifest not found: {args.from_manifest}")
        with open(args.from_manifest, encoding="utf-8") as f:
            manifest = json.load(f)
        print(f"Manifest: {args.from_manifest}")
        print(f"Modules: {len(manifest['modules'])}")
        print_manifest_preview(manifest)

        engine = DrawEngine(lib_uuid=lib_uuid, bridge_url=bridge_url)
        calc = LayoutCalculator(engine)
        draw_from_manifest(manifest, engine, calc, args.module)
        if args.mark_nc:
            mark_unconnected_pins(engine)
        return

    # ── Mode: Generate manifest (and optionally draw) ──
    doc_path = find_design_doc(args.doc)
    print(f"Design doc: {doc_path}")
    lines = read_lines(doc_path)

    # Layout: prefer --layout JSON, fallback to design doc 附录 C + 附录 B
    if args.layout:
        regions, region_map = parse_layout_config(args.layout)
        print(f"Layout: {args.layout}")
    else:
        regions = parse_region_table(lines)
        region_map = parse_module_summary(lines)
    print(f"Regions: {len(regions)} ({', '.join(regions.keys())})")

    modules = parse_all_modules(lines)
    for mod in modules:
        mod["region"] = region_map.get(mod["number"], "")

    print(f"Modules: {len(modules)}")
    for mod in modules:
        print(f"  M{mod['number']}: {mod['name']} → {mod['region']} "
              f"({len(mod['components'])} comps, "
              f"{len(mod['ic_pin_nets'])} IC pins, "
              f"{len(mod['passive_nets'])} passive nets)")

    if args.module:
        modules = [m for m in modules if m["number"] == args.module]
        if not modules:
            sys.exit(f"Module {args.module} not found.")

    # ── Phase 1: Generate manifest ──
    engine = DrawEngine(lib_uuid=lib_uuid, bridge_url=bridge_url)
    calc = LayoutCalculator(engine)

    manifest = generate_manifest(modules, regions, region_map, engine, calc)
    print_manifest_preview(manifest)

    # Save manifest to JSON
    manifest_path = args.manifest_out
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"\nManifest saved to: {manifest_path}")

    if args.dry_run:
        print("\n[Dry run] Manifest generated. Run without --dry-run to draw, "
              "or use --from-manifest to draw from this file later.")
        return

    # ── Phase 2: Draw ──
    print("\nDrawing from manifest...")
    draw_from_manifest(manifest, engine, calc, args.module)
    if args.mark_nc:
        mark_unconnected_pins(engine)


if __name__ == "__main__":
    main()
