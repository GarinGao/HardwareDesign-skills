## 原理图绘制规范（参考手册 / Reference Manual）

> **状态：参考手册。** 本文档中的布局规则（§1）已由 `scripts/draw_engine.py` 的 `LayoutCalculator` 类编码实现，不再需要人工/AI 阅读理解后执行。网络标签方法（§2）、器件操作（§3）、门禁清单（§4）保留作为设计约束参考，供 `/hardware-design` 阶段和手动审查时查阅。
>
> 绘制执行时，`draw_engine.py` 自动应用以下编码规则：
> - 阻容距 IC 引脚 150 单位 (`PASSIVE_OFFSET`)
> - 阻容垂直间距 ≥40 (`PASSIVE_Y_SPACING`)
> - IC 垂直间距 ≥80 (`IC_Y_SPACING`)
> - 所有坐标网格对齐 10 (`GRID`)
> - 短桩长度 5 单位 (`STUB_LEFT` / `STUB_RIGHT`)
> - 碰撞检测 ≥30x, ≥20y
>
> 规则常量可在 `draw_engine.py` 中调整，无需修改本文档。

---

### 1. 布局规则（三列绝对网格）

> 坐标原点 (0,0) = 左下角，X→右，Y→上。1 单位 = 10 mil。所有坐标取 10 的整数倍。

#### 核心原则：绝对坐标，不依赖引脚位置

**三列 X 坐标从区域边界直接计算，Y 从顶部向下排列。** 不再从 IC 引脚推导阻容位置，避免相对坐标导致的错乱。

```
区域 = [x_min, x_max, y_min, y_max]

col_w = (x_max - x_min - 2 × SIDE_MARGIN) / 3

col_left   = x_min + SIDE_MARGIN + col_w/2    （左侧阻容）
col_center = (x_min + x_max) / 2              （IC + 连接器）
col_right  = x_max - SIDE_MARGIN - col_w/2    （右侧阻容）

Y 起始 = y_max - TOP_MARGIN  （从顶部 50 单位开始）
```

```
┌────────────────────────────────────────────────┐
│  ← TOP_MARGIN=50                               │
│  col_left         col_center       col_right   │
│  (左引脚阻容)     (IC + 连接器)    (右引脚阻容) │
│  C1               U1               R1          │ ← Y=top-50
│  C2               U2               R2          │ ← Y递减
│  ...              J1               ...         │
│                   (连接器)                      │
└────────────────────────────────────────────────┘
```

#### 1.1 间距与高度规格

| 器件类型 | 边到边间距 | 默认符号高度 | 列归属 |
|---------|-----------|------------|--------|
| IC | **80** 单位 | 200 单位 | center |
| 连接器 | **60** 单位 | 120 单位 | center |
| 阻容/电感 (CAP/RES/IND) | **40** 单位 | 40-60 单位 | left 或 right（按引脚侧） |
| LED/二极管/MOS/三极管 | **60** 单位 | 60-80 单位 | left 或 right（按引脚侧） |
| 测试点 | **40** 单位 | 30 单位 | 右边缘 bottom-up |
| 网格对齐 | **10** 单位 | — | 全部 |
| 默认封装 | **0402**（阻容 <10μF）、**0603**（≥10μF） | — | — |
| 位号格式 | **类型字母 + 数字编号** | — | 禁止纯字母 |

> **间距 = 边到边距离**，不是中心到中心。每个器件的 Y 中心 = 上一个器件底边 - 间距 - 自身高度/2。首个器件顶边 = 区域顶部 - 50。此规则由 `LayoutCalculator` 编码实现。

> **自定义高度**：模块定义中每个器件可指定 `height` 字段覆盖默认值（如 ESP32 模块约 400 单位）。`LayoutCalculator.compute_module()` 接收 `height` 参数。

> **底部边界检查**：放置完成后检查每个列的 `next_top` 不低于 `region.y_min + BOTTOM_MARGIN(50)`，超出时输出 WARN。

