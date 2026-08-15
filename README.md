# AI 驱动嘉立创 EDA 原理图自动绘制系统

基于**嘉立创 EDA 专业版**（EasyEDA Pro）的 AI 原理图自动绘制工具链。采用**决策与执行分离**的架构设计，将硬件设计划分为设计阶段和绘制阶段，通过结构化数据文档桥接两个阶段。

## 架构概览

```
需求文档 → /hardware-design → 设计文档 (.md) → generic_runner.py Phase 1 → layout_manifest.json → Phase 2 → 原理图完成
               │                      │                        │                          │
               │   器件选型+电路设计    │  结构化表格             │  布局计算+LCSC解析+连线    │  批量绘制
               │   (所有决策在此完成)   │  (器件清单+连线清单)     │  (含精确坐标+net+wire)     │  (纯机械执行)
```

- **Phase 1 — 设计**：需求分析 → 器件选型（P0-P3 优先级）→ 电源树 → GPIO 分配 → 分模块电路设计。输出结构化器件清单 + 引脚连线清单。
- **Phase 2 — 绘制**：
  - Level 1：自动状态检测（设计文档/EDA 连接/图纸现状）→ 智能路由
  - Level 2 Phase 1：`generic_runner.py` 解析设计文档 → 布局计算（三列绝对网格）→ LCSC 解析 → 两趟高度修正 → 生成 `layout_manifest.json`（含精确坐标、net 名、wire 端点）
  - Level 2 Phase 2：读取 `layout_manifest.json` → 批量绘制 → 收尾校验（BOM 交叉验证 + DRC + NC 引脚标记）
  - 支持 `--dry-run`（仅生成 JSON）和 `--from-manifest`（仅从 JSON 绘制）独立运行

## 环境要求

| 组件 | 说明 |
|------|------|
| 嘉立创 EDA 专业版 | **桌面客户端**（网页版无法连接本地 WebSocket） |
| EDA 扩展 | `run-api-gateway.eext` 扩展插件 |
| Bridge Server | WebSocket Bridge（端口 49620-49629），HTTP POST `/execute` |
| Python | 3.9+（运行绘制脚本） |
| lcsc-mcp | 立创商城元器件搜索（可选，用于器件选型阶段） |

### 外部依赖

以下组件不在本仓库内，需单独获取安装（详见 [NOTICE](NOTICE)）：

| 组件 | 作用 | 获取方式 |
|------|------|---------|
| `easyeda-api` | EasyEDA Pro API 参考 skill，提供 Bridge Server（`bridge-server.mjs`） | 配套 skill，安装到 `~/.claude/skills/easyeda-api` |
| `run-api-gateway.eext` | 嘉立创 EDA 专业版扩展插件，暴露本地 API 网关 | 嘉立创 EDA 扩展管理器搜索「RUNAPI」安装 |
| `lcsc-mcp` | 立创商城元器件搜索 MCP | 自行安装为 Claude Code MCP（可选） |

### 启动 Bridge Server

```bash
bash scripts/start-bridge.sh          # 一键启动
curl http://localhost:49620/health    # 验证连接（edaConnected=true）
```

## 文件说明

```
├── README.md                        # 本文件
├── LICENSE                          # MIT 许可证
├── NOTICE                           # 版权声明与第三方依赖
├── CONTRIBUTING.md                  # 贡献指南
├── CLAUDE.md.template               # 项目配置模板（复制到新项目根目录）
├── skills/                          # Claude Code Skill 文件
│   ├── hardware-design.md           # Phase 1: 硬件详细设计
│   ├── schematic-draw.md            # Phase 2: 原理图绘制
│   ├── schematic-rules.md           # 参考手册：布局规则、网络标签方法、门禁清单
│   ├── review-sch.md                # 原理图审查
│   └── component-research.md        # 器件研报
└── scripts/                         # Python 绘制引擎
    ├── draw_engine.py               # 核心绘制引擎（批量桥接、布局计算、安全检查）
    └── generic_runner.py            # 通用绘制入口（解析设计文档、驱动引擎）
```

## 快速开始

### 1. 初始化新项目

```bash
mkdir my-hardware-project && cd my-hardware-project
cp /path/to/published-skills/CLAUDE.md.template ./CLAUDE.md
mkdir -p .claude/commands scripts
cp /path/to/published-skills/skills/*.md .claude/commands/
cp /path/to/published-skills/scripts/*.py scripts/
```

### 2. 编辑 CLAUDE.md

将 `CLAUDE.md` 中的 `<项目名>` 替换为实际项目名称。

### 3. 配置 EDA 环境

- 安装嘉立创 EDA 专业版桌面客户端
- 安装 `run-api-gateway.eext` 扩展
- 创建 `scripts/start-bridge.sh` 启动脚本

### 4. 开始设计

在 Claude Code 中：
- 输入 "设计硬件" 启动 Phase 1 硬件设计
- 设计文档生成后，输入 "画原理图" 启动 Phase 2 自动绘制

## 核心设计理念

### 决策与执行分离

所有设计决策（选型、引脚分配、网络命名）在设计文档中完成，绘制阶段为纯机械执行，不涉及 AI 推理。这确保了：

- **可复现性**：同一份设计文档每次绘制结果一致
- **可审查性**：设计决策在文档中可追溯、可讨论
- **效率**：绘制阶段每模块 ~4 次 HTTP 调用，无需 AI 推理

### 三列绝对网格布局

```
┌──────────────────────────────────────────────┐
│  col_left        col_center      col_right   │
│  (左引脚阻容)    (IC + 连接器)   (右引脚阻容) │
│  C1              U1              R1          │
│  C2              U2              R2          │
│  ...             J1              ...         │
└──────────────────────────────────────────────┘
```

- X 坐标从区域边界直接计算，不依赖引脚坐标
- Y 从顶部向下排列，边到边间距按器件类型区分
- 所有坐标网格对齐（10 单位）

### 只打标签，禁止连线

每个引脚只画独立短桩 wire（左 30 / 右 10 单位），带 net 名。同名 net 在 EDA 中自动合并为同一网络，无需物理连线。

### 放置安全检查

```
place_components()
  ├─ filter_duplicates()  ← 前置去重，防止重复放置
  ├─ [bridge call]        ← 执行放置
  └─ verify_placement()   ← 后置验证，确认所有器件已到位
```

## 注意事项

- 必须使用嘉立创 EDA **桌面客户端**（网页版无法连接本地 WebSocket）
- Bridge 内部有 30s 超时限制，超时不等于操作失败（EDA 操作可能已静默完成）
- 电源/地符号（VCC/GND）需手动放置，AI 自动放置可能不正确
- EDA 扩展在非原理图/PCB 页面不会自动连接

## 许可证

Copyright (c) 2026 **AI伙伴计划**

本项目基于 [MIT License](LICENSE) 发布，详见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE) 文件。

- 你可以自由使用、修改、分发和商用本项目的代码
- 只需在副本中保留版权声明与许可证文本
- 本项目按「现状」提供，不附带任何担保
