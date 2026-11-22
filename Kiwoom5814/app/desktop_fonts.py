"""Read the Korean font installed by setup-steamos.sh for PNG chart rendering."""
import os
from pathlib import Path


def korean_font(bold=False):
    if os.name == 'nt':
        return 'C:/Windows/Fonts/' + ('malgunbd.ttf' if bold else 'malgun.ttf')
    fonts = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'fonts/steamdeck-5814'
    return str(fonts / ('NotoSansCJKkr-Bold.otf' if bold else 'NotoSansCJKkr-Regular.otf'))
