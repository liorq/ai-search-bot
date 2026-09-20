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
# A real interpreter, chosen by asking one to identify itself rather than by
# name. `python` on Windows is often the Store stub, which exits silently, and
# the `py` launcher here answered `--version` by opening a REPL.
$Python = $null
$candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    "python", "python3", "py"
)
foreach ($candidate in $candidates) {
    $found = if ($candidate -like "*\*") {
        if (Test-Path $candidate) { $candidate } else { $null }
    } else {
        (Get-Command $candidate -ErrorAction SilentlyContinue).Source
    }
    if (-not $found) { continue }
    $reported = & $found -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $reported -match '^\d+\.\d+$') {
        $Python = $found
        $PyVersion = $reported
        break
    }
}
if (-not $Python) {
    Warn "לא נמצא פייתון שעונה. התקן מ-python.org וסמן 'Add Python to PATH'"
    exit 1
}
Ok "פייתון $PyVersion — $Python"

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

# ---- יעדי כתיבה -------------------------------------------
# רשימה ריקה חוסמת כל כתיבה. זו ברירת המחדל בכוונה: יעד נכנס לכאן רק
# כשמישהו הוסיף אותו במפורש, ולא כי שם הדומיין נראה כמו סביבת פיתוח.
$Targets = Join-Path $SeoDir "write_targets.json"
if (-not (Test-Path $Targets)) {
    '{"rehearsal": [], "publish": []}' | Set-Content -Path $Targets -Encoding utf8
    Warn "נוצר $Targets — ריק, ולכן כתיבה חסומה עד שתוסיף יעד"
} else {
    Ok "write_targets.json קיים — לא נדרס"
}

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
# Every installed skill is asked to answer, from the installed copy rather than
# the repo. A skill that imports here is a skill a fresh session can run.
Info "מריץ בדיקת תקינות"
$env:PYTHONPATH = $CoreDest
& $Python -c "from seo_core.schema import Finding; from seo_core.clients import load_all" 2>$null
if ($LASTEXITCODE -eq 0) {
    Ok "seo_core נטען בהצלחה"
} else {
    Warn "seo_core לא נטען — נדרש פייתון 3.10 ומעלה"
    exit 1
}

$checked = 0
$broken = @()
Get-ChildItem -Path $SkillsDest -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $script = Get-ChildItem (Join-Path $_.FullName "scripts") -Filter *.py -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $script) { return }          # סקיל הוראות בלבד — אין מה לייבא
    # מריצים מחוץ לריפו, כי משם סשן חדש יריץ אותו: סקריפט שמוצא את seo_core רק
    # כשהוא יושב בעץ הפיתוח נראה תקין כאן ונשבר אצל המשתמש.
    Push-Location $HOME
    & $Python $script.FullName --help *> $null
    if ($LASTEXITCODE -eq 0) { $checked++ } else { $broken += $_.Name }
    Pop-Location
}
if ($broken.Count -eq 0) {
    Ok "$checked סקריפטים נטענים מההתקנה"
} else {
    Warn ("סקילים שלא נטענים: " + ($broken -join ", "))
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
