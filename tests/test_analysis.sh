#!/bin/bash
# Test KataGo analysis via WSL interop (Windows exe)
PROJ="${GO_ANALYSIS_PROJ:-.}"
KATAGO="$PROJ/kata-go/windows/katago-v1.16.5-opencl-windows-x64.exe"
MODEL="$PROJ/kata-go/models/kata1-b18c384nbt-s6582191360-d3422816034.bin.gz"
CFG="$PROJ/kata-go/windows/analysis_config.cfg"

# Single game analysis test
echo '{"id":"test001","sgf":"(;GM[1]FF[4]SZ[19]KM[7.5]PB[Test]PW[Test];B[pd];W[dd];B[dp])","maxVisits":96,"rules":"chinese","komi":7.5,"boardXSize":19,"boardYSize":19,"includePolicy":true}' | "$KATAGO" analysis -model "$MODEL" -config "$CFG" 2>/dev/null
