"""
Go Analyzer CLI — 主入口。

用法::

    # 分析单盘棋谱
    go-analyzer analyze game.sgf --visits 96

    # 批量分析
    go-analyzer analyze-batch ./sgfs/ --visits 50

    # 注册远程主机
    go-analyzer host register --name worker-1 --platform ssh --host 10.0.0.1

    # 查看集群状态
    go-analyzer cluster status

    # 训练
    go-analyzer train --epochs 100 --lr 0.001

    # 增量训练
    go-analyzer train --incremental --checkpoint v2

    # 导出模型
    go-analyzer export --version v3 --format onnx

    # 启动 worker 服务
    go-analyzer serve --port 8080
"""

import click
import os
import sys
from pathlib import Path


# ── 全局上下文 ───────────────────────────────────────

@click.group()
@click.option("--config", "-c", default=None, help="Config file path")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.pass_context
def cli(ctx, config, verbose):
    """Go Analyzer — KataGo 分析引擎 + 段位预测训练"""
    ctx.ensure_object(dict)
    ctx.obj["VERBOSE"] = verbose
    ctx.obj["CONFIG"] = config

    # 延迟加载 ConfigManager
    from go_analysis.config import load_config
    path = config or "config.yaml"
    if not os.path.exists(path):
        path = None
    cfg = load_config(path)
    ctx.obj["CFG"] = cfg


# ── analyze ───────────────────────────────────────────

@cli.command()
@click.argument("sgf", type=click.Path(exists=True))
@click.option("--visits", default=None, type=int, help="Visits per position")
@click.option("--platform", default=None, help="Adapter platform")
@click.option("--output", "-o", default=None, help="Output path")
@click.pass_context
def analyze(ctx, sgf, visits, platform, output):
    """分析单盘棋谱"""
    cfg = ctx.obj["CFG"]
    visits = visits or cfg.get("analyzer.visits", 96)
    platform = platform or cfg.get("analyzer.default_platform", "auto")

    click.echo(f"Analyzing {sgf} (visits={visits}, platform={platform})...")

    # 惰性加载 torch 依赖模块
    from go_analysis.analyzer import create_adapter
    from go_analysis.sgf_parser import SGF

    content = Path(sgf).read_text(encoding="utf-8", errors="replace")
    adapter = create_adapter(platform=platform, visits=visits)

    try:
        adapter.start()
        result = adapter.analyze(content, game_id=Path(sgf).stem, visits=visits)
        if result.success:
            click.echo(f"  ✅ {result.move_count} moves, {result.duration_s:.1f}s")
            if output:
                import json
                Path(output).write_text(json.dumps(result.raw_json, indent=2))
                click.echo(f"  Results saved to {output}")
        else:
            click.echo(f"  ❌ {result.error}", err=True)
            sys.exit(1)
    finally:
        adapter.shutdown()


# ── analyze-batch ─────────────────────────────────────

@cli.command()
@click.argument("sgf_dir", type=click.Path(exists=True))
@click.option("--visits", default=None, type=int)
@click.option("--platform", default=None)
@click.option("--output", "-o", default="./analysis_store", help="Output store dir")
@click.option("--limit", default=0, type=int, help="Max games to analyze")
@click.option("--mode", default="meta", type=click.Choice(["meta", "full"]))
@click.pass_context
def analyze_batch(ctx, sgf_dir, visits, platform, output, limit, mode):
    """批量分析棋谱"""
    cfg = ctx.obj["CFG"]
    visits = visits or cfg.get("analyzer.visits", 96)
    platform = platform or cfg.get("analyzer.default_platform", "auto")

    click.echo(f"Batch analyzing {sgf_dir} ({mode=}, {visits=})...")
    from go_analysis.pipeline import run_analysis_pipeline
    result = run_analysis_pipeline(
        sgf_dir=sgf_dir,
        output_dir=output,
        mode=mode,
        max_visits=visits,
        platform=platform,
        limit=limit,
    )
    click.echo(f"  Done: {result['game_count']} games")


# ── host ──────────────────────────────────────────────

