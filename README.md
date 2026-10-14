# MMSearch：基于 EmbeddingGemma 2 的本地多模态快速搜索（Windows）

用一句话（中英文都行）、一张图片或一段音频，在本地文件夹里搜索**文本 / 代码 / PDF / 图片 / 音频 / 视频**。
所有数据和模型都在本机运行，不联网。

## 快速开始

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1   # 安装依赖、llama.cpp 运行时、模型（约 1.3 GB）
.\.venv\Scripts\pythonw.exe main.py
```

1. 首次打开时勾选常用位置（文档、图片、视频……）后点「开始索引」，或选择其他文件夹，也可以直接把文件夹拖进窗口
2. 索引在后台增量进行（只处理新增或修改的文件，已删除的文件会自动清理；移动、改名、复制过的文件按内容指纹直接复用已有结果，不重新编码），期间就可以搜索，搜索请求优先于索引
3. 在搜索框输入描述即可；也可以把图片/音频/视频拖进窗口、Ctrl+V 粘贴截图，或右键结果选「查找相似内容」

之后按 **Alt+Space**（可在设置中更改）随时唤出搜索面板。

| 操作 | 快捷键 |
|---|---|
| 选择 / 打开 / 在文件夹中显示 | ↑↓ / ↵ / Ctrl+↵ |
| 结果的更多操作（右键菜单） | 右键、Shift+F10 |
| 播放命中的音视频片段 | Ctrl+P |
| 查找相似内容 | Ctrl+S |
| 复制文件 / 复制路径 | Ctrl+C / Ctrl+Shift+C |
| 切换类型（全部、文档、图片、音频、视频） | Tab、Ctrl+1~5 |
| 以文件搜索 / 以剪贴板图片搜索 | Ctrl+O / Ctrl+V |
| 索引管理 / 设置 / 立即更新索引 | Ctrl+I / Ctrl+, / F5 |

## 界面外观

设置页（`Ctrl+,`）支持跟随系统 / 深色 / 浅色主题，以及半透明磨砂背景。也可以选择本地图片作为背景，并调节模糊强度与底色遮罩；图片会统一处理后复制到 `~/.mmsearch/background/background.jpg`，原图移动或删除不影响使用，且不会上传。

## 架构

```
            ┌──── MMSearch（pywebview + Vue 启动器面板）────┐
文件夹 ──▶ 扫描/增量 ──▶ 媒体预处理（PyAV / Pillow / pypdf）
                         │ 文本切块 · 图片缩放 · 音频 16kHz 30s 分段
                         │ 视频 30s 分段 × 4 帧 + 音轨单独分段
                         ▼
              llama-server（子进程，Vulkan 核显 / CPU）
              embeddinggemma-2-Q8_0.gguf + mmproj-F16.gguf
                         │ /v1/embeddings → 768d 归一化向量
                         ▼
     SQLite（768d float16 持久化） + 内存矩阵（256d 粗排 → 768d 精排）