> **阻容归属**：IC 左半侧引脚的阻容 → col_left；右半侧引脚 → col_right。由设计文档中 `side` 字段指定，不依赖运行时引脚坐标判断。

#### 1.2 器件高度：硬编码值仅供粗略估算

> **🔴 DEFAULT_HEIGHTS 仅用于 Pass 1 粗略估算，不可作为最终布局依据。** 实测表明硬编码高度与实际 EDA 符号尺寸偏差 2-5 倍：

| 器件 | 硬编码高度 | 实际引脚 Y 范围 | 偏差 |
|------|----------|---------------|------|
| TPS54335A (U8) | 200 | ~40 | 5x |
| PMW3901 (U5) | 200 | ~140 | 1.4x |
| SS34 二极管 (D1) | 80 | ~40 | 2x |
| ESP32-S3-MINI-1 (U1) | 200 | ~290 | 0.7x（偏小） |

> **正确做法**：放置后通过 `query_all_ic_pins()` 查询实际引脚 Y 范围，用 `max(pin_y) - min(pin_y)` 作为实际高度。`LayoutCalculator.query_actual_heights()` 已封装此查询。
>
> **空间紧张时执行两趟布局**：Pass 1 用估算值放置 → `query_actual_heights()` 获取实际高度 → Pass 2 用实际值重新计算 Y 坐标并重放。

#### 1.3 放置安全检查（防重复、防遗漏）

**每次 `place_components()` 必须执行以下三步：**

```
1. 【前置去重】查询 EDA 已有 designator → 过滤已存在位号，禁止重复放置
2. 【执行放置】仅对"新"位号调用 create + setState_Designator + done
3. 【后置验证】逐位号查询确认已在图纸上，报告 missing 列表
```

**Bridge 超时处理：**

```
BridgeError (HTTP timeout, bridge 内部 30s 限制)
  ├─ 禁止直接重试！超时不等于失败，EDA 操作可能已静默完成
  ├─ 先调用 verify_placement() 确认各 designator 是否已在图纸上
  ├─ 全部到位 → 继续执行，不需重试
  └─ 有缺失 → 仅对 missing 器件重试 place_components
```

> **铁律：任何时候不重复放置同一位号。** 重复放置 = 两个同 designator 的器件重叠在同一位置，难以清理。
> 此规则由 `draw_engine.py` 的 `place_components()` 内置执行，手工操作时也须遵循。

---

### 2. 网络标签方法（核心）

#### 🔴 铁律：只打标签，禁止连线！(最高优先级)

**每个引脚只画一根短桩 wire 带 net 名。禁止画任何 wire 连接两个不同器件。同名 net 自动互联，不需要物理连线。**

```
❌ 错误: IC引脚 ────[长线 150单位]──── 电容引脚    （器件间连线）
❌ 错误: IC引脚A ──[长线]── IC引脚B                （引脚间连线）
❌ 错误: GND总线贯穿多个器件                        （总线连线）
✅ 正确: IC引脚 → 30单位短桩(net="GND")             （独立短桩）
✅ 正确: 电容引脚 → 10单位短桩(net="GND")           （独立短桩）
✅ 正确: 同名net在EDA自动合并，无需物理连接
```

**单根 wire 最大长度限制：**
| 引脚位置 | 最大线长 | 方向 |
|---------|---------|------|
| IC 左侧引脚 | ≤ **30** 单位 | 向左 (pin_x - 30) |
| IC 右侧引脚 | ≤ **10** 单位 | 向右 (pin_x + 10) |
| 阻容等无源器件 | ≤ **10** 单位 | 向外侧 |
| 任何 wire | **禁止 > 40** 单位 | — |

#### 引脚方向自动判定

**Stub 方向由引脚在器件哪一侧自动决定**，不允许硬编码方向：

