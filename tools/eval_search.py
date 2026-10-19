"""检索质量评测：在现有索引上跑一组「查询 → 应命中的文件」，比较纯向量和不同关键词加分下的排名。

    .venv\\Scripts\\python.exe tools\\eval_search.py [查询文件] [--boost 0,0.05,0.1,0.2] [--show 0.1]

查询文件默认 ~/.mmsearch/eval.json（里面是自己的文件路径，不放进仓库），格式见 tools/eval.example.json：
    [{"q": "查询", "expect": ["应命中文件路径的一部分", ...], "tab": "all", "tag": "lexical"}, ...]
expect 里任意一项是结果路径的子串（不区分大小写）就算命中；tab 是界面上的类型标签（all/text/image/audio/video）。
索引库先复制一份再打开，不会改动正在用的索引；查询向量缓存在 ~/.mmsearch/eval_cache.npz，
同一模型下重复评测不需要启动模型。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mmsearch import config  # noqa: E402
from mmsearch.store import VectorStore  # noqa: E402
from mmsearch.webapp import FILTERS, TOP_K  # noqa: E402

CACHE = config.DATA_DIR / "eval_cache.npz"


def query_vectors(queries: list[str]) -> dict[str, np.ndarray]:
    fp = config.model_fingerprint()
    key = lambda q: hashlib.sha1(f"{fp}\n{q}".encode()).hexdigest()
    cache = dict(np.load(CACHE)) if CACHE.exists() else {}
    todo = [q for q in queries if key(q) not in cache]
    if todo:
        from mmsearch.embedder import Embedder, LlamaServer
        s = config.load_settings()
        server = LlamaServer(s["backend"], s["image_tokens"])
        print(f"编码 {len(todo)} 条新查询（启动模型）…", file=sys.stderr)
        try:
            emb = Embedder(server)
            for q in todo:
                cache[key(q)] = emb.encode_query(q)
        finally:
            server.stop()
        np.savez(CACHE, **cache)
    return {q: cache[key(q)] for q in queries}


def first_rank(hits, expect: list[str]) -> int | None:
    pats = [e.lower().replace("/", "\\") for e in expect]
    for i, h in enumerate(hits, 1):
        if any(p in h.path.lower() for p in pats):
            return i
    return None


def metrics(ranks: list[int | None]) -> dict[str, float]:
    n = max(len(ranks), 1)
    return {"R@1": sum(r is not None and r <= 1 for r in ranks) / n,
            "R@5": sum(r is not None and r <= 5 for r in ranks) / n,
            "R@10": sum(r is not None and r <= 10 for r in ranks) / n,
            "MRR@10": sum(1 / r for r in ranks if r is not None and r <= 10) / n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="?", default=str(config.DATA_DIR / "eval.json"))
    ap.add_argument("--boost", default="0,0.05,0.1,0.15,0.2,0.3", help="要比较的关键词加分，逗号分隔")
    ap.add_argument("--show", type=float, default=config.LEXICAL_BOOST, help="逐条对比 0 与这个加分下的排名")
    ap.add_argument("--top", type=int, default=0, help="没排第一的查询，列出这个加分下的前 N 个结果")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    config.set_model_source(config.load_settings().get("model_source"))
    cases = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    vecs = query_vectors([c["q"] for c in cases])

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "index.db"
        src, dst = sqlite3.connect(config.DB_PATH), sqlite3.connect(db)
        src.backup(dst)  # 连同 WAL 里尚未合并的内容一起复制
        src.close(), dst.close()
        store = VectorStore(db)
        boosts = sorted({float(b) for b in args.boost.split(",")} | {0.0, args.show})
        ranks, misses = {b: [] for b in boosts}, []
        for c in cases:
            lex = store.lexical(c["q"])
            for b in boosts:
                hits = store.search(vecs[c["q"]], k=TOP_K, kinds=FILTERS[c.get("tab", "all")], lex=lex, boost=b)
                ranks[b].append(first_rank(hits, c["expect"]))
                if b == args.show and ranks[b][-1] != 1:
                    misses.append((c, hits[: args.top]))
        store.db.close()

    tags = sorted({c.get("tag", "") for c in cases})
    print(f"{len(cases)} 条查询，索引 {len(store)} 条向量\n")
    for tag in [None] + [t for t in tags if t]:
        idx = [i for i, c in enumerate(cases) if tag is None or c.get("tag") == tag]
        print(f"[{tag or '全部'}] {len(idx)} 条")
        print(f"  {'boost':>6} {'R@1':>6} {'R@5':>6} {'R@10':>6} {'MRR@10':>7}")
        for b in boosts:
            m = metrics([ranks[b][i] for i in idx])
            print(f"  {b:>6.2f} {m['R@1']:>6.2f} {m['R@5']:>6.2f} {m['R@10']:>6.2f} {m['MRR@10']:>7.3f}")
        print()

    fmt = lambda r: "-" if r is None else str(r)
    print(f"逐条排名（纯向量 → 加分 {args.show}）")
    for c, r0, r1 in zip(cases, ranks[0.0], ranks[args.show]):
        mark = "" if r0 == r1 else ("  ↑" if (r1 or 999) < (r0 or 999) else "  ↓")
        print(f"  {fmt(r0):>3} → {fmt(r1):<3} {c.get('tag', ''):<9} {c['q']}{mark}")
    for c, hits in misses if args.top else []:
        print(f"\n「{c['q']}」应命中 {c['expect']}")
        for h in hits:
            print(f"  {h.score:.3f}{' *' if h.lexical else '  '} {h.path}")


if __name__ == "__main__":
    main()
