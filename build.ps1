# 构建 Windows 分发包：dist\MMSearch\MMSearch.exe（以及 dist\MMSearch-<版本>-win64.zip）
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-WithModels] [-NoZip]
param(
    [switch]$WithModels,   # 把 models\ 一起打包（离线分发，约 +1.3 GB）
    [switch]$NoZip
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$version = (Select-String -Path mmsearch\config.py -Pattern 'APP_VERSION = "(.+)"').Matches[0].Groups[1].Value

Write-Host "== 1/4 构建前端"
Push-Location frontend
if (-not (Test-Path node_modules)) { npm install --no-audit --no-fund }
npx vite build
Pop-Location

Write-Host "== 2/4 准备干净的构建环境"
if (-not (Test-Path .venv-build)) { python -m venv .venv-build }
& .\.venv-build\Scripts\python.exe -m pip install -q --upgrade pip
& .\.venv-build\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller

Write-Host "== 3/4 PyInstaller"
& .\.venv-build\Scripts\python.exe -m PyInstaller mmsearch.spec --noconfirm --clean --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

Write-Host "== 4/4 运行时与模型"
$dist = "dist\MMSearch"
# llama.cpp：只保留 llama-server 及其依赖
$keep = @("llama-server.exe", "llama-server-impl.dll", "llama.dll", "llama-common.dll", "mtmd.dll", "ggml*.dll", "libomp.dll", "LICENSE*")
foreach ($b in "vulkan", "cpu") {
    $src = "runtime\llama-$b"
    if (-not (Test-Path "$src\llama-server.exe")) { throw "缺少 $src，请先运行 setup.ps1" }
    $dst = "$dist\runtime\llama-$b"
    New-Item -ItemType Directory -Force $dst | Out-Null
    foreach ($pat in $keep) { Copy-Item "$src\$pat" $dst -ErrorAction SilentlyContinue }
    Remove-Item "$dst\ggml-rpc.dll" -ErrorAction SilentlyContinue
}
if ($WithModels) {
    New-Item -ItemType Directory -Force "$dist\models" | Out-Null
    Copy-Item models\embeddinggemma-2-Q8_0.gguf, models\mmproj-F16.gguf "$dist\models\"
}
Copy-Item README.md $dist

if (-not $NoZip) {
    $zip = "dist\MMSearch-$version-win64" + ($(if ($WithModels) { "-full" } else { "" })) + ".zip"
    if (Test-Path $zip) { Remove-Item $zip }
    Compress-Archive -Path $dist -DestinationPath $zip -CompressionLevel Optimal
    Write-Host ("压缩包：{0}（{1:N0} MB）" -f $zip, ((Get-Item $zip).Length / 1MB))
}
$size = (Get-ChildItem $dist -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("完成：{0}\MMSearch.exe（目录共 {1:N0} MB）" -f $dist, $size)
