## 原理图模块化绘制 / Schematic Drawing

`/schematic-draw [设计文档路径]`

> **🔴 执行方式（先读）**：本 skill 的核心执行主线是下方的「执行铁律」+「执行任务清单 T1–T9」。**开始执行时先用 TaskCreate 建立 T1–T9 待办项，再逐项执行并勾销**；每个任务的详细要求见其下的子条目，展开说明见 Level 1 / Level 2 章节。

**决策与执行分离的三层架构**，自动检测项目状态，决定从哪个阶段开始执行。

> **前置条件**：`bash scripts/start-bridge.sh` → `curl http://localhost:49620/health` 确认 `edaConnected=true`。

---

## 架构概览

```
┌──────────────────────────────────────────────────────────────┐
│  Level 1: 智能入口 — 自动状态检测                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 1. 检测设计文档 → 提取结构化器件清单与连线清单             │  │
│  │ 2. 检测 EDA 连接状态                                     │  │
│  │ 3. 检测图纸现状 (已放置器件 vs BOM)                        │  │
│  │ 4. 智能路由: 完整绘制 / 续绘 / 收尾校验 / 审查             │  │
│  └────────────────────────────────────────────────────────┘  │
│                        │                                      │
│                        ▼                                      │
│  Level 2: generic_runner.py 两阶段执行                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Phase 1 — 生成布局清单 (layout_manifest.json)          │  │
│  │  │  1. 解析设计文档 markdown 表格                        │  │
│  │  │  2. 解析 LCSC → 放置 IC → 查询引脚坐标                │  │
│  │  │  3. 自动布局计算 (LayoutCalculator, 两趟高度修正)     │  │
│  │  │  4. 生成连线 (wire stubs, net 名)                     │  │
│  │  │  5. 保存 layout_manifest.json（含精确坐标+net+wire）  │  │
│  │  │      ↓                                                │  │
│  │  Phase 2 — 批量绘制（从 JSON 驱动，每模块 ~4 次调用）     │  │
│  │  │  1. 读取 layout_manifest.json                         │  │
│  │  │  2. 逐模块：放置阻容 + 画 wire stubs (IC 自动去重)    │  │
│  │  │  3. 收尾校验 — BOM交叉验证 + DRC + NC标记              │  │
│  │  └──────────────────────────────────────────────────────│  │
│  │  支持 --dry-run 仅生成 JSON / --from-manifest 仅绘制     │  │
│  └────────────────────────────────────────────────────────┘  │
│                        │                                      │
│                        ▼                                      │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  draw_engine.py (底层绘制引擎，被 generic_runner 调用) │    │
│  │  · 批量放置器件 (1 次 bridge 调用)                     │    │
│  │  · 批量画 wire stubs 带 net 名 (1 次 bridge 调用)     │    │
│  │  · query_actual_heights() 获取实际器件高度             │    │
│  │  · 放置安全检查: filter_duplicates + verify_placement  │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

> **关键中间产物**：`layout_manifest.json` 是设计文档（markdown）到 EDA 图纸之间的桥梁。Phase 1 完成所有计算和决策，Phase 2 纯机械执行。两阶段可独立运行，便于调试和重试。

---

## 🔴 执行铁律（最高优先级，违反即失败）

> **本 skill 的绘制必须由 `scripts/generic_runner.py` + `scripts/draw_engine.py` 脚本完成，AI 只负责运行脚本命令和解读输出，不负责写绘制逻辑。**

1. **禁止 AI 编写任何绘制脚本，也禁止 AI 直接调用 bridge API 做「写操作」。** 放置器件、画线、NC 标记、改属性等**写操作**一律由 `generic_runner.py` 脚本完成。
   - AI **允许**的 bridge 调用仅限于**只读查询**（用于 Level 1 状态检测，不改变图纸）：`getPages()`、`sch_PrimitiveComponent.getAll()`、`getState_Designator()`、`curl /health`。
   - 除上述只读查询外，任何 bridge 写调用都必须由脚本发起，AI 不得手写。
2. **必须严格按两阶段流程执行，不可跳步、不可合并：**
   - **Phase 1**：`python scripts/generic_runner.py <设计文档> --layout layout.json --dry-run` → 生成 `layout_manifest.json` → 打印布局预览 → **等待用户确认**。
   - **Phase 2**：`python scripts/generic_runner.py --from-manifest layout_manifest.json` → 纯机械绘制。
3. **遇到问题的处理顺序（严禁绕过）：**
   1. **先怀疑设计文档数据**（UUID 错 / 引脚号错 / net 名带括号 / 器件缺连线条目）→ 回 `/hardware-design` 修正数据 → 重跑 Phase 1；
   2. **再怀疑 `layout.json`**（区域坐标越界 / 模块未映射区域）→ 修正布局配置 → 重跑 Phase 1；
   3. **只有确认是脚本 bug（而非数据错误）时**才允许改 `generic_runner.py` / `draw_engine.py`，且必须先告知用户并获得确认。
4. **`layout_manifest.json` 是唯一绘制数据源。** Phase 1 生成的 JSON 未经用户确认不得直接绘制；Phase 2 只读取 JSON，不重新计算坐标或连线。
5. **出现任何"脚本报错/跑不通"，禁止改用自己手写的 bridge 调用替代。** 报错信息要原样呈现，按第 3 条顺序排查。

---

## 🔴 执行任务清单（AI 必须先建 Todo 再逐项执行）

> **开始执行 `/schematic-draw` 时，第一步就是用 TaskCreate 工具把下面 T1–T9 逐条建成待办项，然后按序执行：每开始一项标 `in_progress`，完成标 `completed`。**
> **禁止跳过任务、禁止合并任务、禁止自写脚本替代（详见上方「执行铁律」）。**
> 详细展开见下方「Level 1 / Level 2」对应章节，任务里标注了对应步骤号。

### T1 — 检测设计文档（步骤 1.1）

- 搜索 `Hardware_Design_*.md`（或用户指定的设计文档路径）
- 找到 ≥1 个 → 选最新，解析出：模块清单及位号范围、完整 BOM 位号列表、模块与 IC 的映射
- 未找到 → **终止**，提示用户先运行 `/hardware-design`

### T2 — 检测 EDA 连接（步骤 1.2）

- 运行 `curl http://localhost:49620/health`，检查返回的 `edaConnected` 字段
- `true` → 进入 T3；失败 / `false` → **终止**，提示 `bash scripts/start-bridge.sh` 并确认扩展已加载