```

- **统一向量空间**：EmbeddingGemma 2 把文本、图片、视频、音频映射到同一个 768 维空间，所以“文字搜图片/视频/音频”“图搜图”“音频搜视频”用的是同一套索引和查询逻辑。
- **任务前缀**：查询用 `task: search result | query: …`，文档用 `title: {文件名} | text: …`；图片、音频、视频不加前缀（模型卡要求）。
- **两阶段检索**：利用 Matryoshka（MRL），先在 256 维前缀上做全量矩阵乘法粗排，再对前 200 个候选用完整 768 维精排；结果按文件去重，只保留得分最高的片段。「查找相似」和「以文件搜索」会排除查询文件本身。
- **「全部」按类型分区**：不同模态的相似度尺度不同（文本的基线分数高，图片分数被压缩在很窄的区间），不能用同一个阈值筛选，否则图片和视频会被大量文档挤出去。所以「全部」先给出最佳匹配，再按类型各展示最好的几个；单一类型的标签内分数可比，才按与最高分的差距折叠低相关结果。
- **搜索优先**：llama-server 编码图片时会阻塞另一个槽位，而一个请求里的多张图片会被拆成多个任务排队，所以索引改为单条小批量请求、同一时间只占一个推理槽；查询进行中、刚结束 3 秒内（可能还在输入）和面板唤出后 8 秒内，索引暂停发新请求。实测索引图片期间查询编码从约 40 秒降到：连续输入时只有第一个查询可能等一张正在编码的图片（约 1–3 秒），之后约 0.1 秒。
- **音视频片段**：每 30 秒一段，结果会显示命中片段的时间；装了 VLC 时双击会直接从该时间点播放。视频的画面和音轨分别编码，所以能按台词或声音找到视频。

## 实测（i5-1135G7 + Iris Xe 核显，16 GB）

| | Vulkan（核显，默认） | CPU |
|---|---|---|
| 文本查询编码 | **~80–120 ms** | ~800 ms |
| 图片 | ~1.5 s/张 | ~5 s/张 |
| 音频 | ~0.6 s/段 | ~1.8 s/段 |
| 视频 | ~6 s/30 秒片段（4 帧） | — |
| 检索（10 万条向量） | **~9 ms**（numpy，与设备无关） | |

与 PyTorch 原版权重（`google/embeddinggemma-2`，fp32）对比余弦相似度：文本 0.9999、音频 0.999、图片 0.94–0.97。图片的差距来自 llama.cpp 与 HF 预处理方式不同，**不是量化造成的**：mmproj 用 Q8_0 和 F16 结果一样。由于索引和查询走的是同一条路径，排序不受影响，测试集的 top-1 全部正确。

为什么不用 PyTorch：同一台机器上 PyTorch CPU fp32 处理一张图约 30 秒，文本查询约 0.7 秒；llama.cpp 用核显的 Vulkan 后端快 5–20 倍，而且客户端不需要安装 torch。

## 可调参数（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MMSEARCH_HOME` | `~/.mmsearch` | 索引库、设置、llama-server 日志 |
| `MMSEARCH_GGUF` | `embeddinggemma-2-Q8_0.gguf` | 文本模型，可换 `UD-Q4_K_XL` 等（用 `setup.ps1 -Quant` 下载） |
| `MMSEARCH_MMPROJ` | `mmproj-F16.gguf` | 视觉/音频编码器；核显上 F16 比 Q8_0 快约 40% |
| `MMSEARCH_IMAGE_TOKENS` | `280` | 每张图片/帧的视觉 token 预算；设为 `70` 大约快 2 倍，细节会少一些 |
| `MMSEARCH_LLAMA_ARGS` | 空 | 追加给 llama-server 的参数 |

文本切块、视频分段与抽帧数量、粗排维度等在 `mmsearch/config.py` 中设置。

## 已知限制

- 单个输入最多 8192 token：长文本按约 1200 字切块，长音视频按 30 秒分段。
- 日志、数据导出这类文件（`.log` `.json` `.csv` `.xml` `.sql` `.yaml`）往往很大且内容重复，每个文件只索引开头、中间、结尾各 10 块（`config.DATA_CHUNKS_EACH`），其他文本每个文件最多 200 块。
- 视频按“多帧图片”编码，没有使用 llama.cpp 原生的 `input_video`：原生方式依赖系统里的 ffmpeg，而且默认 4 fps、会插入时间戳文本，和模型训练时的设置（1 fps、无时间戳）不一致。
- llama.cpp 的音频支持目前还标注为实验性（启动日志里有提示），不过测试中与 PyTorch 的结果一致（0.999）。
- 128 维截断会明显损失多模态质量（模型卡数据），所以粗排固定用 256 维。

## 目录

```
main.py                 入口
mmsearch/webapp.py      桌面外壳（pywebview / WebView2）与暴露给前端的 Api
mmsearch/shell.py       全局快捷键、托盘、开机自启、常用文件夹
mmsearch/mediaserver.py 本地缩略图 / 波形 / 媒体流服务
mmsearch/embedder.py    llama-server 托管 + 多模态 embedding 客户端
mmsearch/indexer.py     增量扫描与并发索引
mmsearch/media.py       文本抽取/切块、音频解码、视频抽帧
mmsearch/store.py       SQLite + 两阶段向量检索
mmsearch/web/           前端构建产物（由 frontend/ 构建：cd frontend && npx vite build）
frontend/               界面（Vue 3 + Vite）
setup.ps1 / build.ps1   下载运行时与模型 / 打包分发
```

## 许可证

代码以 [MIT](LICENSE) 许可证开源。模型文件（EmbeddingGemma 2 GGUF）、llama.cpp 运行时等由 `setup.ps1` 另行下载，不包含在本仓库中，使用时请遵守它们各自的许可条款。
