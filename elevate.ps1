$ErrorActionPreference = 'Stop'

$bat = Join-Path $PSScriptRoot 'start.bat'
if (-not (Test-Path -LiteralPath $bat)) {
    Write-Host "未找到 start.bat：$bat"
    exit 1
}

try {
    Start-Process -FilePath 'cmd.exe' `
        -ArgumentList '/c', "`"$bat`"" `
        -WorkingDirectory $PSScriptRoot `
        -Verb RunAs
} catch {
    Write-Host "提权失败：$($_.Exception.Message)"
    exit 1
}
