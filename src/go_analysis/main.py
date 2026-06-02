#!/usr/bin/env python3
"""Go Analyzer CLI — `go-analyzer` 命令入口。

安装后可用::

    go-analyzer --help
    go-analyzer analyze game.sgf
    go-analyzer train --epochs 100
"""

from go_analysis.cli import cli

if __name__ == "__main__":
    cli()
