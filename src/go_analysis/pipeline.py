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
    platform: str = "auto",
    use_router: bool = True,
):
    """模式 2: 完整分析 — 适配器模式 + 路由器。

    优先使用 AnalysisRouter 选择最佳主机,
    回退到 create_adapter() 直接创建适配器。
    """
    from go_analysis.analyzer import create_adapter
    from .models import extract_features_from_analysis, compute_global_stats

    hw = collect_hardware()
    sw = collect_software(katago_path=katago_path, katago_model=model_path, max_visits=max_visits)
    store = AnalysisStore(output_dir)

    sgf_files = list(iter_sgf_files(sgf_dir, limit))
    if limit > 0:
        sgf_files = sgf_files[:limit]

    if not sgf_files:
        print("[Pipeline] No SGF files found.")
        return {"game_count": 0}

    # 使用路由器选择主机
    adapter = None
    router = None
    assigned_host = None
    if use_router:
        from go_analysis.router import AnalysisRouter
        from go_analysis.config import ConfigManager
        cfg = ConfigManager()
        router = AnalysisRouter(cfg)
        router.health_check_all(timeout_s=5)

        # 选择一个主机
        host = router.select_best(preferred_platform=platform if platform != "auto" else None)
        if host:
            assigned_host = host.name
            router.schedule(task_size=len(sgf_files))
            print(f"[Pipeline] Router: assigned to {host.name} ({host.platform}, load={host.load})")
            # 用主机的路径创建适配器
            try:
                adapter = create_adapter(
                    platform=host.platform,
                    kata_path=host.kata_path,
                    model_path=host.model_path,
                    host=host.ssh_host,
                    port=host.ssh_port,
                    user=host.ssh_user,
                    visits=max_visits,
                )
            except Exception as e:
                print(f"[Pipeline] Router host failed, fallback to auto: {e}")
                adapter = None

    # 回退: 直接创建适配器
    if adapter is None:
        print(f"[Pipeline] Creating adapter directly (platform={platform}, visits={max_visits})...")
        adapter = create_adapter(
            platform=platform,
            kata_path=katago_path,
            model_path=model_path or None,
            config_path=config_path or None,
            visits=max_visits,
        )

    print(f"[Pipeline] Analyzing {len(sgf_files)} games (visits={max_visits})...")
    print(f"  Adapter: {adapter.__class__.__name__}")
    print(f"  Min moves: {MIN_MOVES}+")

    try:
        adapter.start()

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
                content = Path(sgf_path).read_text(encoding="utf-8", errors="replace")
                result = adapter.analyze(content, game_id=game_id, visits=max_visits)
            except Exception as e:
                print(f"  [ERROR] Analysis failed on {sgf_path}: {e}")
                errors += 1
                continue

            if not result or not result.success:
                print(f"  [WARN] No analysis result for {sgf_path}")
                errors += 1
                continue

            # 适配器已内部完成特征提取
            features_list = result.raw_json.get("features_list", [])
            if not features_list:
                print(f"  [WARN] No valid moves in {sgf_path}")
                errors += 1
                continue

            all_moves = features_list

            features = np.stack([m["features"] for m in all_moves], axis=0)
            gs = compute_global_stats([type("", (), {"features": m["features"]})() for m in all_moves])

            record = AnalysisRecord.compress(features, gs, hw, sw, game)
            store.put(game_id, record)
            analyzed += 1

    finally:
        adapter.shutdown()
        if router and assigned_host:
            router.release(assigned_host, task_size=len(sgf_files))

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


def run_analysis_pipeline(
    sgf_dir: str,
    output_dir: str,
    mode: str = "meta",
    max_visits: int = 96,
    platform: str = "auto",
    limit: int = 0,
    **kwargs,
) -> dict:
    """
    CLI 可调用的分析管线入口。

    支持通过 platform 选择适配器:
    - auto: create_adapter() 自动选择
    - windows_native: WSL Windows KataGo
    - ssh/http: 远程主机
    """
    if mode == "meta":
        return batch_collect_meta(
            sgf_dir=sgf_dir,
            output_dir=output_dir,
            limit=limit,
        )
    else:
        return analyze_and_store(
            sgf_dir=sgf_dir,
            output_dir=output_dir,
            katago_path=kwargs.get("katago_path"),
            model_path=kwargs.get("model_path"),
            config_path=kwargs.get("config_path"),
            max_visits=max_visits,
            limit=limit,
            skip_existing=kwargs.get("skip_existing", True),
            platform=platform,
            use_router=kwargs.get("use_router", True),
        )
