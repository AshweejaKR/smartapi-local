"""Process entry point used only by the restart integration test."""
from pathlib import Path
import sys
import threading

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

app.DB_PATH = Path(sys.argv[1])
server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=int(sys.argv[2]), log_level="error"))


def stop_on_input():
    sys.stdin.readline()
    server.should_exit = True


threading.Thread(target=stop_on_input, daemon=True).start()
server.run()