```python
def pin_side(pin_x, pin_y, comp_x, comp_y) -> str:
    dx = pin_x - comp_x     # pin 相对于器件中心的 X 偏移
    dy = pin_y - comp_y     # pin 相对于器件中心的 Y 偏移
    if abs(dx) >= abs(dy):
        return "left" if dx < 0 else "right"
    else:
        return "bottom" if dy < 0 else "top"
```

| 判定结果 | stub 方向 | 长度 |
|---------|----------|------|
| `"left"` | 向左 `(pin_x - 30, pin_y)` | ≤30 |
| `"right"` | 向右 `(pin_x + 10, pin_y)` | ≤10 |
| `"top"` | 向上 `(pin_x, pin_y + 10)` | ≤10 |
| `"bottom"` | 向下 `(pin_x, pin_y - 10)` | ≤10 |

> **关键约束**：
> - 器件中心从 `sch_PrimitiveComponent.x/y` 获取
> - 引脚坐标从 `getAllPinsByPrimitiveId()` 获取
> - 比较 `px` 与 `comp_x`：`px < comp_x` → 引脚在左 → stub 向左
> - **必须查询引脚实际坐标后再画线**，不可根据位号猜测引脚位置
> - **🔴 禁止在 netlist 中手动指定 `side` 字段来覆盖自动判定方向。** 实测验证：`pin_side()` 在所有已测 IC 上 100% 正确，手动覆盖反而导致方向错误。`draw_engine.py` 的 `_generate_connections()` 已移除 `override_side` 参数。

**🔴 所有网络标签通过 wire 带 net 名实现，禁止额外放置 net_flag / net_port / createNetFlag / createNetPort 符号。即使 `draw_engine.py` 仍保留了 `create_net_flags()` 和 `create_net_ports()` 方法（向后兼容），绘制脚本中也不应调用它们。**

```python
def wire(x1, y1, x2, y2, net=None):
    net_arg = f", '{net}'" if net else ""
    return exec_eda(f"return await eda.sch_PrimitiveWire.create([{x1},{y1},{x2},{y2}]{net_arg});")

# 左侧引脚 → 向外画 30 单位短线 + net 名
wire(ic_x_left,  pin_y, ic_x_left - 30, pin_y, "SDA")

# 右侧引脚 → 10 单位即可，文字自然向外延伸
wire(ic_x_right, pin_y, ic_x_right + 10, pin_y, "SCL")

# 电容/GND引脚 → 10 单位短线
wire(cap_x, cap_y, cap_x + 10, cap_y, "GND")
```

> **左侧引脚线长 30 单位**，配上限 8 字符标签名确保不重叠。API 无标签对齐控制，只能通过线长解决。
> 右侧引脚 10 单位即可。
>
> **网络标签命名限制：最长 8 个字符。** 超出时使用缩写。

| 场景 | 方法 |
|------|------|
| 所有信号、电源、地标签 | wire 带 net 名，不需要 net_flag |
| 跨 Sheet 连接（仅收尾阶段） | net_port |

**同名 net 的 wire 在 EDA 中自动合并为同一网络，跨模块即自动互联。**

---

### 3. 器件操作参考

#### 3.1 获取器件

```python
# 首选 LCSC 编号
r = exec_eda('return await eda.lib_Device.getByLcscIds("C138706");')

# 次选关键字搜索
r = exec_eda('return await eda.lib_Device.search("INA219", LIB, undefined, undefined, 10, 1);')
```

> `libraryUuid` 必须用 `getByLcscIds()` 返回的实际值，传空字符串会超时 30s。

#### 3.2 放置 IC → 查引脚 → 放阻容

