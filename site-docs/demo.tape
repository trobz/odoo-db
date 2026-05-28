# odoo-db terminal demo
# Showcases: --help, modules, prepare-audit, bloat

# ── Output ────────────────────────────────────────────────────────────────────
Output ./docs/demo.gif

# ── Dependencies ──────────────────────────────────────────────────────────────
Require odoo-db

# ── Settings ──────────────────────────────────────────────────────────────────
Set Shell bash
Set TypingSpeed 25ms
Set Width 2300
Set Height 2400
Set FontSize 49
Set FontFamily "JetBrains Mono, Fira Code, monospace"
Set Theme { "name": "odoo-db", "background": "#111111", "foreground": "#f5f5f5", "cursor": "#ff6b35", "black": "#2a2a2a", "red": "#ef4444", "green": "#22c55e", "yellow": "#eab308", "blue": "#3b82f6", "magenta": "#d946ef", "cyan": "#06b6d4", "white": "#f5f5f5", "brightBlack": "#52525b", "brightRed": "#f87171", "brightGreen": "#4ade80", "brightYellow": "#facc15", "brightBlue": "#60a5fa", "brightMagenta": "#e879f9", "brightCyan": "#22d3ee", "brightWhite": "#ffffff" }
Set WindowBar Colorful
Set WindowBarSize 60
Set BorderRadius 15
Set Margin 30
Set Padding 40
Set LineHeight 1.3

# ── Scene 1: discover the CLI surface ─────────────────────────────────────────
Hide
Type "export PS1='\[\033[38;2;255;107;53m\]❯\[\033[0m\] '"
Enter
Type "clear"
Enter
Show

Sleep 500ms
Type "odoo-db --help"
Sleep 400ms
Enter

Sleep 4s

# ── Scene 2: inspect installed modules ────────────────────────────────────────
Hide
Type "clear"
Enter
Show

Sleep 500ms
Type "odoo-db modules mydb"
Sleep 400ms
Enter

Sleep 4s

# ── Scene 3: bundle the migration audit ───────────────────────────────────────
Hide
Type "clear"
Enter
Show

Sleep 500ms
Type "odoo-db prepare-audit mydb"
Sleep 400ms
Enter

Sleep 6s

Type "ls -lh mydb.json"
Sleep 400ms
Enter

Sleep 3s

# ── Scene 4: estimate reclaimable bloat ───────────────────────────────────────
Hide
Type "clear"
Enter
Show

Sleep 500ms
Type "odoo-db bloat mydb --top 3"
Sleep 400ms
Enter

Sleep 6s