@cli.group()
def host():
    """管理分析主机"""

@host.command()
@click.option("--name", required=True, help="Host name")
@click.option("--platform", required=True, type=click.Choice(["windows_native", "ssh", "http"]))
@click.option("--host", default="localhost", help="Host address")
@click.option("--port", default=22, type=int, help="SSH port")
@click.option("--user", default=None, help="SSH username")
@click.option("--kata-path", default=None, help="KataGo path on host")
@click.option("--model-path", default=None, help="KataGo model path")
@click.option("--max-concurrent", default=2, type=int, help="Max concurrent tasks")
@click.pass_context
def register(ctx, name, platform, host, port, user, kata_path, model_path, max_concurrent):
    """注册分析主机并持久化到 config.yaml"""
    import yaml
    from pathlib import Path

    cfg = ctx.obj["CFG"]
    config_path = ctx.obj.get("CONFIG") or "config.yaml"

    # 读取当前配置
    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    hosts = config.setdefault("hosts", [])

    # 去重: 同名覆盖
    for i, h in enumerate(hosts):
        if h.get("name") == name:
            hosts.pop(i)
            break

    hosts.append({
        "name": name,
        "platform": platform,
        "host": host,
        "port": port,
        "user": user or "",
        "kata_path": kata_path or "",
        "model_path": model_path or "",
        "max_concurrent": max_concurrent,
    })

    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    click.echo(f"Registered host: {name} ({platform} @ {host}:{port}) → {config_path}")


@host.command()
@click.option("--project-root", default=".", help="Project root for scanning")
@click.pass_context
def discover(ctx, project_root):
    """自动发现本机 KataGo 安装"""
    cfg = ctx.obj["CFG"]
    click.echo(f"Scanning for KataGo installations in {project_root}...")

    from go_analysis.discovery import discover_katago, register_discovered_hosts

    hosts = discover_katago(project_root)
    if not hosts:
        click.echo("  No KataGo installations found.")
        return

    click.echo(f"  Found {len(hosts)} KataGo installation(s):")
    for h in hosts:
        click.echo(f"    [{h['platform']}] {h['name']}")
        click.echo(f"      Path: {h['kata_path']}")
        click.echo(f"      Model: {h['model_path'] or 'N/A'}")
        click.echo(f"      Capabilities: {', '.join(h['capabilities'])}")

    # 注册到配置
    from go_analysis.discovery import register_discovered_hosts
    register_discovered_hosts(cfg, hosts)
    click.echo(f"  Registered {len(hosts)} host(s) to config.")
    click.echo("  Use 'host save' to persist to disk.")


@host.command()
@click.option("--file", default="config.yaml", help="Target config file")
@click.pass_context
def save(ctx, file):
    """持久化主机注册表到配置文件"""
    from go_analysis.router import AnalysisRouter
    from go_analysis.config import ConfigManager
    cfg = ConfigManager()
    router = AnalysisRouter(cfg)
    router.save_to_config(file)
    click.echo(f"Saved {router.host_count} host(s) to {file}")


@host.command()
@click.pass_context
def list(ctx):
    """列出注册的分析主机"""
    cfg = ctx.obj["CFG"]
    hosts = cfg.hosts
    if not hosts:
        click.echo("No hosts registered.")
        return
    click.echo(f"{'Name':>20s}  {'Platform':>16s}  {'Host':>20s}")
    click.echo("-" * 60)
    for h in hosts:
        click.echo(f"{h.get('name','?'):>20s}  {h.get('platform','?'):>16s}  {h.get('host','?'):>20s}")


# ── cluster ───────────────────────────────────────────

@cli.group()
def cluster():
    """管理分析集群"""

