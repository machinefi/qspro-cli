"""Allow `python -m quicksilverpro ...` as an alias for the `qsp` entrypoint."""
from .cli import main

if __name__ == "__main__":
    main()
