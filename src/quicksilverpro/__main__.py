"""Allow `python -m quicksilverpro ...` as an alias for the `qsp` entrypoint."""
from .cli import run

if __name__ == "__main__":
    run()
