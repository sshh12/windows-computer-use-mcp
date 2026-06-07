"""On-device OCR via the built-in Windows.Media.Ocr engine — zero pip installs.

Windows ships an OCR engine (Windows.Media.Ocr) that is reachable from WinRT. We
drive it from a short PowerShell script so we don't need any Python OCR package
(pytesseract, easyocr, ...) or native binaries. The flow is:

  1. Save the PIL image to a temp PNG on disk.
  2. Spawn powershell.exe running a here-string that loads the WinRT
     Windows.Graphics.Imaging + Windows.Media.Ocr projections, decodes the file
     into a SoftwareBitmap, creates an OcrEngine for the user's profile
     languages, and prints the recognized text to stdout (UTF-8).
  3. Capture stdout, strip it, return it.

This module is stdio-MCP-safe: it never writes to stdout itself and never raises
out of its public functions — failures return "" / False and are logged to
stderr. The availability probe result is cached so we only pay for the probe
once per process.
"""
import logging
import os
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

# CreateProcess flag so the spawned powershell.exe never flashes a console
# window. Defined here so the module imports cleanly on non-Windows too.
CREATE_NO_WINDOW = 0x08000000

# Per-invocation wall-clock budget. WinRT + assembly load is the slow part; the
# OCR itself is fast. 15s is generous and bounds a hung shell.
_TIMEOUT_SECONDS = 15

# Cached result of available(); None means "not probed yet".
_AVAILABLE: bool | None = None

# PowerShell script that performs the OCR. It reads the image path from the
# WCU_OCR_IMAGE environment variable (passing it that way avoids command-line
# quoting/injection issues — a script passed via -Command can't receive param()
# args). WinRT async ops are awaited by blocking on the Task produced by the
# AsTask extension method, which is far more reliable than spin-polling .Status.
_OCR_PS_SCRIPT = r"""
$ImagePath = $env:WCU_OCR_IMAGE
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Load the assembly that holds the WinRT async extension methods (AsTask) before
# referencing the type, then resolve the type from the loaded assembly (the
# [Type] accelerator isn't reliably present until after Add-Type runs).
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$winRtExt = [System.AppDomain]::CurrentDomain.GetAssemblies() |
    ForEach-Object { $_.GetType('System.WindowsRuntimeSystemExtensions') } |
    Where-Object { $_ } | Select-Object -First 1

# Locate the generic AsTask overload that takes an IAsyncOperation<T> so we can
# block on WinRT IAsyncOperation results from PowerShell.
$asTaskGeneric = ($winRtExt.GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $resultType) {
    $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    return $task.Result
}

# Force-load the WinRT type projections we use.
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    [Console]::Error.WriteLine('OcrEngine could not be created for user profile languages.')
    exit 2
}
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
[Console]::Out.Write($result.Text)
"""


def _run_powershell(
    script: str,
    image_path: str | None = None,
    timeout: int = _TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    """Run a PowerShell script with no window, UTF-8 stdout, optional image path.

    If `image_path` is given it is exposed to the script via the WCU_OCR_IMAGE
    environment variable. Returns the CompletedProcess; stdout/stderr are decoded
    as UTF-8 with errors replaced so a stray byte never raises. Raises on timeout
    (callers handle that).
    """
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ]
    env = None
    if image_path is not None:
        env = os.environ.copy()
        env["WCU_OCR_IMAGE"] = image_path
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        env=env,
        # Decode bytes ourselves to guarantee UTF-8 (text=True would use the
        # locale codepage and mangle non-ASCII OCR output).
        encoding="utf-8",
        errors="replace",
    )


def available() -> bool:
    """Return True if the built-in Windows OCR engine is usable in this process.

    Probes once (creating an OcrEngine via PowerShell) and caches the result for
    the process lifetime. Returns False on non-Windows, when no OCR language
    pack is installed, or on any error. Never raises.
    """
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE

    if sys.platform != "win32":
        _AVAILABLE = False
        return False

    probe = r"""
$ErrorActionPreference = 'Stop'
try {
    [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null
    $e = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $e) { [Console]::Out.Write('NO'); exit 0 }
    [Console]::Out.Write('YES')
} catch {
    [Console]::Out.Write('NO')
}
"""
    try:
        proc = _run_powershell(probe, timeout=_TIMEOUT_SECONDS)
        _AVAILABLE = proc.stdout.strip() == "YES"
        if not _AVAILABLE:
            logger.warning(
                "Windows OCR unavailable (stdout=%r stderr=%r)",
                proc.stdout.strip(), proc.stderr.strip(),
            )
    except Exception as exc:  # subprocess timeout, missing powershell, etc.
        logger.warning("Windows OCR availability probe failed: %s", exc)
        _AVAILABLE = False
    return _AVAILABLE


def ocr_image(img) -> str:
    """Run OCR on a PIL image and return the recognized text ("" on failure).

    `img` is a PIL.Image. The image is written to a temp PNG, handed to the
    Windows OCR engine via PowerShell, and the recognized text is returned
    stripped. Any failure (no OCR engine, PowerShell error, timeout, bad image)
    is logged to stderr and yields "". Never raises.
    """
    tmp_path = None
    try:
        # NamedTemporaryFile(delete=False) so we can close the handle and let
        # PowerShell reopen the path (Windows forbids two writers to one handle).
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="wcu_ocr_")
        os.close(fd)
        # Convert to a mode the PNG encoder + WinRT decoder both accept.
        save_img = img if img.mode in ("RGB", "RGBA", "L") else img.convert("RGB")
        save_img.save(tmp_path, format="PNG")

        # WinRT's StorageFile.GetFileFromPathAsync requires a fully-qualified
        # Windows path with backslashes; forward slashes are rejected as invalid.
        ps_path = os.path.normpath(os.path.abspath(tmp_path))
        proc = _run_powershell(_OCR_PS_SCRIPT, image_path=ps_path, timeout=_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            logger.warning(
                "Windows OCR exited %s (stderr=%r)",
                proc.returncode, proc.stderr.strip(),
            )
            return ""
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("Windows OCR timed out after %ss", _TIMEOUT_SECONDS)
        return ""
    except Exception as exc:
        logger.warning("Windows OCR failed: %s", exc)
        return ""
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
