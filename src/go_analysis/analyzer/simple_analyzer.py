"""
KataGo subprocess-based batch analyzer.

Launches KataGo analysis engine, sends SGF queries, and collects
per-move analysis results as structured JSON.
"""

import json
import subprocess
import threading
import queue
import time
import os
import re
from typing import Optional

from tqdm import tqdm


class KataGoBatchAnalyzer:
    """Multi-process KataGo analysis engine wrapper.

    Example::

        analyzer = KataGoBatchAnalyzer(
            katago_path="/usr/local/bin/katago",
            model_path="/path/to/kata1-b18c384nbt-s9761732864-d4253420187.bin.gz",
            config_path="/path/to/analysis_example.cfg",
            num_threads=4,
        )
        for sgf_path in sgf_files:
            result = analyzer.analyze_sgf_file(sgf_path)
            # result is a JSON dict with moveInfos, rootInfo, etc.
        analyzer.shutdown()
    """

    def __init__(
        self,
        katago_path: str = "katago",
        model_path: Optional[str] = None,
        config_path: Optional[str] = None,
        num_threads: int = 4,
        max_visits: int = 50,
    ):
        self.katago_path = katago_path
        self.model_path = model_path
        self.config_path = config_path
        self.max_visits = max_visits
        self.num_threads = num_threads

        # Build the command
        cmd = [katago_path, "analysis"]
        if model_path:
            cmd.extend(["-model", model_path])
        if config_path:
            cmd.extend(["-config", config_path])

        # Launch the subprocess
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )

        self.req_queue: queue.Queue = queue.Queue()
        self.results: dict = {}
        self._running = True
        self._lock = threading.Lock()

        # Start send/receive threads
        self._send_thread = threading.Thread(target=self._send_worker, daemon=True)
        self._recv_thread = threading.Thread(target=self._recv_worker, daemon=True)
        self._send_thread.start()
        self._recv_thread.start()

        # Give KataGo a moment to initialise
        time.sleep(2)

    def _send_worker(self):
        """Read analysis requests from queue and write to KataGo stdin."""
        while self._running:
            try:
                req = self.req_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.proc.stdin.write(req + "\n")
                self.proc.stdin.flush()
            except BrokenPipeError:
                break

    def _recv_worker(self):
        """Read analysis results from KataGo stdout."""
        while self._running:
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                result = json.loads(line.strip())
                query_id = result.get("id", "")
                with self._lock:
                    self.results[query_id] = result
            except json.JSONDecodeError:
                continue

    def _parse_sgf_moves(self, sgf_content: str):
        """Extract move list from SGF content.
        
        Returns list of [player, coord] pairs for KataGo query.
        """
        moves = re.findall(r';([BW])\[([a-z][0-9]+)\]', sgf_content, re.IGNORECASE)
        return [(m[0].upper(), m[1].lower()) for m in moves]

    def analyze_sgf(self, sgf_content: str, game_id: str = "game_001") -> Optional[dict]:
        """Analyze one SGF game.

        Parameters
        ----------
        sgf_content : str
            Full SGF content as a string.
        game_id : str
            Unique identifier for this analysis query.

        Returns
        -------
        dict or None
            KataGo analysis result JSON dict, or None if timed out.
        """
        moves = self._parse_sgf_moves(sgf_content)
        if not moves:
            print(f"[WARN] No moves found in SGF {game_id}")
            return None

        query = json.dumps({
            "id": game_id,
            "moves": moves,
            "maxVisits": self.max_visits,
            "rules": "chinese",
            "komi": 7.5,
            "boardXSize": 19,
            "boardYSize": 19,
            "includePolicy": True,
        })

        self.req_queue.put(query)

        # Poll for result
        deadline = time.time() + 120  # 2 minute timeout per game
        while time.time() < deadline:
            with self._lock:
                if game_id in self.results:
                    result = self.results.pop(game_id)
                    return result
            time.sleep(0.1)

        print(f"[WARN] Timeout waiting for analysis of {game_id}")
        return None

    def analyze_sgf_file(self, sgf_path: str) -> Optional[dict]:
        """Read an SGF file and analyze it."""
        with open(sgf_path, "r", encoding="utf-8") as f:
            content = f.read()
        game_id = os.path.basename(sgf_path)
        return self.analyze_sgf(content, game_id=game_id)

    def analyze_batch(
        self, sgf_paths: list, desc: str = "Analyzing"
    ) -> list:
        """Analyze multiple SGF files in batch.

        Results are returned in the same order as input paths.
        Games that fail analysis are returned as None.

        Parameters
        ----------
        sgf_paths : list of str
            Paths to SGF files.
        desc : str
            tqdm progress bar description.

        Returns
        -------
        list of dict or None
        """
        results = []
        for sgf_path in tqdm(sgf_paths, desc=desc):
            result = self.analyze_sgf_file(sgf_path)
            results.append(result)
        return results

    def shutdown(self):
        """Cleanly terminate KataGo subprocess."""
        self._running = False
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
