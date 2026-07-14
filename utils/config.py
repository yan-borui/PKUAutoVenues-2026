from pathlib import Path
from datetime import datetime

PROJECT_DIR = Path(__file__).resolve().parent.parent

LOGS_DIR = PROJECT_DIR / "logs"
LOG_FILE = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

CONFIG_FILE = PROJECT_DIR / "config.ini"
