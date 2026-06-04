"""cli.py — go-analyzer 命令行接口。

使用 Click 框架，提供完整的命令行交互。
"""
import json
import os
import sys
from pathlib import Path

import click

from ..data.source import FolderSource, SourceRegistry
from ..data.store import NpzStore, StoreRegistry
from ..analyzer import create_analyzer, discover_katago
from ..analysis import Pipeline, extract_main_line
from ..evaluation import GoStrengthModel, GoDataset, Trainer


# ── 全局选项 ──
@click.group()
@click.option("--verbose", "-v", is_flag=True, help="详细输出")
def cli(verbose):
    """Go Analyzer — 围棋棋谱分析与段位评估工具。"""
    import logging
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# ── analyze 子命令 ──
@cli.command()
@click.argument("sgf_path", type=click.Path(exists=True))
@click.option("--visits", default=25, help="KataGo visits/手")
@click.option("--analyzer", "analyzer_type", default="auto",
              type=click.Choice(["auto", "local", "windows"]),
              help="分析器类型")
@click.option("--katago", "katago_path", default="", help="KataGo 路径")
@click.option("--model", "model_path", default="", help="模型文件路径")
@click.option("--config", "config_path", default=None, help="配置文件路径")
@click.option("--json", "output_json", is_flag=True, help="JSON 格式输出")
def analyze(sgf_path, visits, analyzer_type, katago_path, model_path,
            config_path, output_json):
    """分析一局棋谱，输出 NPZ 或 JSON。"""
    sgf_content = Path(sgf_path).read_text(encoding="utf-8", errors="replace")
    moves = extract_main_line(sgf_content)

    if not moves:
        click.echo("ERROR: No moves extracted from SGF")
        sys.exit(1)

    click.echo(f"Extracted {len(moves)} moves from main line")

    # 自动发现 KataGo
    if not katago_path:
        discovered = discover_katago()
        if discovered:
            katago_path = discovered[0]["path"]
            model_path = model_path or discovered[0].get("model", "")
            config_path = config_path or discovered[0].get("config", "")
            click.echo(f"Auto-discovered: {katago_path}")
        else:
            click.echo("ERROR: No KataGo found. Use --katago to specify path.")
            sys.exit(1)

    analyzer = create_analyzer(
        analyzer_type,
        katago_path=katago_path,
        model_path=model_path,
        config_path=config_path or None,
        visits=visits,
    )

    click.echo(f"Analyzing {len(moves)} moves with {visits} visits...")
    result = analyzer.analyze(moves)

    if not result.success:
        click.echo("ERROR: Analysis failed")
        sys.exit(1)

    if output_json:
        click.echo(json.dumps({
            "moves": len(moves),
            "features_shape": list(result.features.shape),
            "duration_s": round(result.duration_s, 2),
            "sample_features": result.features[:3].tolist(),
        }, indent=2))
    else:
        click.echo(f"✓ Analysis complete: {result.num_moves} moves, "
                   f"{result.duration_s:.1f}s")


# ── train 子命令 ──
@cli.command()
@click.option("--store", "store_dir", default="./analysis_store",
              help="分析结果目录")
@click.option("--epochs", default=50, help="训练轮数")
@click.option("--batch-size", default=32, help="批次大小")
@click.option("--lr", default=1e-4, help="学习率")
@click.option("--checkpoint", "checkpoint_dir", default="./checkpoints",
              help="检查点目录")
def train(store_dir, epochs, batch_size, lr, checkpoint_dir):
    """训练段位预测模型。"""
    store = NpzStore(store_dir)
    games = store.list()
    click.echo(f"Found {len(games)} analyzed games in {store_dir}")

    dataset = GoDataset(store)
    click.echo(f"Dataset: {len(dataset)} records")

    model = GoStrengthModel()
    trainer = Trainer(model=model, train_dataset=dataset, lr=lr)

    click.echo(f"Training for {epochs} epochs...")
    result = trainer.train(
        epochs=epochs,
        batch_size=batch_size,
        checkpoint_dir=checkpoint_dir,
    )
    click.echo(f"✓ Best epoch: {result['best_epoch']}, "
               f"Val loss: {result['best_val_loss']:.4f}")


# ── discover 子命令 ──
@cli.command()
def discover():
    """自动发现可用的 KataGo 环境。"""
    results = discover_katago()
    if results:
        click.echo(f"Found {len(results)} KataGo installation(s):")
        for r in results:
            click.echo(f"  [{r['type']:8s}] {r['path']}")
            if r.get("model"):
                click.echo(f"           model: {r['model']}")
    else:
        click.echo("No KataGo installation found")


# ── pipeline 子命令 ──
@cli.command()
@click.option("--source", "source_dir", default="./training", help="SGF 目录")
@click.option("--store", "store_dir", default="./analysis_store", help="输出目录")
@click.option("--visits", default=25, help="KataGo visits/手")
@click.option("--min-moves", default=50, help="最小手数")
@click.option("--katago", "katago_path", default="", help="KataGo 路径")
@click.option("--model", "model_path", default="", help="模型文件路径")
def pipeline(source_dir, store_dir, visits, min_moves, katago_path, model_path):
    """执行批量分析管线。"""
    # 自动发现 KataGo
    if not katago_path:
        discovered = discover_katago()
        if discovered:
            katago_path = discovered[0]["path"]
            model_path = model_path or discovered[0].get("model", "")
            config_path = discovered[0].get("config", "")
            click.echo(f"Auto-discovered: {katago_path}")
        else:
            click.echo("ERROR: No KataGo found")
            sys.exit(1)
    else:
        config_path = ""

    source = FolderSource(source_dir)
    store = NpzStore(store_dir)
    analyzer = create_analyzer(
        "windows" if "exe" in katago_path else "auto",
        katago_path=katago_path,
        model_path=model_path,
        config_path=config_path or None,
        visits=visits,
    )

    pipe = Pipeline(analyzer, source, store, visits=visits, min_moves=min_moves)
    click.echo(f"Starting pipeline: {source.count()} SGFs → {store_dir}")
    stats = pipe.run_all()

    click.echo(f"✓ Pipeline complete:")
    click.echo(f"  Total: {stats['total']}")
    click.echo(f"  OK:    {stats.get('ok', 0)}")
    click.echo(f"  Skip:  {stats.get('skip', 0) + stats.get('skip_exists', 0)}")
    click.echo(f"  Fail:  {stats.get('fail', 0)}")
    click.echo(f"  Time:  {stats.get('duration_s', 0):.0f}s")


if __name__ == "__main__":
    cli()
