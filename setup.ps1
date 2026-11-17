# One-time setup: venv + deps, OneOCR runtime from the local Snipping Tool,
# llama.cpp (CUDA) and the translation model. Re-running skips what already exists.
#   setup.ps1            LLM engine (default)
#   setup.ps1 -WithNmt   also build the optional non-LLM engine (mt5-game-large, ~5 GB download)
param([switch]$WithNmt)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

# 1) Python venv
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    py -3.11 -m venv "$root\.venv"
}
& "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip -q
& "$root\.venv\Scripts\python.exe" -m pip install -q -r "$root\requirements.txt"

# 2) OneOCR (Windows 11 Snipping Tool engine) — copied locally, never redistributed
New-Item -ItemType Directory -Force "$root\ocr" | Out-Null
if (-not (Test-Path "$root\ocr\oneocr.onemodel")) {
    $pkg = Get-AppxPackage Microsoft.ScreenSketch
    if (-not $pkg) { throw 'Snipping Tool (Microsoft.ScreenSketch) not installed; install/update it from Microsoft Store.' }
    foreach ($f in 'oneocr.dll', 'oneocr.onemodel', 'onnxruntime.dll') {
        Copy-Item "$($pkg.InstallLocation)\SnippingTool\$f" "$root\ocr\$f" -Force
    }
}

# 3) llama.cpp CUDA build (CUDA 13.x build has native RTX 50 / Blackwell kernels)
if (-not (Test-Path "$root\bin\llama\llama-server.exe")) {
    New-Item -ItemType Directory -Force "$root\bin\llama", "$root\downloads" | Out-Null
    $rel = Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases/latest' -Headers @{ 'User-Agent' = 'setup' }
    $bin = $rel.assets | Where-Object { $_.name -match '^llama-.*-bin-win-cuda-13\.\d+-x64\.zip$' } | Select-Object -First 1
    $rt = $rel.assets | Where-Object { $_.name -match '^cudart-llama-bin-win-cuda-13\.\d+-x64\.zip$' } | Select-Object -First 1
    if (-not $bin) { throw 'Could not find a CUDA 13 Windows build in the latest llama.cpp release.' }
    curl.exe -L -o "$root\downloads\llama.zip" $bin.browser_download_url
    curl.exe -L -o "$root\downloads\cudart.zip" $rt.browser_download_url
    Expand-Archive -Force "$root\downloads\llama.zip" "$root\bin\llama"
    Expand-Archive -Force "$root\downloads\cudart.zip" "$root\bin\llama"
}

# 4) Translation model (Sakura-GalTransl 7B v3.7, IQ4_XS, ~4 GB; license CC BY-NC-SA 4.0)
$model = "$root\models\Sakura-Galtransl-7B-v3.7-IQ4_XS.gguf"
if (-not (Test-Path $model)) {
    New-Item -ItemType Directory -Force "$root\models" | Out-Null
    curl.exe -L -o $model 'https://huggingface.co/SakuraLLM/Sakura-GalTransl-7B-v3.7/resolve/main/Sakura-Galtransl-7B-v3.7-IQ4_XS.gguf'
}
# 5) Optional non-LLM engine: convert iryneko571/mt5-translation-ja_zh-game-large to CTranslate2.
#    Conversion needs torch + transformers, so it runs in a throwaway venv that is deleted afterwards.
$nmt = "$root\models\nmt\mt5-game-large"
if ($WithNmt -and -not (Test-Path "$nmt\model.bin")) {
    $dl = "$root\downloads"
    $cv = "$dl\convert-venv"
    $hf = "$dl\hf"
    New-Item -ItemType Directory -Force $dl | Out-Null
    if (-not (Test-Path "$cv\Scripts\python.exe")) { py -3.11 -m venv $cv }
    & "$cv\Scripts\python.exe" -m pip install -q --upgrade pip
    & "$cv\Scripts\python.exe" -m pip install -q torch --index-url https://download.pytorch.org/whl/cpu
    & "$cv\Scripts\python.exe" -m pip install -q transformers ctranslate2==4.9.0 sentencepiece huggingface_hub protobuf
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
    & "$cv\Scripts\python.exe" -I -c @"
from huggingface_hub import snapshot_download as d
d('iryneko571/mt5-translation-ja_zh-game-large', local_dir=r'$hf\mt5-game-large',
  allow_patterns=['*.json', 'model.safetensors'])
# the large repo ships no spiece.model; its tokenizer is identical to the small model's
d('iryneko571/mt5-translation-ja_zh-game-small', local_dir=r'$hf\mt5-game-small',
  allow_patterns=['spiece.model'])
"@
    if ($LASTEXITCODE -ne 0) { throw 'model download failed' }
    Copy-Item "$hf\mt5-game-small\spiece.model" "$hf\mt5-game-large\spiece.model" -Force
    & "$cv\Scripts\python.exe" -I "$root\tools\convert_nmt.py" "$hf\mt5-game-large" $nmt int8 spiece.model
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$nmt\model.bin")) { throw 'NMT conversion failed' }
    Remove-Item -Recurse -Force $dl   # ~6 GB of temporary files
}
Write-Host 'Setup complete. Run start.bat'
