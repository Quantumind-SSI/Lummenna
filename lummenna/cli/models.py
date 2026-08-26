"""D8: Model Download & ONNX Export Pipeline."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

try:
    from huggingface_hub import hf_hub_download
except Exception:
    hf_hub_download = None  # type: ignore[assignment,misc]

console = Console()

# alias -> (repo_id, pinned revision). Revisions are immutable commit SHAs so
# a compromised upstream repo cannot silently swap model weights (supply-chain
# hardening; bandit B615).
KNOWN_MODELS: dict[str, tuple[str, str]] = {
    "bge-small-en-v1.5": (
        "BAAI/bge-small-en-v1.5",
        "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
    ),
    "all-MiniLM-L6-v2": (
        "sentence-transformers/all-MiniLM-L6-v2",
        "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    ),
}


class HuggingfaceUnavailableError(RuntimeError):
    """Raised when huggingface_hub is required but not installed."""

    def __init__(self, message: str = "huggingface_hub is not installed.") -> None:
        super().__init__(message)


def _export_via_optimum(repo_id: str, dest: Path, config=None, revision: str | None = None) -> Path:
    """Use ``optimum-cli`` or Python API to export a model to ONNX.

    Args:
        repo_id: HuggingFace model repository ID.
        dest: Output directory for the ONNX export.
        config: Optional runtime config; enforces the sovereign boundary.
        revision: Optional immutable commit SHA to pin the download to. When
            set, the ``optimum-cli`` path is skipped (it cannot pin a
            revision) and the Python API is used directly.

    Returns:
        Path to the exported ``model.onnx`` file.

    Raises:
        RuntimeError: If neither ``optimum-cli`` nor the Python API is available.
    """
    if config is not None:
        config.assert_sovereign("model_download_hf")
    dest.mkdir(parents=True, exist_ok=True)

    # Attempt 1: optimum-cli subprocess (fastest, handles caching). Only used
    # for unpinned exports: the installed CLI has no --revision flag, so
    # pinned downloads must go through the Python API below.
    if revision is None:
        try:
            import subprocess

            subprocess.run(
                [
                    "optimum-cli",
                    "export",
                    "onnx",
                    "--model",
                    repo_id,
                    str(dest),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
            onnx_file = dest / "model.onnx"
            if onnx_file.exists():
                return onnx_file
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    # Attempt 2: Python API (only path that supports revision pinning).
    # optimum is intentionally NOT a base dependency (it pulls torch/CUDA, ~2GB).
    # If it's missing, guide the user to the [export] extra — or, better, to a
    # prebuilt model bundle so they never need the toolchain.
    try:
        from optimum.onnxruntime import ORTModelForFeatureExtraction
    except ImportError as exc:
        raise RuntimeError(
            "Model export requires the optimum toolchain, which is not installed "
            "(it is heavy: it pulls torch/CUDA). Either:\n"
            "  1. Use a prebuilt model bundle (recommended — no toolchain), or\n"
            "  2. Install the export extra:  pip install 'lummenna[export]'"
        ) from exc

    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(repo_id, revision=revision)
        model = ORTModelForFeatureExtraction.from_pretrained(repo_id, revision=revision)
        model.save_pretrained(str(dest))
        tokenizer.save_pretrained(str(dest))

        onnx_file = dest / "model.onnx"
        if onnx_file.exists():
            return onnx_file
        # some exports name it model.onnx, others onnx/model.onnx
        alt = dest / "onnx" / "model.onnx"
        if alt.exists():
            return alt
    except Exception as exc:
        raise RuntimeError(
            f"ONNX export failed for {repo_id}. Install 'lummenna[export]' and try again."
        ) from exc

    raise RuntimeError(f"ONNX export succeeded but model.onnx not found in {dest}")


def provision_embedding_model(model_name: str, dest: Path, config=None) -> Path:
    """Ensure an ONNX embedding model exists at *dest*.

    Strategy:
    1. If ``model.onnx`` already exists at ``dest / model_name``, return it.
    2. Download the HF model and export to ONNX via ``optimum-cli``.
    """
    model_dir = dest / model_name
    onnx_path = model_dir / "model.onnx"
    if onnx_path.exists():
        return onnx_path

    if config is not None:
        config.assert_sovereign("provision_embedding_model")
    entry = KNOWN_MODELS.get(model_name)
    if entry is not None:
        repo_id, revision = entry
    else:
        repo_id, revision = model_name, None
    return _export_via_optimum(repo_id, model_dir, config=config, revision=revision)


model_app = typer.Typer(name="model", help="Model operations")


@model_app.command(name="list")
def model_list() -> None:
    """Print a table of known embedding models."""
    table = Table(title="Known Embedding Models")
    table.add_column("Alias", style="cyan")
    table.add_column("Repo ID", style="magenta")
    table.add_column("Pinned Revision", style="green")
    for alias, (repo_id, revision) in KNOWN_MODELS.items():
        table.add_row(alias, repo_id, revision[:12])
    console.print(table)


@model_app.command(name="download")
def model_download(
    model_name: str = typer.Argument(..., help="Model alias or repo ID"),
) -> None:
    """Download and export the embedding model ONNX file to the default Lummenna model path."""
    from lummenna.config import LummennaConfig

    config = LummennaConfig()
    dest = config.model_path
    dest.mkdir(parents=True, exist_ok=True)

    try:
        path = provision_embedding_model(model_name, dest, config=config)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[bold green]Model exported to {path}[/bold green]")


@model_app.command(name="export")
def model_export(
    repo_id: str = typer.Argument(..., help="HuggingFace repo ID (e.g. BAAI/bge-small-en-v1.5)"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output directory"),  # noqa: B008
) -> None:
    """Export a HuggingFace model to ONNX using optimum-cli."""
    from lummenna.config import LummennaConfig

    config = LummennaConfig()
    if output is None:
        output = config.model_path / repo_id.split("/")[-1]

    try:
        path = _export_via_optimum(repo_id, output, config=config)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[bold green]Model exported to {path}[/bold green]")
