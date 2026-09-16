<#
.SYNOPSIS
    Build a source-only CloudLens App Service ZIP without local state or development files.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Destination,
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root = (Get-Item -LiteralPath $SourceRoot).FullName.TrimEnd('\', '/')
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) {
    throw "Destination already exists: $destinationPath"
}
$files = [System.Collections.Generic.List[object]]::new()
$allowedWeb = @('.html', '.js', '.css', '.svg', '.png', '.ico', '.woff', '.woff2', '.ttf')
foreach ($directory in @('app', 'web')) {
    $base = Join-Path $root $directory
    if (-not (Test-Path -LiteralPath $base -PathType Container)) {
        throw "Package source is missing $directory."
    }
    foreach ($folder in @((Get-Item -LiteralPath $base)) + @(Get-ChildItem -LiteralPath $base -Recurse -Directory -Force)) {
        if ($folder.LinkType -in @('SymbolicLink', 'Junction')) {
            throw "Refusing a linked package directory: $($folder.FullName)"
        }
    }
    foreach ($file in Get-ChildItem -LiteralPath $base -Recurse -File -Force) {
        $relative = $file.FullName.Substring($root.Length).TrimStart('\', '/').Replace('\', '/')
        if ($relative -match '(^|/)(\.|__pycache__|node_modules|tests/)' -or
            $relative -match '\.(test|spec)\.js$') { continue }
        if (($directory -eq 'app' -and $file.Extension -ne '.py') -or
            ($directory -eq 'web' -and $file.Extension -notin $allowedWeb)) { continue }
        if ($file.LinkType -in @('SymbolicLink', 'Junction')) {
            throw "Refusing a linked package file: $relative"
        }
        $files.Add([pscustomobject]@{ Path = $relative; Source = $file.FullName; Size = $file.Length })
    }
}
$requirements = Join-Path $root 'requirements.txt'
if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
    throw 'Package source is missing requirements.txt.'
}
if ((Get-Item -LiteralPath $requirements).LinkType -in @('SymbolicLink', 'Junction')) {
    throw 'Refusing linked requirements.txt.'
}
$files.Add([pscustomobject]@{
    Path = 'requirements.txt'; Source = $requirements; Size = (Get-Item -LiteralPath $requirements).Length
})
$paths = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($file in $files) { $paths.Add($file.Path) | Out-Null }
foreach ($required in @('app/__init__.py', 'app/main.py', 'web/index.html', 'web/login.html',
                        'web/assets/dashboard.js', 'web/assets/app.js', 'web/assets/app.css')) {
    if (-not $paths.Contains($required)) { throw "Package is missing $required." }
}

# Validate local module imports before writing any archive, including untracked lazy views.
$imports = '(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)["''](?<path>\.{1,2}/[^"'']+)["'']'
foreach ($file in $files | Where-Object { $_.Path.EndsWith('.js') }) {
    $source = Get-Content -LiteralPath $file.Source -Raw
    foreach ($match in [regex]::Matches($source, $imports)) {
        $module = ($match.Groups['path'].Value -split '[?#]', 2)[0]
        $absolute = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $file.Source) $module))
        if (-not $absolute.StartsWith($root + [IO.Path]::DirectorySeparatorChar,
                                      [StringComparison]::OrdinalIgnoreCase)) {
            throw "Module import escapes the package: $($file.Path) -> $module"
        }
        $relative = $absolute.Substring($root.Length).TrimStart('\', '/').Replace('\', '/')
        if (-not $paths.Contains($relative)) {
            throw "Missing imported module: $($file.Path) -> $module"
        }
    }
}

# Parse source only: packaging must not initialize the warehouse or read local configuration.
$validation = Join-Path $PSScriptRoot 'validate_package.py'
@($files.Path) | ConvertTo-Json -Compress | & $Python $validation $root
if ($LASTEXITCODE -ne 0) { throw 'Package source validation failed.' }

$manifest = [ordered]@{
    application = 'CloudLens'
    createdUtc = [DateTime]::UtcNow.ToString('o')
    files = @($files | Sort-Object Path | ForEach-Object {
        [ordered]@{ path = $_.Path; bytes = $_.Size; sha256 = (Get-FileHash -LiteralPath $_.Source -Algorithm SHA256).Hash.ToLowerInvariant() }
    })
}
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destinationPath)) | Out-Null
$stream = [IO.File]::Open($destinationPath, [IO.FileMode]::CreateNew)
$archive = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in $files | Sort-Object Path) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive, $file.Source, $file.Path, [IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
    $entry = $archive.CreateEntry('release-manifest.json')
    $writer = [IO.StreamWriter]::new($entry.Open(), [Text.UTF8Encoding]::new($false))
    try { $writer.Write(($manifest | ConvertTo-Json -Depth 5)) } finally { $writer.Dispose() }
} catch {
    $archive.Dispose()
    $stream.Dispose()
    Remove-Item -LiteralPath $destinationPath -Force
    throw
} finally {
    $archive.Dispose()
    $stream.Dispose()
}
[pscustomobject]@{
    Path = $destinationPath
    Files = $files.Count
    Bytes = (Get-Item -LiteralPath $destinationPath).Length
    Sha256 = (Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256).Hash.ToLowerInvariant()
}
