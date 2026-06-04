"""interface.py — Python Package 调用接口。"""
from typing import Optional

from ..data.source import BaseSource, FolderSource, SourceRegistry
from ..data.store import BaseStore, NpzStore, StoreRegistry
from ..data.format import AnalysisRecord
from ..analyzer import BaseAnalyzer, create_analyzer, discover_katago
from ..analysis import Pipeline, extract_main_line
from ..evaluation import GoStrengthModel, GoDataset, Trainer


def analyze_sgf(sgf_content: str,
                analyzer_type: str = "auto",
                katago_path: str = "",
                model_path: str = "",
                config_path: Optional[str] = None,
                visits: int = 25,
                min_moves: int = 50,
                **analyzer_kwargs) -> dict:
    """分析单局棋谱 (SGF 字符串输入)。

    Returns: {"game_id": ..., "moves": N, "features": ..., "duration_s": X, "status": "ok"|"fail"}
    """
    from ..analysis.sgf_parser import extract_main_line

    moves = extract_main_line(sgf_content)
    if not moves:
        return {"status": "fail", "error": "no_moves", "moves": 0}
    if len(moves) < min_moves:
        return {"status": "skip", "moves": len(moves), "reason": "too_short"}

    analyzer = create_analyzer(
        analyzer_type,
        katago_path=katago_path,
        model_path=model_path,
        config_path=config_path,
        visits=visits,
        **analyzer_kwargs,
    )

    result = analyzer.analyze(moves)
    if not result.success:
        return {"status": "fail", "error": "analysis_failed", "moves": len(moves)}

    return {
        "status": "ok",
        "game_id": "inline",
        "moves": len(moves),
        "features": result.features.tolist(),
        "num_features": result.num_moves,
        "duration_s": round(result.duration_s, 2),
        "visits_used": result.visits_used,
    }


def evaluate_game(record: AnalysisRecord,
                  model: GoStrengthModel,
                  device: str = "auto") -> dict:
    """评估一局棋的分析结果，输出段位评级。

    Returns: {"black_rank": int, "white_rank": int,
              "black_confidence": float, "white_confidence": float}
    """
    import torch
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    model = model.to(device)
    model.eval()

    feats = torch.from_numpy(record.features).float().unsqueeze(0).to(device)  # (1, T, 12)
    seq_len = torch.tensor([record.num_moves], dtype=torch.long, device=device)

    with torch.no_grad():
        black_logits = model(feats, seq_len, side="black")
        white_logits = model(feats, seq_len, side="white")

    black_probs = torch.nn.functional.softmax(black_logits, dim=1).squeeze(0)
    white_probs = torch.nn.functional.softmax(white_logits, dim=1).squeeze(0)

    black_rank = int(black_logits.argmax(dim=1).item())
    white_rank = int(white_logits.argmax(dim=1).item())

    return {
        "black_rank": black_rank,
        "white_rank": white_rank,
        "black_confidence": float(black_probs[black_rank].item()),
        "white_confidence": float(white_probs[white_rank].item()),
    }


def train_model(store: BaseStore,
                model: Optional[GoStrengthModel] = None,
                epochs: int = 50,
                batch_size: int = 32,
                lr: float = 1e-4,
                checkpoint_dir: Optional[str] = None,
                **trainer_kwargs) -> dict:
    """训练段位预测模型。

    Returns: {"best_epoch": N, "best_val_loss": X, "history": {...}}
    """
    dataset = GoDataset(store)
    if len(dataset) == 0:
        return {"error": "empty_dataset", "detail": "No training data in store"}

    if model is None:
        model = GoStrengthModel()

    trainer = Trainer(
        model=model,
        train_dataset=dataset,
        lr=lr,
        **trainer_kwargs,
    )

    return trainer.train(
        epochs=epochs,
        batch_size=batch_size,
        checkpoint_dir=checkpoint_dir,
    )


def discover(analyzer_type: str = "auto") -> list[dict]:
    """自动发现可用的 KataGo 环境。"""
    return discover_katago()
