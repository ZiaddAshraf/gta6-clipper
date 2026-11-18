param(
    [switch]$AllGames,
    [string]$ReelCaption = 'GTA 6 MOMENT'
)

$python = Join-Path $PSScriptRoot '.venv-win\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python environment not found. Follow the setup steps in README.md.'
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $binary = Get-ChildItem -LiteralPath $packages -Recurse -Filter ffmpeg.exe -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match 'Gyan\.FFmpeg' } |
        Select-Object -First 1
    if (-not $binary) {
        throw 'FFmpeg not found. Install it with: winget install --id Gyan.FFmpeg.Essentials -e --scope user'
    }
    $env:Path = "$($binary.DirectoryName);$env:Path"
}

$options = @('--reel-caption', $ReelCaption)
if ($AllGames) {
    $options += '--all-games'
}

& $python (Join-Path $PSScriptRoot 'watch_channels.py') @options
exit $LASTEXITCODE