### T3 — 检测图纸现状 + 输出路由决策（步骤 1.3 / 1.4）

- 获取当前页面列表（`getPages()`）与已放置器件位号（`designator ≠ '?'`）
- 与 BOM 对比，算出 `completed / missing / extra`，按模块统计完成度
- 输出状态报告，并按完成度路由：
  - 0% → 完整绘制流程（T4–T9）
  - 1–99% → 增量续绘（跳过已完成模块，从断点续 T4）
  - 100% → 直接跳 T8 收尾校验
- 只有涉及图纸重建 / 迁移等破坏性操作才需用户确认

### T4 — 图纸规划 + 生成 layout.json（步骤 2.2，🔴 最易跳步）

- 按模块数估算图纸大小：≤4→A4 / 5-6→A3 / 7-8→A3 / 9-12→A2 / 13+→A1
- 划分区域，分配模块到区域（最复杂模块各占一区，简单模块可共享）
- 通知用户创建图纸，用户回复"就绪"后获取图纸 UUID 并验证
- **🔴 必须把区域坐标 + 模块→区域映射写成磁盘文件 `layout.json`**（不能只写在对话回复里）。格式：`{"regions": {"Q1": {"x_min","x_max","y_min","y_max"}, ...}, "module_regions": {"1": "Q1", ...}}`，完整示例见步骤 2.2

### T5 — Phase 1：生成 layout_manifest.json（步骤 2.3）

- 运行：`python scripts/generic_runner.py <设计文档> --layout layout.json --dry-run`
- 检查输出：`unresolved_lcsc` 为空、无 `[WARN]` / `[ERROR]`、无 missing
- 有报错 → **按执行铁律第 3 条**回设计文档或 layout.json 修数据，重跑 T5，**禁止自写脚本绕过**

### T6 — 布局预览 → 用户确认（🔴 不可跳步）

- 把脚本打印的布局预览呈现给用户（模块→区域、三列坐标、器件数、连线数）
- **必须等用户确认**后才能进入 T7
- 用户要求调整 → 改 `layout.json`（区域）或设计文档（器件/连线）→ 重跑 T5

### T7 — Phase 2：从 manifest 批量绘制 + NC 标记（步骤 2.4 / 2.5.3）

- 运行：`python scripts/generic_runner.py --from-manifest layout_manifest.json --mark-nc`
- 逐模块检查：`placed` 数量、`wires` 数、`missing` 列表
- 有 `missing` → 先 `verify_placement` 区分"桥接超时但已放置" vs "真失败"，按执行铁律第 3 条处理，重跑
- `--mark-nc` 让脚本自动检测未连接引脚并打 No-Connect 标记（detect → mark → verify 三步，**无需 AI 自写代码**）

### T8 — 收尾校验（步骤 2.5）

