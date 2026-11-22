"""Thin frozen host; the editable application is loaded from adjacent source files."""
import sys
from pathlib import Path

if getattr(sys, 'frozen', False):
    source = Path(sys.executable).parent / 'app'
    sys.path.insert(0, str(source))
else:
    source = Path(__file__).parent

if __name__ == '__main__':
    from app import main
    main()
