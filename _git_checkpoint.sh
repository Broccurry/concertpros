#!/usr/bin/env bash
# ConcertPros git auto-checkpoint — runs at the end of every Claude reply (Stop hook).
#
# Unlike SellHQ, this repo is a normal git clone per machine (not a shared OneDrive folder with two
# separate gitdirs pointing at it), so a plain commit -> pull --rebase -> push is correct here. No
# `-s ours` trickery needed — a real conflict here is a REAL conflict (two machines actually edited the
# same lines) and should surface, not be silently discarded.
#
# Never put a machine-specific path here — $HOME resolves correctly on both machines already.

W="${CLAUDE_PROJECT_DIR:-$PWD}"
LOG="$W/_git_checkpoint.log"
git_() { git -C "$W" "$@"; }

alarm() {   # a box that stays on screen until OK is clicked, launched detached so this hook never hangs.
  local msg="$1"
  echo "$(date '+%F %T') ALARM: $msg" >> "$LOG"
  local dir="${TEMP:-/tmp}"
  local ps1="$dir/concertpros_backup_alarm.ps1"
  {
    printf 'Add-Type -AssemblyName System.Windows.Forms\n'
    printf '$f = New-Object System.Windows.Forms.Form\n'
    printf '$f.TopMost = $true\n'
    printf '[System.Windows.Forms.MessageBox]::Show($f, @"\n%s\n"@, "ConcertPros backup - PROBLEM", "OK", "Warning") | Out-Null\n' "$msg"
  } > "$ps1" 2>/dev/null || return 0
  local win; win=$(cygpath -w "$ps1" 2>/dev/null || printf '%s' "$ps1")
  powershell.exe -NoProfile -WindowStyle Hidden -Command \
    "Start-Process powershell -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File','$win'" >/dev/null 2>&1
}

if [ ! -d "$W/.git" ]; then
  alarm "Backups are NOT running on $(hostname): $W/.git was not found."
  exit 0
fi

git_ add -A 2>/dev/null
git_ commit -q -m "auto-checkpoint

Co-Authored-By: Claude <noreply@anthropic.com>" 2>/dev/null   # "nothing to commit" is fine

# No remote configured yet (first-run before GitHub exists) — commit locally, skip push quietly.
if ! git_ remote get-url origin >/dev/null 2>&1; then
  echo "$(date '+%F %T') local-only commit (no origin yet)" >> "$LOG"
  exit 0
fi

if ! git_ fetch -q origin 2>>"$LOG"; then
  alarm "Could not reach GitHub from $(hostname) to back up ConcertPros. Check the internet connection. Work is saved locally."
  exit 0
fi

if git_ rev-parse --verify -q origin/main >/dev/null 2>&1; then
  if ! git_ pull -q --rebase origin main 2>>"$LOG"; then
    alarm "ConcertPros backup could not merge with GitHub on $(hostname) — likely a real conflict with the other machine's edits. Work is saved locally but NOT uploaded. Resolve manually: cd \"$W\" && git status"
    exit 0
  fi
fi

if git_ push -q origin main 2>>"$LOG"; then
  echo "$(date '+%F %T') ok $(git_ rev-parse --short HEAD)" >> "$LOG"
else
  alarm "Backup upload to GitHub FAILED on $(hostname). This computer's ConcertPros work is NOT backed up."
fi
exit 0
