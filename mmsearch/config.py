import json
import os
import re
import sys
from pathlib import Path

APP_NAME = "MMSearch"
APP_VERSION = "1.0.0"

# 数据目录：索引库、设置、日志、（安装目录不可写时的）模型
DATA_DIR = Path(os.environ.get("MMSEARCH_HOME", Path.home() / ".mmsearch"))
DB_PATH = DATA_DIR / "index.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
# 自定义背景图（统一转成一张 JPEG 存在这里，原图移动/删除都不影响）
BG_DIR = DATA_DIR / "background"
BG_MAX_SIDE = 3840  # 背景图最长边（超过就等比缩小，4K 壁纸够用）

# 打包后（PyInstaller）程序目录是 exe 所在目录；只读资源在 _MEIPASS
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
RUNTIME_DIR = APP_DIR / "runtime"

# 模型：llama.cpp 运行时 + EmbeddingGemma 2 GGUF
GGUF_REPO = "unsloth/embeddinggemma-2-GGUF"
GGUF_NAME = os.environ.get("MMSEARCH_GGUF", "embeddinggemma-2-Q8_0.gguf")
MMPROJ_NAME = os.environ.get("MMSEARCH_MMPROJ", "mmproj-F16.gguf")
MODEL_SIZES = {"embeddinggemma-2-Q8_0.gguf": 309855520, "mmproj-F16.gguf": 980895232}
HF_ENDPOINTS = [("https://huggingface.co", "Hugging Face"), ("https://hf-mirror.com", "hf-mirror（国内镜像）")]
MODEL_DIRS = [APP_DIR / "models", DATA_DIR / "models"]
# 模型来源：official = 官方仓库（文件名可用上面的环境变量换量化）；repo = 其他 Hugging Face 仓库；
# local = 用户指定的本地 GGUF 文件（绝对路径）。设置里官方来源只存 kind，环境变量改了也能生效
OFFICIAL_SOURCE = {"kind": "repo", "repo": GGUF_REPO, "model": GGUF_NAME, "mmproj": MMPROJ_NAME}
_REPO_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")