@cluster.command()
@click.option("--visits", default=0, type=int, help="Visits (0=smart)")
@click.option("--sgf", required=True, help="SGF file or directory")
@click.option("--parallel", default=0, type=int, help="Parallel engines (0=config)")
@click.pass_context
def benchmark(ctx, visits, sgf, parallel):
    """性能基准测试"""
    cfg = ctx.obj["CFG"]
    from go_analysis.parallel import ParallelEngine
    path = Path(sgf)
    if path.is_dir():
        sgf_files = sorted(str(f) for f in path.rglob("*.sgf"))[:10]
    else:
        sgf_files = [sgf]

    engine = ParallelEngine(cfg)
    engine.start()

    try:
        results = engine.analyze_all(sgf_files, visits=visits, max_workers=parallel)
        for r in results:
            if r.get("success"):
                print(engine.monitor.print_report())
            else:
                print(f"  ❌ {r.get('game_id','?')}: {r.get('error','unknown')}")
    finally:
        engine.shutdown()

@cluster.command()
@click.pass_context
def status(ctx):
    """查看集群状态"""
    cfg = ctx.obj["CFG"]
    click.echo("Cluster: scanning...")
    from go_analysis.router import AnalysisRouter
    router = AnalysisRouter(cfg)

    hosts = router.health_check_all(timeout_s=5)
    click.echo(f"  {len(hosts)} host(s) found:")

    for h in hosts:
        status_icon = "✅" if h.healthy else ("⚠️" if h.alive else "❌")
        click.echo(f"  {status_icon} {h.name}")
        click.echo(f"      Platform: {h.platform}")
        click.echo(f"      Path:     {h.kata_path}")
        click.echo(f"      Alive:    {h.alive}, Latency: {h.latency_ms:.0f}ms")
        click.echo(f"      Load:     {h.load}/{h.max_concurrent}, Available: {h.available_slots}")
        if h.error:
            click.echo(f"      Error:    {h.error}")


@cluster.command()
@click.argument("name", required=True)
@click.pass_context
def health(ctx, name):
    """检查单台主机健康"""
    cfg = ctx.obj["CFG"]
    from go_analysis.router import AnalysisRouter
    router = AnalysisRouter(cfg)
    host = router.health_check(name)
    if host.healthy:
        click.echo(f"✅ {host.name}: alive, {host.available_slots}/{host.max_concurrent} slots free")
    else:
        click.echo(f"❌ {host.name}: {'busy' if host.alive else 'dead'} — {host.error or 'no available slots'}")


# ── train ─────────────────────────────────────────────

@cli.command()
@click.option("--epochs", default=None, type=int)
@click.option("--lr", "--learning-rate", default=None, type=float)
@click.option("--batch-size", default=None, type=int)
@click.option("--incremental", is_flag=True, default=False, help="Incremental training")
@click.option("--checkpoint", default=None, help="Resume from checkpoint")
@click.option("--data", default="./analysis_store", help="Training data store")
@click.option("--output", default="./models", help="Model output dir")
@click.pass_context
def train(ctx, epochs, lr, batch_size, incremental, checkpoint, data, output):
    """训练段位预测模型"""
    cfg = ctx.obj["CFG"]
    click.echo(f"Training {'(incremental)' if incremental else '(full)'}...")
    click.echo(f"  Data: {data}")
    click.echo(f"  Output: {output}")
    click.echo("  (TrainingPipe implementation TBD)")


# ── export ────────────────────────────────────────────

@cli.command()
@click.option("--version", default="latest", help="Model version")
@click.option("--format", default="onnx", type=click.Choice(["onnx", "torchscript", "pt"]))
@click.option("--output", "-o", default="./export", help="Export directory")
@click.pass_context
def export(ctx, version, format, output):
    """导出训练好的模型"""
    click.echo(f"Exporting model {version} → {format}")
    click.echo(f"  Output: {output}")
    click.echo("  (ModelRegistry implementation TBD)")


# ── serve ─────────────────────────────────────────────

@cli.command()
@click.option("--port", default=8080, type=int, help="Server port")
@click.option("--host", default="0.0.0.0", help="Bind address")
@click.option("--kata-path", default=None, help="KataGo binary path")
@click.pass_context
def serve(ctx, port, host, kata_path):
    """启动 worker 服务供路由器注册"""
    click.echo(f"Starting worker on {host}:{port}...")
    click.echo("  (WorkerServer TBD)")


# ── 入口 ──────────────────────────────────────────────

if __name__ == "__main__":
    cli()