```python
# 1) 放置 IC
r = exec_eda(f'return await eda.sch_PrimitiveComponent.create({{libraryUuid:"{LIB}",uuid:"{UUID}"}}, {x}, {y});')
pid = r['result']['primitiveId']
exec_eda(f"const c=await eda.sch_PrimitiveComponent.get('{pid}');c.setState_Designator('U{idx}');return await c.done();")

# 2) 查询引脚位置（必须！）
pins = exec_eda(f'return await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId("{pid}");')
# → [{pinNumber, pinName, x, y, pinType}, ...]

# 3) 根据引脚位置放阻容（按 §1 间距规则）
r = exec_eda(f'return await eda.sch_PrimitiveComponent.create({{libraryUuid:"{LIB}",uuid:"{UUID}"}}, {x}, {y});')
pid = r['result']['primitiveId']
exec_eda(f"const c=await eda.sch_PrimitiveComponent.get('{pid}');c.setState_Designator('{DES}');return await c.done();")

# 4) 打网络标签（按 §2 方法），同名 net 自动互联，无需额外连物理线
```

**每放完一颗阻容，立即在 BOM 勾销表中打勾 ✓。**

#### 3.3 非 IC 锚定器件放置

以下器件不连接 IC 的 VCC/GND/信号引脚，无法从 `getAllPinsByPrimitiveId()` 推导位置，必须逐类单独处理：

**A. 跨引脚电容（如 QMC5883P SETC、PMW3901 VREG）**

电容跨接在 IC 的两个专用引脚之间，位置取两引脚中点并偏移到 IC 外侧：

```python
pins = exec_eda(f'return await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId("{pid}");')
pin_a = next(p for p in pins if p.pinName == 'SETC')
pin_b = next(p for p in pins if p.pinName == 'SETP')
cx = round((pin_a.x + pin_b.x) / 2 / 10) * 10
cy = round((pin_a.y + pin_b.y) / 2 / 10) * 10
# 水平偏移到 IC 左侧或右侧，按 §1 间距
x = cx - 150 if side == 'left' else cx + 150
```

**B. 连接器阻容**

连接器无 `getAllPinsByPrimitiveId()`，以连接器坐标为基准 + 固定偏移放置：

```python
# 连接器 VCC 去耦电容 → 连接器 VCC 引脚侧，水平偏移 150
cap_x = conn_x + 150  # 右侧
cap_y = conn_vcc_y    # 与连接器 VCC 引脚 Y 对齐
```

**C. 总线串联器件（如 I2C 0Ω 隔离电阻）**

串联在信号路径中的器件，放在 IC 引脚与上拉电阻/目标器件之间：

```python
# 每路 I2C 的 SDA/SCL 各串一颗 0Ω，位置取 IC 引脚到总线的中间点
r_x = ic_pin_x + 60  # 从 IC 引脚水平偏移 60 单位
r_y = ic_pin_y       # 与该路 I2C 引脚 Y 对齐
```

**D. 测试点**

以模块区域边界为参考放置，集中排列，垂直间距 ≥ 40：

```python
tp_base_x = module_region['x_max'] - 100
tp_base_y = module_region['y_min'] + 50
for i, tp_label in enumerate(test_point_labels):
    y = tp_base_y + i * 40
    # 放置测试点符号并连线
```

---

### 4. 模块完成门禁（强制执行）

**本模块所有器件放置完成后，必须对照 BOM 勾销表逐项自检，全部通过才能返回结果：**

```
模块 X 完成门禁：
  □ BOM 勾销表所有项目已打勾（对照主 Agent 提供的 BOM 逐行确认）
  □ 每个 VCC/VDD/VDDIO/VLOGIC 引脚旁有对应的去耦电容
  □ 每个 EN/RESET/XSHUT/NCS/MOTION/IO_CFG 等控制引脚有上拉/下拉电阻
  □ 跨引脚专用电容（SETC、VREG、CP 等）已放置
  □ 连接器配套阻容（去耦电容、保护电阻）已放置
  □ 总线串联器件（隔离电阻、匹配电阻）已放置
  □ 测试点/跳线已放置（如本模块需要）
  □ 所有信号引脚有 wire 带 net 名引出
  □ 所有电源/GND 引脚有 wire 带 net 名引出
  □ 🔴 所有 wire 长度 ≤ 40 单位（无器件间连线！每个引脚独立短桩）
  □ 占位跟踪表已更新，无碰撞
  □ DRC 无新增错误

未通过项: ___________ → 立即补齐，不得推迟
```

