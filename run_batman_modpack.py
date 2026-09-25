"""Start the locally installed Streamlit UI without changing global Python packages."""

from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
LOCAL_DEPS = HERE / ".batman_modpack_deps"
if LOCAL_DEPS.is_dir():
    sys.path.insert(0, str(LOCAL_DEPS))

try:
    from streamlit.web import cli as stcli
except ImportError as exc:
    raise SystemExit(
        "Streamlit est absent. Installe-le avec : "
        "python -m pip install --target .batman_modpack_deps "
        "-r requirements-batman-modpack.txt"
    ) from exc

sys.argv = [
    "streamlit", "run", str(HERE / "batman_modpack_builder.py"),
    "--global.developmentMode", "false",
    "--server.port", "8501",
    "--server.address", "127.0.0.1",
    "--browser.serverAddress", "127.0.0.1",
    "--browser.serverPort", "8501",
    "--server.headless", "true",
    "--browser.gatherUsageStats", "false",
]
raise SystemExit(stcli.main())
