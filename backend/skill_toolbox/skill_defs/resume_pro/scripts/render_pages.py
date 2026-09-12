"""Compatibility CLI for the shared DOCX → PDF → PNG renderer."""
import sys
from pathlib import Path

# Support direct script execution both from the checkout and installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from skill_toolbox.docx_render import main


if __name__ == "__main__":
    main()