CTX_PER_SLOT = 8192  # 模型最大上下文，所有模态共享
PARALLEL = 2  # 两个槽位：索引进行中查询也不用排队等整段视频
CPU_THREADS = max(1, (os.cpu_count() or 4) // 2)  # 物理核数
# 每张图片/视频帧的视觉 token 预算（模型支持 70~1120）。越小越快，细节越少
IMAGE_TOKEN_PRESETS = [(70, "快速"), (280, "标准"), (560, "精细")]
LLAMA_EXTRA_ARGS = os.environ.get("MMSEARCH_LLAMA_ARGS", "").split()

DEFAULT_SETTINGS = {
    "backend": "auto",
    "image_tokens": int(os.environ.get("MMSEARCH_IMAGE_TOKENS", 280)),
    "auto_index": True,
    "watch": True,  # 监听已添加的文件夹，文件新增/修改/删除后自动更新索引
    "view": "grid",
    "hf_endpoint": HF_ENDPOINTS[0][0],
    "model_source": {"kind": "official"},
    "theme": "system",
    "hotkey": "alt+space",
    "autohide": True,
    "glass": True,  # 半透明磨砂：面板透出桌面/窗口，由 Windows 系统做模糊
    "bg_image": "",  # 自定义背景图文件名（BG_DIR 下）
    "bg_blur": 18,  # 背景图模糊半径（px）
    "bg_dim": 42,  # 背景遮罩浓度（%，越大越暗、文字越清楚）
}


_source: dict = dict(OFFICIAL_SOURCE)


def set_model_source(src: dict | None):
    """切换当前模型来源（启动时和设置里更换模型时调用）。"""
    global _source
    _source = dict(OFFICIAL_SOURCE if not src or src.get("kind") == "official" else src)


def model_source() -> dict:
    """实际生效的来源（official 已展开成官方仓库和文件名）。"""
    return dict(_source)


def check_model_source(src: dict) -> str:
    """校验用户填写的来源，返回错误说明（空串表示没问题）。"""
    kind = src.get("kind")
    if kind == "official":
        return ""
    model, mmproj = str(src.get("model") or "").strip(), str(src.get("mmproj") or "").strip()
    if kind == "repo":
        if not _REPO_RE.match(str(src.get("repo") or "").strip()):
            return "仓库名应形如 作者/仓库名"
        for name in (model, mmproj):
            if not name.lower().endswith(".gguf") or ".." in name.replace("\\", "/").split("/") or name.startswith("/"):
                return f"文件名应是仓库里的 .gguf 文件：{name or '（空）'}"
    elif kind == "local":
        for path in (model, mmproj):
            if not path.lower().endswith(".gguf") or not Path(path).is_absolute():
                return f"请选择本地的 .gguf 文件：{path or '（未选择）'}"
    else:
        return "未知的模型来源"
    if model == mmproj:
        return "模型文件和 mmproj 文件不能是同一个"
    return ""


def _repo_subdir(repo: str) -> str:
    """非官方仓库下载到 models/<作者>__<仓库>/，免得同名文件（如 mmproj-F16.gguf）互相覆盖。"""
    return "" if repo == GGUF_REPO else repo.replace("/", "__")


def model_files() -> dict[str, Path | None]:
    """当前来源下 {"model": 路径, "mmproj": 路径}，找不到的为 None。"""
    out: dict[str, Path | None] = {}
    for role in ("model", "mmproj"):
        value = str(_source.get(role) or "")
        if _source.get("kind") == "local":
            p = Path(value)
            out[role] = p if value and p.is_file() else None
            continue
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        sub = _repo_subdir(_source.get("repo", ""))
        out[role] = next((d / sub / name for d in MODEL_DIRS if name and (d / sub / name).is_file()), None)
    return out


def model_download_dir() -> Path:
    """优先放到程序目录（便携），不可写时放到数据目录；非官方仓库再分子目录。"""
    sub = _repo_subdir(_source.get("repo", ""))
    d = MODEL_DIRS[0]
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        d = MODEL_DIRS[1]
    (d / sub).mkdir(parents=True, exist_ok=True)
    return d / sub


def known_size(name: str) -> int:
    """官方仓库文件的已知大小（用来校验下载）；其他来源不知道，返回 0。"""
    official = _source.get("kind") == "repo" and _source.get("repo") == GGUF_REPO
    return MODEL_SIZES.get(name, 0) if official else 0


def missing_models() -> list[str]:
    """缺少的文件（仓库来源是仓库内文件名，本地来源是路径）。"""
    found = model_files()
    return [str(_source.get(role) or role) for role in ("model", "mmproj") if found[role] is None]


def model_fingerprint() -> str:
    """索引库记录的模型标识：换了模型（哪怕只是换量化）旧向量就不能和新查询混用。"""
    parts = []
    for role, p in model_files().items():
        parts.append(f"{role}={p.name}:{p.stat().st_size}" if p else f"{role}=?")
    return ";".join(parts)


FULL_DIM = 768
# 粗排维度（Matryoshka 截断）。256d 接近无损，打分速度是 768d 的 3 倍
COARSE_DIM = 256
# 粗排后用 768d 精排的候选数
RERANK_CANDIDATES = 200
# 混合检索：文字查询的关键词命中（文件名、正文）给向量相似度加分，最多加这么多（见 store.lexical）
LEXICAL_BOOST = 0.1
LEXICAL_CANDIDATES = 50  # 正文关键词命中取 bm25 最好的前 N 条

# 文本切块
TEXT_CHUNK_CHARS = 1200
TEXT_CHUNK_OVERLAP = 150
TEXT_MAX_CHUNKS_PER_FILE = 200
# 日志、数据导出这类机器生成的文件往往很大、内容重复：只取开头、中间、结尾各几块
DATA_EXT = {".log", ".json", ".csv", ".xml", ".sql", ".yaml", ".yml"}
DATA_CHUNKS_EACH = 10

# 音频：每段 30s（25 token/s → 750 token）
AUDIO_SEGMENT_SEC = 30.0
AUDIO_SR = 16000
# 视频：每段 30s，每段采样帧数（每帧 140 token）
VIDEO_SEGMENT_SEC = 30.0
VIDEO_FRAMES_PER_SEGMENT = 4  # 每帧约 1.5s（Iris Xe），4 帧 ≈ 6s 处理 30s 视频
VIDEO_MAX_SEGMENTS = 240  # 最多处理 2 小时

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".m4v"}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".xml", ".html", ".htm", ".py", ".go", ".js", ".ts", ".tsx", ".jsx", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rs", ".rb", ".php", ".sh", ".ps1", ".sql", ".kt",
    ".swift", ".lua", ".vue", ".css", ".scss",
}
DOC_EXT = {".pdf", ".docx"}
CODE_EXT = TEXT_EXT - {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".html", ".htm"}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "$RECYCLE.BIN",
             "System Volume Information", ".idea", ".vs", "dist", "build"}
MAX_FILE_BYTES = {"text": 20 * 1024 * 1024, "image": 200 * 1024 * 1024}


def modality_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in TEXT_EXT or ext in DOC_EXT:
        return "text"
    return None


def load_settings() -> dict:
    d = dict(DEFAULT_SETTINGS)
    try:
        d.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return d


def save_settings(d: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
