Add-Type -AssemblyName System.Drawing

$repoRoot = Split-Path -Parent $PSScriptRoot
$iconPath = Join-Path $repoRoot 'mail/icon.png'
$wordmarkPath = Join-Path $repoRoot 'assets/home-mail-wordmark.png'
$outputPath = Join-Path $repoRoot 'mail/logo.png'

$icon = [System.Drawing.Bitmap]::FromFile($iconPath)
$wordmark = [System.Drawing.Bitmap]::FromFile($wordmarkPath)
$glyphs = New-Object System.Drawing.Bitmap(301, 48, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$banner = New-Object System.Drawing.Bitmap(1250, 240, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
try {
    # Isolate the existing lettering; the source banner has a dark, opaque background.
    for ($y = 0; $y -lt 48; $y++) {
        $sourceY = 73 + $y
        $background = $wordmark.GetPixel(499, $sourceY)
        for ($x = 0; $x -lt 301; $x++) {
            $pixel = $wordmark.GetPixel(165 + $x, $sourceY)
            $difference = ([int]$pixel.R + [int]$pixel.G + [int]$pixel.B -
                [int]$background.R - [int]$background.G - [int]$background.B) / 3
            $alpha = [int][Math]::Round([Math]::Min(255, [Math]::Max(0, $difference * 255 / (255 - $background.R))))
            if ($alpha -lt 16) { $alpha = 0 }
            $glyphs.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($alpha, 255, 255, 255))
        }
    }

    $graphics = [System.Drawing.Graphics]::FromImage($banner)
    try {
        $graphics.Clear([System.Drawing.Color]::FromArgb(255, 27, 27, 27))
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $graphics.DrawImage($icon, 20, 20, 200, 200)
        $graphics.DrawImage($glyphs, 260, 48, 903, 144)
    } finally {
        $graphics.Dispose()
    }

    $banner.Save($outputPath, [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    $banner.Dispose()
    $glyphs.Dispose()
    $wordmark.Dispose()
    $icon.Dispose()
}
