import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"


def run():
    if Path(sys.prefix).resolve() != VENV_DIR.resolve():
        if not VENV_PYTHON.is_file():
            raise SystemExit(
                "Project environment not found. Create it with "
                "'py -3.12 -m venv .venv' and install dependencies with "
                "'.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt'."
            )
        return subprocess.call(
            [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=PROJECT_DIR,
        )

    from main import main
    main()


if __name__ == "__main__":
    run()
