"""
Go 分析管道 — 端到端批处理脚本。

使用::

    # 仅收集棋谱元数据 (自动选 visits)
    python -m go_analysis.pipeline --sgf-dir ./Bob --output analysis_store_meta --mode meta

    # 完整分析 (需 KataGo)
    python -m go_analysis.pipeline --sgf-dir ./Bob --output analysis_store --mode full

Visits 策略:
    自动: 棋谱数 ≤ 50 → 96 visits, 否则 → 50 visits
    手动: --visits 128 覆盖
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np
from tqdm import tqdm

from .analysis_format import (
    AnalysisRecord, AnalysisStore, GameMeta,
    HardwareEnv, SoftwareEnv,
)
from .env_collector import (
    collect_hardware, collect_software,
    extract_game_meta_from_sgf_file,
)

MIN_MOVES = 10  # 不足10手的棋谱视为未开始，废弃


def iter_sgf_files(sgf_dir: str, limit: int = 0):
    """递归遍历目录下所有 .sgf 文件。"""
    sgf_dir = Path(sgf_dir)
    if sgf_dir.is_file() and sgf_dir.suffix.lower() == ".sgf":
        yield str(sgf_dir)
        return
    for path in sorted(sgf_dir.rglob("*.sgf")):
        yield str(path)


def auto_visits(sgf_files: list, visits_override: int = 0) -> int:
    """自动选择 visits: 少量→96, 批量→50, 显式指定则覆盖。"""
    if visits_override > 0:
        return visits_override
    return 96 if len(sgf_files) <= 50 else 50


def batch_collect_meta(
    sgf_dir: str,
    output_dir: str,
    limit: int = 0,
    skip_existing: bool = True,
) -> dict:
    """模式 1: 仅收集棋谱元数据 + 环境向量。

    自动过滤 <10 手棋谱。生成不带特征的记录。
    """
    hw = collect_hardware()
    sw = collect_software()
    store = AnalysisStore(output_dir)

    sgf_files = list(iter_sgf_files(sgf_dir, limit))
    if limit > 0:
        sgf_files = sgf_files[:limit]

    collected = 0
    skipped = 0
    errors = 0
    filtered = 0  # < 10 moves

    print(f"[Pipeline] Collecting metadata from {len(sgf_files)} SGF files...")
    print(f"  Hardware: {hw.gpu_model or hw.cpu_model}")
    print(f"  Software: Torch {sw.torch_version}, CUDA {sw.cuda_version}")
    print(f"  Min moves: {MIN_MOVES}+")
    print(f"  Output:   {output_dir}")

    for sgf_path in tqdm(sgf_files, desc="Collecting"):
        game_id = Path(sgf_path).stem

        if skip_existing and store.get(game_id) is not None:
            skipped += 1
            continue

        try:
            game = extract_game_meta_from_sgf_file(sgf_path, game_id=game_id)
        except Exception as e:
            print(f"  [WARN] Failed to parse {sgf_path}: {e}")
            errors += 1
            continue

        # 过滤 <10 手棋谱
        if game.total_moves < MIN_MOVES:
            filtered += 1
            continue

        record = AnalysisRecord(
            features=np.zeros((0, 12), dtype=np.float16),
            global_stats=np.zeros(12, dtype=np.float16),
            move_count=game.total_moves,
            hw=hw, sw=sw, game=game,
        )
        store.put(game_id, record)
        collected += 1

    stats = store.stats()
    stats.update({"skipped": skipped, "errors": errors, "filtered": filtered, "new": collected})
    print(f"\n[Pipeline] Done: {collected} new, {filtered} filtered (<{MIN_MOVES}), {skipped} skipped")
    print(f"  Total: {stats['game_count']} games, {stats['total_mb']:.1f}MB")
    return stats


def analyze_and_store(
    sgf_dir: str,
    output_dir: str,
    katago_path: str = "katago",
    model_path: str = "",
    config_path: str = "",
    max_visits: int = 50,
    limit: int = 0,
    skip_existing: bool = True,
):
    """模式 2: 完整分析 — KataGo + 特征提取 + 压缩。"""
    try:
        from go_analysis.analyzer import KataGoBatchAnalyzer
        from .models import extract_features_from_analysis, compute_global_stats
    except ImportError as e:
        print(f"[ERROR] {e}")
        print("  full mode requires go_analysis.analyzer with KataGo installed.")
        sys.exit(1)

    hw = collect_hardware()
    sw = collect_software(katago_path=katago_path, katago_model=model_path, max_visits=max_visits)
    store = AnalysisStore(output_dir)

    sgf_files = list(iter_sgf_files(sgf_dir, limit))
    if limit > 0:
        sgf_files = sgf_files[:limit]

    if not sgf_files:
        print("[Pipeline] No SGF files found.")
        return {"game_count": 0}

    print(f"[Pipeline] Analyzing {len(sgf_files)} games with KataGo (visits={max_visits})...")
    print(f"  Min moves: {MIN_MOVES}+")

    analyzer = KataGoBatchAnalyzer(
        katago_path=katago_path,
        model_path=model_path or None,
        config_path=config_path or None,
        max_visits=max_visits,
    )

    analyzed = 0
    skipped = 0
    errors = 0
    filtered = 0

    for sgf_path in tqdm(sgf_files, desc="Analyzing"):
        game_id = Path(sgf_path).stem
        if skip_existing and store.get(game_id) is not None:
            skipped += 1
            continue

        try:
            game = extract_game_meta_from_sgf_file(sgf_path, game_id=game_id)
        except Exception as e:
            print(f"  [WARN] Failed to parse {sgf_path}: {e}")
            errors += 1
            continue

        if game.total_moves < MIN_MOVES:
            filtered += 1
            continue

        try:
            result = analyzer.analyze_sgf_file(sgf_path)
        except Exception as e:
            print(f"  [ERROR] KataGo failed on {sgf_path}: {e}")
            errors += 1
            continue

        if result is None:
            print(f"  [WARN] No analysis result for {sgf_path}")
            errors += 1
            continue

        all_moves = []
        for player in ("B", "W"):
            all_moves.extend(extract_features_from_analysis(result, player))

        if not all_moves:
            print(f"  [WARN] No valid moves in {sgf_path}")
            errors += 1
            continue

        features = np.stack([m.features for m in all_moves], axis=0)
        gs = compute_global_stats(all_moves)

        record = AnalysisRecord.compress(features, gs, hw, sw, game)
        store.put(game_id, record)
        analyzed += 1

    analyzer.shutdown()

    stats = store.stats()
    stats.update({"analyzed": analyzed, "skipped": skipped, "errors": errors, "filtered": filtered})
    print(f"\n[Pipeline] Done: {analyzed} analyzed, {filtered} filtered, {skipped} skipped, {errors} errors")
    print(f"  Total: {stats['game_count']} games, {stats['total_mb']:.1f}MB")
    return stats


def prune_short_games(store_dir: str):
    """清理已存储中 <10 手的棋谱。"""
    store = AnalysisStore(store_dir)
    games = store.list_games()
    pruned = 0
    for gid in games:
        rec = store.get(gid)
        if rec and (rec.game.total_moves < MIN_MOVES):
            path = os.path.join(store_dir, f"{gid}.npz")
            os.remove(path)
            pruned += 1
            print(f"  pruned {gid}: {rec.game.total_moves} moves")
    print(f"[Prune] Removed {pruned} games with <{MIN_MOVES} moves from {store_dir}")
    return pruned


def main():
    parser = argparse.ArgumentParser(description="Go analysis pipeline")
    parser.add_argument("--sgf-dir", default="./Bob", help="SGF files directory")
    parser.add_argument("--output", default="analysis_store", help="Output AnalysisStore directory")
    parser.add_argument("--mode", choices=["meta", "full", "prune"], default="meta",
                        help="'meta': metadata only. 'full': with KataGo. 'prune': clean short games.")
    parser.add_argument("--katago", default="katago", help="Path to KataGo binary")
    parser.add_argument("--model", default="", help="KataGo model path")
    parser.add_argument("--config", default="", help="KataGo config path")
    parser.add_argument("--visits", type=int, default=0,
                        help="Visits per move. 0=auto (≤50→96, >50→50)")
    parser.add_argument("--limit", type=int, default=0, help="Max games to process (0 = all)")
    parser.add_argument("--force", action="store_true", help="Re-process existing games")
    parser.add_argument("--min-moves", type=int, default=10, help="Minimum moves to keep a game")

    args = parser.parse_args()

    # 全局 MIN_MOVES
    global MIN_MOVES
    MIN_MOVES = args.min_moves

    if args.mode == "prune":
        prune_short_games(args.output)
        return

    sgf_files = list(iter_sgf_files(args.sgf_dir, args.limit))
    if args.limit > 0:
        sgf_files = sgf_files[:args.limit]

    visits = auto_visits(sgf_files, args.visits)
    print(f"[Pipeline] Mode={args.mode}, SGFs={len(sgf_files)}, Visits={visits}")

    if args.mode == "meta":
        batch_collect_meta(
            sgf_dir=args.sgf_dir,
            output_dir=args.output,
            limit=args.limit,
            skip_existing=not args.force,
        )
    else:
        analyze_and_store(
            sgf_dir=args.sgf_dir,
            output_dir=args.output,
            katago_path=args.katago,
            model_path=args.model,
            config_path=args.config,
            max_visits=visits,
            limit=args.limit,
            skip_existing=not args.force,
        )


if __name__ == "__main__":
    main()
