#!/usr/bin/env bash
# EasyEDA Bridge Server 一键启动脚本
# 用法: bash start-bridge.sh

SKILL_DIR="$HOME/.claude/skills/easyeda-api"
BRIDGE_PORT=""

# 1. 检查是否已运行
for port in $(seq 49620 49629); do
  resp=$(curl -s http://localhost:$port/health 2>/dev/null)
  if echo "$resp" | grep -q '"easyeda-bridge"'; then
    BRIDGE_PORT=$port
    break
  fi
done

if [ -n "$BRIDGE_PORT" ]; then
  echo "Bridge 已在端口 $BRIDGE_PORT 运行"
  curl -s http://localhost:$BRIDGE_PORT/health
  exit 0
fi

# 2. 安装依赖（如需要）
if [ ! -d "$SKILL_DIR/node_modules/ws" ]; then
  echo "正在安装依赖..."
  cd "$SKILL_DIR" && npm install --silent
fi

# 3. 启动 Bridge Server（后台）
echo "正在启动 Bridge Server..."
node "$SKILL_DIR/scripts/bridge-server.mjs" &
sleep 2

# 4. 查找端口并验证
for port in $(seq 49620 49629); do
  resp=$(curl -s http://localhost:$port/health 2>/dev/null)
  if echo "$resp" | grep -q '"easyeda-bridge"'; then
    BRIDGE_PORT=$port
    break
  fi
done

if [ -n "$BRIDGE_PORT" ]; then
  echo "Bridge 启动成功，端口: $BRIDGE_PORT"
  curl -s http://localhost:$BRIDGE_PORT/health
else
  echo "错误: Bridge 启动失败"
  exit 1
fi