- BOM 交叉验证：`bom_required` vs `placed`，输出 missing / extra
- 网络标签一致性：确认跨模块同名 net 已合并
- 提示用户运行 EDA 内置 DRC

### T9 — 输出最终报告（步骤 2.6）

- 统计表（模块数 / 图纸数 / 元件总数 / IC 数 / 电容数 / 电阻数 / NC 数）
- BOM 校验结果（required / placed / missing / extra）
- 待用户操作清单（DRC、NC 复核、BOM 导出比对、手工布局调整）

---

# Level 1: 智能入口 — 自动状态检测与路由

> **这是 `/schematic-draw` 的入口行为。** 用户运行此命令时，首先执行 Level 1 的状态检测，根据检测结果自动决定进入哪个 Level 2 路径。

## 步骤 1.1：检测设计文档

```bash
# 搜索设计文档
ls Hardware_Design_*.md
```

**路由规则：**

| 检测结果 | 行为 |
|---------|------|
| 找到 ≥1 个设计文档 | 选择最新（或用户指定），解析 BOM 与模块清单 |
| 未找到设计文档 | **终止**，提示用户先运行 `/hardware-design` |

**解析设计文档，提取：**
- 所有模块清单及其位号范围
- 完整 BOM 位号列表（从设计文档各模块提取）
- 模块与 IC 的映射关系

## 步骤 1.2：检测 EDA 连接

```bash
curl http://localhost:49620/health
```

检查返回的 `edaConnected` 字段。

**路由规则：**

| 检测结果 | 行为 |
|---------|------|
| `edaConnected: true` | 继续 |
| 连接失败 / `edaConnected: false` | **终止**，提示用户运行 `bash scripts/start-bridge.sh` 并确认 EDA 扩展已加载 |

## 步骤 1.3：检测图纸现状

**获取当前所有页面：**

```javascript
const pages = await eda.sch_Document.getPages();
// 返回页面列表，包含 pageUuid、pageName
```

**获取已放置器件：**

```javascript
const allComps = await eda.sch_PrimitiveComponent.getAll(undefined, true);
// 提取所有 designator ≠ '?' 的器件
const placedDesignators = allComps
  .filter(c => c.designator && c.designator !== '?')
  .map(c => c.designator);
```

**与 BOM 对比：**

```python
bom_required = set(从设计文档提取的全部位号)
placed = set(从 EDA 提取的已放置位号)

completed = bom_required & placed    # 已完成的位号
missing = bom_required - placed      # 未绘制的位号
extra = placed - bom_required        # 多余位号（可能是手动放置或旧版本残留）
```

**按模块统计完成度：**

```python
for module in modules:
    module_bom = set(module.designators)
    done = module_bom & placed
    module.completion = len(done) / len(module_bom)
    module.missing_designators = module_bom - placed
```

## 步骤 1.4：输出状态报告 + 智能路由

### 状态报告格式

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 项目状态检测报告

  设计文档: Hardware_Design_<项目名>.md ✓
  EDA 连接: 已连接 ✓
  图纸页数: 1 页 (Sheet_1, UUID: xxx)

  模块完成度:
  ✅ 模块1 — MCU最小系统        100% (8/8)
  ✅ 模块2 — 电源模块            100% (25/25)
  ⚠️ 模块3 — 传感器模块          60% (12/20)  缺: C_MPU_CP, C_MPU_VLOGIC, ...
  ❌ 模块4 — 通信接口              0% (0/15)
  ❌ 模块5 — 连接器与测试点        0% (0/10)

  总进度: 45/78 (58%)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 路由决策表

| 完成度 | 路由 | 说明 |
|--------|------|------|
| 0%（无图纸/无器件） | → Level 2 **完整绘制流程** | 从图纸规划开始，全部模块逐一绘制 |
| 1%-99%（部分完成） | → Level 2 **增量续绘流程** | 跳过已完成模块，从第一个未完成模块继续 |
| 100%（全部完成） | → Level 2 **收尾校验流程** | 跳至步骤 2.5，执行 BOM 交叉验证 + DRC |
| 用户指定 `--review` | → `/review-sch` | 直接进入审查流程 |

**增量续绘时的特殊处理：**
- 保留已有图纸和已放置器件，**不做任何修改**
- 跳过 100% 完成的模块，从第一个 < 100% 的模块开始
- 如果新模块需要额外空间，重新评估图纸大小（可能需要用户创建更大的图纸并迁移）

### 用户确认

输出状态报告和路由决策后，简要告知用户将执行什么操作，然后直接开始执行。只有涉及**图纸重建**或**迁移已有器件**等破坏性操作时才需要用户确认。

```
继续执行: 增量续绘 — 从模块3 (传感器模块) 开始，共 3 个未完成模块。
```

---

