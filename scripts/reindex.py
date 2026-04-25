#!/usr/bin/env python3
"""
scripts/reindex.py — Re-embed the entire corpus.

Use this when swapping embedding models. Steps:
    1. Update embedding.model_name in config/general_settings.json
    2. python scripts/reindex.py

The script clears all existing embeddings and re-processes every
document using the new model. Document metadata, tags, and ingestion
history are preserved.

Duration estimate (BGE-small, CPU, 4-core droplet):
    1,000 documents / 50,000 chunks ≈ 15-20 minutes
    10,000 documents / 500,000 chunks ≈ 2-3 hours
"""
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.ingest import reindex_all
from src.config_loader import general_settings
from src.logger import get_logger

console = Console()
logger  = get_logger()


@click.command()
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt",
)
def main(confirm: bool):
    """Re-embed the entire corpus with the current embedding model."""
    model = general_settings["embedding"]["model_name"]

    console.print(f"\n[bold yellow]⚠ REINDEX WARNING[/bold yellow]")
    console.print(f"This will clear all embeddings and re-process the full corpus.")
    console.print(f"Embedding model: [cyan]{model}[/cyan]")
    console.print(f"This cannot be undone without a backup.\n")

    if not confirm:
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            console.print("[red]Cancelled.[/red]")
            return

    console.print("\n[blue]Starting reindex...[/blue]")
    t0 = time.perf_counter()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Re-indexing corpus...", total=None)
        result = reindex_all()
        progress.update(task, description="Complete")

    elapsed = time.perf_counter() - t0

    console.print(f"\n[bold green]✓ Reindex complete[/bold green]")
    console.print(f"  Total:    {result['total']}")
    console.print(f"  Complete: [green]{result['complete']}[/green]")
    console.print(f"  Failed:   [red]{result['failed']}[/red]")
    console.print(f"  Time:     {elapsed:.0f}s")


if __name__ == "__main__":
    main()
