Add-Type -AssemblyName System.Drawing

$output = Join-Path $PSScriptRoot 'synthetic_ocr_printed.png'
$bitmap = [System.Drawing.Bitmap]::new(1000, 240)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.Clear([System.Drawing.Color]::White)
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$font = [System.Drawing.Font]::new('Arial', 96, [System.Drawing.FontStyle]::Bold,
    [System.Drawing.GraphicsUnit]::Pixel)
$brush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::Black)
try {
    $graphics.DrawString('LUNA OCR 73', $font, $brush, 55, 55)
    $bitmap.Save($output, [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    $brush.Dispose()
    $font.Dispose()
    $graphics.Dispose()
    $bitmap.Dispose()
}
Write-Output $output
