"""Local entry point for the installed, read-only shadow report command."""

from paperforge_worker.shadow_report import cli, summarize  # noqa: F401

if __name__ == "__main__":
    cli()