# Level 2: generic_runner.py 两阶段执行

> Level 2 由 `generic_runner.py` 驱动，分为 Phase 1（生成布局清单 JSON）和 Phase 2（从 JSON 批量绘制）。两阶段可独立运行。

## 完整绘制流程

### 步骤 2.1：解析设计文档

读取设计文档，提取以下信息：

```markdown
## 模块划分表

| # | 模块名称 | IC 位号 | IC 型号 | 区域 | BOM 器件数 |
|---|---------|--------|--------|------|-----------|
| 1 | MCU最小系统 | U1 | ESP32-S3-MINI-1 | Q1 | ~8 |
| 2 | 电源模块 | U8,U9 | MP1584+ME6211 | Q2 | ~25 |
| 3 | 传感器模块 | U2,U3 | MPU6050+BMP280 | Q3 | ~20 |
| ... | ... | ... | ... | ... | ... |
```

**提取规则：**
- 设计文档中每个 "模块 X：" 节 = 一个子任务
- 统计该模块下的所有元件（IC + 无源 + 连接器）
- 连接器/测试点独立成模块（无 IC 锚点，需特殊处理）
- I2C 上拉电阻归于总线所在的模块；分支隔离电阻归于对应 IC 的模块

### 步骤 2.2：图纸大小估算与区域划分

#### 单页原则

全部模块绘制在**同一张图纸**上，消除页面切换，避免 EDA 状态同步问题。

#### 根据模块数估算图纸大小

| 模块数 | 图纸大小 | 区域数 | 区域排布 |
|--------|---------|--------|---------|
| ≤4 | A4 (1655×1170) | 4 | 2×2 |
| 5-6 | A3 (2339×1655) | 6 | 2×3 |
| 7-8 | A3 (2339×1655) | 8 | 2×4 |
| 9-12 | A2 (3308×2339) | 12 | 3×4 |
| 13+ | A1 (4677×3308) | 16 | 4×4 |

> **尺寸参考**（1 单位 = 10 mil）：
> A4 = 1655×1170, A3 = 2339×1655, A2 = 3308×2339, A1 = 4677×3308

#### 区域划分规则

以 A3 (2339×1655) 2×3 六区为例：

```
┌──────────┬──────────┬──────────┐
│  Q1      │  Q2      │  Q3      │  Y: 830 - 1655
│  X:0-780 │  X:780-1560│ X:1560-2339│
├──────────┼──────────┼──────────┤
│  Q4      │  Q5      │  Q6      │  Y: 0 - 830
│  X:0-780 │  X:780-1560│ X:1560-2339│
└──────────┴──────────┴──────────┘
```

A4 (1655×1170) 2×2 四象限：

```
┌─────────────┬─────────────┐
│   Q1        │   Q2        │  Y: 585 - 1170
│   X:0-800   │   X:855-1655│
├─────────────┼─────────────┤
│   Q3        │   Q4        │  Y: 0 - 585
│   X:0-800   │   X:855-1655│
└─────────────┴─────────────┘
```

**模块分配原则：**
- 最复杂模块（MCU、电源）各占一个区域
- 简单模块（LED、测试点）可共享区域
- 关联模块（如多传感器共享 I2C 总线）放同一区域或相邻区域
- 每个区域内预留 50 单位边距

#### 通知用户创建图纸

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📐 图纸规划方案

本项目共 N 个模块，建议使用 1 张 A3 图纸 (2339×1655)，6 个区域：

  Q1: 模块1 — MCU最小系统          [X:50-730,  Y:880-1605]
  Q2: 模块2 — 电源模块              [X:830-1510, Y:880-1605]
  Q3: 模块3 — 传感器模块            [X:1610-2289,Y:880-1605]
  Q4: 模块4 — 通信接口              [X:50-730,  Y:50-780]
  Q5: 模块5 — 指示与保护            [X:830-1510, Y:50-780]
  Q6: 模块6 — 连接器与测试点        [X:1610-2289,Y:50-780]

请在 EasyEDA Pro 中创建 1 张 {A3} 图纸，完成后回复"就绪"。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**用户就绪后**，通过 API 获取当前图纸 UUID 并验证。

**🔴 区域划分方案必须落成 `layout.json` 文件**（供 Phase 1 的 `--layout` 参数使用）。不要只把方案写在对话里，要写成磁盘文件：

