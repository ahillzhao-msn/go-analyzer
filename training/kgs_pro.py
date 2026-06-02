#!/usr/bin/env python3
"""处理 KGS 4d 年度归档 (2007-2015) 找 Pro 棋谱"""
import sys
sys.path.insert(0, '/mnt/c/users/ahill/documents/python/go_analysis_project/training')
from kgs_download import process_archive, OUTPUT_DIR

yearly = [
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2007-19-150282-.tar.gz",
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2008-19-185304-.tar.gz",
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2009-19-182150-.tar.gz",
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2010-19-193850-.tar.gz",
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2011-19-218358-.tar.gz",
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2012-19-195145-.tar.gz",
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2013-19-156594-.tar.gz",
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2014-19-123572-.tar.gz",
    "https://dl.u-go.net/gamerecords-4d/KGS4d-2015-19-92593-.tar.gz",
]

total = 0
for url in yearly:
    n = process_archive(url, 'kgs4y')
    total += n
    # Check if Pro is done
    d = OUTPUT_DIR / 'pro'
    c = len(list(d.glob("*.sgf"))) if d.exists() else 0
    print(f"  Pro 累计: {c}/800")
    if c >= 800:
        print("Pro 达标!")
        break

print(f"\n共新增: {total}")
