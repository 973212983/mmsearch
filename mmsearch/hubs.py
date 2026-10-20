"""「万能图」修正：有些图片（只有一行字的按钮截图、几乎空白的网页截图）和任何文字查询都比较像，
会出现在大量不相干查询的图片结果前几名。用一批和具体文件无关的通用查询衡量每张图有多「万能」：
万能分 = 这张图和这批查询的平均相似度 - 所有图片的中位数，文字查询时图片得分减去 HUB_PENALTY × 万能分。
不用索引里的文本块做参照：那样挑出来的是字多的图（论文插图、发票），它们和具体查询本来就该相似。"""
from __future__ import annotations

import hashlib

import numpy as np

from . import config

QUERIES = [
    "一只猫", "海边的日落", "生日聚会的照片", "身份证正反面", "会议记录", "项目进度表", "红色的汽车", "公司年会合影",
    "手写的笔记", "超市购物小票", "地铁线路图", "风景照", "截图", "合同扫描件", "孩子在公园玩", "一盘菜",
    "登录页面", "报错信息", "柱状图", "流程图", "微信聊天记录", "火车票", "简历", "护照", "雪山", "城市夜景",
    "狗在草地上跑", "白色背景的产品图", "PPT 封面", "Excel 表格", "证书", "发票", "二维码", "公司 logo",
    "手机壁纸", "表情包", "一张地图", "建筑设计图", "医院检查报告", "家庭照片", "毕业照", "宠物",
    "a cat sleeping on a sofa", "sunset over the ocean", "a screenshot of an error", "bar chart", "a receipt",
    "family photo", "mountain landscape", "a red car", "website homepage", "handwritten notes", "a cup of coffee",
    "people in a meeting room", "a map", "flowchart", "a dog", "food on a plate", "a city at night", "company logo",
    "如何配置数据库连接", "上个月的报销单", "季度销售报告", "用户手册", "接口文档", "测试用例", "部署步骤",
    "旅行计划", "年终总结", "租房合同", "体检报告", "装修效果图", "学习笔记", "读书笔记", "菜谱", "课程表",
    "how to install", "meeting notes", "invoice", "project plan",
]
CACHE = config.DATA_DIR / "hub_bank.npz"


def bank_vectors(emb) -> np.ndarray:
    """QUERIES 的向量 (n, 768)。同一模型只编码一次，缓存在 CACHE。"""
    key = hashlib.sha1("\n".join([config.model_fingerprint(), *QUERIES]).encode()).hexdigest()
    try:
        with np.load(CACHE) as d:
            if str(d["key"]) == key:
                return d["vecs"]
    except (OSError, KeyError, ValueError):
        pass
    vecs = np.stack([emb.encode_query(q) for q in QUERIES]).astype(np.float32)
    np.savez(CACHE, key=key, vecs=vecs)
    return vecs
