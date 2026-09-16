[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$Checkpoint,
    [string]$Workspace = (Split-Path $PSScriptRoot -Parent)
)

$ErrorActionPreference = 'Stop'
$Workspace = [IO.Path]::GetFullPath($Workspace)
$Checkpoint = [IO.Path]::GetFullPath($Checkpoint)
$entries = @(Import-Clixml -LiteralPath (Join-Path $Checkpoint 'hardening.clixml'))
$actions = foreach ($entry in $entries) {
    $target = [IO.Path]::GetFullPath((Join-Path $Workspace $entry.Path))
    if (-not $target.StartsWith($Workspace.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Invalid rollback path: $($entry.Path)"
    }
    $current = if (Test-Path -LiteralPath $target -PathType Leaf) {
        (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    } else { '' }
    if ($entry.Existed -and $current -eq $entry.Before) { continue }
    if (-not $entry.Existed -and -not $current) { continue }
    if ($current -ne $entry.After) {
        throw "Refusing rollback: $($entry.Path) changed after hardening. Preserve and reconcile that file first."
    }
    $backup = Join-Path $Checkpoint $entry.Path
    if ($entry.Existed -and (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash -ne $entry.Before) {
        throw "Backup checksum mismatch: $($entry.Path)"
    }
    [pscustomobject]@{Target=$target; Backup=$backup; Existed=$entry.Existed}
}
foreach ($action in $actions) {
    if ($PSCmdlet.ShouldProcess($action.Target, 'Restore pre-hardening contents')) {
        if ($action.Existed) {
            Copy-Item -LiteralPath $action.Backup -Destination $action.Target -Force
        } else {
            Remove-Item -LiteralPath $action.Target
        }
    }
}