> **铁律：门禁未通过，不得返回结果。** 每次推迟都等于遗忘。
>
> **🔥 连线自检（门禁前强制执行）：** 遍历所有 wire，任一长度 > 40 单位 = 违规，必须删除重画为短桩。

---

### 5. 常见错误速查

| 错误 | 原因 | 正确做法 |
|------|------|---------|
| **🔴 画了器件间连线（最严重！）** | 不理解"同名net自动互联"机制，画长线连接IC和电容 | **每个引脚只画独立短桩**（左30/右10），禁止任何 >40 单位的 wire |
| **🔴 GND/电源总线贯穿多个器件** | 试图用一根长线串起所有GND引脚 | 每个GND引脚独立画10单位短桩 net="GND"，自动合并 |
| **整个模块阻容全部遗漏** | 只放了 IC + 信号线，跳过 BOM 勾销和门禁 | **必须**逐项对照 BOM 放置，门禁通过才能返回 |
| **跨引脚电容遗漏**（SETC/VREG/CP） | 不在 VCC 引脚上，`pin_x ± 150` 流程覆盖不到 | 按 §3.3-A 从两引脚中点推导位置 |
| **连接器阻容遗漏** | 连接器不是 IC，流程跳过了 | 按 §3.3-B 以连接器坐标为基准放置 |
| **I2C 隔离电阻遗漏** | 串联在总线中，不在任何 IC 引脚上 | 按 §3.3-C 放在 IC 引脚到总线中间 |
| **测试点全部遗漏** | 无 IC 锚点，流程天然不覆盖 | 按 §3.3-D 在模块边界集中排列 |
| **上拉电阻遗漏**（EN/XSHUT/NCS） | 非 VCC/GND 引脚，信号连完就以为完成了 | BOM 勾销表逐引脚对照；每 IC 放完后查 `pinType` 为 input 的控制引脚 |
| 阻容与已有器件重叠 | 放之前没检查已占坐标 | **先查占位表**，保守估计大器件 (二极管/电感) 边界 |
| 阻容距 IC 仅 30 单位 | 坐标直接手写，没计算 | **必须** `pin_x ± 150`，禁止硬编码坐标 |
| 阻容放错侧（左/右颠倒） | 未查询 IC 引脚布局 | 先 `getAllPinsByPrimitiveId()` 获取引脚位置 |
| 阻容垂直重叠 | 间距仅 10–15 单位 | 垂直间距 ≥ **40** 单位 |
| 多芯片间距不足 | 垂直排布太密 | IC 中心 Y 间距 ≥ **80** 单位 |
| 标签文字位置不对 | 用 `createNetPort` 放信号标签 | 用 **wire 带 net 名**，标签自动显示在线上 |
| 冗余 net_port | wire 已带标签又放 net_port | wire 带 net 名后不需再放 net_port |
| `create()` 超时 30s | `libraryUuid` 传空字符串 | 必须用 `getByLcscIds()` 返回值 |
| `create()` 取不到 ID | 用了 `.id` | 字段是 `primitiveId` |
| `create()` 字符串格式超时 | `create("lib:uuid", x, y)` | 必须对象格式 `create({libraryUuid, uuid}, x, y)` |
| `mirror` 报 HTTP 500 | 传数字 `0` | 必须 JS boolean `false` |
| `createNetFlag` 报 undefined | 方法赋给变量后调用 | 必须直接调用 `eda.sch_PrimitiveComponent.createNetFlag(...)` |
| delete 不生效 | 用了错误 ID 或方法名 | `eda.sch_PrimitiveComponent.delete(primitiveId)` |
| 位号纯字母无编号（R、C、U） | `setState_Designator` 只传了类型字母，没加序号 | `setState_Designator('R1')` 而非 `setState_Designator('R')` |
| **netlist 中手动指定 `side` 覆盖** | 不相信 `pin_side()` 自动判定，在 netlist 里加 `"side": "right"` | **禁止手动指定 side 字段**。`pin_side()` 基于实际引脚坐标与器件中心比较，100% 准确 |
| **Y 间距过大，空间浪费** | `DEFAULT_HEIGHTS` 硬编码值比实际 EDA 符号大 2-5 倍 | 放置后用 `query_actual_heights()` 获取实际引脚 Y 范围；空间紧张时执行两趟布局 |
| **放置了 `createNetFlag` / `createNetPort`** | 误以为需要额外的电源/地符号 | **禁止调用。** wire 带 net 名已足够，同名 net 在 EDA 中自动合并。net_flag 符号反而与 wire 标签重复 |

