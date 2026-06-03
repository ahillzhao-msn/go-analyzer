#!/usr/bin/env bash
# go-analyzer-tune.sh — 远程主机调优脚本
# 用法: ssh user@host "bash -s" < go-analyzer-tune.sh
#
# 功能:
#   1. 检测 GPU
#   2. 运行 KataGo 基准测试
#   3. 输出最优配置
#   4. 生成 analysis_config.cfg

KATAGO="${1:-katago}"
MODEL="${2:-}"
SGF="${3:-}"

echo "=== Go Analyzer Tune ==="
echo "KataGo: $KATAGO"
echo ""

# GPU 检测
echo "--- GPU Detection ---"
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
fi
echo ""

# 版本
echo "--- KataGo Version ---"
"$KATAGO" version 2>/dev/null || echo "Cannot run katago"
echo ""

# OpenCL Tuning
echo "--- OpenCL Tuning ---"
"$KATAGO" tuner -model "$MODEL" 2>/dev/null && echo "Tuning OK" || echo "Tuning skipped"
echo ""

# 生成配置
echo "--- Generating Config ---"
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)

# 根据 GPU 推荐参数
case "$GPU_NAME" in
    *RTX*3090*)   ATH=10; STH=48; BS=16;;
    *RTX*3080*)   ATH=8;  STH=40; BS=16;;
    *RTX*3070*)   ATH=6;  STH=32; BS=16;;
    *RTX*3060*)   ATH=4;  STH=24; BS=16;;
    *RTX*4060*)   ATH=4;  STH=24; BS=8;;
    *RTX*4070*)   ATH=6;  STH=32; BS=16;;
    *RTX*4080*)   ATH=8;  STH=40; BS=16;;
    *RTX*4090*)   ATH=12; STH=48; BS=16;;
    *GTX*1660*)   ATH=2;  STH=16; BS=8;;
    *GTX*1060*)   ATH=1;  STH=8;  BS=4;;
    *)            ATH=2;  STH=16; BS=8;;
esac

cat > config/analysis_config.cfg << EOF
logDir = analysis_logs
reportAnalysisWinratesAs = BLACK
analysisPVLen = 15
wideRootNoise = 0.04
numAnalysisThreads = $ATH
numSearchThreads = $STH
nnMaxBatchSize = $BS
EOF

echo "Config generated: $ATH analysis threads, $STH search threads, batch $BS"
echo "--- config/analysis_config.cfg ---"
cat config/analysis_config.cfg

# 基准测试 (如果提供了 SGF)
if [ -n "$SGF" ] && [ -f "$SGF" ]; then
    echo ""
    echo "--- Benchmark ---"
    echo "Testing: $SGF"
    for ath in 1 2 4 8; do
        for sth in 8 16 24; do
            cat > /tmp/bench_cfg.cfg << EOF2
logDir = analysis_logs
reportAnalysisWinratesAs = BLACK
analysisPVLen = 15
wideRootNoise = 0.04
numAnalysisThreads = $ath
numSearchThreads = $sth
nnMaxBatchSize = 8
EOF2
            echo -n "  A${ath}S${sth}: "
            TIMEFORMAT='%3R'
            time ("$KATAGO" analysis -model "$MODEL" -config /tmp/bench_cfg.cfg < /dev/null) 2>&1 | head -1 || echo "SKIP"
        done
    done
fi

echo ""
echo "=== Done ==="
