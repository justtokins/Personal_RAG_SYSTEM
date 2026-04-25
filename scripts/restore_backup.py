#!/usr/bin/env python3
"""
scripts/restore_backup.py — Decrypt and restore a backup.

Usage:
    python scripts/restore_backup.py --backup /opt/ragbase/backups/ragbase_2024-01-15_02-00.db.enc
    python scripts/restore_backup.py --backup backup.db.enc --output /tmp/restored.db

ENCRYPTION_KEY must be set in .env or environment.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import click
from rich.console import Console

from src.backup import decrypt_backup
from src.config_loader import general_settings

console = Console()


@click.command()
@click.option("--backup",  required=True,  help="Path to encrypted backup file (.db.enc)")
@click.option("--output",  default=None,   help="Output path for restored DB (default: ./restored.db)")
@click.option("--replace", is_flag=True,   help="Replace the live database (DANGEROUS)")
def main(backup: str, output: str, replace: bool):
    """Decrypt a RAGBase backup file."""
    backup_path = Path(backup)
    if not backup_path.exists():
        console.print(f"[red]Backup file not found: {backup}[/red]")
        sys.exit(1)

    if replace:
        live_db = general_settings["paths"]["db_path"]
        console.print(f"\n[bold red]⚠ DANGER: This will REPLACE the live database:[/bold red]")
        console.print(f"  {live_db}")
        answer = input("Type 'REPLACE' to confirm: ").strip()
        if answer != "REPLACE":
            console.print("[yellow]Cancelled.[/yellow]")
            return
        output = live_db
    else:
        output = output or "./restored.db"

    console.print(f"\nDecrypting [cyan]{backup_path.name}[/cyan] → [cyan]{output}[/cyan]")

    try:
        decrypt_backup(str(backup_path), output)
        console.print(f"[bold green]✓ Restored to {output}[/bold green]")
    except Exception as e:
        console.print(f"[red]Decryption failed: {e}[/red]")
        console.print("[yellow]Check that ENCRYPTION_KEY matches the key used when the backup was made.[/yellow]")
        sys.exit(1)


if __name__ == "__main__":
    main()
