# 贡献指南

感谢你对本项目的关注。欢迎通过以下方式参与贡献。

## 报告问题

提交 issue 时，请尽量包含：

- 复现步骤（越具体越好）
- 期望行为与实际行为
- 环境信息：操作系统、Python 版本、嘉立创 EDA 专业版版本
- 相关日志或报错信息

如果是原理图绘制相关的问题，请附上：

- 设计文档中相关模块的结构化表格（脱敏后）
- `layout_manifest.json` 中相关片段
- `generic_runner.py` 的完整输出

## 提交代码

1. Fork 本仓库并创建功能分支
2. 遵循现有代码风格：
   - Python 遵循 PEP 8，使用类型注解
   - 保持函数简短、单一职责
3. 提交信息遵循约定式提交（Conventional Commits）：

   ```
   <type>: <description>
   ```

   类型：`feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf` / `ci`

4. 提交前自测：确保 `draw_engine.py`、`generic_runner.py` 可正常导入运行
5. 发起 Pull Request，描述改动动机与影响范围

## 许可证

贡献的代码将随本仓库一起以 MIT License 发布。提交 PR 即表示你同意该授权。
