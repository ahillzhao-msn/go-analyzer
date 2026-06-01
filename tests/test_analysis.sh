#!/bin/bash
# Test KataGo analysis
PROJ="${GO_ANALYSIS_PROJ:-.}"
KATAGO="$PROJ/katago_install/katago"
MODEL="$PROJ/katago_install/kata1-b18c384nbt-s6582191360-d3422816034.bin.gz"
CFG="$PROJ/katago_install/analysis_config.cfg"

# Single game analysis test
echo '{"id":"test001","sgf":"(;GM[1]FF[4]SZ[19]KM[7.5]PB[Test]PW[Test];B[pd];W[dd];B[dp])","maxVisits":96,"rules":"chinese","komi":7.5,"includePolicy":true}' | "$KATAGO" analysis -model "$MODEL" -config "$CFG" 2>&1
