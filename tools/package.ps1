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
if ((Get-Item -LiteralPath $root).LinkType -in @('SymbolicLink', 'Junction')) {
    throw 'Refusing a linked package source root.'
}
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
        if (($relative -notin @('app/archive.py', 'app/archive_jobs.py') -and
             $relative -match '(?i)(^|/)(?:secrets?|credentials?|tokens?|keys?|private|customer[_-]data|logs?|archives?|temp|tmp)(?:[._/-]|$)') -or
            $relative -match '(?i)(?:password[_-]?hash|session[_-]?secret)') {
            throw "Refusing a private/state file in runtime source: $relative"
        }
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
$casePaths = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($file in $files) {
    if (-not $casePaths.Add($file.Path)) { throw "Case-colliding package path: $($file.Path)" }
    $paths.Add($file.Path) | Out-Null
}
foreach ($required in @('app/__init__.py', 'app/main.py', 'web/index.html', 'web/login.html',
                        'web/assets/dashboard.js', 'web/assets/app.js', 'web/assets/app.css')) {
    if (-not $paths.Contains($required)) { throw "Package is missing $required." }
}

# Capture bytes once; the manifest and ZIP always describe this same source snapshot.
$validation = Join-Path $PSScriptRoot 'validate_package.py'
@($files.Path) | ConvertTo-Json -Compress | & $Python $validation $root
if ($LASTEXITCODE -ne 0) { throw 'Package source validation failed.' }
$orderedPaths = [string[]]@($files.Path)
[Array]::Sort($orderedPaths, [StringComparer]::Ordinal)
$byPath = @{}
$sha = [Security.Cryptography.SHA256]::Create()
try {
    foreach ($file in $files) {
        $content = [IO.File]::ReadAllBytes($file.Source)
        $digest = [BitConverter]::ToString($sha.ComputeHash($content)).Replace('-', '').ToLowerInvariant()
        $file | Add-Member -NotePropertyName Content -NotePropertyValue $content
        $file | Add-Member -NotePropertyName Sha256 -NotePropertyValue $digest
        $file.Size = $content.Length
        $byPath[$file.Path] = $file
    }
} finally { $sha.Dispose() }

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
foreach ($file in $files) {
    if ((Get-FileHash -LiteralPath $file.Source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.Sha256) {
        throw "Source changed during validation: $($file.Path)"
    }
}

$manifestFiles = @($orderedPaths | ForEach-Object {
    $file = $byPath[$_]
    [ordered]@{ path = $file.Path; bytes = $file.Size; sha256 = $file.Sha256 }
})
$identity = ($manifestFiles | ForEach-Object { "$($_.path)`t$($_.bytes)`t$($_.sha256)`n" }) -join ''
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $contentSha256 = [BitConverter]::ToString(
        $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity))).Replace('-', '').ToLowerInvariant()
} finally { $sha.Dispose() }
$manifest = [ordered]@{
    application = 'CloudLens'
    formatVersion = 2
    sourceContentSha256 = $contentSha256
    files = $manifestFiles
}
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destinationPath)) | Out-Null
$stream = [IO.File]::Open($destinationPath, [IO.FileMode]::CreateNew)
$archive = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Create)
try {
    $timestamp = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
    foreach ($path in $orderedPaths) {
        $file = $byPath[$path]
        $entry = $archive.CreateEntry($file.Path, [IO.Compression.CompressionLevel]::Optimal)
        $entry.LastWriteTime = $timestamp
        $entryStream = $entry.Open()
        try { $entryStream.Write($file.Content, 0, $file.Content.Length) } finally { $entryStream.Dispose() }
    }
    $entry = $archive.CreateEntry('release-manifest.json')
    $entry.LastWriteTime = $timestamp
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
    SourceContentSha256 = $contentSha256
}
