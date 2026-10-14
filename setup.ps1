# MMSearch 一键安装：Python 依赖 + llama.cpp 运行时 + EmbeddingGemma 2 GGUF 模型
#   powershell -ExecutionPolicy Bypass -File setup.ps1 [-Quant Q8_0] [-Mmproj F16]
param(
    [string]$LlamaBuild = "b11563",          # 需包含 PR #30054（embeddinggemma2 支持）
    [string]$Quant = "Q8_0",                 # 文本模型：UD-Q4_K_XL / UD-Q5_K_XL / UD-Q6_K_XL / Q8_0 / BF16
    [string]$Mmproj = "F16",                 # 视觉+音频编码器：F16（核显上最快）/ Q8_0 / BF16
    [string]$Repo = "unsloth/embeddinggemma-2-GGUF"
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

New-Item -ItemType Directory -Force runtime, models | Out-Null
foreach ($b in "vulkan", "cpu") {
    $dir = "runtime\llama-$b"
    if (-not (Test-Path "$dir\llama-server.exe")) {
        $zip = "runtime\llama-$b.zip"
        Write-Host "下载 llama.cpp ($b) ..."
        curl.exe -L --fail -o $zip "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaBuild/llama-$LlamaBuild-bin-win-$b-x64.zip"
        Expand-Archive -Force $zip $dir
        Remove-Item $zip
    }
}

foreach ($f in "embeddinggemma-2-$Quant.gguf", "mmproj-$Mmproj.gguf") {
    if (-not (Test-Path "models\$f")) {
        Write-Host "下载 $f ..."
        curl.exe -L --fail -C - -o "models\$f.part" "https://huggingface.co/$Repo/resolve/main/$f"
        Move-Item -Force "models\$f.part" "models\$f"
    }
}
if ($Quant -ne "Q8_0" -or $Mmproj -ne "F16") {
    Write-Host "非默认量化：启动前设置环境变量 MMSEARCH_GGUF=embeddinggemma-2-$Quant.gguf MMSEARCH_MMPROJ=mmproj-$Mmproj.gguf"
}
Write-Host "完成。运行： .\.venv\Scripts\pythonw.exe main.py"
