"""检索质量评测：在现有索引上跑一组「查询 → 应命中的文件」，比较纯向量和不同关键词加分下的排名。

    .venv\\Scripts\\python.exe tools\\eval_search.py [查询文件] [--boost 0,0.05,0.1,0.2] [--show 0.1] [--hub 0.5]

查询文件默认 ~/.mmsearch/eval.json（里面是自己的文件路径，不放进仓库），格式见 tools/eval.example.json：
    [{"q": "查询", "expect": ["应命中文件路径的一部分", ...], "tab": "all", "tag": "lexical"}, ...]
expect 里任意一项是结果路径的子串（不区分大小写）就算命中；tab 是界面上的类型标签（all/text/image/audio/video）。
以图搜索写 "image": "图片路径"（排除图片自身，和界面一样）；同时写 q 就是以图搜索后补充文字（两个向量加权）。
「全部可见」：在「全部」标签里不用点进分组就能看到——是最佳匹配，或在自己类型分组的前 PER_GROUP 个里。
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
from mmsearch import config, hubs  # noqa: E402
from mmsearch.store import VectorStore  # noqa: E402
from mmsearch.webapp import FILTERS, TOP_K  # noqa: E402

CACHE = config.DATA_DIR / "eval_cache.npz"
PER_GROUP = 3  # 与 frontend/src/store.js 一致：「全部」里每种类型展示的条数


def query_vectors(cases: list[dict]) -> list[np.ndarray]:
    """每条查询的向量：文字、图片，或两者按 COMPOSE_TEXT_WEIGHT 合成（同 webapp.search_refine）。"""
    fp = config.model_fingerprint()
    # 文字直接用原文做键（兼容已有缓存）；图片带上修改时间，图片改了会重新编码
    parts = lambda c: ([c["q"]] if c.get("q") else []) + (
        [f"image:{c['image']}:{Path(c['image']).stat().st_mtime}"] if c.get("image") else [])
    key = lambda p: hashlib.sha1(f"{fp}\n{p}".encode()).hexdigest()
    cache = dict(np.load(CACHE)) if CACHE.exists() else {}
    todo = list(dict.fromkeys(p for c in cases for p in parts(c) if key(p) not in cache))
    if todo:
        from mmsearch import media
        from mmsearch.embedder import Embedder, LlamaServer
        s = config.load_settings()
        server = LlamaServer(s["backend"], s["image_tokens"])
        print(f"编码 {len(todo)} 条新查询（启动模型）…", file=sys.stderr)
        try:
            emb = Embedder(server)
            for p in todo:
                if p.startswith("image:"):
                    cache[key(p)] = emb.encode_images([media.load_image(Path(p[6:].rsplit(":", 1)[0]))])[0]
                else:
                    cache[key(p)] = emb.encode_query(p)
        finally:
            server.stop()
        np.savez(CACHE, **cache)
    out = []
    for c in cases:
        vs = [cache[key(p)] for p in parts(c)]
        if len(vs) == 2:
            w = config.COMPOSE_TEXT_WEIGHT
            v = w * vs[0] + (1 - w) * vs[1]
            vs = [v / np.linalg.norm(v)]
        out.append(vs[0])
    return out


def first_rank(hits, expect: list[str]) -> int | None:
    pats = [e.lower().replace("/", "\\") for e in expect]
    for i, h in enumerate(hits, 1):
        if any(p in h.path.lower() for p in pats):
            return i
    return None


def visible_in_all(res: dict[str, list], expect: list[str]) -> bool:
    """同 frontend/src/store.js 的 rows：最佳匹配 + 每种类型去掉最佳匹配后的前 PER_GROUP 个。"""
    if not res["all"]:
        return False
    best = res["all"][0]
    if first_rank([best], expect):
        return True
    return any(first_rank([h for h in hits if h.path != best.path][:PER_GROUP], expect)
               for tab, hits in res.items() if tab != "all")


def metrics(ranks: list[int | None], vis: list[bool]) -> dict[str, float]:
    n = max(len(ranks), 1)
    return {"R@1": sum(r is not None and r <= 1 for r in ranks) / n,
            "R@5": sum(r is not None and r <= 5 for r in ranks) / n,
            "R@10": sum(r is not None and r <= 10 for r in ranks) / n,
            "MRR@10": sum(1 / r for r in ranks if r is not None and r <= 10) / n,
            "全部可见": sum(vis) / n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="?", default=str(config.DATA_DIR / "eval.json"))
    ap.add_argument("--boost", default="0,0.05,0.1,0.15,0.2,0.3", help="要比较的关键词加分，逗号分隔")
    ap.add_argument("--show", type=float, default=config.LEXICAL_BOOST, help="逐条对比 0 与这个加分下的排名")
    ap.add_argument("--top", type=int, default=0, help="没排第一的查询，列出这个加分下的前 N 个结果")
    ap.add_argument("--hub", type=float, default=config.HUB_PENALTY, help="万能图修正系数（含文字的查询），0 = 不修正")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    config.set_model_source(config.load_settings().get("model_source"))
    cases = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    vecs = query_vectors(cases + [{"q": q} for q in hubs.QUERIES])  # 通用查询和评测查询一起编码、一起缓存
    vecs, bank = vecs[: len(cases)], np.stack(vecs[len(cases):])
    label = lambda c: " + ".join(x for x in (c.get("image") and "[图] " + Path(c["image"]).name, c.get("q")) if x)

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "index.db"
        src, dst = sqlite3.connect(config.DB_PATH), sqlite3.connect(db)
        src.backup(dst)  # 连同 WAL 里尚未合并的内容一起复制
        src.close(), dst.close()
        store = VectorStore(db)
        store.set_hub_bank(bank)
        boosts = sorted({float(b) for b in args.boost.split(",")} | {0.0, args.show})
        ranks, vis, misses = {b: [] for b in boosts}, {b: [] for b in boosts}, []
        for c, v in zip(cases, vecs):
            lex = store.lexical(c["q"]) if c.get("q") else None
            for b in boosts:
                res = {tab: store.search(v, k=TOP_K, kinds=kinds, exclude_path=c.get("image"), lex=lex, boost=b,
                                         hub=args.hub if c.get("q") else 0.0)
                       for tab, kinds in FILTERS.items()}
                hits = res[c.get("tab", "all")]
                ranks[b].append(first_rank(hits, c["expect"]))
                vis[b].append(visible_in_all(res, c["expect"]))
                if b == args.show and ranks[b][-1] != 1:
                    misses.append((c, hits[: args.top]))
        store.db.close()

    tags = sorted({c.get("tag", "") for c in cases})
    print(f"{len(cases)} 条查询，索引 {len(store)} 条向量\n")
    for tag in [None] + [t for t in tags if t]:
        idx = [i for i, c in enumerate(cases) if tag is None or c.get("tag") == tag]
        print(f"[{tag or '全部'}] {len(idx)} 条")
        print(f"  {'boost':>6} {'R@1':>6} {'R@5':>6} {'R@10':>6} {'MRR@10':>7} {'全部可见':>6}")
        for b in boosts:
            m = metrics([ranks[b][i] for i in idx], [vis[b][i] for i in idx])
            print(f"  {b:>6.2f} {m['R@1']:>6.2f} {m['R@5']:>6.2f} {m['R@10']:>6.2f} {m['MRR@10']:>7.3f}"
                  f" {m['全部可见']:>9.2f}")
        print()

    fmt = lambda r: "-" if r is None else str(r)
    print(f"逐条排名（纯向量 → 加分 {args.show}；× = 在「全部」里看不到）")
    for c, r0, r1, v in zip(cases, ranks[0.0], ranks[args.show], vis[args.show]):
        mark = "" if r0 == r1 else ("  ↑" if (r1 or 999) < (r0 or 999) else "  ↓")
        print(f"  {fmt(r0):>3} → {fmt(r1):<3}{' ' if v else '×'} {c.get('tag', ''):<9} {label(c)}{mark}")
    for c, hits in misses if args.top else []:
        print(f"\n「{label(c)}」应命中 {c['expect']}")
        for h in hits:
            print(f"  {h.score:.3f}{' *' if h.lexical else '  '} {h.path}"
                  + (f"  （另有 {len(h.copies)} 个副本）" if h.copies else ""))


if __name__ == "__main__":
    main()