---

### 6. 设计文档编写规则（🔴 绘制前强制检查）

> **设计文档是绘制的唯一数据源。** 文档中的数据错误不会被脚本报错拦截，只会导致图纸上器件缺线、标错网络、甚至引脚完全遗漏。以下规则在 `/hardware-design` 阶段执行，在 `/schematic-draw` 开始前由 `generic_runner.py` 做自动校验。

#### 6.1 BOM-连线交叉验证

**每个器件清单中的器件，必须在对应模块的无源连线表中有对应条目。** 缺条目 = 该器件放置后两端无出线，属于静默遗漏。

```
检查方法（每模块逐一核对）：
  器件清单: C1, C2, C3, L1, R1, R2, R3, R3b
  无源连线表: C1, C2, C3, R1, R2, R3+R3b
  差异: L1 在器件清单中存在但无源连线表缺失 → 🔴 遗漏！
```

**已发现的问题案例**：L1（功率电感）在模块器件清单中存在但无源连线表缺失 → 放置后两端无出线。

**设计文档编写时**：每完成一个模块，跑一遍器件清单 vs 无源连线表的逐行对比。`generic_runner.py` 在解析阶段自动检查并输出 WARN。

#### 6.2 Net 名禁止使用括号（含中文括号）

**无源连线表和 IC 连线表中的 net 名字段，不得使用 `()` `（）` 包裹任何内容。** 括号内的文本被 `generic_runner.py` 解析器视为注释/占位符，整条连线会被跳过。

```
❌ 错误：
| R20 | PMW_LED_N | (红外LED阴极) |   ← net名被括号包裹，视为无效条目，R20右侧无出线
| R26 | GND | （直连 U4.SDO）     |   ← 同上，中文括号也会被跳过

✅ 正确：
| R20 | PMW_LED_N | PMW_LED |         ← 使用实际的 net 名
| R26 | GND | BMP_SDO |               ← 使用实际的 net 名
```

**如果你需要标注备注**：在表格外另写文字说明，不要把备注放在 net 名字段内。

**已发现的问题案例**：R20 pin2_net=`(红外LED阴极)`, R26 pin2_net=`(直连 U4.SDO)` → 被解析器跳过，器件右侧无出线。

#### 6.3 IC 引脚引用必须先通过 EDA 验证

**设计文档中 IC 引脚连线表的每一条，必须在 EDA 中放置 IC 后验证引脚名称和编号正确。** 不要仅凭 datasheet 写引脚引用——EDA 符号的引脚命名可能与 datasheet 不同。

```
验证流程：
  1. 在 EDA 中放置 IC → 获取 primitiveId
  2. 调用 getAllPinsByPrimitiveId(pid) → 获取 EDA 实际引脚列表
  3. 逐条对照设计文档中的 pin 引用与 EDA 返回的 pinName/pinNumber
  4. 不一致的 → 以 EDA 实际引脚名为准，修改设计文档
```

