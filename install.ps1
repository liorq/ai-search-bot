# התקנת ערכת הסקילים ל-SEO — Windows / PowerShell.
# מקביל ל-install.sh. בטוח להרצה חוזרת — מעדכן במקום.
#
# הרצה:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"

$RepoDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$ClaudeDir  = Join-Path $HOME ".claude"
$CoreDest   = Join-Path $ClaudeDir "seo_core"
$SkillsDest = Join-Path $ClaudeDir "skills"
$SeoDir     = Join-Path $ClaudeDir "seo"

function Write-Log($icon, $msg) {
    Write-Host ("[{0}] {1} {2}" -f (Get-Date -Format "HH:mm:ss"), $icon, $msg)
}
function Ok($m)   { Write-Log "[OK]"   $m }
function Info($m) { Write-Log "[..]"   $m }
function Warn($m) { Write-Log "[!]"    $m }

Write-Host ""
Write-Host "======================================================="
Write-Host "  התקנת ערכת סקילים ל-SEO"
Write-Host "======================================================="
Write-Host ""

# ---- python ------------------------------------------------
# py -3 הוא ה-launcher הרשמי ב-Windows ועובד גם כששם הפקודה שונה.
$Python = $null
foreach ($candidate in @("py", "python", "python3")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $Python = $candidate; break }
}
if (-not $Python) {
    Warn "לא נמצא פייתון. התקן מ-python.org וסמן 'Add Python to PATH'"
    exit 1
}
$PyArgs = if ($Python -eq "py") { @("-3") } else { @() }
Ok ("פייתון: " + (& $Python @PyArgs --version 2>&1))

# ---- seo_core ----------------------------------------------
Info "מעתיק את seo_core ל-$CoreDest"
New-Item -ItemType Directory -Force -Path $CoreDest | Out-Null
$CoreInner = Join-Path $CoreDest "seo_core"
if (Test-Path $CoreInner) { Remove-Item -Recurse -Force $CoreInner }
Copy-Item -Recurse -Force (Join-Path $RepoDir "seo_core") $CoreInner
Ok "seo_core הותקן"

# ---- skills ------------------------------------------------
New-Item -ItemType Directory -Force -Path $SkillsDest | Out-Null
$installed = 0
Get-ChildItem -Path (Join-Path $RepoDir "skills") -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "SKILL.md") } |
    ForEach-Object {
        $target = Join-Path $SkillsDest $_.Name
        if (Test-Path $target) { Remove-Item -Recurse -Force $target }
        Copy-Item -Recurse -Force $_.FullName $target
        Ok ("סקיל הותקן: " + $_.Name)
        $installed++
    }
if ($installed -eq 0) { Info "אין עדיין סקילים — מותקנת התשתית בלבד" }

# ---- config ------------------------------------------------
New-Item -ItemType Directory -Force -Path (Join-Path $SeoDir "data") | Out-Null

$EnvFile = Join-Path $SeoDir ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $RepoDir ".env.example") $EnvFile
    Warn "נוצר $EnvFile — צריך למלא אותו לפני הרצה"
} else {
    Ok ".env קיים — לא נדרס"
}

$Registry = Join-Path $SeoDir "clients.json"
if (-not (Test-Path $Registry)) {
    Copy-Item (Join-Path $RepoDir "clients.example.json") $Registry
    Warn "נוצר $Registry — צריך להשלים פרטי לקוחות"
} else {
    Ok "clients.json קיים — לא נדרס"
}

# ---- הרשאות -----------------------------------------------
# ב-NTFS אין chmod. מסירים את הירושה ומשאירים גישה למשתמש הנוכחי בלבד,
# כדי שסיסמת ה-WordPress לא תהיה קריאה לכל חשבון במחשב.
try {
    $acl = Get-Acl $SeoDir
    $acl.SetAccessRuleProtection($true, $false)
    $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        "$env:USERDOMAIN\$env:USERNAME", "FullControl",
        "ContainerInherit,ObjectInherit", "None", "Allow")
    $acl.AddAccessRule($rule)
    Set-Acl -Path $SeoDir -AclObject $acl
    Ok "ההרשאות הוגבלו למשתמש $env:USERNAME בלבד"
} catch {
    Warn "לא הצלחתי להגביל הרשאות: $($_.Exception.Message)"
}

# ---- קיצור דרך בשולחן העבודה --------------------------------
# הקבצים נשארים ב-$SeoDir. שולחן העבודה מסונכרן ל-OneDrive ברוב המחשבים
# ומופיע בכל שיתוף מסך — junction נותן נוחות בלי להזיז את הסודות לשם.
$Desktop = $null
foreach ($candidate in @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path $HOME "Desktop"),
    (Join-Path $HOME "OneDrive\Desktop")
)) {
    if ($candidate -and (Test-Path $candidate)) { $Desktop = $candidate; break }
}

if ($Desktop) {
    $Link = Join-Path $Desktop "SEO-Keys"
    if (Test-Path $Link) {
        Ok "קיצור דרך קיים: $Link"
    } else {
        try {
            New-Item -ItemType Junction -Path $Link -Target $SeoDir | Out-Null
            Ok "קיצור דרך נוצר: $Link"
        } catch {
            Warn "לא הצלחתי ליצור קיצור דרך: $($_.Exception.Message)"
        }
    }
} else {
    Info "לא נמצא שולחן עבודה — מדלג על קיצור הדרך"
}

# ---- בדיקת שפיות -------------------------------------------
Info "מריץ בדיקת תקינות"
$env:PYTHONPATH = $CoreDest
& $Python @PyArgs -c "from seo_core.schema import Finding; from seo_core.clients import load_all" 2>$null
if ($LASTEXITCODE -eq 0) {
    Ok "seo_core נטען בהצלחה"
} else {
    Warn "seo_core לא נטען — נדרש פייתון 3.10 ומעלה"
}

Write-Host ""
Write-Host "======================================================="
Write-Host "  סיכום"
Write-Host "======================================================="
Write-Host "  סקילים שהותקנו:     $installed"
Write-Host "  seo_core:           $CoreInner"
Write-Host "  הגדרות:             $SeoDir"
Write-Host ""
Write-Host "  הצעד הבא:"
Write-Host "    1. notepad `"$EnvFile`""
if ($Desktop) {
Write-Host "       (או דרך הקיצור בשולחן העבודה: SEO-Keys)"
}
Write-Host "    2. notepad `"$Registry`""
Write-Host "    3. הרץ סקיל עם --self-check"
Write-Host ""