```json
{
  "regions": {
    "Q1": {"x_min": 50,  "x_max": 730,  "y_min": 880,  "y_max": 1605},
    "Q2": {"x_min": 830, "x_max": 1510, "y_min": 880,  "y_max": 1605},
    "Q3": {"x_min": 1610,"x_max": 2289, "y_min": 880,  "y_max": 1605},
    "Q4": {"x_min": 50,  "x_max": 730,  "y_min": 50,   "y_max": 780},
    "Q5": {"x_min": 830, "x_max": 1510, "y_min": 50,   "y_max": 780},
    "Q6": {"x_min": 1610,"x_max": 2289, "y_min": 50,   "y_max": 780}
  },
  "module_regions": {
    "1": "Q1", "2": "Q2", "3": "Q3", "4": "Q4", "5": "Q5", "6": "Q6"
  }
}
```

> `layout.json` 是 Phase 1 的布局输入（对应 `generic_runner.py` 的 `--layout` 参数），与 Phase 1 输出的 `layout_manifest.json`（含坐标+连线）是两个不同文件，勿混淆。

### 步骤 2.3：Phase 1 — 生成布局清单 JSON → 用户确认

> `generic_runner.py` 的 Phase 1（`generate_manifest()`）：解析 markdown → 布局计算 → 保存 `layout_manifest.json`。

**输入：** 设计文档中各模块的结构化数据（器件清单 + 连线清单）+ 区域划分方案

**执行流程：**

```bash
# Phase 1: 仅生成布局清单，不绘制（--dry-run）
python scripts/generic_runner.py Hardware_Design_<项目名>.md --layout layout.json --dry-run

# 输出: layout_manifest.json（含精确坐标、net名、wire端点）
```

> **🔴 `--layout layout.json` 必填**（步骤 2.2 生成的布局配置）。缺失会导致脚本回退到从设计文档解析"附录 C 区域坐标"（通常不存在）而直接 `sys.exit`。

**`generic_runner.py` Phase 1 内部步骤：**

| # | 步骤 | 说明 |
|---|------|------|
| 1 | 解析设计文档 | 提取各模块器件/连线清单；区域坐标与模块→区域映射来自 `--layout layout.json` |
| 2 | 解析 LCSC | `resolve_lcsc_ids()` 批量转换 C 编号 → device UUID |
| 3 | Pass 1 放置 IC | 用估算高度放置所有 IC，查询实际引脚坐标 |
| 4 | 高度修正 | `query_actual_heights()` 获取实际符号高度，修正偏差 |
| 5 | Pass 2 重放 | 用实际高度删除重建，所有器件放置到精确位置 |
| 6 | 查询全部引脚 | 对所有 IC + 无源器件查询实际引脚坐标 |
| 7 | 生成连线 | `_generate_connections()` 为每个引脚计算 stub 端点 + net 名 |
| 8 | 保存 JSON | 输出 `layout_manifest.json` |

**内部调用 `LayoutCalculator.compute_module()` 对每个模块：**

> 下面这段是 `generic_runner.py` **脚本内部**的执行逻辑，仅为说明布局计算原理。**AI 无需、也不应自行编写或复制这段代码**——直接运行上面的 `generic_runner.py` 命令即可，脚本会自动完成以下步骤。

```python
# ↓ 以下为 generic_runner.py 内部逻辑，AI 不需要手写 ↓
from scripts.draw_engine import DrawEngine, LayoutCalculator, Region

engine = DrawEngine(lib_uuid="<从EDA获取>")
calc = LayoutCalculator(engine)

for module_def in design_doc.modules:
    region = Region(**module_def.region_bounds)
    manifest = calc.compute_module(module_def, region, module_def.ic_designators)
    # manifest = {ics, passives, wires, ports, flags} — 含精确坐标
```

**布局计算规则（三列绝对网格，由 `LayoutCalculator` 编码）：**

```
区域 → 三列 X 坐标直接从区域边界计算（绝对坐标，不依赖引脚位置）：

  col_left  = region.x_min + SIDE_MARGIN + col_width/2
  col_center = (region.x_min + region.x_max) / 2
  col_right = region.x_max - SIDE_MARGIN - col_width/2

Y 方向：每列独立游标，从 top-50 开始向下递减。

  ┌──────────────────────────────────────────────┐
  │  ← top_margin = 50                          │
  │  col_left        col_center      col_right  │
  │  (左引脚阻容)    (IC + 连接器)   (右引脚阻容)│
  │                                             │
  │  C1 (左)         U1 (IC)         R1 (右)    │ Y=top-50
  │  C2 (左)         U2 (IC)         R2 (右)    │ Y=top-50-40
  │  C3 (左)         J1 (CONN)       R3 (右)    │ Y=top-50-80
  │  ...              ...             ...       │
  └──────────────────────────────────────────────┘
```