**常见不匹配类型**：

| 类型 | 示例 | 后果 |
|------|------|------|
| **命名体系不同** | datasheet 叫 SDA/SCL，EDA 符号叫 SDI/SCK（BMP280 用 SPI 名称） | 按名称查不到引脚，连线被跳过 |
| **引脚编号偏移** | datasheet 按功能顺序描述，EDA 符号按物理封装排列，所有引脚号差 4-5 位 | 全部引脚连错网络 |
| **引脚功能错误** | 将 SDO 引脚误标为 GND | 信号短路到地 |

**🔴 优先使用数字引脚引用**：当 EDA 符号的引脚名与 datasheet 不一致时，用数字编号引用（如 `U4.3`、`U4.4`）代替名称引用（如 `U4.SDA`、`U4.SCL`）。数字编号在 EDA 中唯一且不会因命名差异而匹配失败。

#### 6.4 `generic_runner.py` 自动校验清单

`generic_runner.py` 在解析设计文档时自动执行以下检查，发现问题时输出 WARN/ERROR：

| 检查项 | 级别 | 说明 |
|--------|------|------|
| 器件清单 vs 无源连线表交叉比对 | WARN | 器件在清单中存在但无源连线表缺失 |
| Net 名括号检测 | ERROR | net 名以 `(` 或 `（` 开头 → 拒绝解析 |
| 复合位号拆分检测 | INFO | 检测到 `+` 连接的设计符号，自动拆分 |
| IC 引脚重复检测 | ERROR | 同一 IC 同一引脚号出现两次 net 分配 |
| 引脚引用格式验证 | WARN | 非 `Ux.N` 或 `Ux.NAME` 格式的条目 |

---

### 7. 收尾校验（全部模块绘制完成后执行）

#### 7.1 NC 引脚标记（自动检测 + 标记）

**全部模块绘制完成后，自动检测所有 IC 的未连接引脚并打上 No-Connect 标志：**

```python
from scripts.draw_engine import DrawEngine

engine = DrawEngine(lib_uuid="<从EDA获取>")

# 1) 检测所有 IC 的未连接引脚
unconnected = engine.detect_unconnected_pins()
# → {"U1": ["4","5","6",...], "U3": [...], ...}

# 2) 标记 NC（每 IC 一次 bridge 调用）
result = engine.mark_no_connect(unconnected)
# → {"marked": 42, "failed": 0, "details": [...]}

# 3) 验证 NC 标记已正确设置
verify = engine.verify_no_connect(list(unconnected.keys()))
# → {"totalNC": 42, "details": [...]}

engine.save()
```

**检测原理**：查询所有 wire 端点，与每个 IC 引脚坐标比较（容差 ±5 单位）。未被任何 wire 触达的引脚判定为未连接。

**标记方法**：调用 EDA API `pin.setState_NoConnected(true)` + `pin.done()`，EDA 在引脚末端显示 X 标记。

> GND/VCC 引脚虽然可能只有短桩 wire，但短桩端点即算"连接"，不会被误标 NC。仅完全不画 wire 的引脚才会被标记。

#### 7.2 全页 BOM 交叉验证

```python
# 从 EDA 获取所有已放置器件
all_comps = await eda.sch_PrimitiveComponent.getAll(undefined, True)
placed = {c.designator for c in all_comps if c.designator and c.designator != '?'}

# 与设计文档 BOM 对比
bom_required = {从设计文档提取的全部位号}
missing = bom_required - placed
extra = placed - bom_required
```

#### 7.3 DRC 检查

通知用户运行 EDA 内置 DRC，检查：
- 单网络节点（仅连接一个引脚的 net）
- 网络短路
- 引脚类型冲突

> 单网络警告中，连接器引脚（外部接口）、测试点、NC 引脚为预期行为，可忽略。
