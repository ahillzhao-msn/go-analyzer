#!/bin/bash
# ============================================================
# Go Analyzer Worker — 一键部署脚本
# 用法: curl -sL https://setup.go-analyzer.dev/worker | bash
#   或: bash deploy_worker.sh <master_host>
# ============================================================
set -e

MASTER_HOST="${1:-192.168.1.100}"
WORKER_DIR="$HOME/go-analyzer-worker"
KATAGO_VERSION="v1.16.4"
SYSTEM=$(uname -s)

echo "=== Go Analyzer Worker 部署 ==="
echo "目标: $WORKER_DIR"
mkdir -p "$WORKER_DIR/models" "$WORKER_DIR/config"

# 1. 检测系统
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
echo "[1/4] 系统: $OS $ARCH"

# 2. 下载 KataGo
echo "[2/4] 下载 KataGo $KATAGO_VERSION..."
if [ "$OS" = "linux" ]; then
    if command -v nvidia-smi &>/dev/null; then
        # GPU 系统 → OpenCL
        URL="https://github.com/lightvector/KataGo/releases/download/$KATAGO_VERSION/katago-${KATAGO_VERSION}-opencl-linux-x64.zip"
    else
        # CPU 系统 → Eigen
        URL="https://github.com/lightvector/KataGo/releases/download/$KATAGO_VERSION/katago-${KATAGO_VERSION}-eigenavx2-linux-x64.zip"
    fi
elif [ "$OS" = "mingw"* ] || [ "$OS" = "cygwin"* ] || [ -n "$COMSPEC" ]; then
    URL="https://github.com/lightvector/KataGo/releases/download/$KATAGO_VERSION/katago-${KATAGO_VERSION}-opencl-windows-x64.zip"
else
    echo "不支持的 OS: $OS"
    exit 1
fi

cd "$WORKER_DIR"
wget -q "$URL" -O katago.zip
unzip -q -o katago.zip
rm -f katago.zip
chmod +x katago 2>/dev/null || true
echo "  KataGo: $(./katago version 2>&1 | head -1 || ./katago.exe version 2>&1 | head -1)"

# 3. 从主控获取模型和配置
echo "[3/4] 从主控 $MASTER_HOST 同步模型..."
scp -o StrictHostKeyChecking=no "ahill@$MASTER_HOST:/mnt/c/users/ahill/documents/python/go_analysis_project/kata-go/models/*.bin.gz" "$WORKER_DIR/models/" 2>/dev/null || {
    echo "  [WARN] 模型同步失败，需要手动复制"
    echo "  scp master:$MODEL_DIR/*.bin.gz $WORKER_DIR/models/"
}

# 4. 创建配置
echo "[4/4] 创建分析配置..."
cat > "$WORKER_DIR/config/analysis.cfg" << 'CONFIG'
logDir = analysis_logs
numAnalysisThreads = 2
numSearchThreads = 16
nnMaxBatchSize = 8
CONFIG

echo ""
echo "=== 部署完成 ==="
echo "  目录: $WORKER_DIR"
echo "  模型: $(ls $WORKER_DIR/models/*.bin.gz 2>/dev/null | wc -l) 个"
echo "  配置: $WORKER_DIR/config/analysis.cfg"
echo ""
echo "启动: $WORKER_DIR/katago analysis -model $WORKER_DIR/models/kata1-*.bin.gz -config $WORKER_DIR/config/analysis.cfg"