| 规则 | 值 | 说明 |
|------|-----|------|
| 顶部边距 | 50 单位 | 第一行器件距区域顶边 |
| 侧边距 | 100 单位 | 列中心距区域侧边 |
| 阻容垂直间距 | **40** 单位 | CAP / RES / IND |
| LED/二极管/MOS 间距 | **60** 单位 | LED / DIODE / OTHER |
| IC 间距 | **80** 单位 | IC |
| 连接器间距 | **60** 单位 | CONN |
| 测试点 | 右边缘，bottom-up 40 单位 | TP |
| 网格对齐 | 10 的整数倍 | 所有坐标 |

> **所有坐标均为绝对坐标**，从区域边界直接计算，不依赖 IC 引脚位置。避免相对推导导致的错乱。

**输出布局预览供用户确认：**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📐 布局预览 — 模块1 (MCU最小系统) Q1 [X:50-730, Y:880-1605]

  三列 X: left=200  center=390  right=580
  起始 Y: 1555 (top-50)

  center列 (IC):
    U1  MCU型号  (390, 1555)

  left列 (阻容):
    C1  100nF  (200, 1555)
    C2  100nF  (200, 1515)   ← 间距40
    C3  10μF   (200, 1475)

  right列 (阻容):
    R1  10kΩ   (580, 1555)
    R2  4.7kΩ  (580, 1515)

  连线 (wire stubs, 全部带 net 名, 无 net_flag/net_port):
    U1.1 → VDD33 (左30)   U1.3 → GND (左30)
    U1.24 → I2C_SDA (右10) U1.23 → I2C_SCL (右10)
    C1.P1 → GND (右10)    C2.P1 → GND (右10)
    ...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**用户审核布局后确认**，进入步骤 2.4 Phase 2 批量绘制。

### 步骤 2.4：Phase 2 — 从 JSON 批量绘制 (draw_engine.py)

> `generic_runner.py` 的 Phase 2（`draw_from_manifest()`）：读取 `layout_manifest.json` → 逐模块绘制。

```bash
# Phase 2: 从已有的布局清单 JSON 执行绘制 + 自动 NC 标记
python scripts/generic_runner.py --from-manifest layout_manifest.json --mark-nc

# 或一次性执行 Phase 1 + Phase 2（不带 --dry-run）
python scripts/generic_runner.py Hardware_Design_<项目名>.md --layout layout.json --mark-nc
```

**Phase 2 内部步骤（每模块 ~4 次 bridge 调用）**，无需 AI 推理：

> 下面这段是 `generic_runner.py` **脚本内部**逻辑，仅为说明绘制原理。**AI 无需、也不应自行编写这段代码**——直接运行上面的命令即可。

```python
# ↓ 以下为 generic_runner.py 内部逻辑，AI 不需要手写 ↓
# 串行逐模块绘制（同一张图纸）
for module_name, manifest in approved_manifests:
    try:
        result = engine.draw_module(manifest, layout)
    except BridgeError as e:
        # Bridge 超时时 EDA 操作可能已完成 → 先验证再决定是否重试
        present, missing = engine.verify_placement(all_expected)
        if not missing:
            print(f"  {module_name}: bridge timed out but all components placed, continuing")
        else:
            raise  # 真正失败，向上报告
    print(f"  {module_name}: {result}")

engine.save()
```

**`draw_module()` 内部步骤（每步 1 次 bridge 调用）+ 安全检查：**

| # | 步骤 | 调用 | 安全检查 |
|---|------|------|---------|
| 1 | 放置 IC | `place_components(ics)` | **前置去重**：查询 EDA 已有位号，跳过已存在器件 |
| 2 | 查询 IC 引脚 | `query_all_ic_pins(designators)` | — |
| 3 | 放置阻容 | `place_components(passives)` | **前置去重** + 从引脚坐标推导位置 |
| 4 | 画线（wire stubs 带 net 名） | `draw_wires()` | 每个 stub 长度 ≤40 单位，`pin_side()` 自动判定方向 |
| 5 | 放置后验证 | `verify_placement(all_expected)` | **逐位号比对**，报告 missing 列表 |

> **🔴 `create_net_flags()` / `create_net_ports()` 已废弃，不在此流程中调用。** wire 带 net 名后同名 net 自动合并，不需要额外符号。这两个方法仅保留向后兼容。

**安全机制（`draw_engine.py` 内置）：**

```
place_components(components)
  ├─ filter_duplicates()      ← 查询 EDA，过滤已存在位号，防止重复放置
  ├─ [bridge call: create + setState_Designator + done]
  └─ verify_placement()       ← 逐位号确认已在图纸上，报告 missing

BridgeError (HTTP timeout)
  └─ 不立即重试 → verify_placement() 先确认是否已静默成功
       ├─ 全部到位 → 继续（bridge 超时但 EDA 操作已完成）
       └─ 有缺失 → 仅对 missing 器件重试
```

