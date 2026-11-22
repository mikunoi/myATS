"""Shared interface for the selected dashboard desktop."""
import os

if os.name == 'nt':
    from platform_windows import (create_tray, ensure_startup, open_api_settings,
                                  preferences_dir, read_preferences, run_elevated,
                                  save_preferences, set_startup, startup_enabled)
else:
    from platform_linux import (create_tray, ensure_startup, open_api_settings,
                                preferences_dir, read_preferences, run_elevated,
                                save_preferences, set_startup, startup_enabled)
