# Copyright (c) 2026 AI伙伴计划
# SPDX-License-Identifier: MIT

"""Generic schematic drawing engine — batch-executes from structured manifest.

Replaces per-component/wire bridge calls with batch JS blocks.
Typical module: 4 bridge calls (vs ~100 in sub-agent approach).

Usage:
    engine = DrawEngine(lib_uuid="<your-library-uuid>")
    layout = LayoutCalculator(region_bounds, existing_placements)
    result = engine.draw_module(manifest, layout)
"""

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# ── Configuration ────────────────────────────────────────
# Override via environment variable or pass explicitly to DrawEngine
DEFAULT_LIB_UUID = os.environ.get("EDA_LIB_UUID", "your-library-uuid-here")
DEFAULT_BRIDGE_URL = os.environ.get("EDA_BRIDGE_URL", "http://localhost:49620/execute")

# ── Bridge ──────────────────────────────────────────────

class BridgeError(Exception):
    pass


class DrawEngine:
    """Batch drawing via EDA bridge."""

    def __init__(self, lib_uuid: str = None, bridge_url: str = None):
        self.lib = lib_uuid or DEFAULT_LIB_UUID
        self.bridge = bridge_url or DEFAULT_BRIDGE_URL

    def _exec(self, code: str, timeout: int = 120) -> dict:
        """Single bridge call. code must contain `return ...`.

        Raises BridgeError on HTTP/network failure.
        NOTE: bridge has internal 30s timeout. If bridge times out,
        the EDA operation MAY have still completed. Caller must verify.
        """
        data = json.dumps({"code": code}).encode()
        req = urllib.request.Request(self.bridge, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            raise BridgeError(f"Bridge call failed: {e}") from e

    def get_placed_designators(self) -> set[str]:
        """Query EDA for all placed designators (excluding '?' and sheet)."""
        result = self._exec(
            "var all=await eda.sch_PrimitiveComponent.getAll(undefined,true);"
            "return JSON.stringify(all.filter(function(c){return c.designator&&c.designator!=='?'&&c.componentType!=='sheet';}).map(function(c){return c.designator;}));"
        )
        return set(json.loads(result["result"]))

    def filter_duplicates(self, components: list[dict]) -> tuple[list[dict], list[str]]:
        """Filter out components whose designator already exists in EDA.

        Returns (new_components, skipped_designators).
        Always call before place_components() to prevent duplicates.
        """
        existing = self.get_placed_designators()
        new = []
        skipped = []
        for c in components:
            if c["des"] in existing:
                skipped.append(c["des"])
            else:
                new.append(c)
        return new, skipped

    def resolve_lcsc_ids(self, lcsc_ids: list[str]) -> dict[str, dict]:
        """Resolve LCSC C-numbers to device UUIDs via EDA library API.

        Returns {C_number: {uuid: str, libraryUuid: str}}.
        """
        if not lcsc_ids:
            return {}
        valid = [cid for cid in lcsc_ids if cid and cid.startswith("C")]
        if not valid:
            return {}
        batch_size = 50
        result_map = {}
        for i in range(0, len(valid), batch_size):
            batch = valid[i:i + batch_size]
            js = (
                f"var devices=await eda.lib_Device.getByLcscIds("
                f"{json.dumps(batch)},undefined,true);"
                f"var map={{}};"
                f"for(var j=0;j<devices.length;j++){{"
                f"if(devices[j]&&devices[j].supplierId){{"
                f"map[devices[j].supplierId]={{uuid:devices[j].uuid,libraryUuid:devices[j].libraryUuid||''}};"
                f"}}"
                f"}}"
                f"return JSON.stringify(map);"
            )
            result = self._exec(js)
            batch_map = json.loads(result["result"])
            result_map.update(batch_map)
        return result_map

    def verify_placement(self, designators: list[str]) -> tuple[list[str], list[str]]:
        """Post-placement check: which designators are on the canvas.

        Returns (present, missing).
        Call after place_components() to confirm all components landed,
        especially after a BridgeError/timeout.
        """
        existing = self.get_placed_designators()
        present = [d for d in designators if d in existing]
        missing = [d for d in designators if d not in existing]
        return present, missing

    # ── Component operations ────────────────────────────

    def place_components(self, components: list[dict], skip_duplicates: bool = True,
                         batch_size: int = 20) -> list[str]:
        """Batch place components + set designators. Returns list of primitiveIds.

        Each component dict: {des, uuid, x, y, rot?, lib_uuid?}

        If skip_duplicates=True (default), auto-filters designators already on canvas.
        Batched internally to avoid bridge timeout on large placements.
        """
        if not components:
            return []

        # Pre-placement dedup
        placed = components
        if skip_duplicates:
            placed, skipped = self.filter_duplicates(components)
            if skipped:
                import sys
                print(f"  [dedup] skipping already-placed: {skipped}", file=sys.stderr)
        if not placed:
            return []

        all_ids = []
        for batch_start in range(0, len(placed), batch_size):
            batch = placed[batch_start:batch_start + batch_size]
            js_lines = []
            var_names = []
            for i, c in enumerate(batch):
                var = f"c{i}"
                var_names.append(var)
                rot = c.get("rot", 0)
                lib = c.get("lib_uuid", self.lib)
                js_lines.append(
                    f"var {var}=await eda.sch_PrimitiveComponent.create("
                    f"{{libraryUuid:'{lib}',uuid:'{c['uuid']}'}},{c['x']},{c['y']},{rot});"
                )
            for i, c in enumerate(batch):
                var = var_names[i]
                js_lines.append(
                    f"var d{i}=await eda.sch_PrimitiveComponent.get({var}.primitiveId);"
                    f"d{i}.setState_Designator('{c['des']}');"
                    f"await d{i}.done();"
                )
            js_lines.append(
                f"return JSON.stringify([" +
                ",".join(f"{v}.primitiveId" for v in var_names) +
                "]);"
            )
            result = self._exec("".join(js_lines))
            all_ids.extend(json.loads(result["result"]))
        return all_ids

    def query_all_ic_pins(self, designators: list[str], batch_size: int = 1) -> dict:
        """Query pin positions for ICs by designator.

        Returns: {designator: [{pinNumber, pinName, x, y}, ...]}

        Batches queries to avoid bridge timeout on many/large ICs.
        """
        if not designators:
            return {}

        result_map = {}
        for batch_start in range(0, len(designators), batch_size):
            batch = designators[batch_start:batch_start + batch_size]
            js_lines = [
                "var allComps=await eda.sch_PrimitiveComponent.getAll(undefined,true);",
                "var result={};",
            ]
            for des in batch:
                js_lines.append(
                    f"var c_{des}=allComps.find(function(c){{return c.designator==='{des}';}});"
                    f"if(c_{des}){{"
                    f"var pins_{des}=await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId(c_{des}.primitiveId||c_{des}.uuid);"
                    f"result['{des}']=pins_{des}.map(function(p){{return{{n:p.pinNumber,name:p.pinName,x:p.x,y:p.y}};}});"
                    f"}}"
                )
            js_lines.append("return JSON.stringify(result);")
            batch_result = self._exec("".join(js_lines))
            result_map.update(json.loads(batch_result["result"]))
        return result_map

    # ── Wire operations ─────────────────────────────────

    def draw_wires(self, wires: list[dict], batch_size: int = 10) -> int:
        """Batch draw wire stubs. Each wire: {x1, y1, x2, y2, net?}

        All wires must be horizontal or vertical (EDA constraint).
        Batched to avoid bridge 30s timeout on large wire sets.
        Wires are deduplicated by (x1,y1,x2,y2) position to prevent
        overlapping net labels on the same pin.
        """
        if not wires:
            return 0

        seen = set()
        unique = []
        for w in wires:
            key = (w["x1"], w["y1"], w["x2"], w["y2"])
            if key not in seen:
                seen.add(key)
                unique.append(w)

        drawn = 0
        for i in range(0, len(unique), batch_size):
            batch = unique[i:i + batch_size]
            js_lines = []
            for w in batch:
                net_arg = f",'{w['net']}'" if w.get("net") else ""
                js_lines.append(
                    f"await eda.sch_PrimitiveWire.create("
                    f"[{w['x1']},{w['y1']},{w['x2']},{w['y2']}]{net_arg});"
                )
            js_lines.append(f"return 'drew {len(batch)} wires';")
            self._exec("".join(js_lines))
            drawn += len(batch)
        return drawn

    def create_net_ports(self, ports: list[dict]) -> int:
        """Batch create net ports. Each: {net, x, y, rot?}

        Deprecated: wire stubs with net names auto-merge; net ports are unnecessary.
        """
        if not ports:
            return 0

        js_lines = []
        for p in ports:
            rot = p.get("rot", 0)
            js_lines.append(
                f"await eda.sch_PrimitiveComponent.createNetPort("
                f"'BI','{p['net']}',{p['x']},{p['y']},{rot},false);"
            )
        js_lines.append(f"return 'created {len(ports)} ports';")
        self._exec("".join(js_lines))
        return len(ports)

    def create_net_flags(self, flags: list[dict]) -> int:
        """Batch create net flags (power/ground symbols). Each: {kind, net, x, y}

        Deprecated: wire stubs with net names auto-merge; net flags are unnecessary.
        """
        if not flags:
            return 0

        js_lines = []
        for f in flags:
            js_lines.append(
                f"await eda.sch_PrimitiveComponent.createNetFlag("
                f"'{f['kind']}','{f['net']}',{f['x']},{f['y']},0,false);"
            )
        js_lines.append(f"return 'created {len(flags)} flags';")
        self._exec("".join(js_lines))
        return len(flags)

    # ── No-Connect (NC) pin marking ──────────────────────

    def detect_unconnected_pins(self, designators: list[str] = None) -> dict[str, list[str]]:
        """Find all pins without wire connections.

        Queries all wires and compares endpoints against pin positions.
        A pin is considered connected if any wire endpoint is within 5 units.

        Args:
            designators: IC designators to check. If None, auto-detects all
                         components with designator prefix in ('U','J','Q').

        Returns: {designator: [pinNumber, ...]}
        """
        # Auto-detect IC-like components if not specified
        if designators is None:
            result = self._exec(
                "var all=await eda.sch_PrimitiveComponent.getAll(undefined,true);"
                "return JSON.stringify(all.filter(function(c){"
                "var d=c.designator||'';"
                "return d&&d!=='?'&&c.componentType!=='sheet'&&"
                "/^(U|J|Q)/.test(d);"
                "}).map(function(c){return c.designator;}));"
            )
            designators = json.loads(result["result"])

        if not designators:
            return {}

        # Query all pins for specified ICs + all wires in one bridge call
        js_lines = [
            "var allComps=await eda.sch_PrimitiveComponent.getAll(undefined,true);",
            "var ics=" + json.dumps(designators) + ";",
            "var icPins=[];",
        ]
        js_lines.append(
            "for(var i=0;i<ics.length;i++){"
            "var comp=allComps.find(function(c){return c.designator===ics[i];});"
            "if(!comp){icPins.push({ic:ics[i],pins:[]});continue;}"
            "var pins=await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId(comp.primitiveId||comp.uuid);"
            "icPins.push({ic:ics[i],pins:pins.map(function(p){return{n:p.pinNumber,x:p.x,y:p.y};})});"
            "}"
        )
        js_lines.append(
            "var wires=await eda.sch_PrimitiveWire.getAll();"
            "var wireEnds=[];"
            "for(var j=0;j<wires.length;j++){"
            "var line=wires[j].line||[];"
            "if(line.length>=4){wireEnds.push([line[0],line[1]]);wireEnds.push([line[2],line[3]]);}"
            "}"
            "return JSON.stringify({icPins:icPins,wireEnds:wireEnds});"
        )
        result = self._exec("".join(js_lines))
        data = json.loads(result["result"])
        ic_pins = data["icPins"]
        wire_ends = data["wireEnds"]

        # Local detection: pin is connected if a wire endpoint is within 5 units
        def _is_connected(px: int, py: int) -> bool:
            for we in wire_ends:
                if abs(we[0] - px) <= 5 and abs(we[1] - py) <= 5:
                    return True
            return False

        unconnected = {}
        for ic in ic_pins:
            ic_name = ic["ic"]
            for pin in ic.get("pins", []):
                if not _is_connected(pin["x"], pin["y"]):
                    if ic_name not in unconnected:
                        unconnected[ic_name] = []
                    unconnected[ic_name].append(pin["n"])

        return unconnected

    def mark_no_connect(self, unconnected: dict[str, list[str]] = None,
                         designators: list[str] = None) -> dict:
        """Mark unconnected pins with No-Connect flag.

        Two-step process:
        1. detect_unconnected_pins() to find floating pins
        2. For each IC, setState_NoConnected(true) + pin.done()

        Args:
            unconnected: Pre-detected {designator: [pinNumbers]}.
                         If None, auto-detects via detect_unconnected_pins().
            designators: Passed to detect_unconnected_pins() if unconnected is None.

        Returns: {marked: int, failed: int, details: [{ic, count, pins}]}
        """
        if unconnected is None:
            unconnected = self.detect_unconnected_pins(designators)

        if not unconnected:
            return {"marked": 0, "failed": 0, "details": []}

        marked = 0
        failed = 0
        details = []

        for ic_name, pin_numbers in unconnected.items():
            pin_blocks = []
            for pn in pin_numbers:
                pin_blocks.append(
                    f"var p{pn}=pins.find(function(p){{return p.pinNumber==='{pn}';}});"
                    f"p{pn}.setState_NoConnected(true);"
                    f"await p{pn}.done();"
                )
            js = (
                f"var allComps=await eda.sch_PrimitiveComponent.getAll(undefined,true);"
                f"var comp=allComps.find(function(c){{return c.designator==='{ic_name}';}});"
                f"var pins=await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId(comp.primitiveId||comp.uuid);"
                + "".join(pin_blocks) +
                f"return JSON.stringify({{ic:'{ic_name}',marked:{len(pin_numbers)}}});"
            )
            try:
                r = self._exec(js)
                m = json.loads(r["result"]).get("marked", 0)
                marked += m
                details.append({"ic": ic_name, "count": m, "pins": pin_numbers})
            except BridgeError as e:
                import sys
                print(f"  [NC] {ic_name}: FAILED - {e}", file=sys.stderr)
                failed += len(pin_numbers)

        return {"marked": marked, "failed": failed, "details": details}

    def verify_no_connect(self, designators: list[str]) -> dict:
        """Verify which pins have No-Connect markers set.

        Returns: {total_nc: int, details: [{ic, ncCount, pins}]}
        """
        if not designators:
            return {"total_nc": 0, "details": []}

        js = (
            "var ics=" + json.dumps(designators) + ";"
            "var allComps=await eda.sch_PrimitiveComponent.getAll(undefined,true);"
            "var totalNC=0;"
            "var ncDetails=[];"
            "for(var i=0;i<ics.length;i++){"
            "var comp=allComps.find(function(c){return c.designator===ics[i];});"
            "if(!comp)continue;"
            "var pins=await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId(comp.primitiveId||comp.uuid);"
            "var ncPins=[];"
            "for(var j=0;j<pins.length;j++){"
            "if(pins[j].getState_NoConnected()){"
            "ncPins.push(pins[j].pinNumber);totalNC++;}"
            "}"
            "if(ncPins.length>0)ncDetails.push({ic:ics[i],ncCount:ncPins.length,pins:ncPins});"
            "}"
            "return JSON.stringify({totalNC:totalNC,details:ncDetails});"
        )
        result = self._exec(js)
        return json.loads(result["result"])

    # ── Module-level orchestration ──────────────────────

    def draw_module(self, manifest: dict, layout: dict, dry_run: bool = False) -> dict:
        """Draw one complete module with safety checks.

        manifest keys:
            ics: [{des, uuid, x, y}]
            passives: [{des, uuid, x, y}]
            wires: [{x1, y1, x2, y2, net}]
            ports: [{net, x, y, rot}]
            flags: [{kind, net, x, y}]

        Safety guarantees:
            - Pre-placement dedup: skips designators already on canvas
            - Post-placement verify: checks all expected designators landed
            - Reports skipped / missing to caller
        """
        result = {"placed_ics": [], "placed_passives": [], "wires": 0,
                  "ports": 0, "flags": 0, "skipped": [], "missing": []}

        # Step 1: Place ICs (with auto-dedup)
        all_expected = []
        if manifest.get("ics"):
            result["placed_ics"] = self.place_components(manifest["ics"])
            all_expected.extend(c["des"] for c in manifest["ics"])

        # Step 2: Place passives (with auto-dedup)
        if manifest.get("passives"):
            result["placed_passives"] = self.place_components(manifest["passives"])
            all_expected.extend(c["des"] for c in manifest["passives"])

        # Step 3: Post-placement verification
        if all_expected:
            present, missing = self.verify_placement(all_expected)
            result["missing"] = missing
            if missing:
                import sys
                print(f"  [WARN] missing after placement: {missing}", file=sys.stderr)

        # Step 4: Draw wires
        if manifest.get("wires"):
            result["wires"] = self.draw_wires(manifest["wires"])

        # Wire stubs carry net names — EDA auto-merges same-named nets.
        # No net ports or flags needed.

        return result

    def save(self):
        self._exec("await eda.sch_Document.save();return 'saved';", timeout=30)


# ── Layout Calculator ───────────────────────────────────

@dataclass
class Region:
    name: str
    x_min: int
    x_max: int
    y_min: int
    y_max: int


class LayoutCalculator:
    """Three-column absolute grid layout — no relative/pin-derived coordinates.

    Columns (X pre-computed from region bounds):
      col_left   — passives on left side of ICs
      col_center — ICs, connectors
      col_right  — passives on right side of ICs

    Y placement: top-50 → downward, each column tracks its own Y cursor.
    Spacing by component type (not pin position).
    """

    STUB_RIGHT = 10
    STUB_LEFT = 30
    GRID = 10

    # Layout geometry
    TOP_MARGIN = 50        # from region top edge to first component top edge
    SIDE_MARGIN = 100      # from region side edge inward
    BOTTOM_MARGIN = 50     # minimum clearance from region bottom edge

    # Vertical edge-to-edge spacing by component type
    SPACING = {
        "IC": 80,
        "CONN": 60,
        "LED": 30,
        "DIODE": 30,
        "OTHER": 30,
    }
    DEFAULT_SPACING = 20  # caps, resistors, inductors, test points

    # Default symbol heights in EDA units (1 unit = 10 mil)
    # DEPRECATED: These are rough estimates. Actual EDA symbol heights can differ
    # by 2-5x. Use query_actual_heights() after placement to get real pin Y ranges.
    # For final layout, prefer two-pass: place with estimates → query actual → re-place.
    DEFAULT_HEIGHTS = {
        "IC": 60,
        "CONN": 80,
        "LED": 40,
        "DIODE": 40,
        "CAP": 20,
        "RES": 20,
        "IND": 30,
        "TP": 20,
        "OTHER": 50,
    }

    @staticmethod
    def _spacing_for(ctype: str) -> int:
        return LayoutCalculator.SPACING.get(ctype, LayoutCalculator.DEFAULT_SPACING)

    @classmethod
    def _height_for(cls, ctype: str) -> int:
        return cls.DEFAULT_HEIGHTS.get(ctype, 100)

    def query_actual_heights(self, designators: list[str]) -> dict[str, int]:
        """Query actual EDA symbol heights from pin Y ranges.

        After placing components, call this to get real heights for layout
        verification. Heights are computed as max(pin_y) - min(pin_y) + 20.

        Returns: {designator: actual_height_in_eda_units}
        """
        pin_maps = self.engine.query_all_ic_pins(designators)
        heights = {}
        for des, pins in pin_maps.items():
            if not pins:
                continue
            ys = [p["y"] for p in pins]
            raw = max(ys) - min(ys)
            heights[des] = self.snap(raw + 20) if raw > 0 else self._height_for("IC")
        return heights

    def __init__(self, engine: DrawEngine):
        self.engine = engine

    def snap(self, val: int) -> int:
        return round(val / self.GRID) * self.GRID

    @staticmethod
    def pin_side(pin_x: int, pin_y: int, comp_x: int, comp_y: int,
                 bounds: tuple = None) -> str:
        """Auto-detect which side of component a pin is on.

        When bounds=(min_x, max_x, min_y, max_y) is provided, uses fractional
        edge proximity for corner-safe detection. Falls back to dx/dy comparison.
        """
        if bounds:
            min_x, max_x, min_y, max_y = bounds
            if max_x == min_x:
                if pin_x < comp_x: return "left"
                if pin_x > comp_x: return "right"
            if max_y == min_y:
                if pin_y < comp_y: return "bottom"
                if pin_y > comp_y: return "top"
            w = max_x - min_x or 1
            h = max_y - min_y or 1
            dl = (pin_x - min_x) / w
            dr = (max_x - pin_x) / w
            dt = (max_y - pin_y) / h
            db = (pin_y - min_y) / h
            d = min(dl, dr, dt, db)
            if d == dl:
                return "left"
            if d == dr:
                return "right"
            if d == dt:
                return "top"
            return "bottom"

        dx = pin_x - comp_x
        dy = pin_y - comp_y
        if abs(dx) >= abs(dy):
            return "left" if dx < 0 else "right"
        else:
            return "bottom" if dy < 0 else "top"

    # ── Column X computation ──────────────────────────────

    def column_x(self, region: Region, col: str) -> int:
        """Absolute X center for a column, computed from region bounds only."""
        usable = region.x_max - region.x_min - 2 * self.SIDE_MARGIN
        col_w = usable // 3
        if col == "left":
            return self.snap(region.x_min + self.SIDE_MARGIN + col_w // 2)
        elif col == "center":
            return self.snap((region.x_min + region.x_max) // 2)
        else:  # right
            return self.snap(region.x_max - self.SIDE_MARGIN - col_w // 2)

    # ── Placement-only (no pin query, no wire generation) ──

    def compute_placement_only(self, module_def: dict, region: Region) -> dict:
        """Three-column grid layout for one module — placements only, no wires.

        Use this when ICs are not yet on canvas (pin query would fail).
        Call _generate_connections() separately after ICs are placed.
        """
        cx = {
            "left": self.column_x(region, "left"),
            "center": self.column_x(region, "center"),
            "right": self.column_x(region, "right"),
        }

        top_y = self.snap(region.y_max - self.TOP_MARGIN)
        next_top = {"left": top_y, "center": top_y, "right": top_y}

        def _place(col: str, des: str, uuid: str, ctype: str, height_override: int = None,
                  lib_uuid: str = "") -> dict:
            h = height_override if height_override else self._height_for(ctype)
            spacing = self._spacing_for(ctype)
            center_y = self.snap(next_top[col] - h // 2)
            next_top[col] = self.snap(center_y - h // 2 - spacing)
            entry = {"des": des, "uuid": uuid, "x": cx[col], "y": center_y}
            if lib_uuid:
                entry["lib_uuid"] = lib_uuid
            return entry

        ics_list = []
        passives_list = []
        others_list = []

        for ic in module_def.get("ics", []):
            ics_list.append(_place("center", ic["des"], ic["uuid"],
                                   ic.get("type", "IC"), ic.get("height"),
                                   ic.get("lib_uuid", "")))

        for p in module_def.get("passives", []):
            side = p.get("side", "right")
            col = "left" if side == "left" else "right"
            passives_list.append(_place(col, p["des"], p["uuid"],
                                        p.get("type", "CAP"), p.get("height"),
                                        p.get("lib_uuid", "")))

        for c in module_def.get("connectors", []):
            others_list.append(_place("center", c["des"], c["uuid"],
                                      "CONN", c.get("height"),
                                      c.get("lib_uuid", "")))

        tps = module_def.get("test_points", [])
        if tps:
            tp_x = self.snap(region.x_max - 50)
            tp_y = self.snap(region.y_min + self.BOTTOM_MARGIN)
            spacing = self._spacing_for("TP")
            tp_h = self._height_for("TP")
            for i, tp in enumerate(tps):
                entry = {
                    "des": tp["des"], "uuid": tp.get("uuid", ""),
                    "x": tp_x, "y": self.snap(tp_y + tp_h // 2 + i * (tp_h + spacing)),
                }
                if tp.get("lib_uuid"):
                    entry["lib_uuid"] = tp["lib_uuid"]
                others_list.append(entry)

        return {"ics": ics_list, "passives": passives_list + others_list,
                "wires": [], "ports": [], "flags": []}

    # ── Main layout entry point ───────────────────────────

    def compute_module(self, module_def: dict, region: Region,
                       ic_designators: list[str]) -> dict:
        """Three-column absolute grid layout for one module.

        Y placement tracks top-edge of each column:
          - First component: top edge = y_max - TOP_MARGIN
          - Center Y = top_edge - height/2
          - Next top_edge = center_y - height/2 - spacing  (edge-to-edge gap)

        module_def keys (from design doc structured data):
            ics:        [{des, uuid, type, height?}]
            passives:   [{des, uuid, type, side, height?}]
            connectors: [{des, uuid, height?}]
            test_points:[{des, uuid, height?}]
            netlist:    [{net, pin, side}]
            passive_nets: [{des, net, side}]
            power_nets: [{kind, net, x, y}]

        Returns: {ics, passives, wires, ports, flags}
        """
        # ── Column X positions (absolute) ──
        cx = {
            "left": self.column_x(region, "left"),
            "center": self.column_x(region, "center"),
            "right": self.column_x(region, "right"),
        }

        # ── Top-edge trackers per column ──
        top_y = self.snap(region.y_max - self.TOP_MARGIN)
        next_top = {"left": top_y, "center": top_y, "right": top_y}

        def _place(col: str, des: str, uuid: str, ctype: str, height_override: int = None,
                  lib_uuid: str = "") -> dict:
            """Place one component at column `col`. Returns {des, uuid, x, y, lib_uuid?}.

            Uses height to compute center-Y from top-edge, then advances
            next_top past this component + spacing.
            """
            h = height_override if height_override else self._height_for(ctype)
            spacing = self._spacing_for(ctype)
            center_y = self.snap(next_top[col] - h // 2)
            next_top[col] = self.snap(center_y - h // 2 - spacing)
            entry = {"des": des, "uuid": uuid, "x": cx[col], "y": center_y}
            if lib_uuid:
                entry["lib_uuid"] = lib_uuid
            return entry

        ics_list = []
        passives_list = []
        others_list = []

        # ── Place ICs (center column) ──
        for ic in module_def.get("ics", []):
            ics_list.append(_place("center", ic["des"], ic["uuid"],
                                   ic.get("type", "IC"), ic.get("height"),
                                   ic.get("lib_uuid", "")))

        # ── Place passives (left/right columns by side) ──
        for p in module_def.get("passives", []):
            side = p.get("side", "right")
            col = "left" if side == "left" else "right"
            passives_list.append(_place(col, p["des"], p["uuid"],
                                        p.get("type", "CAP"), p.get("height"),
                                        p.get("lib_uuid", "")))

        # ── Place connectors (center, below ICs) ──
        for c in module_def.get("connectors", []):
            others_list.append(_place("center", c["des"], c["uuid"],
                                      "CONN", c.get("height"),
                                      c.get("lib_uuid", "")))

        # ── Place test points (right edge, bottom-up) ──
        tps = module_def.get("test_points", [])
        if tps:
            tp_x = self.snap(region.x_max - 50)
            tp_y = self.snap(region.y_min + self.BOTTOM_MARGIN)
            spacing = self._spacing_for("TP")
            tp_h = self._height_for("TP")
            for i, tp in enumerate(tps):
                entry = {
                    "des": tp["des"], "uuid": tp.get("uuid", ""),
                    "x": tp_x, "y": self.snap(tp_y + tp_h // 2 + i * (tp_h + spacing)),
                }
                if tp.get("lib_uuid"):
                    entry["lib_uuid"] = tp["lib_uuid"]
                others_list.append(entry)

        # ── Bottom boundary check ──
        min_y = region.y_min + self.BOTTOM_MARGIN
        for col in ["left", "center", "right"]:
            if next_top[col] < min_y:
                import sys
                print(f"  [WARN] col={col} bottom overflow: "
                      f"next_top={next_top[col]} < min={min_y}", file=sys.stderr)

        # ── Generate wire connections ──
        pin_maps = self.engine.query_all_ic_pins(ic_designators)
        wires, _, _ = self._generate_connections(
            module_def, pin_maps, ics_list, passives_list, others_list)

        return {
            "ics": ics_list,
            "passives": passives_list + others_list,
            "wires": wires,
            "ports": [],
            "flags": [],
        }

    def _generate_connections(self, module_def: dict, pin_maps: dict,
                               ic_placements: list, passive_placements: list,
                               other_placements: list) -> tuple:
        """Generate wire manifest — returns (wires, [], []).

        Wire stubs carry net names; EDA auto-merges same-named nets.
        Pin side is auto-detected via pin_side().
        """
        wires = []

        all_placements = {p["des"]: p for p in ic_placements + passive_placements + other_placements}

        # Pre-compute pin bounding boxes for corner-safe pin_side detection
        pin_bounds = {}
        for des, pins in pin_maps.items():
            if pins:
                xs = [p["x"] for p in pins]
                ys = [p["y"] for p in pins]
                pin_bounds[des] = (min(xs), max(xs), min(ys), max(ys))

        def _stub_end(pin_x, pin_y, comp_x, comp_y, comp_des=None):
            """Return (end_x, end_y, port_rot) for a stub from this pin.

            Side is always auto-detected via pin_side(). When comp_des is
            provided, the IC's pin bounding box is used for corner-safe detection.
            """
            bounds = pin_bounds.get(comp_des) if comp_des else None
            side = self.pin_side(pin_x, pin_y, comp_x, comp_y, bounds)
            if side == "left":
                return (pin_x - self.STUB_LEFT, pin_y, 180)
            elif side == "right":
                return (pin_x + self.STUB_RIGHT, pin_y, 0)
            elif side == "top":
                return (pin_x, pin_y + self.STUB_RIGHT, 90)
            else:  # bottom
                return (pin_x, pin_y - self.STUB_RIGHT, 270)

        # Process IC pin connections
        netlist = module_def.get("netlist", [])
        seen_pins = set()  # (designator, pin_x, pin_y) — prevent duplicate stubs
        for entry in netlist:
            net = entry["net"]
            pin_ref = entry["pin"]  # e.g. "U2.24"

            ic_des, pin_num = pin_ref.split(".")
            ic_pins = pin_maps.get(ic_des, [])
            pin = next((p for p in ic_pins if p["n"] == pin_num), None)
            if not pin:
                pin = next((p for p in ic_pins if p.get("name", "").upper() == pin_num.upper()), None)
            if not pin:
                import sys
                print(f"  [WARN] Pin {pin_ref} not found (number or name)", file=sys.stderr)
                continue

            pin_key = (ic_des, pin["x"], pin["y"])
            if pin_key in seen_pins:
                import sys
                print(f"  [WARN] Pin {pin_ref} ({net}) skipped — duplicate pin position "
                      f"({pin['x']},{pin['y']}) already used for another net", file=sys.stderr)
                continue
            seen_pins.add(pin_key)

            ic_center = all_placements.get(ic_des, {})
            end_x, end_y, rot = _stub_end(
                pin["x"], pin["y"],
                ic_center.get("x", pin["x"]), ic_center.get("y", pin["y"]),
                comp_des=ic_des,
            )
            wires.append({"x1": pin["x"], "y1": pin["y"], "x2": end_x, "y2": end_y, "net": net})

        # Process passive pin connections
        # Direction determined by position relative to center column,
        # NOT by the "side" field (which is the component pin side).
        ic_center_x = None
        if ic_placements:
            ic_center_x = sum(p["x"] for p in ic_placements) / len(ic_placements)
        elif all_placements:
            all_xs = [p["x"] for p in all_placements.values()]
            ic_center_x = sum(all_xs) / len(all_xs)

        passive_nets = module_def.get("passive_nets", [])
        for entry in passive_nets:
            des = entry["des"]
            net = entry.get("net", "")
            comp = all_placements.get(des, {})
            if not comp:
                continue

            comp_pins = pin_maps.get(des, [])
            if comp_pins and len(comp_pins) >= 1:
                # Match pin by side: "left"=pin1 (smallest x), "right"=pin2 (largest x)
                side = entry.get("side", "right")
                sorted_by_x = sorted(comp_pins, key=lambda p: p["x"])
                if side == "left":
                    pin = sorted_by_x[0]
                else:
                    pin = sorted_by_x[-1]
                end_x, end_y, _rot = _stub_end(
                    pin["x"], pin["y"], comp["x"], comp["y"], comp_des=des)
                wires.append({"x1": pin["x"], "y1": pin["y"], "x2": end_x, "y2": end_y, "net": net})
            else:
                # Fallback: no pin data, use center with column-based direction
                cx, cy = comp["x"], comp["y"]
                if ic_center_x is not None and cx < ic_center_x:
                    wires.append({"x1": cx, "y1": cy, "x2": cx + self.STUB_RIGHT, "y2": cy, "net": net})
                else:
                    wires.append({"x1": cx, "y1": cy, "x2": cx - self.STUB_LEFT, "y2": cy, "net": net})

        # Process shared nets
        shared_nets = module_def.get("shared_nets", [])
        for entry in shared_nets:
            net = entry["net"]
            for pin_ref in entry.get("pins", []):
                if "." in str(pin_ref):
                    ic_des, pin_num = pin_ref.split(".")
                    ic_pins = pin_maps.get(ic_des, [])
                    pin = next((p for p in ic_pins if p["n"] == pin_num), None)
                    if pin:
                        ic_center = all_placements.get(ic_des, {})
                        end_x, _, _ = _stub_end(
                            pin["x"], pin["y"],
                            ic_center.get("x", pin["x"]), ic_center.get("y", pin["y"]),
                            comp_des=ic_des,
                        )
                        wires.append({"x1": pin["x"], "y1": pin["y"], "x2": end_x, "y2": pin["y"], "net": net})
                else:
                    comp = all_placements.get(pin_ref, {})
                    if comp:
                        cx, cy = comp["x"], comp["y"]
                        side = self.pin_side(cx, cy, cx, cy)  # fallback
                        stub = self.STUB_LEFT if side == "left" else self.STUB_RIGHT
                        dx = -stub if side == "left" else stub
                        wires.append({"x1": cx, "y1": cy, "x2": cx + dx, "y2": cy, "net": net})

        return wires, [], []


# ── Manifest Builder ────────────────────────────────────

def build_simple_manifest(components: list, wires: list,
                           ports: list = None, flags: list = None) -> dict:
    """Build a manifest dict from flat lists.

    components: [{des, uuid, x, y, rot?}]
    wires: [{x1, y1, x2, y2, net?}]
    ports: [{net, x, y, rot?}]
    flags: [{kind, net, x, y}]
    """
    return {
        "ics": [],
        "passives": components,
        "wires": wires,
        "ports": ports or [],
        "flags": flags or [],
    }