> **耗时估算：** 每模块 ~4 次 HTTP 调用 × ~250ms = ~1 秒。N 个模块 + 布局计算 ≈ **1-2 分钟**（vs 旧版 30-60 分钟）。

## 增量续绘流程

当 Level 1 检测到部分模块已完成时，进入此流程：

1. **保留现状**：已有图纸、已放置器件一概不动
2. **跳过已完成模块**：从 EDA 读取已放置器件，标记 100% 模块为完成
3. **从断点继续**：对未完成模块执行步骤 2.3（布局计算）→ 步骤 2.4（批量绘制）
4. **空间检查**：LayoutCalculator 接收 `existing_placements` 参数，自动避让已放置器件
5. **后续流程同完整绘制**：步骤 2.5-2.6 的收尾校验不变

### 步骤 2.5：收尾校验

全部子模块完成后，执行最终校验：

#### 5.1 全页 BOM 交叉验证

> 以下为**只读查询**（`getAll` + `getState_Designator`），属于执行铁律允许 AI 直接通过 bridge 执行的范围，不改变图纸。

```python
# 从 EDA 获取所有页面所有器件
all_comps = await eda.sch_PrimitiveComponent.getAll(undefined, True)

# 提取已放置位号
placed = set()
for c in all_comps:
    des = c.getState_Designator()
    if des and des != '?':
        placed.add(des)

# 与设计文档 BOM 对比
bom_required = {从设计文档提取的全部位号}
missing = bom_required - placed
extra = placed - bom_required
```

#### 5.2 DRC 检查

通知用户运行 EDA 内置 DRC，检查：
- 单网络节点（未连接引脚）
- 网络短路
- 引脚类型冲突

#### 5.3 NC 引脚标记（自动检测 + 标记）

**全部模块绘制完成后，自动检测所有未连接引脚并打上 No-Connect 标志。此步骤已由脚本 `--mark-nc` 参数完成，无需 AI 编写任何代码：**

```bash
# 绘制时带上 --mark-nc，脚本自动完成「检测 → 标记 → 验证」三步
python scripts/generic_runner.py --from-manifest layout_manifest.json --mark-nc
```

> 脚本内部依次调用 `detect_unconnected_pins()` → `mark_no_connect()` → `verify_no_connect()`，并输出三行统计：
> `Detected N unconnected pins across M ICs` / `Marked: ...` / `Verified NC: ...`
>
> **检测原理**：查询所有 wire 端点，与每个 IC 引脚坐标比较（容差 ±5 单位）。未被任何 wire 触达的引脚判定为未连接。
>
> **标记方法**：调用 EDA API `pin.setState_NoConnected(true)` + `pin.done()`，EDA 会在引脚末端显示 X 标记。
>
> **注意**：GND/VCC 引脚虽然可能只有短桩 wire，但短桩端点即算"连接"，不会被误标 NC。仅完全不画 wire 的引脚才会被标记。

#### 5.4 网络标签一致性检查

确认各模块间共享信号（I2C 总线、电源网络等）的 net 名一致，在同一张图纸内同名 net 已自动合并。

### 步骤 2.6：输出最终报告

```markdown
## 原理图绘制完成报告

### 统计
| 项目 | 数量 |
|------|------|
| 模块总数 | {N} |
| 图纸数 | {M} |
| 放置元件总数 | {total} |
| IC 数 | {ic_count} |
| 电容数 | {cap_count} |
| 电阻数 | {res_count} |
| NC 标记引脚 | {nc_count}（分布于 {nc_ic_count} 个 IC） |

### BOM 校验
- 设计文档要求: {required} 个位号
- EDA 已放置: {placed} 个位号
- 遗漏: {missing}（如有）
- 冗余: {extra}（如有）

### 待用户操作
1. 运行 EDA DRC 检查
2. 标记未使用 GPIO 为 NC
3. 导出 BOM 比对设计文档
4. 手工调整布局（如有需要）
```

---

## 绘制引擎参考

所有底层绘制操作由 `scripts/draw_engine.py` 完成，详见该文件源码。核心类：

| 类 | 职责 |
|------|------|
| `DrawEngine` | 批量 bridge 调用：`place_components()`, `query_all_ic_pins()`, `draw_wires()`, `draw_module()`, `save()` |
| `DrawEngine` (NC 标记) | `detect_unconnected_pins()`, `mark_no_connect()`, `verify_no_connect()` — 收尾阶段自动检测并标记未连接引脚 |
| `DrawEngine` (deprecated) | `create_net_ports()`, `create_net_flags()` — 仅向后兼容，禁止在绘制脚本中调用 |
| `LayoutCalculator` | 坐标计算：`compute_module()`, `query_actual_heights()`, `_generate_connections()` |
| `Region` | 数据类：`name`, `x_min`, `x_max`, `y_min`, `y_max` |

布局规则常量（可调整）：`TOP_MARGIN=50`, `SIDE_MARGIN=100`, `BOTTOM_MARGIN=50`, `STUB_LEFT=30`, `STUB_RIGHT=10`, `GRID=10`, `IC_SPACING=80`, `DEFAULT_SPACING=40`

> **`DEFAULT_HEIGHTS` 仅供 Pass 1 估算**，实际高度请用 `LayoutCalculator.query_actual_heights()` 查询。

---

## 关键约束速查

| 约束 | 说明 |
|------|------|
| **智能入口** | Level 1 自动检测项目状态，决定从哪开始，用户无需手动判断 |
| **增量续绘** | 部分完成时自动跳过已完成模块，从断点继续 |
| **决策与执行分离** | 设计文档承载所有决策（型号/引脚/网络），绘制阶段只做机械执行 |
| **🔴 禁止 AI 自写脚本** | 绘制一律由 `generic_runner.py` 完成，AI 只运行命令+解读输出，不写绘制逻辑、不直接调 bridge 手绘 |
| **🔴 两阶段硬性顺序** | 必须先 `--dry-run` 生成 `layout_manifest.json` → 用户确认 → 再 `--from-manifest` 绘制；`--layout layout.json` 必填 |
| **🔴 禁止连线** | 每个引脚只画独立短桩，**禁止 >40 单位 wire**，禁止器件间连线。同名 net 自动互联。 |
| **🔴 禁止 net_flag/net_port** | wire 带 net 名后同名 net 自动合并，不需要额外符号。`create_net_flags()` / `create_net_ports()` 禁止调用 |
| **🔴 禁止手动 side 覆盖** | `pin_side()` 基于实际引脚坐标自动判定方向（实测 100% 准确），netlist 中不得指定 `side` 字段覆盖 |
| **批量 bridge 调用** | 每模块 ~4 次 HTTP 调用（vs 旧版 ~100 次），由 `draw_engine.py` 执行 |
| **单页图纸** | 全模块共用一张图纸，禁止跨页，消除切换开销 |
| **BOM 驱动** | 设计文档结构化清单是唯一权威来源，LCSC UUID 必须在设计阶段确认 |
| **布局计算自动化** | 坐标由 `LayoutCalculator` 从区域边界直接计算（三列绝对网格），用户确认后执行 |
| **器件高度查询** | `DEFAULT_HEIGHTS` 仅供粗略估算，用 `query_actual_heights()` 获取实际引脚 Y 范围 |
| **门禁铁律** | 每模块放置完成后对照 BOM 逐项检查，全部通过才继续 |

---

## 常见故障处理

| 故障 | 处理方式 |
|------|---------|
| **脚本报错 / `sys.exit`（如"附录 C not found"）** | **禁止自写脚本绕过。** 先看报错定位：缺 `--layout layout.json` → 回步骤 2.2 生成；缺附录 C/附录 B → 用 `--layout` 参数替代；UUID 失效 → 回设计文档修正。按「执行铁律」第 3 条顺序排查 |
| 某器件 LCSC UUID 失效 | 回设计阶段用 `lcsc-mcp` 重新搜索，更新设计文档中对应 UUID |
| 布局坐标超出区域 | 调整区域边界或缩减模块内阻容间距，重跑 `LayoutCalculator` |
| EDA Bridge 连接断开 | 重新运行 `start-bridge.sh`，通知用户检查 EDA 扩展 |
| `draw_module()` 返回部分失败 | 检查 bridge 返回的 error 字段；从已放置的 primitiveId 继续，不需重来 |
| 图纸空间不足 | 重新评估图纸大小（A4→A3→A2），通知用户创建新图纸并迁移 |
| Level 1 检测到图纸状态与 BOM 不一致 | 输出差异报告，让用户选择：忽略 / 清理重建 / 手动修复 |
| 设计文档缺少结构化清单 | 回 `/hardware-design` 补充器件清单和连线清单章节 |
| **Y 间距过大，空间紧张** | `DEFAULT_HEIGHTS` 偏大导致 → 调用 `query_actual_heights()` 获取实际高度，执行两趟布局（Pass 1 估算 → Pass 2 精确） |
| **wire 方向画反了** | 检查是否在 netlist 中手动指定了 `side` → 删除所有 `side` 字段，信任 `pin_side()` 自动判定 |
| **图纸上出现重复的电源/地符号** | 调用了 `create_net_flags()` 或 `create_net_ports()` → 删除这些调用，wire 带 net 名已足够 |
